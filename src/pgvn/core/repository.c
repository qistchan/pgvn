#include <string.h>
#include "internal.h"
#include "telemetry.h"
#include <apr_strings.h>
#include <apr_uuid.h>
#include "repos.h"
#include "pgfs_native.h"

static void query_observer(void *baton,const char *state,apr_interval_time_t elapsed)
{
    pgvn_metric_query(((pgvn_repository *)baton)->context,state,elapsed);
}

static void query_started_observer(void *baton) { pgvn_telemetry_query_start(); }
static void writer_observer(void *baton,int change) { pgvn_telemetry_writer(change); }

static void published_observer(void *baton,svn_boolean_t receipt)
{
    pgvn_telemetry_publication(receipt);
}

static void close_observer(void *baton)
{
    pgvn_metric_closed(((pgvn_repository *)baton)->context);
}

static svn_error_t *bind_repository(pgvn_repository **output,pgvn_context *context,
    const char *id,svn_boolean_t administrative,apr_pool_t *pool)
{
    *output=NULL;
    apr_uuid_t parsed; char canonical_id[APR_UUID_FORMATTED_LENGTH+1];
    if(!id || apr_uuid_parse(&parsed,id)!=APR_SUCCESS)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid PgVN repository ID");
    apr_uuid_format(canonical_id,&parsed);
    pgvn_repository *repository=apr_pcalloc(pool,sizeof(*repository));
    repository->context=context;
    repository->repository_id=apr_pstrdup(pool,canonical_id);
    id=repository->repository_id;
    repository->administrative=administrative;
    if(administrative) SVN_ERR(pgvn_require_admin(context));
    SVN_ERR(pgvn_policy_acquire_internal(&repository->policy,context,id,0,administrative,pool));
    /* The pinned public constructor is deprecated but supplies the complete
     * loader-owned layout. No path dispatch or filesystem operation occurs. */
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    repository->fs=svn_fs_new(NULL,pool);
#pragma clang diagnostic pop
    PGconn *connection;
    SVN_ERR(pgvn_connect(&connection,context));
    pgfs_observer observer={query_observer,close_observer,administrative ? NULL : pgvn_guard_publish,repository,
        administrative ? NULL : pgvn_guard_mutation,published_observer,query_started_observer,writer_observer};
    SVN_ERR(pgfs_native_bind(repository->fs,connection,context->connection_string,id,&observer,pool));
    if(!administrative) SVN_ERR(pgvn_policy_revalidate(repository->policy,context,pool));
    *output=repository;
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_fs_bind(svn_fs_t **fs,pgvn_context *context,const char *id,apr_pool_t *pool)
{
    pgvn_repository *repository;
    *fs=NULL;
    SVN_ERR(bind_repository(&repository,context,id,FALSE,pool));
    *fs=repository->fs;
    return SVN_NO_ERROR;
}

static svn_error_t *repos_open(pgvn_repository **output,pgvn_context *context,
    const char *id,svn_boolean_t administrative,apr_pool_t *pool)
{
    *output=NULL;
    pgvn_repository *repository;
    SVN_ERR(bind_repository(&repository,context,id,administrative,pool));
    svn_repos_t *repos=apr_pcalloc(pool,sizeof(*repos));
    repos->fs=repository->fs;
    repos->path=apr_pstrcat(pool,"pgvn:",repository->repository_id,NULL);
    repos->format=SVN_REPOS__FORMAT_NUMBER;
    repos->fs_type="pg";
    repos->repository_capabilities=apr_hash_make(pool);
    repos->pool=pool;
    repos->pgvn_baton=repository;
    /* conf/hooks/locks/db and hooks_env stay NULL. Patched hook dispatch and
     * explicit unsupported path APIs prevent treating identity as a directory. */
    repository->svn=repos;
    *output=repository;
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_repos_open(pgvn_repository **output,pgvn_context *context,
    const char *id,apr_pool_t *pool)
{
    return repos_open(output,context,id,FALSE,pool);
}

svn_error_t *pgvn_repos_open_admin(pgvn_repository **output,pgvn_context *context,
    const char *id,apr_pool_t *pool)
{
    return repos_open(output,context,id,TRUE,pool);
}
svn_repos_t *pgvn_repo_svn(pgvn_repository *repository)
{
    return repository->svn;
}

static svn_error_t *receipt_body(svn_revnum_t *revision,pgvn_repository *repository,
    const char *txn,const char *username,apr_pool_t *pool)
{
    const char *values[]={repository->repository_id,txn};
    PGresult *result;
    SVN_ERR(pgvn_exec(&result,repository->context,repository->context->control,
        "SELECT t.state,t.committed_rev,convert_from(p.value,'UTF8') "
        "FROM pgfs.transactions t LEFT JOIN pgfs.transaction_properties p "
        "ON p.repository_id=t.repository_id AND p.txn_id=t.txn_id AND p.name='svn:author' "
        "WHERE t.repository_id=$1::uuid AND t.txn_id=$2 FOR UPDATE OF t",2,values));
    if(PQntuples(result)!=1 || PQgetisnull(result,0,2) || !username || strcmp(username,PQgetvalue(result,0,2))) {
        PQclear(result);
        return svn_error_create(SVN_ERR_AUTHZ_UNREADABLE,NULL,"PgVN transaction receipt is unavailable to this identity");
    }
    if(!strcmp(PQgetvalue(result,0,0),"committed") && !PQgetisnull(result,0,1))
        *revision=(svn_revnum_t)apr_atoi64(PQgetvalue(result,0,1));
    PQclear(result);
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_transaction_receipt(svn_revnum_t *revision,pgvn_repository *repository,
    const char *txn,const char *username,apr_pool_t *pool)
{
    *revision=SVN_INVALID_REVNUM;
    SVN_ERR(pgvn_control_begin(repository->context));
    svn_error_t *error=pgvn_control_end(repository->context,receipt_body(revision,repository,txn,username,pool));
    if(!error && SVN_IS_VALID_REVNUM(*revision)) pgvn_telemetry_publication(TRUE);
    return error;
}
