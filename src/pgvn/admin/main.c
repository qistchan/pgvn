#include <fcntl.h>
#include <io.h>
#include <string.h>
#include "admin.h"
#include <apr_general.h>
#include "svn_config.h"
#include "svn_pools.h"

static volatile LONG cancelled;
static BOOL WINAPI console_control(DWORD control)
{
    if(control==CTRL_C_EVENT || control==CTRL_BREAK_EVENT || control==CTRL_CLOSE_EVENT) {
        InterlockedExchange(&cancelled,1); return TRUE;
    }
    return FALSE;
}

svn_error_t *admin_cancel(void *baton)
{
    if(InterlockedCompareExchange(&cancelled,0,0))
        return svn_error_create(SVN_ERR_CANCELLED,NULL,"PgVN administrative command cancelled");
    return SVN_NO_ERROR;
}

static svn_error_t *run(admin_command *command)
{
    if(command->flags & OPTION(OPT_HELP)) { admin_help(); return SVN_NO_ERROR; }
    if(command->flags & OPTION(OPT_VERSION)) {
        SVN_ERR(admin_options(command,OPTION(OPT_VERSION),0,0));
        char buffer[1024]; pgvn_build_json(buffer,sizeof(buffer)); fputs(buffer,stdout);
        return SVN_NO_ERROR;
    }
    SVN_ERR(admin_preflight(command));
    if(!strcmp(command->argv[0],"authz") && !strcmp(command->argv[1],"validate"))
        return admin_policy(command);
    const char *dsn=admin_env(L"PGVN_DSN",command->pool);
    if(command->values[OPT_CONFIG]) {
        svn_config_t *config;
        svn_error_t *error=svn_config_read3(&config,command->values[OPT_CONFIG],TRUE,TRUE,TRUE,command->pool);
        if(error) { svn_error_clear(error); return admin_usage("Unable to read a valid PgVN process configuration"); }
        svn_config_get(config,&dsn,"pgvn","connection",NULL);
    }
    if((!dsn || !*dsn) && !strcmp(command->argv[0],"diagnose")) {
        char buffer[1024]; pgvn_build_json(buffer,sizeof(buffer));
        fputs("{\"database\":\"not_configured\",\"build\":",stdout); fputs(buffer,stdout); puts("}");
        return SVN_NO_ERROR;
    }
    pgvn_context_options options={dsn,3,5000};
    if(!strcmp(command->argv[0],"db") || !strcmp(command->argv[0],"diagnose"))
        SVN_ERR(pgvn_database_open(&command->context,&options,command->pool));
    else {
        SVN_ERR(pgvn_context_create(&command->context,&options,command->pool));
        SVN_ERR(admin_db_check(command->context,TRUE,command->pool));
        SVN_ERR(pgvn_require_admin(command->context));
    }
    return admin_dispatch(command);
}

int wmain(int argc,wchar_t **wide)
{
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
    SetConsoleCtrlHandler(console_control,TRUE);
    svn_error_t *error=admin_parse(&command,argc,argv);
    if(!error) error=run(&command);
    int code=error ? admin_error(error,structured_errors) : PGVN_EXIT_OK;
    if(error) svn_error_clear(error);
    if(!code && (ferror(stdout) || fflush(stdout)==EOF)) code=PGVN_EXIT_FAILURE;
    SetConsoleCtrlHandler(console_control,FALSE);
    svn_pool_destroy(pool); apr_terminate();
    return code;
}
