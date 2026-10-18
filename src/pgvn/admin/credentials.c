#include <string.h>
#ifdef _WIN32
#include <io.h>
#else
#include <errno.h>
#include <poll.h>
#include <unistd.h>
#endif
#include "admin.h"
#include "credentials.h"
#include "../platform/credentials.h"

static svn_error_t *read_password(char *password,size_t capacity,size_t *size)
{
#ifdef _WIN32
    DWORD mode;
    if(GetConsoleMode((HANDLE)_get_osfhandle(_fileno(stdin)),&mode))
        return admin_usage("Provide the SVN password on redirected standard input; interactive echo is not permitted");
    *size=fread(password,1,capacity,stdin);
    if(ferror(stdin)) return admin_usage("Unable to read SVN password on standard input");
#else
    if(isatty(STDIN_FILENO))
        return admin_usage("Provide the SVN password on redirected standard input; interactive echo is not permitted");
    *size=0;
    while(*size<capacity) {
        SVN_ERR(admin_cancel(NULL));
        struct pollfd input={STDIN_FILENO,POLLIN,0};
        int ready=poll(&input,1,100);
        if(ready<0) {
            if(errno==EINTR) continue;
            return admin_usage("Unable to poll standard input");
        }
        if(!ready) continue;
        ssize_t amount=read(STDIN_FILENO,password+*size,capacity-*size);
        if(amount<0) {
            if(errno==EINTR) continue;
            return admin_usage("Unable to read SVN password on standard input");
        }
        if(!amount) break;
        *size+=(size_t)amount;
    }
#endif
    return SVN_NO_ERROR;
}

svn_error_t *admin_credentials(admin_command *command)
{
    if(!strcmp(command->argv[0],"key")) {
        SVN_ERR(pgvn_key_create(command->argv[2],command->argv[3],command->pool));
        admin_ok("key_created"); return SVN_NO_ERROR;
    }
    const char *verb=command->argv[1];
    const char *domain=command->argc>2 ? command->argv[2] : NULL;
    const char *user=command->argc>3 ? command->argv[3] : NULL;
    const char *values[]={domain,user};
    if(!strcmp(verb,"list")) return admin_emit_query(command,
        "SELECT COALESCE(json_agg(row_to_json(q) ORDER BY identity_domain,username),'[]'::json) FROM "
        "(SELECT identity_domain,username,generation,enabled,key_id,updated_at FROM pgvn.credentials "
        "WHERE $1 IS NULL OR identity_domain=$1 ORDER BY identity_domain,username LIMIT 1000) q",1,values);
    SVN_ERR(pgvn_credential_identity(domain,user));
    if(!strcmp(verb,"disable")) {
        PGresult *result;
        SVN_ERR(pgvn_control_query(&result,command->context,
            "UPDATE pgvn.credentials SET enabled=false,generation=generation+1,updated_at=clock_timestamp() "
            "WHERE identity_domain=$1 AND username=$2 RETURNING generation",2,values));
        svn_boolean_t found=PQntuples(result)==1; PQclear(result);
        if(!found) return svn_error_create(SVN_ERR_FS_NOT_FOUND,NULL,"PgVN credential identity was not found");
        admin_ok("user_disabled"); return SVN_NO_ERROR;
    }
    const char *path=command->values[OPT_KEY_FILE] ? command->values[OPT_KEY_FILE] : command->credential_key_file;
    pgvn_credential_key *key;
    SVN_ERR(pgvn_key_load(&key,path,command->pool));
    if(!strcmp(verb,"rekey")) {
        const char *old_path=command->values[OPT_OLD_KEY_FILE] ? command->values[OPT_OLD_KEY_FILE] : command->old_credential_key_file;
        pgvn_credential_key *old_key;
        SVN_ERR(pgvn_key_load(&old_key,old_path,command->pool));
        SVN_ERR(pgvn_credential_rekey(command->context,old_key,key,domain,user,command->pool));
        admin_ok("user_rekeyed"); return SVN_NO_ERROR;
    }
    char password[4099]; size_t size=0;
    svn_error_t *error=read_password(password,sizeof(password),&size);
    if(error) { pgvn_secure_zero(password,sizeof(password)); return error; }
    if(size>=sizeof(password)) {
        pgvn_secure_zero(password,sizeof(password)); return admin_usage("Invalid SVN password on standard input");
    }
    if(memchr(password,0,size)) {
        pgvn_secure_zero(password,sizeof(password)); return admin_usage("Invalid SVN password on standard input");
    }
    if(size && password[size-1]=='\n') { --size; if(size && password[size-1]=='\r') --size; }
    password[size]=0;
    error=admin_cancel(NULL);
    if(!error) error=pgvn_credential_put(command->context,key,domain,user,password,command->pool);
    pgvn_secure_zero(password,sizeof(password));
    if(error) return error;
    admin_ok("user_set"); return SVN_NO_ERROR;
}
