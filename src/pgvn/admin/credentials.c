#include <string.h>
#include <io.h>
#include "admin.h"
#include "credentials.h"

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
    DWORD mode;
    if(GetConsoleMode((HANDLE)_get_osfhandle(_fileno(stdin)),&mode))
        return admin_usage("Provide the SVN password on redirected standard input; interactive echo is not permitted");
    char password[4099]; size_t size=fread(password,1,sizeof(password),stdin);
    if(ferror(stdin) || size==sizeof(password) || memchr(password,0,size)) {
        SecureZeroMemory(password,sizeof(password)); return admin_usage("Invalid SVN password on standard input");
    }
    if(size && password[size-1]=='\n') { --size; if(size && password[size-1]=='\r') --size; }
    password[size]=0;
    svn_error_t *error=pgvn_credential_put(command->context,key,domain,user,password,command->pool);
    SecureZeroMemory(password,sizeof(password));
    if(error) return error;
    admin_ok("user_set"); return SVN_NO_ERROR;
}
