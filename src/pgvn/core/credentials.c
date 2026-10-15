#include <string.h>
#include <stdio.h>
#include <wchar.h>
#include "credentials.h"
#include <bcrypt.h>
#include <aclapi.h>
#include <sddl.h>
#include <apr_strings.h>

static svn_error_t *key_error(void)
{
    return svn_error_create(SVN_ERR_AUTHN_FAILED,NULL,"PgVN credential key is unavailable, invalid or insufficiently protected");
}

static wchar_t *wide_path(const char *path,apr_pool_t *pool)
{
    if(!path || !*path) return NULL;
    int size=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,path,-1,NULL,0);
    if(!size) return NULL;
    wchar_t *wide=apr_palloc(pool,size*sizeof(*wide));
    return MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,path,-1,wide,size) ? wide : NULL;
}

static int valid_id(const char *id)
{
    if(!id || !*id || strlen(id)>64) return 0;
    for(const unsigned char *p=(const unsigned char *)id;*p;++p)
        if(!((*p>='a' && *p<='z') || (*p>='A' && *p<='Z') || (*p>='0' && *p<='9') || *p=='_' || *p=='-' || *p=='.')) return 0;
    return 1;
}

static void hex_encode(char *output,const unsigned char *input,size_t size)
{
    static const char digits[]="0123456789abcdef";
    for(size_t i=0;i<size;++i) { output[i*2]=digits[input[i]>>4]; output[i*2+1]=digits[input[i]&15]; }
    output[size*2]=0;
}

static int hex_decode(unsigned char *output,const char *input,size_t size)
{
    if(strlen(input)!=size*2) return 0;
    for(size_t i=0;i<size;++i) {
        unsigned value=0;
        for(int k=0;k<2;++k) {
            unsigned char c=(unsigned char)input[i*2+k];
            if(c>='0' && c<='9') value=value*16+c-'0';
            else if(c>='a' && c<='f') value=value*16+c-'a'+10;
            else return 0;
        }
        output[i]=(unsigned char)value;
    }
    return 1;
}

static apr_status_t clear_key(void *baton)
{
    SecureZeroMemory(baton,sizeof(pgvn_credential_key)); return APR_SUCCESS;
}

typedef struct secret_buffer { void *data; size_t size; } secret_buffer;
static apr_status_t clear_buffer(void *baton)
{
    secret_buffer *buffer=baton; SecureZeroMemory(buffer->data,buffer->size); return APR_SUCCESS;
}

static void *secret_allocate(apr_size_t size,apr_pool_t *pool)
{
    secret_buffer *buffer=apr_palloc(pool,sizeof(*buffer));
    buffer->data=apr_pcalloc(pool,size); buffer->size=size;
    apr_pool_cleanup_register(pool,buffer,clear_buffer,apr_pool_cleanup_null);
    return buffer->data;
}

static svn_boolean_t broad_sid(PSID sid)
{
    const WELL_KNOWN_SID_TYPE kinds[]={WinWorldSid,WinAuthenticatedUserSid,WinBuiltinUsersSid,
        WinBuiltinGuestsSid,WinAnonymousSid,WinNetworkSid,WinInteractiveSid};
    for(size_t i=0;i<sizeof(kinds)/sizeof(kinds[0]);++i) if(IsWellKnownSid(sid,kinds[i])) return TRUE;
    return FALSE;
}

static int key_acl_valid(HANDLE file)
{
    PSECURITY_DESCRIPTOR descriptor=NULL; PACL dacl=NULL;
    DWORD error=GetSecurityInfo(file,SE_FILE_OBJECT,DACL_SECURITY_INFORMATION,NULL,NULL,&dacl,NULL,&descriptor);
    if(error || !descriptor) return 0;
    SECURITY_DESCRIPTOR_CONTROL control=0; DWORD revision=0;
    int valid=dacl && GetSecurityDescriptorControl(descriptor,&control,&revision) && (control&SE_DACL_PROTECTED);
    for(DWORD i=0;valid && i<dacl->AceCount;++i) {
        void *ace=NULL;
        if(!GetAce(dacl,i,&ace)) { valid=0; break; }
        ACE_HEADER *header=ace;
        if(header->AceFlags&INHERIT_ONLY_ACE) continue;
        if(header->AceType==ACCESS_ALLOWED_ACE_TYPE) {
            ACCESS_ALLOWED_ACE *allowed=ace;
            if(allowed->Mask && broad_sid((PSID)&allowed->SidStart)) valid=0;
        } else if(header->AceType!=ACCESS_DENIED_ACE_TYPE) {
            /* Unknown/object/callback allow ACEs require an explicit ACL repair. */
            valid=0;
        }
    }
    LocalFree(descriptor); return valid;
}

svn_error_t *pgvn_key_create(const char *path,const char *key_id,apr_pool_t *pool)
{
    wchar_t *wide=wide_path(path,pool);
    if(!wide || !valid_id(key_id)) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid credential key path or ID");
    HANDLE token=NULL; DWORD size=0;
    if(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token)) return key_error();
    (void)GetTokenInformation(token,TokenUser,NULL,0,&size);
    TOKEN_USER *user=apr_palloc(pool,size);
    BOOL got=GetTokenInformation(token,TokenUser,user,size,&size); CloseHandle(token);
    wchar_t *sid=NULL;
    if(!got || !ConvertSidToStringSidW(user->User.Sid,&sid)) return key_error();
    size_t length=wcslen(sid)+80;
    wchar_t *sddl=apr_palloc(pool,length*sizeof(*sddl));
    swprintf(sddl,length,L"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;%ls)",sid); LocalFree(sid);
    PSECURITY_DESCRIPTOR descriptor=NULL;
    if(!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,SDDL_REVISION_1,&descriptor,NULL)) return key_error();
    SECURITY_ATTRIBUTES security={sizeof(security),descriptor,FALSE};
    HANDLE file=CreateFileW(wide,GENERIC_WRITE|READ_CONTROL,0,&security,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,NULL);
    DWORD error=file==INVALID_HANDLE_VALUE ? GetLastError() : ERROR_SUCCESS;
    LocalFree(descriptor);
    if(file==INVALID_HANDLE_VALUE) return svn_error_create(error==ERROR_FILE_EXISTS || error==ERROR_ALREADY_EXISTS ?
        SVN_ERR_FS_ALREADY_EXISTS : SVN_ERR_AUTHN_FAILED,NULL,"PgVN credential key creation failed; existing files are never replaced");
    unsigned char secret[32]={0}; char encoded[65],text[160]; DWORD written=0;
    int valid=BCryptGenRandom(NULL,secret,sizeof(secret),BCRYPT_USE_SYSTEM_PREFERRED_RNG)>=0;
    hex_encode(encoded,secret,sizeof(secret));
    int count=snprintf(text,sizeof(text),"PGVNKEY1\n%s\n%s\n",key_id,encoded);
    valid=valid && count>0 && count<(int)sizeof(text) && WriteFile(file,text,(DWORD)count,&written,NULL)
        && written==(DWORD)count && FlushFileBuffers(file) && key_acl_valid(file);
    SecureZeroMemory(secret,sizeof(secret)); SecureZeroMemory(encoded,sizeof(encoded)); SecureZeroMemory(text,sizeof(text));
    CloseHandle(file);
    if(!valid) { (void)DeleteFileW(wide); return key_error(); }
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_key_load(pgvn_credential_key **output,const char *path,apr_pool_t *pool)
{
    *output=NULL;
    wchar_t *wide=wide_path(path,pool);
    if(!wide) return key_error();
    HANDLE file=CreateFileW(wide,GENERIC_READ|READ_CONTROL,FILE_SHARE_READ,NULL,OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT|FILE_FLAG_SEQUENTIAL_SCAN,NULL);
    if(file==INVALID_HANDLE_VALUE) return key_error();
    BY_HANDLE_FILE_INFORMATION info; char text[160]={0}; DWORD size=0;
    int valid=GetFileInformationByHandle(file,&info) && !(info.dwFileAttributes&(FILE_ATTRIBUTE_REPARSE_POINT|FILE_ATTRIBUTE_DIRECTORY))
        && !info.nFileSizeHigh && info.nFileSizeLow>0 && info.nFileSizeLow<sizeof(text) && key_acl_valid(file)
        && ReadFile(file,text,sizeof(text)-1,&size,NULL) && size==info.nFileSizeLow;
    CloseHandle(file);
    pgvn_credential_key *key=apr_pcalloc(pool,sizeof(*key));
    apr_pool_cleanup_register(pool,key,clear_key,apr_pool_cleanup_null);
    if(valid && !memcmp(text,"PGVNKEY1\n",9) && strlen(text)==size) {
        char *id=text+9,*newline=strchr(id,'\n');
        if(newline) {
            *newline=0;
            char *encoded=newline+1;
            valid=valid_id(id) && strlen(encoded)==65 && encoded[64]=='\n';
            if(valid) { encoded[64]=0; valid=hex_decode(key->secret,encoded,32); }
            if(valid) memcpy(key->id,id,strlen(id)+1);
        } else valid=0;
    } else valid=0;
    SecureZeroMemory(text,sizeof(text));
    if(!valid) { SecureZeroMemory(key,sizeof(*key)); return key_error(); }
    *output=key; return SVN_NO_ERROR;
}

svn_error_t *pgvn_credential_identity(const char *domain,const char *user)
{
    const char *values[]={domain,user}; const size_t limits[]={128,256};
    for(int i=0;i<2;++i) {
        if(!values[i] || !*values[i] || strlen(values[i])>limits[i]
            || !MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,values[i],-1,NULL,0))
            return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid PgVN credential identity");
        for(const unsigned char *p=(const unsigned char *)values[i];*p;++p)
            if(*p<32 || *p==127) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Credential identity contains a control character");
    }
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_keyring_load(pgvn_credential_key **output,const char *current,const char *previous,apr_pool_t *pool)
{
    *output=NULL;
    pgvn_credential_key *keys;
    SVN_ERR(pgvn_key_load(&keys,current,pool));
    if(previous && *previous) {
        SVN_ERR(pgvn_key_load(&keys->previous,previous,pool));
        if(!strcmp(keys->id,keys->previous->id))
            return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN deployment keys must have distinct key IDs");
    }
    *output=keys; return SVN_NO_ERROR;
}

static svn_error_t *crypt_password(svn_boolean_t encrypt,const pgvn_credential_key *key,const char *domain,const char *user,
    unsigned char *nonce,unsigned char *tag,const unsigned char *input,ULONG size,unsigned char *output,apr_pool_t *pool)
{
    BCRYPT_ALG_HANDLE algorithm=NULL; BCRYPT_KEY_HANDLE handle=NULL; ULONG bytes=0,object_size=0;
    NTSTATUS status=BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_AES_ALGORITHM,NULL,0);
    if(status>=0) status=BCryptSetProperty(algorithm,BCRYPT_CHAINING_MODE,(PUCHAR)BCRYPT_CHAIN_MODE_GCM,sizeof(BCRYPT_CHAIN_MODE_GCM),0);
    if(status>=0) status=BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,(PUCHAR)&object_size,sizeof(object_size),&bytes,0);
    unsigned char *object=secret_allocate(object_size ? object_size : 1,pool);
    if(status>=0) status=BCryptGenerateSymmetricKey(algorithm,&handle,object,object_size,(PUCHAR)key->secret,32,0);
    const char *aad=apr_psprintf(pool,"%s\n%s\n%s",domain,user,key->id);
    BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO auth; BCRYPT_INIT_AUTH_MODE_INFO(auth);
    auth.pbNonce=nonce; auth.cbNonce=12; auth.pbTag=tag; auth.cbTag=16;
    auth.pbAuthData=(PUCHAR)aad; auth.cbAuthData=(ULONG)strlen(aad);
    if(status>=0) status=encrypt ? BCryptEncrypt(handle,(PUCHAR)input,size,&auth,NULL,0,output,size,&bytes,0)
        : BCryptDecrypt(handle,(PUCHAR)input,size,&auth,NULL,0,output,size,&bytes,0);
    if(handle) BCryptDestroyKey(handle);
    if(algorithm) BCryptCloseAlgorithmProvider(algorithm,0);
    SecureZeroMemory(object,object_size);
    if(status<0 || bytes!=size) {
        SecureZeroMemory(output,size);
        return svn_error_create(SVN_ERR_AUTHN_FAILED,NULL,"PgVN credential encryption or authentication failed");
    }
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_credential_put(pgvn_context *context,const pgvn_credential_key *key,
    const char *domain,const char *user,const char *password,apr_pool_t *pool)
{
    SVN_ERR(pgvn_require_admin(context));
    SVN_ERR(pgvn_credential_identity(domain,user));
    if(!key || !password || !*password || strlen(password)>4096
        || !MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,password,-1,NULL,0))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"A nonempty UTF-8 SVN password of at most 4096 bytes is required");
    unsigned char nonce[12],tag[16],cipher[4096]; size_t size=strlen(password);
    if(BCryptGenRandom(NULL,nonce,sizeof(nonce),BCRYPT_USE_SYSTEM_PREFERRED_RNG)<0) return key_error();
    SVN_ERR(crypt_password(TRUE,key,domain,user,nonce,tag,(const unsigned char *)password,(ULONG)size,cipher,pool));
    char nonce_hex[25],tag_hex[33],cipher_hex[8193];
    hex_encode(nonce_hex,nonce,sizeof(nonce)); hex_encode(tag_hex,tag,sizeof(tag)); hex_encode(cipher_hex,cipher,size);
    SecureZeroMemory(cipher,sizeof(cipher));
    const char *values[]={domain,user,key->id,nonce_hex,cipher_hex,tag_hex}; PGresult *result;
    SVN_ERR(pgvn_control_query(&result,context,
        "INSERT INTO pgvn.credentials(identity_domain,username,key_id,nonce,ciphertext,authentication_tag) "
        "VALUES($1,$2,$3,decode($4,'hex'),decode($5,'hex'),decode($6,'hex')) "
        "ON CONFLICT(identity_domain,username) DO UPDATE SET generation=pgvn.credentials.generation+1,enabled=true,"
        "key_id=EXCLUDED.key_id,nonce=EXCLUDED.nonce,ciphertext=EXCLUDED.ciphertext,"
        "authentication_tag=EXCLUDED.authentication_tag,updated_at=clock_timestamp()",6,values));
    PQclear(result); return SVN_NO_ERROR;
}

static svn_error_t *credential_read(const char **password,apr_int64_t *generation,pgvn_context *context,
    const pgvn_credential_key *keys,const char *domain,const char *user,svn_boolean_t disabled,apr_pool_t *pool)
{
    *password=NULL; *generation=0;
    SVN_ERR(pgvn_credential_identity(domain,user));
    if(!keys) return key_error();
    const char *values[]={domain,user,disabled ? "true" : "false"}; PGresult *result;
    SVN_ERR(pgvn_control_query(&result,context,
        "SELECT generation,key_id,encode(nonce,'hex'),encode(ciphertext,'hex'),encode(authentication_tag,'hex') "
        "FROM pgvn.credentials WHERE identity_domain=$1 AND username=$2 AND (enabled OR $3::boolean)",3,values));
    if(PQntuples(result)!=1) { PQclear(result); return SVN_NO_ERROR; }
    size_t size=strlen(PQgetvalue(result,0,3))/2;
    const pgvn_credential_key *key=keys;
    while(key && strcmp(key->id,PQgetvalue(result,0,1))) key=key->previous;
    unsigned char nonce[12],tag[16],cipher[4096];
    int valid=key && size>0 && size<=sizeof(cipher)
        && hex_decode(nonce,PQgetvalue(result,0,2),12) && hex_decode(tag,PQgetvalue(result,0,4),16)
        && hex_decode(cipher,PQgetvalue(result,0,3),size);
    apr_int64_t version=apr_atoi64(PQgetvalue(result,0,0)); PQclear(result);
    if(!valid) return key_error();
    unsigned char *plain=secret_allocate(size+1,pool);
    svn_error_t *error=crypt_password(FALSE,key,domain,user,nonce,tag,cipher,(ULONG)size,plain,pool);
    SecureZeroMemory(cipher,sizeof(cipher));
    if(error) return error;
    if(memchr(plain,0,size) || !MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,(const char *)plain,-1,NULL,0)) {
        SecureZeroMemory(plain,size); return key_error();
    }
    *password=(const char *)plain; *generation=version;
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_credential_get(const char **password,apr_int64_t *generation,pgvn_context *context,
    const pgvn_credential_key *keys,const char *domain,const char *user,apr_pool_t *pool)
{
    return credential_read(password,generation,context,keys,domain,user,FALSE,pool);
}

svn_error_t *pgvn_credential_rekey(pgvn_context *context,const pgvn_credential_key *old_key,
    const pgvn_credential_key *new_key,const char *domain,const char *user,apr_pool_t *pool)
{
    SVN_ERR(pgvn_require_admin(context));
    if(!old_key || !new_key || !strcmp(old_key->id,new_key->id))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Rekey requires old and new deployment keys with distinct IDs");
    apr_pool_t *scratch=NULL;
    /* A child pool owns all decrypted material and is cleared before returning. */
    apr_status_t status=apr_pool_create(&scratch,pool);
    if(status) return svn_error_wrap_apr(status,"Credential rotation scratch pool failed");
    const char *password=NULL; apr_int64_t generation=0;
    svn_error_t *error=credential_read(&password,&generation,context,old_key,domain,user,TRUE,scratch);
    if(!error && !password) error=svn_error_create(SVN_ERR_FS_NOT_FOUND,NULL,"PgVN credential identity was not found");
    if(!error) {
        unsigned char nonce[12],tag[16],cipher[4096]; size_t size=strlen(password);
        if(BCryptGenRandom(NULL,nonce,sizeof(nonce),BCRYPT_USE_SYSTEM_PREFERRED_RNG)<0) error=key_error();
        if(!error) error=crypt_password(TRUE,new_key,domain,user,nonce,tag,(const unsigned char *)password,(ULONG)size,cipher,scratch);
        if(!error) {
            char nonce_hex[25],tag_hex[33],cipher_hex[8193];
            hex_encode(nonce_hex,nonce,12); hex_encode(tag_hex,tag,16); hex_encode(cipher_hex,cipher,size);
            const char *values[]={domain,user,new_key->id,nonce_hex,cipher_hex,tag_hex,
                apr_psprintf(scratch,"%" APR_INT64_T_FMT,generation),old_key->id}; PGresult *result=NULL;
            error=pgvn_control_query(&result,context,
                "UPDATE pgvn.credentials SET key_id=$3,nonce=decode($4,'hex'),ciphertext=decode($5,'hex'),"
                "authentication_tag=decode($6,'hex'),generation=generation+1,updated_at=clock_timestamp() "
                "WHERE identity_domain=$1 AND username=$2 AND generation=$7::bigint AND key_id=$8 RETURNING generation",8,values);
            if(!error && PQntuples(result)!=1) error=svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"Credential changed during key rotation; retry with its current key");
            if(result) PQclear(result);
        }
        SecureZeroMemory(cipher,sizeof(cipher));
    }
    apr_pool_destroy(scratch); return error;
}

svn_error_t *pgvn_credential_check(pgvn_context *context,const char *domain,
    const char *user,apr_int64_t generation,apr_pool_t *pool)
{
    const char *values[]={domain,user,apr_psprintf(pool,"%" APR_INT64_T_FMT,generation)}; PGresult *result;
    SVN_ERR(pgvn_control_query(&result,context,"SELECT enabled AND generation=$3::bigint FROM pgvn.credentials "
        "WHERE identity_domain=$1 AND username=$2",3,values));
    svn_boolean_t valid=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
    return valid ? SVN_NO_ERROR : svn_error_create(SVN_ERR_RA_NOT_AUTHORIZED,NULL,"PgVN credential was revoked or changed; authenticate again");
}

svn_error_t *pgvn_credential_guard(pgvn_context *context,PGconn *connection,
    const char *domain,const char *user,apr_int64_t generation,apr_pool_t *pool)
{
    const char *values[]={domain,user,apr_psprintf(pool,"%" APR_INT64_T_FMT,generation)}; PGresult *result;
    SVN_ERR(pgvn_exec(&result,context,connection,"SELECT pgvn.guard_credential($1,$2,$3::bigint)",3,values));
    svn_boolean_t valid=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
    return valid ? SVN_NO_ERROR : svn_error_create(SVN_ERR_RA_NOT_AUTHORIZED,NULL,"PgVN credential changed before publication");
}
