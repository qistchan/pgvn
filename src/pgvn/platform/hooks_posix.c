#include <stdlib.h>
#include <string.h>
#include "internal.h"
#include "process.h"
#include <apr_strings.h>
svn_error_t *pgvn_external_hook(const pgvn_policy *policy,const pgvn_hook_context *event,apr_pool_t *pool)
{
    const pgvn_hook_handler *handler=policy->external_hooks[event->event];
    if(!handler || !pgvn_hook_filename_valid(handler->executable,FALSE) || !policy->context->hook_root ||
       handler->timeout_seconds<1 || handler->timeout_seconds>60 || (handler->arguments && handler->arguments->nelts>16))
        return svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN external hook is not configured on this node");
    svn_stringbuf_t *input; SVN_ERR(pgvn_hook_event_json(&input,event,pool));
    int count=handler->arguments ? handler->arguments->nelts : 0;
    const char **arguments=apr_pcalloc(pool,(size_t)(count+6)*sizeof(*arguments));
    arguments[0]="pgvn-hook-runner"; arguments[1]="--hook";
    arguments[2]=apr_psprintf(pool,"%u",handler->timeout_seconds); arguments[3]=policy->context->hook_root;
    arguments[4]=handler->executable;
    for(int i=0;i<count;++i) arguments[i+5]=APR_ARRAY_IDX(handler->arguments,i,const char *);
    char *environment[32]; int entries=0;
    environment[entries++]="PATH=/usr/bin:/bin"; environment[entries++]="LANG=C.UTF-8";
    const char *inherit[]={"PGSERVICEFILE","PGPASSFILE","PGSYSCONFDIR"};
    for(unsigned i=0;i<sizeof(inherit)/sizeof(*inherit);++i) {
        const char *value=getenv(inherit[i]);
        if(value) {
            if(strlen(value)>8192) return svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN hook environment exceeds its limit");
            environment[entries++]=apr_pstrcat(pool,inherit[i],"=",value,NULL);
        }
    }
    const char *names[]={"PGVN_EVENT","PGVN_REPOSITORY_ID","PGVN_USER","PGVN_TRANSACTION","PGVN_REVISION","PGVN_CONFIG"};
    const char *values[]={pgvn_hook_name(event->event),event->repository_id,event->username,event->transaction_id,
        apr_psprintf(pool,"%ld",event->revision),policy->context->configuration_path};
    for(unsigned i=0;i<sizeof(names)/sizeof(*names);++i)
        environment[entries++]=apr_pstrcat(pool,names[i],"=",values[i] ? values[i] : "",NULL);
    environment[entries]=NULL;
    pgvn_process_result result;
    svn_error_t *error=pgvn_process_run(&result,policy->context->hook_runner,arguments,environment,
        input->data,input->len,policy->context->cancellation_event,pool);
    if(error) return svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,error,"PgVN hook could not run");
    if(result.cancelled) return svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN hook cancelled during service shutdown");
    return pgvn_hook_result(event,(unsigned)result.exit_code,result.output.bytes,result.output.size,result.output.truncated,
        result.error.bytes,result.error.size,pool);
}
