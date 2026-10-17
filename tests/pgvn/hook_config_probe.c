/* Real policy/configuration/SQL code; process dispatch is an explicit adapter.
 * This probe does not execute hooks or accept the POSIX process launcher. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <apr_general.h>
#include <apr_strings.h>
#include "internal.h"
#include "svn_utf.h"

static unsigned checks,dispatches;
static const char *id="11111111-1111-4111-8111-111111111111";
static const char *rules="[groups]\nwriters = alice\n[/]\n@writers = rw\n";
static const char *groups=NULL;
static const char *settings="{\"identity_domain\":\"身份域\",\"svn_authz_mode\":\"bypass\"}";
static const char *declarations="{\"pre-commit\":{\"version\":1,\"name\":\"check-policy\",\"arguments\":[\"\",\"中文 参数\",\"& ; $(literal)\",\"quote \\\" tail\\\\\"],\"timeout_seconds\":7},\"post-commit\":{\"version\":1,\"name\":\"notify\"}}";

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
static void denied(svn_error_t *error,apr_status_t code,const char *name)
{
    require(error && error->apr_err==code,name); svn_error_clear(error);
}
static void sql(pgvn_context *context,const char *statement)
{
    PGresult *result=PQexec(context->control,statement);
    require(result && (PQresultStatus(result)==PGRES_COMMAND_OK || PQresultStatus(result)==PGRES_TUPLES_OK),"fixture SQL");
    PQclear(result);
}
static apr_int64_t generation(pgvn_context *context)
{
    PGresult *result=PQexec(context->control,"SELECT generation FROM pgvn.repositories");
    require(result && PQresultStatus(result)==PGRES_TUPLES_OK && PQntuples(result)==1,"generation SQL");
    apr_int64_t value=apr_atoi64(PQgetvalue(result,0,0)); PQclear(result); return value;
}
void pgvn_telemetry_query(const char *state,apr_interval_time_t elapsed) { (void)state; (void)elapsed; }
void pgvn_telemetry_query_start(void) { }
void pgvn_telemetry_connection(int change,int rejected,int failed) { (void)change; (void)rejected; (void)failed; }
void pgvn_telemetry_waiter(int change) { (void)change; }
svn_error_t *pgvn_external_hook(const pgvn_policy *policy,const pgvn_hook_context *event,apr_pool_t *pool)
{
    (void)policy; (void)event; (void)pool; ++dispatches;
    return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"Process execution is outside this configuration probe");
}
static svn_config_t *configuration(const char *program,apr_pool_t *pool)
{
    svn_config_t *config;
    success(svn_config_create2(&config,TRUE,TRUE,pool),"create node mapping");
    svn_config_set(config,"pgvn-hooks","check-policy",program);
    svn_config_set(config,"pgvn-hooks","notify","notify");
    return config;
}
static void available_file(const char *path)
{
    FILE *file=fopen(path,"w"); require(file!=NULL,"create availability fixture");
    require(fputs("AVAILABILITY FIXTURE ONLY; NOT EXECUTED\n",file)>=0 && !fclose(file),"write availability fixture");
    require(!chmod(path,0700),"make fixture executable");
}
static void unavailable(pgvn_context *context,apr_pool_t *pool,const char *name)
{
    pgvn_policy *policy=(pgvn_policy *)1;
    denied(pgvn_policy_acquire(&policy,context,id,0,pool),SVN_ERR_AUTHZ_INVALID_CONFIG,name);
    require(policy==NULL,"failed acquire publishes no partial policy");
}
int main(int argc,char **argv)
{
    if(argc==2 && !strcmp(argv[1],"--version")) { char json[PGVN_BUILD_JSON_SIZE]; pgvn_build_json(json,sizeof(json)); puts(json); return 0; }
    require(argc==2,"scratch root argument");
    require(apr_initialize()==APR_SUCCESS,"APR initialization");
    apr_pool_t *pool; require(apr_pool_create(&pool,NULL)==APR_SUCCESS,"APR pool");
    /* Planned Linux hosts use UTF-8 native paths, matching the core contract. */
    svn_utf_initialize2(TRUE,pool);
    const char *scratch=argv[1];
    const char *a=apr_pstrcat(pool,scratch,"/check-a",NULL),*b=apr_pstrcat(pool,scratch,"/check-b",NULL);
    const char *notify=apr_pstrcat(pool,scratch,"/notify",NULL);
    available_file(a); available_file(b); available_file(notify);
    pgvn_context_options options={getenv("PGVN_HOOK_CONFIG_DSN"),3,5000};
    pgvn_context *first,*second;
    success(pgvn_context_create(&first,&options,pool),"first production context");
    success(pgvn_context_create(&second,&options,pool),"second production context");
    require(first->catalog_version==8 && second->catalog_version==8,"schema 8 recognized");
    first->hook_root=second->hook_root=scratch;
    pgvn_policy *policy;
    success(pgvn_policy_acquire(&policy,first,id,0,pool),"legacy string policy remains usable");
    pgvn_hook_context event={0}; event.event=PGVN_HOOK_PRE_COMMIT;
    success(pgvn_hook_dispatch(policy,&event,pool),"legacy allow remains allow");
    success(pgvn_policy_publish_settings(first,id,1,rules,groups,declarations,settings,"probe",pool),"publish version 1 without a local mapping");
    require(generation(first)==2,"publication increments generation once");
    unavailable(first,pool,"missing node mapping refuses repository load");
    const char *config_path=apr_pstrcat(pool,scratch,"/节点.ini",NULL);
    FILE *config_file=fopen(config_path,"w"); require(config_file!=NULL,"create INI fixture");
    require(fputs("[pgvn-hooks]\ncheck-policy = check-a\nnotify = notify\n",config_file)>=0 && !fclose(config_file),"write INI fixture");
    svn_config_t *config;
    success(svn_config_read3(&config,config_path,TRUE,TRUE,TRUE,pool),"read real node INI");
    success(pgvn_hooks_configure(first,config,pool),"first node configuration");
    success(pgvn_hooks_configure(second,configuration("check-b",pool),pool),"second node configuration");
    success(pgvn_policy_acquire(&policy,first,id,0,pool),"resolve first node mapping");
    pgvn_hook_handler *handler=policy->external_hooks[PGVN_HOOK_PRE_COMMIT];
    require(handler && !strcmp(handler->executable,"check-a") && handler->timeout_seconds==7,"first native implementation and timeout");
    require(handler->arguments->nelts==4 && !strcmp(APR_ARRAY_IDX(handler->arguments,0,const char *),""),"empty argument preserved");
    require(!strcmp(APR_ARRAY_IDX(handler->arguments,1,const char *),"中文 参数") &&
        !strcmp(APR_ARRAY_IDX(handler->arguments,2,const char *),"& ; $(literal)") &&
        !strcmp(APR_ARRAY_IDX(handler->arguments,3,const char *),"quote \" tail\\"),"UTF-8, shell punctuation, quotes and slashes preserved");
    handler=policy->external_hooks[PGVN_HOOK_POST_COMMIT];
    require(handler && !strcmp(handler->executable,"notify") && handler->timeout_seconds==30 && !handler->arguments->nelts,"second declaration and defaults");
    require(policy->svn_authz_bypass && !strcmp(policy->identity_domain,"身份域"),"hook declaration preserves native settings");
    denied(pgvn_hook_dispatch(policy,&event,pool),SVN_ERR_UNSUPPORTED_FEATURE,"required external hook reaches process adapter");
    require(dispatches==1,"required hook is never silently skipped");
    pgvn_policy *other;
    success(pgvn_policy_acquire(&other,second,id,0,pool),"resolve second node mapping");
    require(!strcmp(other->external_hooks[PGVN_HOOK_PRE_COMMIT]->executable,"check-b"),"nodes select independent implementations");
    /* Bad snapshots must not replace a previously valid mapping. */
    const char *bad[]={"../check-a","/check-a","check a","check-a --flag","check.exe","check.sh","","中文","$(check)","a\\b"};
    for(unsigned i=0;i<sizeof(bad)/sizeof(*bad);++i)
        denied(pgvn_hooks_configure(first,configuration(bad[i],pool),pool),SVN_ERR_AUTHZ_INVALID_CONFIG,"invalid native filename rejected");
    require(!strcmp(apr_hash_get(first->hook_implementations,"check-policy",APR_HASH_KEY_STRING),"check-a"),"invalid reload preserves complete previous mapping");
    config=configuration("check-a",pool); svn_config_set(config,"pgvn-hooks","Check-policy","check-a");
    denied(pgvn_hooks_configure(first,config,pool),SVN_ERR_AUTHZ_INVALID_CONFIG,"logical names have stable lowercase spelling");
    config=configuration("check-a",pool);
    for(int i=0;i<127;++i) svn_config_set(config,"pgvn-hooks",apr_psprintf(pool,"extra-%d",i),"check-a");
    denied(pgvn_hooks_configure(first,config,pool),SVN_ERR_AUTHZ_INVALID_CONFIG,"node map bounded at 128 entries");
    for(unsigned i=0;i<sizeof(bad)/sizeof(*bad);++i) require(!pgvn_hook_filename_valid(bad[i],FALSE),"Linux filename grammar");
    require(pgvn_hook_filename_valid("Check.v1.EXE",TRUE) && !pgvn_hook_filename_valid("check.sh",TRUE) &&
        !pgvn_hook_filename_valid("../check.exe",TRUE) && !pgvn_hook_filename_valid("check.exe -x",TRUE),"Windows filename grammar retains exe compatibility");
    require(!chmod(a,0600),"remove executable bit"); unavailable(first,pool,"non-executable implementation refuses load");
    require(!chmod(a,0700) && !unlink(a),"remove first node implementation"); unavailable(first,pool,"missing implementation refuses load");
    success(pgvn_policy_acquire(&other,second,id,0,pool),"second node remains available");
    require(!mkdir(a,0700),"directory fixture"); unavailable(first,pool,"directory cannot implement a hook");
    require(!rmdir(a) && !symlink(b,a),"symlink fixture"); unavailable(first,pool,"symlink cannot implement a hook");
    require(!unlink(a),"remove symlink"); available_file(a);
    first->hook_root="relative"; unavailable(first,pool,"relative hook-root rejected"); first->hook_root=scratch;
    first->catalog_version=7; unavailable(first,pool,"already-connected old node rejects a new declaration");
    denied(pgvn_policy_publish_settings(first,id,2,rules,groups,declarations,settings,"old-node",pool),SVN_ERR_INCORRECT_PARAMS,"old publisher rejects declaration");
    require(generation(first)==2,"old publisher cannot advance generation"); first->catalog_version=8;
    denied(pgvn_policy_publish_settings(first,id,1,rules,groups,"{}",settings,"stale",pool),SVN_ERR_FS_TXN_OUT_OF_DATE,"stale publisher cannot overwrite hooks");
    denied(pgvn_policy_publish_settings(first,id,2,rules,groups,"{\"pre-commit\":{\"version\":2,\"name\":\"check-policy\"}}",settings,"future",pool),SVN_ERR_INCORRECT_PARAMS,"unknown version refused at publication");
    require(generation(first)==2,"failed publication leaves generation unchanged");
    success(pgvn_policy_publish_settings(first,id,2,rules,groups,"{\"pre-commit\":\"reject\"}",NULL,"probe",pool),"legacy reject publication");
    denied(pgvn_policy_revalidate(policy,first,pool),SVN_ERR_AUTHZ_UNWRITABLE,"old policy invalidated after publish");
    success(pgvn_policy_acquire(&policy,first,id,0,pool),"acquire explicit reject policy");
    denied(pgvn_hook_dispatch(policy,&event,pool),SVN_ERR_REPOS_HOOK_FAILURE,"legacy rejection semantics preserved");
    success(pgvn_policy_publish_settings(first,id,3,rules,groups,"{\"pre-commit\":{\"executable\":\"check.exe\"}}",NULL,"probe",pool),"legacy exe declarations remain valid in catalog");
    unavailable(first,pool,"Linux refuses implicit exe translation");
    sql(first,"BEGIN; ALTER FUNCTION pgvn.valid_hooks(jsonb,boolean) RENAME TO missing_hook_validator");
    denied(pgvn_database_validate(first),PGVN_ERR_SCHEMA,"schema 8 requires its validator"); sql(first,"ROLLBACK");
    success(pgvn_database_validate(first),"validator restored");
    require(!unlink(a) && !unlink(b) && !unlink(notify) && !unlink(config_path),"remove owned fixture files");
    apr_pool_destroy(pool); apr_terminate();
    printf("{\"status\":\"PASS\",\"assertions\":%u,\"process_adapter_calls\":%u}\n",checks,dispatches);
    return 0;
}
