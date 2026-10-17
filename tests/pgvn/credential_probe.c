/* Credential components with real libpq/OpenSSL and filesystem checks.
 * Telemetry sinks are adapters; crypto, key files, SQL and guards are real.
 * Constants below are public test vectors, never deployment keys. */
#define _XOPEN_SOURCE 700
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#include <openssl/crypto.h>
#include <apr_general.h>
#include <apr_strings.h>
#include "svn_pools.h"
#include "credentials.h"
#include "../platform/credentials.h"
#include "build_info.h"

static unsigned assertions;
static const char *domain="身份域",*user="用戶é",*password="pássword/密碼";
static const char *secret_hex="000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f";
static void require(int value,const char *name)
{
    if(!value) { fprintf(stderr,"FAIL: %s\n",name); exit(1); }
    ++assertions;
}
static void success(svn_error_t *error,const char *name)
{
    if(error) { fprintf(stderr,"FAIL: %s (%ld)\n",name,(long)error->apr_err); svn_error_clear(error); exit(1); }
    ++assertions;
}
static void denied(svn_error_t *error,apr_status_t code,const char *name)
{
    require(error && error->apr_err==code,name); svn_error_clear(error);
}
void pgvn_telemetry_query(const char *state,apr_interval_time_t elapsed) { (void)state; (void)elapsed; }
void pgvn_telemetry_query_start(void) { }
void pgvn_telemetry_connection(int change,int rejected,int failed) { (void)change; (void)rejected; (void)failed; }
void pgvn_telemetry_waiter(int change) { (void)change; }

static void decode(unsigned char *out,const char *hex)
{
    for(size_t i=0;i<strlen(hex)/2;++i) { unsigned byte; require(sscanf(hex+i*2,"%2x",&byte)==1,"vector hex"); out[i]=(unsigned char)byte; }
}
static void encode(char *out,const unsigned char *input,size_t size)
{
    const char *digits="0123456789abcdef";
    for(size_t i=0;i<size;++i) { out[i*2]=digits[input[i]>>4]; out[i*2+1]=digits[input[i]&15]; }
    out[size*2]=0;
}
static int all_zero(const unsigned char *data,size_t size)
{
    for(size_t i=0;i<size;++i) if(data[i]) return 0;
    return 1;
}

static void vectors(apr_pool_t *pool)
{
    unsigned char key[32]={0},nonce[12]={0},plain[64]={0},cipher[64],tag[16],expected[64],expected_tag[16],out[64];
    decode(expected,"cea7403d4d606b6e074ec5d3baf39d18"); decode(expected_tag,"d0d1c8a799996bf0265b98b5d48ab919");
    require(pgvn_credential_cipher(1,key,(const unsigned char *)"",0,nonce,tag,plain,16,cipher,pool),"NIST AES-256-GCM encrypt");
    require(!memcmp(cipher,expected,16) && !memcmp(tag,expected_tag,16),"NIST ciphertext and tag");
    require(pgvn_credential_cipher(0,key,(const unsigned char *)"",0,nonce,tag,cipher,16,out,pool) && all_zero(out,16),"NIST decrypt");
    decode(expected_tag,"530f8afbc74536b9a963b4f1c4cb738b");
    require(pgvn_credential_cipher(1,key,(const unsigned char *)"",0,nonce,tag,plain,0,cipher,pool) && !memcmp(tag,expected_tag,16),"NIST empty plaintext");
    decode(key,secret_hex); for(int i=0;i<12;++i) nonce[i]=(unsigned char)i;
    const char *aad="身份域\n用戶é\nvector-v1";
    decode(expected,"37c17768b692ad69e96e7224370edad1"); decode(expected_tag,"1285b80ffe554ddf72709a8fcf779450");
    size_t size=strlen(password);
    require(pgvn_credential_cipher(1,key,(const unsigned char *)aad,strlen(aad),nonce,tag,(const unsigned char *)password,size,cipher,pool),"UTF-8 AAD encrypt");
    require(!memcmp(cipher,expected,size) && !memcmp(tag,expected_tag,16),"reference UTF-8 credential vector");
    require(pgvn_credential_cipher(0,key,(const unsigned char *)aad,strlen(aad),nonce,tag,cipher,size,out,pool) && !memcmp(out,password,size),"UTF-8 AAD decrypt");
    for(int i=0;i<16;++i) {
        tag[i]^=1; memset(out,0xa5,sizeof(out));
        require(!pgvn_credential_cipher(0,key,(const unsigned char *)aad,strlen(aad),nonce,tag,cipher,size,out,pool) && all_zero(out,size),"tag tamper rejects and wipes output"); tag[i]^=1;
    }
    for(int i=0;i<12;++i) {
        nonce[i]^=1; memset(out,0xa5,sizeof(out));
        require(!pgvn_credential_cipher(0,key,(const unsigned char *)aad,strlen(aad),nonce,tag,cipher,size,out,pool) && all_zero(out,size),"nonce tamper rejects and wipes output"); nonce[i]^=1;
    }
    key[0]^=1; memset(out,0xa5,sizeof(out));
    require(!pgvn_credential_cipher(0,key,(const unsigned char *)aad,strlen(aad),nonce,tag,cipher,size,out,pool) && all_zero(out,size),"wrong key rejects and wipes output"); key[0]^=1;
    cipher[0]^=1; memset(out,0xa5,sizeof(out));
    require(!pgvn_credential_cipher(0,key,(const unsigned char *)aad,strlen(aad),nonce,tag,cipher,size,out,pool) && all_zero(out,size),"ciphertext tamper rejects and wipes output"); cipher[0]^=1;
    require(!pgvn_credential_cipher(0,key,(const unsigned char *)"身份域\n用戶é\nvector-v2",strlen(aad),nonce,tag,cipher,size,out,pool) && all_zero(out,size),"AAD key ID binding");
    unsigned char random_a[32],random_b[32];
    require(pgvn_secure_random(random_a,sizeof(random_a)) && pgvn_secure_random(random_b,sizeof(random_b)),"secure random source");
    require(!all_zero(random_a,sizeof(random_a)) && memcmp(random_a,random_b,sizeof(random_a)),"random requests differ");
    pgvn_secure_zero(random_a,sizeof(random_a)); require(all_zero(random_a,sizeof(random_a)),"secure clear");
    const char *invalid[]={"\xc0\xaf","\xed\xa0\x80","\xf4\x90\x80\x80","\xe2\x82","\x80"};
    for(size_t i=0;i<sizeof(invalid)/sizeof(invalid[0]);++i) denied(pgvn_credential_identity("pgvn",invalid[i]),SVN_ERR_INCORRECT_PARAMS,"invalid UTF-8 identity");
    denied(pgvn_credential_identity("a\nb","user"),SVN_ERR_INCORRECT_PARAMS,"AAD delimiter rejected in identity");
    success(pgvn_credential_identity("pgvn","é"),"composed identity"); success(pgvn_credential_identity("pgvn","e\xcc\x81"),"decomposed identity retained");
}

static void write_bytes(const char *path,const char *data,size_t size)
{
    int fd=open(path,O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC,0600);
    require(fd>=0 && write(fd,data,size)==(ssize_t)size && !close(fd),"private fixture write");
}
static void key_denied(const char *path,apr_pool_t *pool)
{
    pgvn_credential_key *key=(void *)1;
    denied(pgvn_key_load(&key,path,pool),SVN_ERR_AUTHN_FAILED,"unsafe or malformed key denied");
    require(!key,"failed key load publishes no key");
}
static char *private_directory(apr_pool_t *pool)
{
    char *path=apr_pstrdup(pool,"/tmp/pgvn-credential-XXXXXX");
    require(mkdtemp(path)!=NULL,"private directory"); return path;
}
static void keys(apr_pool_t *pool)
{
    char *directory=private_directory(pool);
    const char *path=apr_pstrcat(pool,directory,"/部署.key",NULL),*alias=apr_pstrcat(pool,directory,"/alias",NULL);
    mode_t previous=umask(0);
    success(pgvn_key_create(path,"key-v1",pool),"create under permissive umask"); umask(previous);
    struct stat info; require(!stat(path,&info) && (info.st_mode&07777)==0600 && info.st_uid==geteuid(),"created owner-only file");
    pgvn_credential_key *key,*actual;
    success(pgvn_key_load(&key,path,pool),"load newly created key"); require(!strcmp(key->id,"key-v1"),"key ID roundtrip");
    int cwd=open(".",O_RDONLY|O_DIRECTORY|O_CLOEXEC);
    require(cwd>=0 && !chdir(directory),"private relative working directory");
    success(pgvn_key_load(&actual,"部署.key",pool),"relative key path"); require(!fchdir(cwd) && !close(cwd),"restore working directory");
    denied(pgvn_key_create(path,"replacement",pool),SVN_ERR_FS_ALREADY_EXISTS,"existing key not overwritten");
    success(pgvn_key_load(&actual,path,pool),"existing key intact"); require(!memcmp(key->secret,actual->secret,32),"existing secret unchanged");
    require(!chmod(path,0400),"read-only mode"); success(pgvn_key_load(&actual,path,pool),"0400 accepted");
    const mode_t bad_modes[]={0,0200,0500,0640,0604,0700,0666,04600,02600,01600};
    for(size_t i=0;i<sizeof(bad_modes)/sizeof(bad_modes[0]);++i) { require(!chmod(path,bad_modes[i]),"fixture mode"); key_denied(path,pool); }
    require(!chmod(path,0600) && !symlink(path,alias),"file symlink fixture"); key_denied(alias,pool);
    denied(pgvn_key_create(alias,"never-replace",pool),SVN_ERR_FS_ALREADY_EXISTS,"symlink target never overwritten"); require(!unlink(alias),"unlink alias");
    require(!link(path,alias),"hardlink fixture"); key_denied(path,pool); key_denied(alias,pool); require(!unlink(alias),"remove hardlink");
    success(pgvn_key_load(&actual,path,pool),"single link restored");
    const char *fifo=apr_pstrcat(pool,directory,"/fifo",NULL); require(!mkfifo(fifo,0600),"FIFO fixture"); key_denied(fifo,pool);
    key_denied(directory,pool);
    require(!symlink(directory,alias),"directory symlink fixture"); key_denied(apr_pstrcat(pool,alias,"/部署.key",NULL),pool);
    denied(pgvn_key_create(apr_pstrcat(pool,alias,"/new.key",NULL),"new",pool),SVN_ERR_AUTHN_FAILED,"symlink parent create denied"); require(!unlink(alias),"remove directory alias");
    require(!chmod(directory,0777),"unsafe parent"); key_denied(path,pool);
    denied(pgvn_key_create(apr_pstrcat(pool,directory,"/new.key",NULL),"new",pool),SVN_ERR_AUTHN_FAILED,"writable parent create denied");
    require(!chmod(directory,0750),"restricted group directory"); success(pgvn_key_load(&actual,path,pool),"non-writable group directory accepted"); require(!chmod(directory,0700),"restore private parent");
    key_denied(apr_pstrcat(pool,directory,"/./部署.key",NULL),pool); key_denied(apr_pstrcat(pool,directory,"//部署.key",NULL),pool);
    key_denied(apr_pstrcat(pool,directory,"/../x",NULL),pool); key_denied(apr_pstrcat(pool,path,"/",NULL),pool);
    char *large=apr_palloc(pool,8194); memset(large,'x',8193); large[8193]=0; key_denied(large,pool);
    denied(pgvn_key_create(apr_pstrcat(pool,directory,"/invalid",NULL),"bad/id",pool),SVN_ERR_INCORRECT_PARAMS,"invalid key ID");
    const char *bad=apr_pstrcat(pool,directory,"/bad.key",NULL);
    const char *lf=apr_psprintf(pool,"PGVNKEY1\nvector-v1\n%s\n",secret_hex);
    const char *crlf=apr_psprintf(pool,"PGVNKEY1\r\nvector-v1\r\n%s\r\n",secret_hex);
    write_bytes(bad,lf,strlen(lf)); success(pgvn_key_load(&actual,bad,pool),"LF key import");
    unsigned char imported[32]; decode(imported,secret_hex); require(!memcmp(actual->secret,imported,32),"fixed key import bytes");
    write_bytes(bad,crlf,strlen(crlf)); success(pgvn_key_load(&actual,bad,pool),"CRLF key import"); require(!memcmp(actual->secret,imported,32),"CRLF preserves bytes");
    const char *malformed[]={"","PGVNKEY2\nkey\n00\n","PGVNKEY1\rkey\r00\r","PGVNKEY1\nkey\n00\n",
        apr_pstrcat(pool,lf,"\n",NULL),apr_psprintf(pool,"PGVNKEY1\r\nvector-v1\n%s\r\n",secret_hex),
        apr_pstrndup(pool,lf,strlen(lf)-1),apr_psprintf(pool,"PGVNKEY1\ninvalid/id\n%s\n",secret_hex)};
    for(size_t i=0;i<sizeof(malformed)/sizeof(malformed[0]);++i) { write_bytes(bad,malformed[i],strlen(malformed[i])); key_denied(bad,pool); }
    char nul[160]; memcpy(nul,lf,strlen(lf)); nul[15]=0; write_bytes(bad,nul,strlen(lf)); key_denied(bad,pool);
    char oversized[1024]; memset(oversized,'x',sizeof(oversized)); write_bytes(bad,oversized,sizeof(oversized)); key_denied(bad,pool);
    char uppercase[160]; memcpy(uppercase,lf,strlen(lf)); uppercase[35]='A'; write_bytes(bad,uppercase,strlen(lf)); key_denied(bad,pool);
    write_bytes(bad,lf,strlen(lf)); denied(pgvn_keyring_load(&actual,bad,bad,pool),SVN_ERR_INCORRECT_PARAMS,"duplicate key IDs rejected");
    /* Concurrent O_EXCL creation must elect exactly one writer. */
    const char *race=apr_pstrcat(pool,directory,"/race.key",NULL); pid_t children[8];
    for(int i=0;i<8;++i) {
        children[i]=fork(); require(children[i]>=0,"fork key creator");
        if(!children[i]) { svn_error_t *error=pgvn_key_create(race,"race",pool); int code=!error ? 0 : error->apr_err==SVN_ERR_FS_ALREADY_EXISTS ? 3 : 4; _exit(code); }
    }
    int winners=0;
    for(int i=0;i<8;++i) { int status; require(waitpid(children[i],&status,0)==children[i] && WIFEXITED(status),"join key creator"); require(WEXITSTATUS(status)==0 || WEXITSTATUS(status)==3,"creator result"); winners+=WEXITSTATUS(status)==0; }
    require(winners==1,"one concurrent key creation"); success(pgvn_key_load(&actual,race,pool),"concurrently created key valid");
    /* Swap an intermediate directory for a symlink while opening keys. */
    const char *moving=apr_pstrcat(pool,directory,"/moving",NULL),*park=apr_pstrcat(pool,directory,"/park",NULL),*trap=apr_pstrcat(pool,directory,"/trap",NULL);
    require(!mkdir(moving,0700) && !mkdir(trap,0700),"swap fixture directories");
    success(pgvn_key_create(apr_pstrcat(pool,moving,"/key",NULL),"safe",pool),"safe swap key");
    success(pgvn_key_create(apr_pstrcat(pool,trap,"/key",NULL),"trap",pool),"trap swap key");
    pid_t child=fork(); require(child>=0,"fork swapper");
    if(!child) {
        for(int i=0;i<512;++i) if(rename(moving,park) || symlink(trap,moving) || unlink(moving) || rename(park,moving)) _exit(4);
        _exit(0);
    }
    for(int i=0;i<128;++i) {
        svn_error_t *error=pgvn_key_load(&actual,apr_pstrcat(pool,moving,"/key",NULL),pool);
        if(error) { require(error->apr_err==SVN_ERR_AUTHN_FAILED,"swap error closed"); svn_error_clear(error); }
        else require(!strcmp(actual->id,"safe"),"swap never follows trap");
    }
    int status; require(waitpid(child,&status,0)==child && WIFEXITED(status) && !WEXITSTATUS(status),"join swapper");
    require(!unlink(apr_pstrcat(pool,moving,"/key",NULL)) && !unlink(apr_pstrcat(pool,trap,"/key",NULL)) && !rmdir(moving) && !rmdir(trap),"remove swap fixtures");
    require(!unlink(path) && !unlink(fifo) && !unlink(bad) && !unlink(race) && !rmdir(directory),"remove key fixtures");
}

static void owner_check(apr_pool_t *pool)
{
    require(geteuid()==0,"owner test container root");
    char *directory=private_directory(pool); const char *path=apr_pstrcat(pool,directory,"/owner.key",NULL);
    success(pgvn_key_create(path,"owner",pool),"root-owned fixture"); require(!chown(path,1000,1000),"change fixture owner");
    key_denied(path,pool); require(!unlink(path) && !rmdir(directory),"remove owner fixture");
}

static PGresult *query(PGconn *connection,const char *sql)
{
    PGresult *result=PQexec(connection,sql);
    require(result && (PQresultStatus(result)==PGRES_TUPLES_OK || PQresultStatus(result)==PGRES_COMMAND_OK),"database fixture SQL"); return result;
}
static void expect_password(pgvn_context *context,const pgvn_credential_key *keys,const char *name,const char *expected,apr_int64_t version,apr_pool_t *pool)
{
    const char *actual=NULL; apr_int64_t generation=0;
    success(pgvn_credential_get(&actual,&generation,context,keys,domain,name,pool),"decrypt stored credential");
    require(actual && !strcmp(actual,expected) && generation==version,"password and generation match");
}
static void expect_auth_failure(pgvn_context *context,const pgvn_credential_key *keys,const char *name,apr_pool_t *pool)
{
    const char *actual=(void *)1; apr_int64_t generation=99;
    denied(pgvn_credential_get(&actual,&generation,context,keys,domain,name,pool),SVN_ERR_AUTHN_FAILED,"credential authentication fails");
    require(!actual && !generation,"failure publishes neither plaintext nor generation");
}
typedef struct rotation {
    pgvn_context *context;
    const pgvn_credential_key *old_key,*new_key;
    apr_pool_t *pool;
    apr_status_t result;
} rotation;
static void *rotate_thread(void *baton)
{
    rotation *test=baton;
    svn_error_t *error=pgvn_credential_rekey(test->context,test->old_key,test->new_key,domain,"concurrent",test->pool);
    test->result=error ? error->apr_err : 0; svn_error_clear(error); return NULL;
}
static void database(apr_pool_t *pool)
{
    const char *dsn=getenv("PGVN_CREDENTIAL_DSN"),*reader_dsn=getenv("PGVN_CREDENTIAL_READER_DSN"); require(dsn && reader_dsn,"isolated DSNs configured");
    pgvn_context_options options={dsn,2,5000}; pgvn_context *context,*reader;
    success(pgvn_context_create(&context,&options,pool),"production admin context"); options.connection_string=reader_dsn;
    success(pgvn_context_create(&reader,&options,pool),"production service context");
    char *directory=private_directory(pool); const char *old_path=apr_pstrcat(pool,directory,"/old.key",NULL),*new_path=apr_pstrcat(pool,directory,"/new.key",NULL);
    const char *bytes=apr_psprintf(pool,"PGVNKEY1\nvector-v1\n%s\n",secret_hex);
    write_bytes(old_path,bytes,strlen(bytes)); pgvn_credential_key *key,*next;
    success(pgvn_key_load(&key,old_path,pool),"import public vector key");
    success(pgvn_key_create(new_path,"random-v2",pool),"generate rotation key"); success(pgvn_key_load(&next,new_path,pool),"load rotation key");
    expect_password(reader,key,user,password,1,pool);
    denied(pgvn_credential_put(reader,key,domain,user,password,pool),SVN_ERR_AUTHZ_UNWRITABLE,"service cannot write credential");
    success(pgvn_credential_put(context,key,domain,user,password,pool),"production encryption and upsert"); expect_password(reader,key,user,password,2,pool);
    unsigned char invalid_bytes[]={0xff,0};
    denied(pgvn_credential_put(context,key,domain,user,(const char *)invalid_bytes,pool),SVN_ERR_INCORRECT_PARAMS,"invalid UTF-8 password");
    char *maximum=apr_palloc(pool,4098); memset(maximum,'x',4097); maximum[4097]=0;
    denied(pgvn_credential_put(context,key,domain,user,maximum,pool),SVN_ERR_INCORRECT_PARAMS,"4097-byte password denied");
    maximum[4096]=0; success(pgvn_credential_put(context,key,domain,"maximum",maximum,pool),"4096-byte password accepted"); expect_password(reader,key,"maximum",maximum,1,pool);
    /* Ciphertext transplant must fail even with a valid key ID and GCM tag. */
    PGresult *result=query(context->control,"INSERT INTO pgvn.credentials SELECT identity_domain,'copied',generation,enabled,key_id,nonce,ciphertext,authentication_tag,updated_at FROM pgvn.credentials WHERE username='用戶é'"); PQclear(result);
    expect_auth_failure(reader,key,"copied",pool);
    result=query(context->control,"UPDATE pgvn.credentials SET authentication_tag=set_byte(authentication_tag,0,get_byte(authentication_tag,0)#1) WHERE username='用戶é'"); PQclear(result);
    expect_auth_failure(reader,key,user,pool);
    success(pgvn_credential_put(context,key,domain,user,password,pool),"restore valid credential");
    success(pgvn_credential_check(reader,domain,user,3,pool),"current generation accepted");
    success(pgvn_control_begin(context),"begin guard transaction"); success(pgvn_credential_guard(context,context->control,domain,user,3,pool),"real SQL credential guard");
    PGconn *actor=PQconnectdb(dsn); require(actor && PQstatus(actor)==CONNECTION_OK,"revoker connection");
    result=query(actor,"SET lock_timeout='200ms'"); PQclear(result);
    result=PQexec(actor,"UPDATE pgvn.credentials SET enabled=false,generation=generation+1 WHERE username='用戶é'");
    require(result && PQresultStatus(result)==PGRES_FATAL_ERROR && !strcmp(PQresultErrorField(result,PG_DIAG_SQLSTATE),"55P03"),"guard holds row lock through transaction"); PQclear(result);
    success(pgvn_control_end(context,NULL),"release guard transaction");
    result=query(actor,"UPDATE pgvn.credentials SET enabled=false,generation=generation+1 WHERE username='用戶é'"); PQclear(result); PQfinish(actor);
    denied(pgvn_credential_check(reader,domain,user,3,pool),SVN_ERR_RA_NOT_AUTHORIZED,"revoked generation rejected");
    const char *actual=(void *)1; apr_int64_t generation=99;
    success(pgvn_credential_get(&actual,&generation,reader,key,domain,user,pool),"disabled lookup"); require(!actual && !generation,"disabled account yields no plaintext");
    success(pgvn_credential_rekey(context,key,next,domain,user,pool),"rotate disabled credential");
    result=query(context->control,"SELECT NOT enabled AND generation=5 FROM pgvn.credentials WHERE username='用戶é'"); require(PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"),"rotation preserves disabled state"); PQclear(result);
    success(pgvn_credential_put(context,next,domain,user,password,pool),"re-enable with new key"); expect_password(reader,next,user,password,6,pool);
    expect_auth_failure(reader,key,user,pool);
    denied(pgvn_credential_rekey(context,next,next,domain,user,pool),SVN_ERR_INCORRECT_PARAMS,"same-ID rotation denied");
    const char *names[]={"Case","case","é","e\xcc\x81"};
    for(int i=0;i<4;++i) { success(pgvn_credential_put(context,key,domain,names[i],password,pool),"identity remains distinct"); expect_password(reader,key,names[i],password,1,pool); }
    next->previous=key; expect_password(reader,next,"Case",password,1,pool); next->previous=NULL;
    expect_auth_failure(reader,next,"Case",pool);
    success(pgvn_credential_put(context,key,domain,"concurrent",password,pool),"create concurrent rotation fixture");
    rotation rotations[2]; pthread_t threads[2]; options.connection_string=dsn;
    for(int i=0;i<2;++i) {
        rotations[i]=(rotation){NULL,key,next,svn_pool_create(pool),0};
        success(pgvn_context_create(&rotations[i].context,&options,rotations[i].pool),"independent rotation context");
        require(!pthread_create(&threads[i],NULL,rotate_thread,&rotations[i]),"start concurrent rotation");
    }
    int winners=0;
    for(int i=0;i<2;++i) {
        require(!pthread_join(threads[i],NULL),"join rotation");
        apr_status_t code=rotations[i].result;
        require(!code || code==SVN_ERR_AUTHN_FAILED || code==SVN_ERR_FS_TXN_OUT_OF_DATE,"concurrent rotation outcome");
        winners+=!code; svn_pool_destroy(rotations[i].pool);
    }
    require(winners==1,"one successful concurrent rotation"); expect_password(reader,next,"concurrent",password,2,pool);
    /* Authenticate a deliberately invalid plaintext with a correct GCM tag. */
    for(int i=0;i<2;++i) {
        const char *name=i ? "nul-plain" : "invalid-plain"; unsigned char plain[3]={'x',i ? 0 : 0xff,'y'},nonce[12]={0},tag[16],cipher[3];
        const char *aad=apr_psprintf(pool,"%s\n%s\n%s",domain,name,key->id);
        require(pgvn_credential_cipher(1,key->secret,(const unsigned char *)aad,strlen(aad),nonce,tag,plain,3,cipher,pool),"seal malformed plaintext fixture");
        char nonce_hex[25],cipher_hex[7],tag_hex[33]; encode(nonce_hex,nonce,12); encode(cipher_hex,cipher,3); encode(tag_hex,tag,16);
        const char *values[]={domain,name,key->id,nonce_hex,cipher_hex,tag_hex};
        success(pgvn_control_query(&result,context,"INSERT INTO pgvn.credentials(identity_domain,username,key_id,nonce,ciphertext,authentication_tag) VALUES($1,$2,$3,decode($4,'hex'),decode($5,'hex'),decode($6,'hex'))",6,values),"insert malformed plaintext fixture"); PQclear(result);
        expect_auth_failure(reader,key,name,pool);
    }
    success(pgvn_credential_put(context,key,domain,"reference-write",password,pool),"write for independent reference decrypt");
    require(!unlink(old_path) && !unlink(new_path) && !rmdir(directory),"remove DB key fixtures");
}

int main(int argc,char **argv)
{
    if(argc==2 && !strcmp(argv[1],"--version")) {
        char json[PGVN_BUILD_JSON_SIZE]; pgvn_build_json(json,sizeof(json));
        size_t length=strlen(json); while(length && (json[length-1]=='\n' || json[length-1]=='\r')) --length;
        require(length && json[length-1]=='}',"version JSON"); json[length-1]=0;
        printf("%s,\"crypto_backend\":\"OpenSSL EVP AES-256-GCM\",\"openssl_build\":\"%s\",\"openssl_runtime\":\"%s\"}\n",json,OPENSSL_VERSION_TEXT,OpenSSL_version(OPENSSL_VERSION)); return 0;
    }
    if(apr_initialize()!=APR_SUCCESS) return 2;
    apr_pool_t *pool=svn_pool_create(NULL);
    require(argc==2,"component operation required");
    if(!strcmp(argv[1],"vectors")) vectors(pool);
    else if(!strcmp(argv[1],"keys")) keys(pool);
    else if(!strcmp(argv[1],"owner")) owner_check(pool);
    else if(!strcmp(argv[1],"database")) database(pool);
    else require(0,"unknown operation");
    svn_pool_destroy(pool); apr_terminate();
    printf("{\"status\":\"PASS\",\"assertions\":%u}\n",assertions); return 0;
}
