#ifndef _WIN32
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <apr_general.h>
#include "listener.h"
#include "../platform/credentials.h"
#include "pgvn.h"
#include "svn_pools.h"
#include "svn_utf.h"
#include "telemetry.h"

static int wait_for_stop(const sigset_t *signals,unsigned seconds)
{
    struct timespec timeout={(time_t)seconds,0};
    int signal_number=sigtimedwait(signals,NULL,&timeout);
    if(signal_number==SIGINT || signal_number==SIGTERM) return 1;
    if(signal_number<0 && errno==EAGAIN) return 0;
    return -1;
}

static int start_listener(pgvn_listener **listener,const char *configuration,
    pgvn_cancel_event stop_event,apr_pool_t *parent,const sigset_t *signals,int *stopped)
{
    static const unsigned delays[]={1,2,4,8,15};
    unsigned attempt=0;
    for(;;) {
        apr_pool_t *pool=svn_pool_create(parent);
        svn_error_t *error=pgvn_listener_start(listener,configuration,stop_event,pool);
        if(!error) return PGVN_EXIT_OK;
        pgvn_exit_code code=pgvn_error_exit_code(error);
        fprintf(stderr,"PgVN SVN startup failed (code %ld)\n",(long)error->apr_err);
        svn_error_clear(error);
        svn_pool_destroy(pool);
        if(code!=PGVN_EXIT_DATABASE) return code;
        unsigned delay=delays[attempt<5 ? attempt : 4];
        if(attempt<5) ++attempt;
        int received=wait_for_stop(signals,delay);
        if(received==1) { *stopped=1; return PGVN_EXIT_OK; }
        if(received<0) return PGVN_EXIT_FAILURE;
    }
}

int main(int argc,char **argv)
{
    const char *configuration=NULL;
    int version=0,monitor_disabled=0;
    for(int i=1;i<argc;++i) {
        if(!strcmp(argv[i],"--version") && argc==2) version=1;
        else if(!strcmp(argv[i],"--config") && i+1<argc && !configuration) configuration=argv[++i];
        else if(!strcmp(argv[i],"--monitor-disabled")) monitor_disabled=1;
        else {
            fputs("Usage: pgvnserve --version | --config FILE --monitor-disabled\n",stderr);
            return PGVN_EXIT_USAGE;
        }
    }
    if(version) {
        char json[PGVN_BUILD_JSON_SIZE];
        pgvn_build_json(json,sizeof(json));
        fputs(json,stdout);
        return ferror(stdout) ? PGVN_EXIT_FAILURE : PGVN_EXIT_OK;
    }
    if(!configuration || !monitor_disabled || !pgvn_utf8_valid(configuration)) {
        fputs("PgVN requires --config FILE --monitor-disabled with a UTF-8 path\n",stderr);
        return PGVN_EXIT_USAGE;
    }
    sigset_t signals;
    sigemptyset(&signals);
    sigaddset(&signals,SIGINT);
    sigaddset(&signals,SIGTERM);
    if(pthread_sigmask(SIG_BLOCK,&signals,NULL)) return PGVN_EXIT_FAILURE;
    struct sigaction ignore_pipe;
    memset(&ignore_pipe,0,sizeof(ignore_pipe));
    ignore_pipe.sa_handler=SIG_IGN;
    sigemptyset(&ignore_pipe.sa_mask);
    if(sigaction(SIGPIPE,&ignore_pipe,NULL)) return PGVN_EXIT_FAILURE;
    if(apr_initialize()!=APR_SUCCESS) return PGVN_EXIT_FAILURE;
    apr_pool_t *pool=svn_pool_create(NULL);
    svn_utf_initialize2(TRUE,pool);
    pgvn_telemetry_enable(0);
    pgvn_cancel_state stop_state={0};
    pgvn_listener *listener=NULL;
    int stopped=0;
    int result=start_listener(&listener,configuration,&stop_state,pool,&signals,&stopped);
    if(result) {
        svn_pool_destroy(pool);
        apr_terminate();
        return result;
    }
    if(stopped) {
        svn_pool_destroy(pool);
        apr_terminate();
        return PGVN_EXIT_OK;
    }
    fputs("PgVN SVN listener ready\n",stdout);
    fflush(stdout);
    for(;;) {
        int received=wait_for_stop(&signals,1);
        if(received==1) break;
        if(received<0) { result=PGVN_EXIT_FAILURE; break; }
        if(pgvn_listener_failed(listener)) { result=PGVN_EXIT_FAILURE; break; }
    }
    pgvn_listener_stop(listener);
    svn_pool_destroy(pool);
    apr_terminate();
    return result;
}
#endif
