/* Cross-process probe for production DAV registry APIs, independent of Apache. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <io.h>
#include <apr_general.h>
#include "internal.h"
#include "dav_state.h"
#include "svn_pools.h"
#include "svn_props.h"

int wmain(int argc,wchar_t **wide)
{
    _setmode(_fileno(stdout),_O_BINARY);
    if(argc!=6 || apr_initialize()!=APR_SUCCESS) return 2;
    apr_pool_t *pool=svn_pool_create(NULL); const char *args[6]={0};
    for(int i=1;i<6;++i) {
        int length=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,wide[i],-1,NULL,0,NULL,NULL);
        if(!length) { svn_pool_destroy(pool); apr_terminate(); return 2; }
        char *text=apr_palloc(pool,length); WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,wide[i],-1,text,length,NULL,NULL); args[i]=text;
    }
    pgvn_context *context=NULL; pgvn_repository *repo=NULL;
    pgvn_context_options options={getenv("PGVN_DSN"),3,5000};
    svn_error_t *error=pgvn_context_create(&context,&options,pool);
    if(!error) { context->hook_root=getenv("PGVN_TEST_HOOK_ROOT"); error=pgvn_repos_open(&repo,context,args[2],pool); }
    const char *user=strcmp(args[3],"-") ? args[3] : NULL;
    pgvn_dav_alias_kind kind=!strcmp(args[4],"native") ? PGVN_DAV_NATIVE :
        !strcmp(args[4],"activity") ? PGVN_DAV_ACTIVITY : !strcmp(args[4],"virtual") ? PGVN_DAV_VIRTUAL : (pgvn_dav_alias_kind)99;
    const char *id=NULL; pgvn_dav_txn_info *info=NULL;
    if(!error && !strcmp(args[1],"create")) {
        apr_hash_t *properties=apr_hash_make(pool);
        const char binary[]={'d',0,(char)0xff,'v'};
        apr_hash_set(properties,"pgvn:binary",APR_HASH_KEY_STRING,svn_string_ncreate(binary,sizeof(binary),pool));
        error=pgvn_dav_create(&id,repo,kind,kind==PGVN_DAV_NATIVE ? NULL : args[5],user,SVN_INVALID_REVNUM,properties,pool);
    } else if(!error && !strcmp(args[1],"lookup")) {
        error=pgvn_dav_lookup(&info,repo,kind,args[5],user,pool); if(!error) id=info->transaction_id;
    } else if(!error && !strcmp(args[1],"cancel")) error=pgvn_dav_cancel(repo,kind,args[5],user,pool);
    else if(!error && !strcmp(args[1],"recover")) error=pgvn_dav_recover_creation(repo,args[5],user,pool);
    else if(!error) error=svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Unknown registry probe operation");
    static const char *phases[]={"creating","active","committed","aborted","deleted"};
    printf("{\"ok\":%s,\"transaction_id\":\"%s\",\"phase\":\"%s\",\"committed_revision\":%ld,\"creation_pending\":%s,\"creation_failed\":%s,\"error_code\":%ld}\n",
        error ? "false" : "true",id ? id : "",info ? phases[info->phase] : "",info ? info->committed_revision : SVN_INVALID_REVNUM,
        info && info->creation_pending ? "true" : "false",info && info->creation_failed ? "true" : "false",(long)(error ? error->apr_err : 0));
    int code=error ? 1 : 0;
    if(error) { svn_handle_error2(error,stderr,FALSE,"PgVN registry probe: "); svn_error_clear(error); }
    svn_pool_destroy(pool); apr_terminate(); return code;
}
