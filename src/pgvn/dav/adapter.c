#include <string.h>
#include "adapter.h"
#include "internal.h"
#include "http_log.h"
#include "http_protocol.h"
#include <apr_strings.h>
#include "svn_config.h"
#include "svn_dirent_uri.h"
#include "svn_hash.h"
#include "svn_pools.h"
#include "status.h"

/* Configuration is global to this dedicated Apache process; repository state
 * and routes come exclusively from the catalog. Each request owns its FS. */
static const char *config_path;
static apr_pool_t *config_pool;
static apr_pool_t *worker_pool;
static apr_thread_mutex_t *init_mutex;
static pgvn_context *worker_context;

const char *pgvn_http_mount(request_rec *r)
{
    const char *root=dav_svn__get_root_dir(r);
    return root ? root : "/";
}

const char *pgvn_http_config(cmd_parms *cmd,void *unused,const char *path)
{
    const char *error=ap_check_cmd_context(cmd,GLOBAL_ONLY);
    if(error) return error;
    if(config_pool!=cmd->pool) { config_pool=cmd->pool;config_path=NULL; }
    if(config_path) return "PgVNConfig may be specified only once per Apache instance";
    config_path=ap_server_root_relative(cmd->pool,path);
    return config_path ? NULL : "PgVNConfig requires a valid global configuration path";
}

void pgvn_http_child_init(apr_pool_t *pool,server_rec *server)
{
    worker_pool=pool;worker_context=NULL;
    if(apr_thread_mutex_create(&init_mutex,APR_THREAD_MUTEX_DEFAULT,pool)!=APR_SUCCESS)
        ap_log_error(APLOG_MARK,APLOG_CRIT,0,server,"PgVN worker mutex initialization failed");
    pgvn_dav_status_start(config_path,pool,server);
}

static svn_error_t *context(pgvn_context **output,request_rec *r)
{
    *output=NULL;
    if(!worker_pool || !init_mutex || !config_path)
        return svn_error_create(PGVN_ERR_SCHEMA,NULL,"PgVNConfig is required for DAV requests");
    apr_thread_mutex_lock(init_mutex);
    svn_error_t *error=NULL;
    if(!worker_context) {
        apr_pool_t *pool=svn_pool_create(worker_pool);
        svn_config_t *config;
        error=svn_config_read3(&config,config_path,TRUE,TRUE,TRUE,pool);
        if(error) {
            svn_error_clear(error);error=svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid PgVN DAV configuration");
        }
        const char *dsn=NULL;
        if(!error) {
            svn_config_get(config,&dsn,"pgvn","connection",NULL);
            pgvn_context_options options={dsn,3,5000};
            error=pgvn_context_create(&worker_context,&options,pool);
        }
        if(!error && worker_context->catalog_version!=PGVN_CATALOG_VERSION)
            error=svn_error_create(PGVN_ERR_SCHEMA,NULL,"PgVN DAV requires the current catalog schema");
        if(!error) {
            worker_context->minimum_catalog_version=PGVN_CATALOG_VERSION;
            worker_context->configuration_path=apr_pstrdup(pool,config_path);
            svn_config_get(config,&worker_context->hook_root,"pgvn","hook-root",NULL);
            pgvn_dav_status_initialized();
        } else {
            worker_context=NULL;svn_pool_destroy(pool);
        }
    }
    *output=worker_context;
    apr_thread_mutex_unlock(init_mutex);
    return error;
}

svn_error_t *pgvn_http_resolve(pgvn_binding **binding,request_rec *r,const char *uri,apr_pool_t *pool)
{
    pgvn_dav_status_request(r);
    /* Apache and the DAV caller have already decoded URI paths. Decoding here
     * again would turn literal percent-encoded repository names into aliases. */
    char *path=apr_pstrdup(pool,uri);
    ap_no2slash(path);
    apr_size_t length=strlen(path);
    if(length>1 && path[length-1]=='/') path[length-1]=0;
    void *data=NULL;apr_pool_userdata_get(&data,"pgvn-http-bindings",r->pool);
    apr_hash_t *bindings=data;
    if(!bindings) { bindings=apr_hash_make(r->pool);apr_pool_userdata_setn(bindings,"pgvn-http-bindings",NULL,r->pool); }
    *binding=svn_hash_gets(bindings,path);
    if(!*binding) {
        if(apr_hash_count(bindings)>=32) return svn_error_create(SVN_ERR_AUTHZ_UNREADABLE,NULL,"PgVN per-request route limit exceeded");
        pgvn_context *ctx;SVN_ERR(context(&ctx,r));
        SVN_ERR(pgvn_catalog_resolve(binding,ctx,path,r->pool));
        svn_hash_sets(bindings,apr_pstrdup(r->pool,path),*binding);
    }
    pgvn_command_repository((*binding)->repository_id);
    pgvn_dav_status_generation((*binding)->configuration_version);
    return SVN_NO_ERROR;
}

static svn_error_t *named_policy(pgvn_policy **policy,request_rec *r,const char *name,apr_pool_t *pool)
{
    void *data=NULL;
    apr_pool_userdata_get(&data,"pgvn-http-policies",r->pool);
    apr_hash_t *policies=data;
    if(!policies) {
        policies=apr_hash_make(r->pool);
        apr_pool_userdata_setn(policies,"pgvn-http-policies",NULL,r->pool);
    }
    *policy=svn_hash_gets(policies,name);
    if(*policy) return SVN_NO_ERROR;
    if(apr_hash_count(policies)>=32)
        return svn_error_create(SVN_ERR_AUTHZ_UNREADABLE,NULL,"PgVN per-request policy limit exceeded");
    pgvn_context *ctx;pgvn_binding *binding;
    SVN_ERR(context(&ctx,r));
    SVN_ERR(pgvn_repository_lookup(&binding,ctx,apr_pstrcat(r->pool,"name:",name,NULL),r->pool));
    SVN_ERR(pgvn_policy_acquire(policy,ctx,binding->repository_id,0,r->pool));
    svn_hash_sets(policies,apr_pstrdup(r->pool,name),*policy);
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_http_policy(svn_authz_t **authz,request_rec *r,apr_pool_t *pool)
{
    pgvn_binding *binding;pgvn_policy *policy;
    SVN_ERR(pgvn_http_resolve(&binding,r,r->uri,pool));
    SVN_ERR(named_policy(&policy,r,binding->canonical_name,pool));
    *authz=policy->authz;return SVN_NO_ERROR;
}

svn_error_t *pgvn_http_check_access(request_rec *r,const char *name,const char *path,
    const char *user,svn_repos_authz_access_t required,svn_boolean_t *allowed,apr_pool_t *pool)
{
    *allowed=FALSE;
    pgvn_policy *policy;
    SVN_ERR(named_policy(&policy,r,name,pool));
    const char *profile=user ? policy->authenticated_access : policy->anonymous_access;
    if(!strcmp(profile,"none") || ((required&svn_authz_write) && strcmp(profile,"write"))) return SVN_NO_ERROR;
    svn_error_t *error=svn_repos_authz_check_access(policy->authz,policy->canonical_name,path,user,required,allowed,pool);
    ap_log_rerror(APLOG_MARK,APLOG_DEBUG,0,r,"PgVN authz method=%s required=%d allowed=%d user=%s repository=%s path=%s",
        r->method,(int)required,(int)*allowed,user ? user : "<anonymous>",name,path ? path : "<any>");
    return error;
}

svn_error_t *pgvn_http_open(dav_svn_repos *repos,request_rec *r)
{
    pgvn_binding *binding;pgvn_context *ctx;
    SVN_ERR(pgvn_http_resolve(&binding,r,r->uri,r->pool));
    SVN_ERR(context(&ctx,r));
    SVN_ERR(pgvn_repos_open(&repos->pgvn_repository,ctx,binding->repository_id,r->pool));
    repos->repos=pgvn_repo_svn(repos->pgvn_repository);
    repos->fs_path=apr_pstrcat(r->pool,"pgvn:",binding->repository_id,NULL);
    repos->pgvn_request=r;
    return SVN_NO_ERROR;
}

static dav_error *convert(svn_error_t *error,const dav_svn_repos *repos)
{
    if(!error) return NULL;
    pgvn_exit_code code=pgvn_error_exit_code(error);
    int status=code==PGVN_EXIT_NOT_FOUND ? HTTP_NOT_FOUND : code==PGVN_EXIT_DENIED ? HTTP_FORBIDDEN :
        code==PGVN_EXIT_USAGE ? HTTP_BAD_REQUEST : code==PGVN_EXIT_CONFLICT ? HTTP_CONFLICT : HTTP_SERVICE_UNAVAILABLE;
    return dav_svn__sanitize_error(error,"PgVN DAV operation failed",status,repos->pgvn_request);
}

const char *pgvn_http_get_txn(const dav_svn_repos *repos,pgvn_dav_alias_kind kind,const char *name)
{
    pgvn_dav_txn_info *info;
    svn_error_t *error=pgvn_dav_lookup(&info,repos->pgvn_repository,kind,name,repos->username,repos->pool);
    if(error) {
        if(pgvn_error_exit_code(error)==PGVN_EXIT_NOT_FOUND) { svn_error_clear(error);return NULL; }
        ((dav_svn_repos *)repos)->pgvn_error=svn_error_compose_create(repos->pgvn_error,error);return NULL;
    }
    if(info->phase==PGVN_DAV_ACTIVE || info->phase==PGVN_DAV_COMMITTED ||
       (info->phase==PGVN_DAV_DELETED && SVN_IS_VALID_REVNUM(info->committed_revision)))
        return info->transaction_id;
    return NULL;
}

dav_error *pgvn_http_pending(const dav_svn_repos *repos)
{
    svn_error_t *error=repos->pgvn_error;
    ((dav_svn_repos *)repos)->pgvn_error=NULL;
    return convert(error,repos);
}

dav_error *pgvn_http_create_txn(dav_svn_repos *repos,const char **name,apr_hash_t *properties,
    pgvn_dav_alias_kind kind,const char *alias,apr_pool_t *pool)
{
    svn_revnum_t youngest;
    svn_error_t *error=svn_fs_youngest_rev(&youngest,repos->fs,pool);
    if(!error) error=pgvn_dav_create(name,repos->pgvn_repository,kind,alias,repos->username,youngest,properties,pool);
    return convert(error,repos);
}

dav_error *pgvn_http_cancel(const dav_svn_repos *repos,pgvn_dav_alias_kind kind,const char *name)
{
    return convert(pgvn_dav_cancel(repos->pgvn_repository,kind,name,repos->username,repos->pool),repos);
}

dav_error *pgvn_http_validate_txn(dav_svn_repos *repos,const char *name,svn_boolean_t receipt_allowed)
{
    if(!name) return convert(svn_error_create(SVN_ERR_FS_NOT_FOUND,NULL,"PgVN DAV transaction is unavailable"),repos);
    pgvn_dav_txn_info *info;
    svn_error_t *error=pgvn_dav_lookup(&info,repos->pgvn_repository,PGVN_DAV_NATIVE,name,repos->username,repos->pool);
    if(!error && info->phase!=PGVN_DAV_ACTIVE &&
       !(receipt_allowed && SVN_IS_VALID_REVNUM(info->committed_revision)))
        error=svn_error_create(SVN_ERR_FS_NO_SUCH_TRANSACTION,NULL,"PgVN DAV transaction is not active");
    return convert(error,repos);
}

svn_error_t *pgvn_http_commit(const char **conflict,svn_revnum_t *revision,dav_svn_repos *repos,
    const char *name,svn_fs_txn_t **txn,apr_pool_t *pool)
{
    *revision=SVN_INVALID_REVNUM;*conflict=NULL;*txn=NULL;
    pgvn_dav_txn_info *info;
    SVN_ERR(pgvn_dav_lookup(&info,repos->pgvn_repository,PGVN_DAV_NATIVE,name,repos->username,pool));
    if(SVN_IS_VALID_REVNUM(info->committed_revision))
        return pgvn_transaction_receipt(revision,repos->pgvn_repository,name,repos->username,pool);
    if(info->phase!=PGVN_DAV_ACTIVE)
        return svn_error_create(SVN_ERR_FS_NO_SUCH_TRANSACTION,NULL,"PgVN DAV transaction is not active");
    SVN_ERR(svn_fs_open_txn(txn,repos->fs,name,pool));
    return svn_repos_fs_commit_txn(conflict,repos->repos,revision,*txn,pool);
}
