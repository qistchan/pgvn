#include <string.h>
#include <stdio.h>
#include "credentials.h"
#include "../platform/credentials.h"
#include <apr_strings.h>

static svn_error_t *key_error(void)
{
    return svn_error_create(SVN_ERR_AUTHN_FAILED,NULL,"PgVN credential key is unavailable, invalid or insufficiently protected");
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
    pgvn_secure_zero(baton,sizeof(pgvn_credential_key)); return APR_SUCCESS;
}

typedef struct secret_buffer { void *data; size_t size; } secret_buffer;
static apr_status_t clear_buffer(void *baton)
{
    secret_buffer *buffer=baton; pgvn_secure_zero(buffer->data,buffer->size); return APR_SUCCESS;
}

static void *secret_allocate(apr_size_t size,apr_pool_t *pool)
{
    secret_buffer *buffer=apr_palloc(pool,sizeof(*buffer));
    buffer->data=apr_pcalloc(pool,size); buffer->size=size;
    apr_pool_cleanup_register(pool,buffer,clear_buffer,apr_pool_cleanup_null);
    return buffer->data;
}

svn_error_t *pgvn_key_create(const char *path,const char *key_id,apr_pool_t *pool)
{
    if(!path || !*path || !pgvn_utf8_valid(path) || !valid_id(key_id))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid credential key path or ID");
    unsigned char secret[32]={0}; char encoded[65]={0},text[160]={0};
    svn_error_t *error=NULL;
    if(!pgvn_secure_random(secret,sizeof(secret))) error=key_error();
    if(!error) {
        hex_encode(encoded,secret,sizeof(secret));
        int count=snprintf(text,sizeof(text),"PGVNKEY1\n%s\n%s\n",key_id,encoded);
        if(count<=0 || count>=(int)sizeof(text)) error=key_error();
        else error=pgvn_keyfile_write(path,text,(size_t)count,pool);
    }
    pgvn_secure_zero(secret,sizeof(secret)); pgvn_secure_zero(encoded,sizeof(encoded)); pgvn_secure_zero(text,sizeof(text));
    return error;
}

svn_error_t *pgvn_key_load(pgvn_credential_key **output,const char *path,apr_pool_t *pool)
{
    *output=NULL;
    if(!path || !*path || !pgvn_utf8_valid(path)) return key_error();
    char text[160]={0},normalized[160]={0}; size_t size=0,length=0;
    svn_error_t *error=pgvn_keyfile_read(path,text,sizeof(text),&size,pool);
    if(error) return error;
    int valid=size>0 && size<sizeof(text) && !memchr(text,0,size);
    int crlf=memchr(text,'\r',size)!=NULL;
    for(size_t i=0;valid && i<size;++i) {
        if(text[i]=='\r') {
            if(i+1>=size || text[i+1]!='\n') { valid=0; break; }
            ++i;
        } else if(crlf && text[i]=='\n') { valid=0; break; }
        normalized[length++]=text[i];
    }
    pgvn_credential_key *key=apr_pcalloc(pool,sizeof(*key));
    apr_pool_cleanup_register(pool,key,clear_key,apr_pool_cleanup_null);
    if(valid && length>9 && !memcmp(normalized,"PGVNKEY1\n",9)) {
        char *id=normalized+9,*newline=strchr(id,'\n');
        if(newline) {
            *newline=0; char *encoded=newline+1;
            valid=valid_id(id) && strlen(encoded)==65 && encoded[64]=='\n';
            if(valid) { encoded[64]=0; valid=hex_decode(key->secret,encoded,32); }
            if(valid) memcpy(key->id,id,strlen(id)+1);
        } else valid=0;
    } else valid=0;
    pgvn_secure_zero(text,sizeof(text)); pgvn_secure_zero(normalized,sizeof(normalized));
    if(!valid) { pgvn_secure_zero(key,sizeof(*key)); return key_error(); }
    *output=key; return SVN_NO_ERROR;
}

svn_error_t *pgvn_credential_identity(const char *domain,const char *user)
{
    const char *values[]={domain,user}; const size_t limits[]={128,256};
    for(int i=0;i<2;++i) {
        if(!values[i] || !*values[i] || strlen(values[i])>limits[i]
            || !pgvn_utf8_valid(values[i]))
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
    unsigned char *nonce,unsigned char *tag,const unsigned char *input,size_t size,unsigned char *output,apr_pool_t *pool)
{
    const char *aad=apr_psprintf(pool,"%s\n%s\n%s",domain,user,key->id);
    if(!pgvn_credential_cipher(encrypt,key->secret,(const unsigned char *)aad,strlen(aad),nonce,tag,input,size,output,pool))
        return svn_error_create(SVN_ERR_AUTHN_FAILED,NULL,"PgVN credential encryption or authentication failed");
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_credential_put(pgvn_context *context,const pgvn_credential_key *key,
    const char *domain,const char *user,const char *password,apr_pool_t *pool)
{
    SVN_ERR(pgvn_require_admin(context));
    SVN_ERR(pgvn_credential_identity(domain,user));
    if(!key || !password || !*password || strlen(password)>4096
        || !pgvn_utf8_valid(password))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"A nonempty UTF-8 SVN password of at most 4096 bytes is required");
    unsigned char nonce[12],tag[16],cipher[4096]; size_t size=strlen(password);
    if(!pgvn_secure_random(nonce,sizeof(nonce))) return key_error();
    SVN_ERR(crypt_password(TRUE,key,domain,user,nonce,tag,(const unsigned char *)password,size,cipher,pool));
    char nonce_hex[25],tag_hex[33],cipher_hex[8193];
    hex_encode(nonce_hex,nonce,sizeof(nonce)); hex_encode(tag_hex,tag,sizeof(tag)); hex_encode(cipher_hex,cipher,size);
    pgvn_secure_zero(cipher,sizeof(cipher));
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
    svn_error_t *error=crypt_password(FALSE,key,domain,user,nonce,tag,cipher,size,plain,pool);
    pgvn_secure_zero(cipher,sizeof(cipher));
    if(error) return error;
    if(memchr(plain,0,size) || !pgvn_utf8_valid((const char *)plain)) {
        pgvn_secure_zero(plain,size); return key_error();
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
        if(!pgvn_secure_random(nonce,sizeof(nonce))) error=key_error();
        if(!error) error=crypt_password(TRUE,new_key,domain,user,nonce,tag,(const unsigned char *)password,size,cipher,scratch);
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
        pgvn_secure_zero(cipher,sizeof(cipher));
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
