#include <stdlib.h>
#include <limits.h>
#include <string.h>
#include "admin.h"
#include <apr_strings.h>
#include "svn_io.h"

typedef struct option_definition { const char *name; admin_option option; int value; } option_definition;
static const option_definition definitions[]={
    {"--json",OPT_JSON,0},{"--config",OPT_CONFIG,1},{"--route",OPT_ROUTE,1},{"--uuid",OPT_UUID,1},
    {"--file",OPT_FILE,1},{"--revision",OPT_REVISION,1},{"-r",OPT_REVISION,1},
    {"--txn",OPT_TXN,1},{"-t",OPT_TXN,1},{"--revprop",OPT_REVPROP,0},{"--full-paths",OPT_FULL_PATHS,0},
    {"--incremental",OPT_INCREMENTAL,0},{"--deltas",OPT_DELTAS,0},{"--force-uuid",OPT_FORCE_UUID,0},
    {"--ignore-uuid",OPT_IGNORE_UUID,0},{"--use-hooks",OPT_USE_HOOKS,0},
    {"--authz",OPT_AUTHZ,1},{"--groups",OPT_GROUPS,1},{"--hooks",OPT_HOOKS,1},
    {"--expected-generation",OPT_GENERATION,1},{"--confirm-id",OPT_CONFIRM_ID,1},
    {"--label",OPT_LABEL,1},{"--expires-in",OPT_EXPIRES,1},{"--timeout",OPT_TIMEOUT,1},
    {"--admin-role",OPT_ADMIN_ROLE,1},{"--service-role",OPT_SERVICE_ROLE,1},{"--monitor-role",OPT_MONITOR_ROLE,1},
    {"--limit",OPT_LIMIT,1},{"--offset",OPT_OFFSET,1},{"--verbose",OPT_VERBOSE,0},
    {"--help",OPT_HELP,0},{"-h",OPT_HELP,0},{"--version",OPT_VERSION,0},{"--key-file",OPT_KEY_FILE,1},
    {"--old-key-file",OPT_OLD_KEY_FILE,1},{"--settings",OPT_SETTINGS,1}
};

svn_error_t *admin_usage(const char *message)
{
    return svn_error_create(SVN_ERR_CL_ARG_PARSING_ERROR,NULL,message);
}

svn_error_t *admin_options(const admin_command *command,uint64_t allowed,int minimum,int maximum)
{
    if(command->argc<minimum || command->argc>maximum)
        return admin_usage("Wrong number of command arguments; see pgvnadmin --help");
    if(command->flags & ~(allowed|GLOBAL_OPTIONS)) return admin_usage("Option is not valid for this command");
    if(command->child_argc && (command->argc<2 || strcmp(command->argv[0],"maintenance") || strcmp(command->argv[1],"freeze")))
        return admin_usage("A child command is accepted only by maintenance freeze");
    return SVN_NO_ERROR;
}

svn_error_t *admin_parse(admin_command *command,int argc,const char **argv)
{
    command->argv=apr_pcalloc(command->pool,(argc+1)*sizeof(*command->argv));
    for(int i=1;i<argc;++i) {
        if(!strcmp(argv[i],"--")) { command->child_argc=argc-i-1; command->child_argv=argv+i+1; break; }
        if(argv[i][0]=='-') {
            const option_definition *definition=NULL;
            for(apr_size_t k=0;k<sizeof(definitions)/sizeof(definitions[0]);++k)
                if(!strcmp(argv[i],definitions[k].name)) { definition=&definitions[k]; break; }
            if(!definition) return admin_usage("Unknown option; see pgvnadmin --help");
            if(command->flags & OPTION(definition->option)) return admin_usage("Duplicate option");
            command->flags|=OPTION(definition->option);
            if(definition->value) {
                if(++i==argc) return admin_usage("Option requires a value");
                command->values[definition->option]=argv[i];
            }
        } else command->argv[command->argc++]=argv[i];
    }
    return SVN_NO_ERROR;
}

svn_error_t *admin_number(apr_int64_t *number,const char *value,apr_int64_t minimum,apr_int64_t maximum)
{
    if(!value || !*value) return admin_usage("An integer value is required");
    apr_int64_t parsed=0;
    for(const unsigned char *p=(const unsigned char *)value;*p;++p) {
        if(*p<'0' || *p>'9' || parsed>(maximum-(*p-'0'))/10)
            return admin_usage("Integer option is outside its permitted range");
        parsed=parsed*10+(*p-'0');
    }
    if(parsed<minimum || parsed>maximum) return admin_usage("Integer option is outside its permitted range");
    *number=parsed;
    return SVN_NO_ERROR;
}

const char *admin_utf8(const wchar_t *value,apr_pool_t *pool)
{
    int length=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,value,-1,NULL,0,NULL,NULL);
    if(!length) return NULL;
    char *text=apr_palloc(pool,length);
    if(!WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,value,-1,text,length,NULL,NULL)) return NULL;
    return text;
}

const char *admin_env(const wchar_t *name,apr_pool_t *pool)
{
    DWORD size=GetEnvironmentVariableW(name,NULL,0);
    if(!size) return NULL;
    wchar_t *value=apr_palloc(pool,size*sizeof(*value));
    if(!GetEnvironmentVariableW(name,value,size)) return NULL;
    return admin_utf8(value,pool);
}

void admin_json_string(FILE *file,const char *value,apr_size_t length)
{
    if(!value) { fputs("null",file); return; }
    fputc('"',file);
    for(apr_size_t i=0;i<length;++i) {
        unsigned char byte=(unsigned char)value[i];
        if(byte=='"' || byte=='\\') { fputc('\\',file); fputc(byte,file); }
        else if(byte<32) fprintf(file,"\\u%04x",byte);
        else fputc(byte,file);
    }
    fputc('"',file);
}

void admin_json_cstr(FILE *file,const char *value)
{
    admin_json_string(file,value,value ? strlen(value) : 0);
}

void admin_ok(const char *action)
{
    fputs("{\"ok\":true,\"action\":",stdout); admin_json_cstr(stdout,action); puts("}");
}

svn_error_t *admin_emit_query(admin_command *command,const char *sql,int count,const char *const *values)
{
    PGresult *result;
    SVN_ERR(pgvn_control_query(&result,command->context,sql,count,values));
    if(PQntuples(result)!=1 || PQgetisnull(result,0,0)) {
        PQclear(result);
        return svn_error_create(SVN_ERR_FS_NOT_FOUND,NULL,"Requested PgVN record does not exist");
    }
    fputs(PQgetvalue(result,0,0),stdout); putchar('\n'); PQclear(result);
    return SVN_NO_ERROR;
}

svn_error_t *admin_read_text(const char **text,const char *path,apr_pool_t *pool)
{
    svn_stringbuf_t *buffer;
    SVN_ERR(svn_stringbuf_from_file2(&buffer,path,pool));
    if(buffer->len>1048576 || memchr(buffer->data,0,buffer->len))
        return admin_usage("Text input must be NUL-free and at most 1 MiB");
    *text=buffer->data;
    return SVN_NO_ERROR;
}

svn_error_t *admin_open(pgvn_repository **repository,admin_command *command,const char *selector)
{
    pgvn_binding *binding;
    SVN_ERR(pgvn_repository_lookup(&binding,command->context,selector,command->pool));
    if(command->read_only_client) return pgvn_repos_open(repository,command->context,binding->repository_id,command->pool);
    return pgvn_repos_open_admin(repository,command->context,binding->repository_id,command->pool);
}

svn_error_t *admin_revision_range(svn_revnum_t *start,svn_revnum_t *end,
    admin_command *command,svn_fs_t *fs,svn_boolean_t loading)
{
    *start=*end=SVN_INVALID_REVNUM;
    const char *range=command->values[OPT_REVISION];
    if(!range) return SVN_NO_ERROR;
    const char *colon=strchr(range,':');
    const char *first=colon ? apr_pstrmemdup(command->pool,range,colon-range) : range;
    const char *last=colon ? colon+1 : first;
    svn_revnum_t head=SVN_INVALID_REVNUM;
    if(!loading) SVN_ERR(svn_fs_youngest_rev(&head,fs,command->pool));
    apr_int64_t parsed;
    if(!strcmp(first,"HEAD") && !loading) *start=head;
    else { SVN_ERR(admin_number(&parsed,first,0,LONG_MAX)); *start=(svn_revnum_t)parsed; }
    if(!strcmp(last,"HEAD") && !loading) *end=head;
    else { SVN_ERR(admin_number(&parsed,last,0,LONG_MAX)); *end=(svn_revnum_t)parsed; }
    if(*start>*end || (!loading && *end>head)) return admin_usage("Invalid revision range");
    return SVN_NO_ERROR;
}

int admin_error(svn_error_t *error,svn_boolean_t json)
{
    int code=pgvn_error_exit_code(error);
    static const char *classes[]={"ok","operation_failed","invalid_arguments","database_unavailable","schema_incompatible","access_denied","not_found","conflict"};
    if(json) {
        fputs("{\"ok\":false,\"error\":{\"code\":",stderr); admin_json_cstr(stderr,classes[code]);
        fprintf(stderr,",\"exit_code\":%d,\"message\":",code);
        admin_json_cstr(stderr,error->message ? error->message : "PgVN operation failed"); fputs("}}\n",stderr);
    } else svn_handle_error2(error,stderr,FALSE,"pgvnadmin: ");
    return code;
}

void admin_help(void)
{
    puts("PgVN administrative CLI (Windows x64)\n"
         "Connection: PGVN_DSN environment variable or --config FILE ([pgvn] connection=...).\n"
         "Metadata commands emit JSON; --json also structures errors and look results.\n"
         "  db init|migrate|check\n"
         "  db grant [--admin-role ROLE] [--service-role ROLE] [--monitor-role ROLE]\n"
         "  repo create NAME [--route /PREFIX] [--uuid UUID]\n"
         "  repo list [--limit N] [--offset N] | repo show SELECTOR\n"
         "  repo register-existing NAME STORAGE_ID [--route /PREFIX]\n"
         "  repo enable|disable SELECTOR\n"
         "  dump SELECTOR [--file FILE] [-r START:END] [--incremental] [--deltas]\n"
         "  load SELECTOR [--file FILE] [-r START:END] [--force-uuid|--ignore-uuid] [--use-hooks]\n"
         "  verify SELECTOR [-r START:END]\n"
         "  look youngest|uuid|info|tree|cat|changed|proplist|propget|transactions|txn-info SELECTOR [ARGS]\n"
         "       [-r REV | --txn TXN] [--revprop] [--full-paths] [--verbose]\n"
         "  maintenance pack|recover|freeze SELECTOR [--timeout SECONDS] [-- PROGRAM ARGS]\n"
         "  maintenance hotcopy SOURCE DESTINATION [--route /PREFIX] [--incremental]\n"
         "  maintenance purge SELECTOR --confirm-id STORAGE_ID\n"
         "  maintenance abort-txn SELECTOR TXN\n"
         "  maintenance recover-dav-creation SELECTOR TXN\n"
         "  config show SELECTOR | config publish SELECTOR --expected-generation N --authz FILE [--groups FILE] [--hooks FILE] [--settings FILE]\n"
         "  authz validate --authz FILE [--groups FILE] | authz check SELECTOR USER PATH read|write\n"
         "  monitor-token create --label LABEL [--expires-in SECONDS]\n"
         "  monitor-token list | monitor-token revoke ID | monitor-token expire ID --expires-in SECONDS\n"
         "  key create FILE KEY_ID             Create an ACL-protected deployment key (offline)\n"
         "  user set DOMAIN USER [--key-file FILE]  Password from redirected stdin\n"
         "  user rekey DOMAIN USER [--key-file NEW] [--old-key-file OLD]\n"
         "  user list [DOMAIN] | user disable DOMAIN USER\n"
         "  diagnose | --version | --help\n"
         "SELECTOR accepts name:NAME, id:UUID, or an unambiguous name/ID.\n"
         "Look accepts the trusted service DB role for enabled repositories; other administration requires admin privileges.\n"
         "Exit codes: 0 success, 1 failure, 2 usage, 3 database, 4 schema, 5 denied, 6 missing, 7 conflict.");
}
