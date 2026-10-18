#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <apr_general.h>
#include <apr_strings.h>
#include "internal.h"
#include "hooks_common.h"
#include "svn_string.h"
#include "svn_utf.h"

typedef struct cancel_baton {
    pgvn_cancel_state *state;
} cancel_baton;

static void *cancel_later(void *baton)
{
    cancel_baton *state=baton;
    struct timespec pause={0,300*1000*1000};
    nanosleep(&pause,NULL);
    pgvn_cancel_signal(state->state);
    return NULL;
}

static int report_error(svn_error_t *error)
{
    if(!error) return 0;
    char message[10000];
    fprintf(stderr,"FAIL: %s\n",svn_err_best_message(error,message,sizeof(message)));
    svn_error_clear(error);
    return 1;
}

int main(int argc,char **argv)
{
    if(argc==2 && !strcmp(argv[1],"--version")) {
        char json[PGVN_BUILD_JSON_SIZE];
        pgvn_build_json(json,sizeof(json));
        puts(json);
        return 0;
    }
    if(argc!=8) {
        fputs("usage: hook_runner_probe MODE HOOK_ROOT CGROUP_ROOT FILE TIMEOUT MARKER resolve|direct\n",stderr);
        return 2;
    }
    if(apr_initialize()!=APR_SUCCESS) return 1;
    apr_pool_t *pool=NULL;
    if(apr_pool_create(&pool,NULL)!=APR_SUCCESS) { apr_terminate(); return 1; }
    svn_utf_initialize2(TRUE,pool);
    int marker=open(argv[6],O_RDONLY);
    if(marker<0) {
        perror("open inherited descriptor fixture");
        apr_pool_destroy(pool); apr_terminate();
        return 1;
    }

    pgvn_context context={0};
    context.hook_root=argv[2];
    context.configuration_path="/etc/pgvn/hooks.ini";
    svn_config_t *config=NULL;
    svn_error_t *error=svn_config_create2(&config,TRUE,TRUE,pool);
    if(report_error(error)) goto fail;
    if(*argv[3]) svn_config_set(config,"pgvn","hook-cgroup-root",argv[3]);
    svn_config_set(config,"pgvn-hooks","runner",argv[4]);
    if(report_error(pgvn_hooks_configure(&context,config,pool))) goto fail;

    pgvn_hook_handler *handler=apr_pcalloc(pool,sizeof(*handler));
    handler->timeout_seconds=(unsigned)strtoul(argv[5],NULL,10);
    handler->arguments=apr_array_make(pool,3,sizeof(const char *));
    APR_ARRAY_PUSH(handler->arguments,const char *)="literal ; $(not-shell)";
    APR_ARRAY_PUSH(handler->arguments,const char *)="中文 参数";
    APR_ARRAY_PUSH(handler->arguments,const char *)="quote \" tail\\";
    if(!strcmp(argv[7],"resolve")) {
        if(report_error(pgvn_hook_resolve(handler,&context,"runner",pool))) goto fail;
    } else handler->executable=argv[4];
    if(!strcmp(argv[1],"large-argument")) {
        char *large=apr_palloc(pool,2050);
        memset(large,'x',2049);
        large[2049]=0;
        APR_ARRAY_PUSH(handler->arguments,const char *)=large;
    }

    pgvn_policy policy={0};
    policy.context=&context;
    policy.external_hooks[PGVN_HOOK_PRE_COMMIT]=handler;
    pgvn_hook_context event={0};
    event.repository_id="11111111-1111-4111-8111-111111111111";
    event.username="测试用户";
    event.transaction_id="txn-42";
    event.revision=42;
    event.event=PGVN_HOOK_PRE_COMMIT;
    event.path="/trunk/文件";
    event.property_name="svn:log";
    event.property_value=svn_string_ncreate("a\0b\xff",4,pool);
    if(!strcmp(argv[1],"large-property")) {
        apr_size_t size=16u*1024u*1024u+1;
        char *large=apr_palloc(pool,size);
        memset(large,'x',size);
        event.property_value=svn_string_ncreate(large,size,pool);
    }
    event.action='M';
    event.force=TRUE;
    apr_array_header_t *paths=apr_array_make(pool,2,sizeof(const char *));
    APR_ARRAY_PUSH(paths,const char *)="/trunk/文件";
    APR_ARRAY_PUSH(paths,const char *)="/trunk/第二个";
    event.paths=paths;
    apr_array_header_t *capabilities=apr_array_make(pool,1,sizeof(const char *));
    APR_ARRAY_PUSH(capabilities,const char *)="mergeinfo";
    event.capabilities=capabilities;
    const char *token=NULL;
    if(!strcmp(argv[1],"token") || !strcmp(argv[1],"bad-token")) event.lock_token_result=&token;

    pgvn_cancel_state cancellation={0};
    if(!strcmp(argv[1],"cancel")) context.cancellation_event=&cancellation;
    pthread_t cancel_thread;
    cancel_baton cancel={&cancellation};
    int cancel_started=!strcmp(argv[1],"cancel") && !pthread_create(&cancel_thread,NULL,cancel_later,&cancel);
    if(!strcmp(argv[1],"cancel") && !cancel_started) {
        fputs("FAIL: cancellation thread\n",stderr);
        goto fail;
    }
    error=pgvn_external_hook(&policy,&event,pool);
    if(cancel_started) pthread_join(cancel_thread,NULL);
    close(marker);
    if(error) {
        int failed=report_error(error);
        apr_pool_destroy(pool); apr_terminate();
        return failed ? 1 : 0;
    }
    if(token) printf("PASS token=%s\n",token);
    else puts("PASS");
    apr_pool_destroy(pool); apr_terminate();
    return 0;
fail:
    close(marker);
    apr_pool_destroy(pool); apr_terminate();
    return 1;
}
