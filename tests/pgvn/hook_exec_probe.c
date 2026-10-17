/* Actual production hook dispatcher, serializer, launcher and supervisor. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <signal.h>
#include <pthread.h>
#include <sys/stat.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>
#include <apr_general.h>
#include <apr_strings.h>
#include "internal.h"
#include "../platform/runtime.h"
#include "svn_utf.h"
static unsigned checks;
static apr_pool_t *pool;
static pgvn_cancel_state cancelled;
static pgvn_context context;
static pgvn_policy policy;
static pgvn_hook_handler handler;
static pgvn_hook_context event;
static void require(int condition,const char *name)
{
    if(!condition) { fprintf(stderr,"FAIL: %s\n",name); exit(1); }
    ++checks;
}
static void success(svn_error_t *error,const char *name)
{
    if(error) { char buffer[1024]; fprintf(stderr,"FAIL: %s: %s\n",name,svn_err_best_message(error,buffer,sizeof(buffer))); svn_error_clear(error); exit(1); }
    ++checks;
}
static svn_error_t *invoke(const char *mode,const char *first,const char *second)
{
    handler.arguments=apr_array_make(pool,4,sizeof(const char *));
    APR_ARRAY_PUSH(handler.arguments,const char *)=mode;
    if(first) APR_ARRAY_PUSH(handler.arguments,const char *)=first;
    if(second) APR_ARRAY_PUSH(handler.arguments,const char *)=second;
    return pgvn_hook_dispatch(&policy,&event,pool);
}
static void rejected(svn_error_t *error,const char *name)
{
    require(error && error->apr_err==SVN_ERR_REPOS_HOOK_FAILURE,name); svn_error_clear(error);
}
static pid_t read_pid(const char *path)
{
    FILE *file=fopen(path,"r"); long pid=0;
    require(file && fscanf(file,"%ld",&pid)==1 && pid>0,"read descendant PID"); fclose(file); return (pid_t)pid;
}
static void gone(pid_t pid,const char *name)
{
    require(kill(pid,0)<0 && errno==ESRCH,name);
}
static void *cancel_worker(void *baton)
{
    const char *path=baton;
    for(int i=0;i<500 && access(path,F_OK);++i) apr_sleep(10000);
    pgvn_cancel_signal(&cancelled); return NULL;
}
int main(int argc,char **argv)
{
    require(argc==3,"hook-root and helper arguments");
    require(apr_initialize()==APR_SUCCESS && apr_pool_create(&pool,NULL)==APR_SUCCESS,"APR initialized");
    svn_utf_initialize2(TRUE,pool);
    context.hook_root=argv[1]; context.hook_runner=argv[2]; context.configuration_path="/non-secret/reference.ini"; context.cancellation_event=&cancelled;
    policy.context=&context; handler.executable="probe-child"; handler.timeout_seconds=5;
    for(int i=0;i<9;++i) policy.external_hooks[i]=&handler;
    event.event=PGVN_HOOK_PRE_COMMIT; event.repository_id="11111111-1111-4111-8111-111111111111";
    event.username="用戶é"; event.transaction_id="txn-1"; event.revision=42;
    event.path="/中文/quote\""; event.property_name="binary"; event.action='M'; event.force=TRUE;
    const char binary[]={0,(char)0xff,'x','\n'}; event.property_value=svn_string_ncreate(binary,sizeof(binary),pool);
    event.paths=apr_array_make(pool,2,sizeof(const char *));
    APR_ARRAY_PUSH((apr_array_header_t *)event.paths,const char *)="/中文";
    APR_ARRAY_PUSH((apr_array_header_t *)event.paths,const char *)="/a\\b";
    event.capabilities=apr_array_make(pool,1,sizeof(const char *)); APR_ARRAY_PUSH((apr_array_header_t *)event.capabilities,const char *)="mergeinfo";
    require(!setenv("PGVN_DSN","PUBLIC_TEST_SENTINEL",1) && !setenv("PGPASSWORD","PUBLIC_TEST_SENTINEL",1),"set parent sentinels");
    require(!setenv("PGSERVICEFILE","/non-secret/services.conf",1) && !setenv("PGPASSFILE","/non-secret/password-reference",1),"set allowed file references");
    const char *record=apr_pstrcat(pool,argv[1],"/record",NULL);
    handler.arguments=apr_array_make(pool,8,sizeof(const char *));
    const char *values[]={"record",record,"","中文 参数","& ; $(literal)","quote \" tail\\"};
    for(unsigned i=0;i<sizeof(values)/sizeof(*values);++i) APR_ARRAY_PUSH(handler.arguments,const char *)=values[i];
    success(pgvn_hook_dispatch(&policy,&event,pool),"literal arguments and UTF-8 event recorded");
    for(int i=0;i<9;++i) { event.event=(pgvn_hook_event)i; success(invoke("exit","0",NULL),"all nine events run"); }
    event.event=PGVN_HOOK_PRE_COMMIT;
    svn_error_t *error=invoke("exit","7","policy rejected the commit");
    require(error && error->apr_err==SVN_ERR_REPOS_HOOK_FAILURE && strstr(error->message,"Commit blocked") && strstr(error->message,"policy rejected"),"nonzero exit and stderr keep hook semantics"); svn_error_clear(error);
    error=invoke("bad-stderr",NULL,NULL); require(error && strstr(error->message,"non-text output omitted"),"binary stderr is omitted"); svn_error_clear(error);
    event.event=PGVN_HOOK_PRE_LOCK; const char *token=NULL; event.lock_token_result=&token;
    success(invoke("token"," \t令牌-abc\r\n",NULL),"UTF-8 pre-lock token"); require(token && !strcmp(token,"令牌-abc"),"token whitespace trimmed");
    rejected(invoke("token","line1\nline2",NULL),"multiline token rejected");
    rejected(invoke("binary-token",NULL,NULL),"NUL token rejected");
    rejected(invoke("long-token",NULL,NULL),"oversized/truncated token rejected");
    event.event=PGVN_HOOK_PRE_COMMIT; event.lock_token_result=NULL;
    char *large=apr_palloc(pool,16u*1024u*1024u); memset(large,0xa5,16u*1024u*1024u);
    event.property_value=svn_string_ncreate(large,16u*1024u*1024u,pool);
    success(invoke("flood",NULL,NULL),"large input and simultaneous stdout/stderr are drained without deadlock");
    success(invoke("close-input",NULL,NULL),"early stdin closure does not deliver SIGPIPE to caller");
    svn_string_t too_large={large,16u*1024u*1024u+1}; event.property_value=&too_large;
    rejected(invoke("exit","0",NULL),"property limit checked before launch"); event.property_value=NULL;
    event.username="bad\xff"; rejected(invoke("exit","0",NULL),"invalid UTF-8 event rejected"); event.username="用戶é";
    const char *pid_path=apr_pstrcat(pool,argv[1],"/detached.pid",NULL);
    success(invoke("grandchild",pid_path,NULL),"successful parent still reaps detached double-fork descendant"); gone(read_pid(pid_path),"successful detached descendant reaped"); require(!unlink(pid_path),"remove PID fixture");
    handler.timeout_seconds=1; uint64_t start=pgvn_tick();
    rejected(invoke("tree-sleep",pid_path,NULL),"timeout rejects sleeping process tree");
    require(pgvn_tick()-start>=900 && pgvn_tick()-start<5000,"monotonic timeout bounded"); gone(read_pid(pid_path),"timeout reaps setsid descendant"); require(!unlink(pid_path),"remove timeout PID fixture");
    handler.timeout_seconds=60; pthread_t canceller;
    require(!pthread_create(&canceller,NULL,cancel_worker,(void *)pid_path),"start cancellation worker"); start=pgvn_tick();
    rejected(invoke("tree-sleep",pid_path,NULL),"service cancellation rejects hook");
    require(!pthread_join(canceller,NULL) && pgvn_tick()-start<5000,"cancel promptly joins worker"); gone(read_pid(pid_path),"cancel reaps setsid descendant"); require(!unlink(pid_path),"remove cancel PID fixture"); pgvn_exchange32(&cancelled.signalled,0);
    /* Test caller death without relying on the caller's own cleanup handlers. */
    require(!prctl(PR_SET_CHILD_SUBREAPER,1),"test can adopt orphaned supervisor");
    pid_t caller=fork(); require(caller>=0,"fork isolated caller");
    if(!caller) { svn_error_t *child_error=invoke("tree-sleep",pid_path,NULL); svn_error_clear(child_error); _exit(22); }
    for(int i=0;i<500 && access(pid_path,F_OK);++i) apr_sleep(10000);
    pid_t detached=read_pid(pid_path); require(!kill(caller,SIGKILL),"kill only owned caller"); int status;
    require(waitpid(caller,&status,0)==caller,"reap owned caller");
    pid_t supervisor=0; start=pgvn_tick();
    while(pgvn_tick()-start<5000) { supervisor=waitpid(-1,&status,WNOHANG); if(supervisor>0) break; apr_sleep(10000); }
    require(supervisor>0 && WIFEXITED(status) && WEXITSTATUS(status)==143,"private pipe EOF makes orphaned supervisor clean up and exit");
    gone(detached,"caller death reaps detached descendant"); require(!unlink(pid_path) && !prctl(PR_SET_CHILD_SUBREAPER,0),"restore fixture subreaper state");
    handler.timeout_seconds=5;
    const char *child=apr_pstrcat(pool,argv[1],"/probe-child",NULL),*linked=apr_pstrcat(pool,argv[1],"/linked-child",NULL);
    require(!symlink(child,linked),"create executable symlink fixture"); handler.executable="linked-child";
    rejected(invoke("exit","0",NULL),"launch-time symlink rejected"); require(!unlink(linked),"remove executable symlink"); handler.executable="probe-child";
    require(!chmod(child,0777),"make executable unprotected"); rejected(invoke("exit","0",NULL),"launch-time writable executable rejected"); require(!chmod(child,0700),"restore executable mode");
    const char *script=apr_pstrcat(pool,argv[1],"/script-hook",NULL); FILE *file=fopen(script,"w");
    require(file && fputs("#!/usr/bin/python3\nimport sys\nsys.stdin.buffer.read()\nprint('script-中文')\n",file)>=0 && !fclose(file) && !chmod(script,0700),"create protected shebang fixture");
    handler.executable="script-hook"; event.event=PGVN_HOOK_PRE_LOCK; event.lock_token_result=&token;
    success(invoke("ignored",NULL,NULL),"kernel shebang interpreter works without a shell"); require(token && !strcmp(token,"script-中文"),"shebang token preserved"); require(!unlink(script),"remove shebang fixture");
    apr_pool_destroy(pool); apr_terminate(); printf("{\"status\":\"PASS\",\"assertions\":%u}\n",checks); return 0;
}
