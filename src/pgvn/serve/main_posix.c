#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <locale.h>
#include <apr_general.h>
#include "http_posix.h"
#include "listener.h"
#include "internal.h"
#include "credentials.h"
#include "../platform/credentials.h"
#include "svn_pools.h"
#include "svn_utf.h"
static pgvn_cancel_state stopping;
static void stop_signal(int number) { (void)number; pgvn_cancel_signal(&stopping); }
static svn_error_t *configuration_check(const char *path,apr_pool_t *pool)
{
    svn_config_t *config; SVN_ERR(svn_config_read3(&config,path,TRUE,TRUE,TRUE,pool));
    const char *dsn,*key_path,*previous,*port,*host;
    svn_config_get(config,&dsn,"pgvn","connection",NULL);
    svn_config_get(config,&key_path,"pgvn","credential-key-file",NULL);
    svn_config_get(config,&previous,"pgvn","credential-previous-key-file",NULL);
    svn_config_get(config,&port,"pgvn","listen-port","3690");
    svn_config_get(config,&host,"pgvn","listen-host","127.0.0.1");
    if(!dsn || !*dsn || !host || !*host || !port || !*port) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN configuration requires connection, host and key file");
    unsigned value=0;
    for(const char *p=port;*p;++p) { if(*p<'0' || *p>'9' || value>6553) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid listen-port"); value=value*10+(unsigned)(*p-'0'); }
    if(value<1024 || value>65535) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid listen-port");
    pgvn_credential_key *key; SVN_ERR(pgvn_keyring_load(&key,key_path,previous,pool));
    pgvn_context context={0}; SVN_ERR(pgvn_hooks_configure(&context,config,pool));
    SVN_ERR(pgvn_cache_configure(&context,config,pool));
    return SVN_NO_ERROR;
}
static int run(const char *configuration,const char *monitor_url,int no_monitor)
{
    apr_pool_t *pool=svn_pool_create(NULL),*listener_pool=svn_pool_create(pool);
    pgvn_listener *listener=NULL; pgvn_monitor monitor={0}; int code=0;
    if(configuration) {
        svn_error_t *error=configuration_check(configuration,pool);
        if(error) { code=pgvn_error_exit_code(error); svn_handle_error2(error,stderr,FALSE,"pgvnserve: "); svn_error_clear(error); goto finish; }
        error=pgvn_listener_start(&listener,configuration,&stopping,listener_pool);
        if(error) {
            code=pgvn_error_exit_code(error); fprintf(stderr,"PgVN SVN startup failed (code %ld)\n",(long)error->apr_err); svn_error_clear(error);
            if(code!=PGVN_EXIT_DATABASE) goto finish;
            svn_pool_clear(listener_pool); code=0;
        }
    }
    if(!no_monitor) {
        int status=pgvn_monitor_start(&monitor,monitor_url,configuration);
        if(status) { fprintf(stderr,"PgVN monitor startup failed (errno %d)\n",status); code=status==EADDRINUSE ? PGVN_EXIT_CONFLICT : status==EINVAL ? PGVN_EXIT_USAGE : PGVN_EXIT_FAILURE; goto finish; }
    }
    pgvn_monitor_business(monitor.data,listener!=NULL); pgvn_monitor_tick(monitor.data);
    uint64_t retry_at=pgvn_tick()+1000; unsigned retry_delay=1000;
    while(!pgvn_wait_stop(&stopping,1000)) {
        pgvn_monitor_tick(monitor.data);
        if(!no_monitor && pgvn_thread_wait(monitor.thread,0)) { code=PGVN_EXIT_FAILURE; fputs("PgVN monitor worker stopped unexpectedly\n",stderr); break; }
        if(configuration && !listener && pgvn_tick()>=retry_at) {
            svn_error_t *error=pgvn_listener_start(&listener,configuration,&stopping,listener_pool);
            if(error) {
                code=pgvn_error_exit_code(error); svn_error_clear(error); svn_pool_clear(listener_pool);
                if(code!=PGVN_EXIT_DATABASE) break;
                code=0; retry_delay=retry_delay<8000 ? retry_delay*2 : 15000; retry_at=pgvn_tick()+retry_delay;
            } else { pgvn_monitor_business(monitor.data,1); pgvn_telemetry_event("service","business_ready",0); }
        }
    }
finish:
    pgvn_monitor_business(monitor.data,0);
    pgvn_listener_stop(listener,NULL); pgvn_monitor_stop(&monitor);
    svn_pool_destroy(pool); return code;
}
int main(int argc,char **argv)
{
    if(!setlocale(LC_ALL,"C.UTF-8")) { fputs("PgVN requires the C.UTF-8 locale\n",stderr); return PGVN_EXIT_USAGE; }
    if(apr_initialize()!=APR_SUCCESS) return PGVN_EXIT_FAILURE;
    apr_pool_t *pool=svn_pool_create(NULL); svn_utf_initialize2(TRUE,pool);
    const char *configuration=NULL,*url="http://127.0.0.1:8033/"; int no_monitor=0,check=0,code=PGVN_EXIT_USAGE;
    for(int i=1;i<argc;++i) {
        if(!pgvn_utf8_valid(argv[i])) goto finish;
        if(!strcmp(argv[i],"--version") && argc==2) { char json[PGVN_BUILD_JSON_SIZE]; pgvn_build_json(json,sizeof(json)); fputs(json,stdout); code=0; goto finish; }
        if(!strcmp(argv[i],"--help") && argc==2) { puts("PgVN Linux server\n  --config FILE [--console] [--monitor-url http://127.0.0.1:PORT/] [--no-monitor]\n  --check-config --config FILE\n  --version"); code=0; goto finish; }
        if(!strcmp(argv[i],"--console")) continue;
        if(!strcmp(argv[i],"--no-monitor") && !no_monitor) { no_monitor=1; continue; }
        if(!strcmp(argv[i],"--check-config") && !check) { check=1; continue; }
        if(!strcmp(argv[i],"--config") && !configuration && i+1<argc) { configuration=argv[++i]; continue; }
        if(!strcmp(argv[i],"--monitor-url") && i+1<argc) { url=argv[++i]; continue; }
        fputs("Invalid PgVN option; see --help\n",stderr); goto finish;
    }
    if(!pgvn_monitor_url_valid(url) || ((!configuration && no_monitor) || (check && !configuration))) goto finish;
    if(check) {
        svn_error_t *error=configuration_check(configuration,pool);
        if(error) { code=pgvn_error_exit_code(error); svn_handle_error2(error,stderr,FALSE,"pgvnserve: "); svn_error_clear(error); }
        else { puts("{\"valid\":true}"); code=0; }
        goto finish;
    }
    struct sigaction action={0}; action.sa_handler=stop_signal; sigemptyset(&action.sa_mask);
    sigaction(SIGTERM,&action,NULL); sigaction(SIGINT,&action,NULL); signal(SIGPIPE,SIG_IGN);
    code=run(configuration,url,no_monitor);
finish:
    svn_pool_destroy(pool); apr_terminate(); return code;
}
