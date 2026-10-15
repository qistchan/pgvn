#include <string.h>
#include "internal.h"
#include "credentials.h"
#include <apr_strings.h>
#include "svn_io.h"

static const char *events[]={"start-commit","pre-commit","post-commit","pre-revprop-change",
    "post-revprop-change","pre-lock","post-lock","pre-unlock","post-unlock"};

const char *pgvn_hook_name(pgvn_hook_event event)
{
    return event>=0 && event<=PGVN_HOOK_POST_UNLOCK ? events[event] : "unknown";
}

static svn_error_t *parse_authz(svn_authz_t **authz,const char *text,const char *groups,apr_pool_t *pool)
{
    if(!text || strlen(text)>1048576 || (groups && strlen(groups)>1048576))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN authz text exceeds supported limits");
    svn_stream_t *rules=svn_stream_from_string(svn_string_create(text,pool),pool);
    svn_stream_t *group_stream=groups ? svn_stream_from_string(svn_string_create(groups,pool),pool) : NULL;
    svn_error_t *error=svn_repos_authz_parse2(authz,rules,group_stream,NULL,NULL,pool,pool);
    return error ? svn_error_create(SVN_ERR_AUTHZ_INVALID_CONFIG,error,"Invalid authorization rules") : SVN_NO_ERROR;
}

svn_error_t *pgvn_policy_acquire_internal(pgvn_policy **output,pgvn_context *context,
    const char *id,apr_int64_t generation,svn_boolean_t allow_disabled,apr_pool_t *pool)
{
    *output=NULL;
    const char *values[]={id,apr_psprintf(pool,"%" APR_INT64_T_FMT,generation),allow_disabled ? "true" : "false"};
    PGresult *result;
    SVN_ERR(pgvn_control_query(&result,context,
        "SELECT r.canonical_name,r.generation,c.authz_text,c.groups_text,"
        "c.hooks->>'start-commit',c.hooks->>'pre-commit',c.hooks->>'post-commit',"
        "c.hooks->>'pre-revprop-change',c.hooks->>'post-revprop-change',"
        "c.hooks->>'pre-lock',c.hooks->>'post-lock',c.hooks->>'pre-unlock',c.hooks->>'post-unlock' "
        "FROM pgvn.repositories r JOIN pgvn.configurations c ON c.repository_id=r.repository_id AND c.generation=r.generation "
        "WHERE r.repository_id=$1::uuid AND (r.enabled OR $3::boolean) AND ($2::bigint=0 OR r.generation=$2::bigint)",3,values));
    if(PQntuples(result)!=1) {
        PQclear(result);
        return svn_error_create(SVN_ERR_FS_NOT_FOUND,NULL,"PgVN repository or configuration is unavailable");
    }
    pgvn_policy *policy=apr_pcalloc(pool,sizeof(*policy));
    policy->context=context;
    policy->repository_id=apr_pstrdup(pool,id);
    policy->canonical_name=apr_pstrdup(pool,PQgetvalue(result,0,0));
    policy->generation=apr_atoi64(PQgetvalue(result,0,1));
    const char *text=apr_pstrdup(pool,PQgetvalue(result,0,2));
    const char *groups=PQgetisnull(result,0,3) ? NULL : apr_pstrdup(pool,PQgetvalue(result,0,3));
    for(int i=0;i<9;++i)
        policy->hook_handlers[i]=PQgetisnull(result,0,i+4) ? NULL : apr_pstrdup(pool,PQgetvalue(result,0,i+4));
    PQclear(result);
    SVN_ERR(parse_authz(&policy->authz,text,groups,pool));
    SVN_ERR(pgvn_policy_load_protocol(policy,pool));
    *output=policy;
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_policy_acquire(pgvn_policy **output,pgvn_context *context,
    const char *id,apr_int64_t generation,apr_pool_t *pool)
{
    return pgvn_policy_acquire_internal(output,context,id,generation,FALSE,pool);
}

svn_error_t *pgvn_policy_check(svn_boolean_t *allowed,const pgvn_policy *policy,
    const char *user,const char *path,svn_repos_authz_access_t required,apr_pool_t *pool)
{
    SVN_ERR(pgvn_validate_path(path));
    return svn_repos_authz_check_access(policy->authz,policy->canonical_name,path,user,required,allowed,pool);
}

svn_error_t *pgvn_policy_revalidate(pgvn_policy *policy,pgvn_context *context,apr_pool_t *pool)
{
    const char *values[]={policy->repository_id};
    PGresult *result;
    SVN_ERR(pgvn_control_query(&result,context,
        "SELECT enabled,generation FROM pgvn.repositories WHERE repository_id=$1::uuid",1,values));
    svn_boolean_t current=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t")
        && apr_atoi64(PQgetvalue(result,0,1))==policy->generation;
    PQclear(result);
    if(!current) return svn_error_create(SVN_ERR_AUTHZ_UNWRITABLE,NULL,"PgVN configuration changed or repository was disabled; reopen the request");
    return SVN_NO_ERROR;
}

static svn_error_t *publish_body(pgvn_context *context,const char *id,
    apr_int64_t expected,const char *authz,const char *groups,const char *hooks,
    const char *settings,const char *by,apr_pool_t *pool)
{
    const char *values[]={id,apr_psprintf(pool,"%" APR_INT64_T_FMT,expected),authz,groups,hooks,by,settings};
    PGresult *result;
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "SELECT generation FROM pgvn.repositories WHERE repository_id=$1::uuid FOR UPDATE",1,values));
    svn_boolean_t current=PQntuples(result)==1 && apr_atoi64(PQgetvalue(result,0,0))==expected;
    PQclear(result);
    if(!current) return svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"PgVN configuration generation conflict");
    if(context->catalog_version>=4) {
        SVN_ERR(pgvn_exec(&result,context,context->control,
            "SELECT pgvn.valid_hooks($5::jsonb) AND pgvn.valid_settings(COALESCE($7::jsonb,settings)) "
            "FROM pgvn.configurations WHERE repository_id=$1::uuid AND generation=$2::bigint",7,values));
    } else {
        if(settings) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN protocol settings require catalog schema 4");
        SVN_ERR(pgvn_exec(&result,context,context->control,
        "SELECT COALESCE(bool_and(key=ANY(ARRAY['start-commit','pre-commit','post-commit',"
        "'pre-revprop-change','post-revprop-change','pre-lock','post-lock','pre-unlock','post-unlock']) "
        "AND jsonb_typeof(value)='string' AND value#>>'{}' IN ('allow','reject')),true) "
        "FROM jsonb_each($5::jsonb)",5,values));
    }
    svn_boolean_t valid=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t");
    PQclear(result);
    if(!valid) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN hook definitions or protocol settings are invalid");
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "INSERT INTO pgvn.configurations(repository_id,generation,authz_text,groups_text,settings,hooks,published_by) "
        "SELECT repository_id,$2::bigint+1,$3,$4,COALESCE($7::jsonb,settings),$5::jsonb,$6 FROM pgvn.configurations "
        "WHERE repository_id=$1::uuid AND generation=$2::bigint",7,values));
    PQclear(result);
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "UPDATE pgvn.repositories SET generation=generation+1 WHERE repository_id=$1::uuid",1,values));
    PQclear(result);
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_policy_publish(pgvn_context *context,const char *id,apr_int64_t expected,
    const char *authz,const char *groups,const char *hooks,const char *by,apr_pool_t *pool)
{
    return pgvn_policy_publish_settings(context,id,expected,authz,groups,hooks,NULL,by,pool);
}

svn_error_t *pgvn_policy_publish_settings(pgvn_context *context,const char *id,apr_int64_t expected,
    const char *authz,const char *groups,const char *hooks,const char *settings,const char *by,apr_pool_t *pool)
{
    if(expected<1 || expected==APR_INT64_MAX || !by || !*by || !hooks || strlen(hooks)>16384 || (settings && strlen(settings)>4096))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid PgVN configuration publication");
    svn_authz_t *parsed;
    SVN_ERR(parse_authz(&parsed,authz,groups,pool));
    SVN_ERR(pgvn_control_begin(context));
    return pgvn_control_end(context,publish_body(context,id,expected,authz,groups,hooks,settings,by,pool));
}

svn_error_t *pgvn_hook_dispatch(const pgvn_policy *policy,const pgvn_hook_context *event,apr_pool_t *pool)
{
    if(event->event<0 || event->event>PGVN_HOOK_POST_UNLOCK)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Unknown PgVN hook event");
    const char *handler=policy->hook_handlers[event->event];
    if(policy->external_hooks[event->event]) return pgvn_external_hook(policy,event,pool);
    if(!handler && event->event==PGVN_HOOK_PRE_REVPROP)
        return svn_error_create(SVN_ERR_REPOS_DISABLED_FEATURE,NULL,"Revision property changes require an explicit PgVN hook policy");
    if(!handler || !strcmp(handler,"allow")) return SVN_NO_ERROR;
    if(!strcmp(handler,"reject"))
        return svn_error_createf(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN %s policy rejected the event",events[event->event]);
    return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"PgVN hook handler has not been implemented");
}

svn_error_t *pgvn_repos_hook(void *baton,const char *event,const char *user,
    const char *txn,svn_revnum_t rev,apr_pool_t *pool)
{
    return pgvn_repos_hook_context(baton,event,user,txn,rev,NULL,pool);
}

svn_error_t *pgvn_repos_hook_context(void *baton,const char *event,const char *user,
    const char *txn,svn_revnum_t rev,const pgvn_hook_context *details,apr_pool_t *pool)
{
    pgvn_repository *repository=baton;
    int code=-1;
    for(int i=0;i<9;++i) if(!strcmp(events[i],event)) code=i;
    if(code<0) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Unknown PgVN hook event");
    if(!repository->administrative && (code==0 || code==1 || code==3 || code==5 || code==7))
        SVN_ERR(pgvn_policy_revalidate(repository->policy,repository->context,pool));
    if(!user) {
        user=repository->credential_user;
        if(!user) {
            svn_fs_access_t *access;
            SVN_ERR(svn_fs_get_access(&access,repository->fs));
            if(access) SVN_ERR(svn_fs_access_get_username(&user,access));
        }
    }
    pgvn_hook_context context;
    if(details) context=*details; else memset(&context,0,sizeof(context));
    context.repository_id=repository->repository_id; context.username=user;
    context.transaction_id=txn; context.revision=rev; context.event=(pgvn_hook_event)code;
    return pgvn_hook_dispatch(repository->policy,&context,pool);
}

static svn_error_t *guard_current_policy(pgvn_repository *repository,PGconn *connection,apr_pool_t *pool)
{
    if(repository->credential_user)
        SVN_ERR(pgvn_credential_guard(repository->context,connection,repository->credential_domain,
            repository->credential_user,repository->credential_generation,pool));
    const char *values[]={repository->repository_id,
        apr_psprintf(pool,"%" APR_INT64_T_FMT,repository->policy->generation)};
    PGresult *result;
    /* This lock is in the SAME database transaction as revision publication.
     * A concurrent disable/configuration update cannot slip past the recheck. */
    SVN_ERR(pgvn_exec(&result,repository->context,connection,
        "SELECT pgvn.guard_repository($1::uuid,$2::bigint)",2,values));
    svn_boolean_t current=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t");
    PQclear(result);
    if(!current) return svn_error_create(SVN_ERR_AUTHZ_UNWRITABLE,NULL,"PgVN write policy expired or repository was disabled");
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_guard_mutation(void *baton,PGconn *connection,const char *path,apr_pool_t *pool)
{
    pgvn_repository *repository=baton;
    SVN_ERR(guard_current_policy(repository,connection,pool));
    /* Revprop visibility and the explicit pre hook are checked by libsvn_repos.
     * A NULL path is not a repository-root write request. */
    if(!path) return SVN_NO_ERROR;
    const char *user=repository->credential_user;
    if(!user) {
        svn_fs_access_t *access;
        SVN_ERR(svn_fs_get_access(&access,repository->fs));
        if(access) SVN_ERR(svn_fs_access_get_username(&user,access));
    }
    svn_boolean_t allowed;
    SVN_ERR(pgvn_policy_check(&allowed,repository->policy,user,path,svn_authz_write,pool));
    return allowed ? SVN_NO_ERROR : svn_error_create(SVN_ERR_AUTHZ_UNWRITABLE,NULL,"PgVN lock access denied");
}

svn_error_t *pgvn_guard_publish(void *baton,PGconn *connection,const char *txn,apr_pool_t *pool)
{
    pgvn_repository *repository=baton;
    SVN_ERR(guard_current_policy(repository,connection,pool));
    const char *values[]={repository->repository_id,txn}; PGresult *result;
    svn_fs_access_t *access;
    const char *user=NULL;
    SVN_ERR(svn_fs_get_access(&access,repository->fs));
    if(access) SVN_ERR(svn_fs_access_get_username(&user,access));
    SVN_ERR(pgvn_exec(&result,repository->context,connection,
        "SELECT path,node_kind,change_kind,copyfrom_path FROM pgfs.transaction_changes "
        "WHERE repository_id=$1::uuid AND txn_id=$2",2,values));
    svn_error_t *error=NULL;
    svn_boolean_t allowed=FALSE;
    /* Even an empty commit requires write permission. */
    if(!PQntuples(result)) {
        error=pgvn_policy_check(&allowed,repository->policy,user,"/",svn_authz_write,pool);
        if(!error && !allowed) error=svn_error_create(SVN_ERR_AUTHZ_UNWRITABLE,NULL,"PgVN commit access denied");
    }
    for(int i=0;!error && i<PQntuples(result);++i) {
        svn_repos_authz_access_t required=svn_authz_write;
        if(!strcmp(PQgetvalue(result,i,1),"dir") && !strcmp(PQgetvalue(result,i,2),"delete")) required|=svn_authz_recursive;
        error=pgvn_policy_check(&allowed,repository->policy,user,PQgetvalue(result,i,0),required,pool);
        if(!error && !allowed) error=svn_error_create(SVN_ERR_AUTHZ_UNWRITABLE,NULL,"PgVN commit access denied");
        if(!error && !PQgetisnull(result,i,3)) {
            error=pgvn_policy_check(&allowed,repository->policy,user,PQgetvalue(result,i,3),svn_authz_read|svn_authz_recursive,pool);
            if(!error && !allowed) error=svn_error_create(SVN_ERR_AUTHZ_UNREADABLE,NULL,"PgVN copy source access denied");
        }
    }
    PQclear(result);
    return error;
}
