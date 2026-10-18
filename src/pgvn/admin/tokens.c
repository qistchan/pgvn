#include <string.h>
#include "admin.h"
#include "../platform/credentials.h"
#ifdef _WIN32
#include <bcrypt.h>
#else
#include <openssl/evp.h>
#endif
#include <apr_strings.h>
#include <apr_uuid.h>

static svn_error_t *sha256(unsigned char digest[32],const unsigned char *value,size_t length,apr_pool_t *pool)
{
#ifdef _WIN32
    BCRYPT_ALG_HANDLE algorithm=NULL; BCRYPT_HASH_HANDLE hash=NULL;
    DWORD object_size=0,received=0;
    NTSTATUS status=BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,NULL,0);
    if(!status) status=BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,(PUCHAR)&object_size,sizeof(object_size),&received,0);
    if(!status && (!object_size || object_size>1048576)) status=(NTSTATUS)0xc000000d;
    if(!status) status=BCryptCreateHash(algorithm,&hash,apr_palloc(pool,object_size),object_size,NULL,0,0);
    if(!status) status=BCryptHashData(hash,(PUCHAR)value,(ULONG)length,0);
    if(!status) status=BCryptFinishHash(hash,digest,32,0);
    if(hash) BCryptDestroyHash(hash);
    if(algorithm) BCryptCloseAlgorithmProvider(algorithm,0);
    if(status) return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"Windows SHA-256 operation failed");
#else
    unsigned int size=0;
    if(EVP_Digest(value,length,digest,&size,EVP_sha256(),NULL)!=1 || size!=32)
        return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"SHA-256 operation failed");
#endif
    return SVN_NO_ERROR;
}

static void hex(char *output,const unsigned char *bytes,apr_size_t size)
{
    static const char digits[]="0123456789abcdef";
    for(apr_size_t i=0;i<size;++i) { output[2*i]=digits[bytes[i]>>4]; output[2*i+1]=digits[bytes[i]&15]; }
    output[2*size]=0;
}

svn_error_t *admin_tokens(admin_command *command)
{
    const char *op=command->argv[1];
    if(!strcmp(op,"create")) {
        const char *label=command->values[OPT_LABEL];
        int characters=0;
        if(label) for(const unsigned char *p=(const unsigned char *)label;*p;++p)
            if((*p & 0xc0)!=0x80) ++characters;
        if(characters<1 || characters>100) return admin_usage("Token label must contain 1-100 characters");
        apr_int64_t seconds=86400;
        if(command->values[OPT_EXPIRES]) SVN_ERR(admin_number(&seconds,command->values[OPT_EXPIRES],1,31536000));
        unsigned char random[32],digest[32]; char token[70],hash[65];
        if(!pgvn_secure_random(random,sizeof(random))) {
            pgvn_secure_zero(random,sizeof(random));
            return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"Cryptographic random generation failed");
        }
        memcpy(token,"pgvn_",5); hex(token+5,random,sizeof(random));
        pgvn_secure_zero(random,sizeof(random));
        svn_error_t *error=sha256(digest,(const unsigned char *)token,strlen(token),command->pool);
        if(error) { pgvn_secure_zero(token,sizeof(token)); return error; }
        hex(hash,digest,sizeof(digest));
        const char *id=pgvn_new_uuid(command->pool);
        const char *values[]={id,hash,label,apr_psprintf(command->pool,"%" APR_INT64_T_FMT,seconds)};
        PGresult *result;
        error=pgvn_control_query(&result,command->context,
            "WITH stamp AS (SELECT clock_timestamp() AS now) "
            "INSERT INTO pgvn.monitor_tokens(token_id,token_hash,label,created_at,expires_at) "
            "SELECT $1::uuid,decode($2,'hex'),$3,now,now+make_interval(secs=>$4::integer) FROM stamp RETURNING expires_at::text",4,values);
        if(error) { pgvn_secure_zero(token,sizeof(token)); return error; }
        fputs("{\"id\":",stdout); admin_json_cstr(stdout,id);
        fputs(",\"token\":",stdout); admin_json_cstr(stdout,token);
        fputs(",\"scope\":\"monitor:read\",\"expires_at\":",stdout); admin_json_cstr(stdout,PQgetvalue(result,0,0)); puts("}");
        pgvn_secure_zero(token,sizeof(token)); PQclear(result);
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"list")) return admin_emit_query(command,
        "SELECT json_build_object('items',COALESCE(json_agg(q),'[]'::json)) FROM "
        "(SELECT token_id AS id,label,scope,created_at,expires_at,revoked_at,expires_at<=clock_timestamp() AS expired "
        "FROM pgvn.monitor_tokens ORDER BY created_at,token_id) q",0,NULL);
    apr_uuid_t parsed;
    if(apr_uuid_parse(&parsed,command->argv[2])!=APR_SUCCESS) return admin_usage("Token ID must be a UUID");
    const char *values[]={command->argv[2],NULL};
    if(!strcmp(op,"expire")) {
        apr_int64_t seconds;
        SVN_ERR(admin_number(&seconds,command->values[OPT_EXPIRES],0,31536000));
        values[1]=apr_psprintf(command->pool,"%" APR_INT64_T_FMT,seconds);
        return admin_emit_query(command,
            "UPDATE pgvn.monitor_tokens SET expires_at=clock_timestamp()+make_interval(secs=>$2::integer) WHERE token_id=$1::uuid "
            "RETURNING json_build_object('id',token_id,'expires_at',expires_at,'revoked_at',revoked_at)",2,values);
    }
    return admin_emit_query(command,
        "UPDATE pgvn.monitor_tokens SET revoked_at=COALESCE(revoked_at,clock_timestamp()) WHERE token_id=$1::uuid "
        "RETURNING json_build_object('id',token_id,'revoked_at',revoked_at)",1,values);
}
