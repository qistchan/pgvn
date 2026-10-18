#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#else
#define _POSIX_C_SOURCE 200809L
#include <signal.h>
#endif
#include <string.h>
#include "admin.h"
#include "../platform/credentials.h"
#include <apr_general.h>
#include "svn_config.h"
#include "svn_pools.h"
#include "svn_dirent_uri.h"
#include "svn_utf.h"

#ifdef _WIN32
static volatile LONG cancelled;
static HANDLE cancellation_event;
static BOOL WINAPI console_control(DWORD control)
{
    if(control==CTRL_C_EVENT || control==CTRL_BREAK_EVENT || control==CTRL_CLOSE_EVENT) {
        InterlockedExchange(&cancelled,1); SetEvent(cancellation_event); return TRUE;
    }
    return FALSE;
}
#else
static pgvn_cancel_state cancellation_state;
static pgvn_cancel_event cancellation_event=&cancellation_state;
_Static_assert(__atomic_always_lock_free(sizeof(pgvn_atomic32),0),
    "Signal cancellation requires lock-free atomic storage");

static void console_control(int signal_number)
{
    (void)signal_number;
    pgvn_cancel_signal(cancellation_event);
}
#endif

svn_error_t *admin_cancel(void *baton)
{
#ifdef _WIN32
    if(InterlockedCompareExchange(&cancelled,0,0))
#else
    if(pgvn_cancel_is_set(cancellation_event))
#endif
        return svn_error_create(SVN_ERR_CANCELLED,NULL,"PgVN administrative command cancelled");
    return SVN_NO_ERROR;
}

static svn_error_t *run(admin_command *command)
{
    if(command->flags & OPTION(OPT_HELP)) { admin_help(); return SVN_NO_ERROR; }
    if(command->flags & OPTION(OPT_VERSION)) {
        SVN_ERR(admin_options(command,OPTION(OPT_VERSION),0,0));
        char buffer[PGVN_BUILD_JSON_SIZE]; pgvn_build_json(buffer,sizeof(buffer)); fputs(buffer,stdout);
        return SVN_NO_ERROR;
    }
    SVN_ERR(admin_preflight(command));
    if(!strcmp(command->argv[0],"key")) return admin_credentials(command);
    if(!strcmp(command->argv[0],"authz") && !strcmp(command->argv[1],"validate"))
        return admin_policy(command);
#ifdef _WIN32
    const char *dsn=admin_env(L"PGVN_DSN",command->pool);
#else
    const char *dsn=admin_env("PGVN_DSN",command->pool);
#endif
    const char *hook_root=NULL;
    svn_config_t *config=NULL;
    if(command->values[OPT_CONFIG]) {
        svn_error_t *error=svn_config_read3(&config,command->values[OPT_CONFIG],TRUE,TRUE,TRUE,command->pool);
        if(error) { svn_error_clear(error); return admin_usage("Unable to read a valid PgVN process configuration"); }
        svn_config_get(config,&dsn,"pgvn","connection",NULL);
        svn_config_get(config,&command->credential_key_file,"pgvn","credential-key-file",NULL);
        svn_config_get(config,&command->old_credential_key_file,"pgvn","credential-previous-key-file",NULL);
        svn_config_get(config,&hook_root,"pgvn","hook-root",NULL);
    }
#ifndef _WIN32
    if(dsn && !pgvn_utf8_valid(dsn))
        return admin_usage("PgVN connection setting must be valid UTF-8");
#endif
    if((!dsn || !*dsn) && !strcmp(command->argv[0],"diagnose")) {
        char buffer[PGVN_BUILD_JSON_SIZE]; pgvn_build_json(buffer,sizeof(buffer));
        fputs("{\"database\":\"not_configured\",\"build\":",stdout); fputs(buffer,stdout); puts("}");
        return SVN_NO_ERROR;
    }
    pgvn_context_options options={dsn,3,5000};
    if(!strcmp(command->argv[0],"db") || !strcmp(command->argv[0],"diagnose")) {
        SVN_ERR(pgvn_database_open(&command->context,&options,command->pool));
        command->context->cancellation_event=cancellation_event;
    } else {
        SVN_ERR(pgvn_context_create(&command->context,&options,command->pool));
        command->context->cancellation_event=cancellation_event;
        SVN_ERR(admin_db_check(command->context,TRUE,command->pool));
        svn_error_t *privilege=pgvn_require_admin(command->context);
        if(privilege && privilege->apr_err==SVN_ERR_AUTHZ_UNWRITABLE && !strcmp(command->argv[0],"look")) {
            svn_error_clear(privilege); command->read_only_client=TRUE;
        } else SVN_ERR(privilege);
    }
    command->context->hook_root=hook_root;
    SVN_ERR(pgvn_hooks_configure(command->context,config,command->pool));
    SVN_ERR(admin_cancel(NULL));
    if(command->values[OPT_CONFIG]) SVN_ERR(svn_dirent_get_absolute(&command->context->configuration_path,command->values[OPT_CONFIG],command->pool));
    return admin_dispatch(command);
}

#ifdef _WIN32
int wmain(int argc,wchar_t **wide)
{
    if(GetACP()!=CP_UTF8) {
        fputs("PgVN requires a UTF-8 process code page (Windows 10 1903 or later).\n",stderr);
        return PGVN_EXIT_USAGE;
    }
    _setmode(_fileno(stdin),_O_BINARY); _setmode(_fileno(stdout),_O_BINARY);
    SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX);
    if(apr_initialize()!=APR_SUCCESS) return PGVN_EXIT_FAILURE;
    apr_pool_t *pool=svn_pool_create(NULL);
    admin_command command;
    memset(&command,0,sizeof(command)); command.pool=pool;
    const char **argv=apr_pcalloc(pool,(argc+1)*sizeof(*argv));
    for(int i=0;i<argc;++i) {
        argv[i]=admin_utf8(wide[i],pool);
        if(!argv[i]) { svn_pool_destroy(pool); apr_terminate(); return PGVN_EXIT_USAGE; }
    }
    svn_boolean_t structured_errors=FALSE;
    for(int i=1;i<argc && strcmp(argv[i],"--");++i)
        if(!strcmp(argv[i],"--json")) structured_errors=TRUE;
    cancellation_event=CreateEventW(NULL,TRUE,FALSE,NULL);
    if(!cancellation_event) { svn_pool_destroy(pool); apr_terminate(); return PGVN_EXIT_FAILURE; }
    SetConsoleCtrlHandler(console_control,TRUE);
    svn_error_t *error=admin_parse(&command,argc,argv);
    if(!error) error=run(&command);
    int code=error ? admin_error(error,structured_errors) : PGVN_EXIT_OK;
    if(error) svn_error_clear(error);
    if(!code && (ferror(stdout) || fflush(stdout)==EOF)) code=PGVN_EXIT_FAILURE;
    SetConsoleCtrlHandler(console_control,FALSE);
    svn_pool_destroy(pool); CloseHandle(cancellation_event); apr_terminate();
    return code;
}
#else
int main(int argc,char **argv)
{
    struct sigaction action;
    memset(&action,0,sizeof(action));
    sigemptyset(&action.sa_mask);
    action.sa_handler=console_control;
    if(sigaction(SIGINT,&action,NULL) || sigaction(SIGTERM,&action,NULL))
        return PGVN_EXIT_FAILURE;
    action.sa_handler=SIG_IGN;
    if(sigaction(SIGPIPE,&action,NULL)) return PGVN_EXIT_FAILURE;
    if(apr_initialize()!=APR_SUCCESS) return PGVN_EXIT_FAILURE;
    apr_pool_t *pool=svn_pool_create(NULL);
    svn_utf_initialize2(TRUE,pool);
    admin_command command;
    memset(&command,0,sizeof(command)); command.pool=pool;
    svn_boolean_t structured_errors=FALSE;
    for(int i=1;i<argc && strcmp(argv[i],"--");++i)
        if(!strcmp(argv[i],"--json")) structured_errors=TRUE;
    svn_error_t *error=NULL;
    for(int i=0;i<argc;++i)
        if(!pgvn_utf8_valid(argv[i])) { error=admin_usage("Command arguments must be valid UTF-8"); break; }
    if(!error) error=admin_parse(&command,argc,(const char **)argv);
    if(!error) error=admin_cancel(NULL);
    if(!error) error=run(&command);
    int code=error ? admin_error(error,structured_errors) : PGVN_EXIT_OK;
    if(error) svn_error_clear(error);
    if(!code && (ferror(stdout) || fflush(stdout)==EOF)) code=PGVN_EXIT_FAILURE;
    svn_pool_destroy(pool); apr_terminate();
    return code;
}
#endif
