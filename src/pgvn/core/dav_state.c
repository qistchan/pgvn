#include <stdio.h>
#include <string.h>
#include <apr_strings.h>
#include "internal.h"
#include "dav_state.h"
#include "pgfs_native.h"
#include "repos.h"
#include "svn_props.h"
#include "private/svn_utf_private.h"
#include "private/svn_repos_private.h"

static const char *kind_name(pgvn_dav_alias_kind kind)
{
    static const char *names[]={"native","activity","virtual"};
    return kind>=PGVN_DAV_NATIVE && kind<=PGVN_DAV_VIRTUAL ? names[kind] : NULL;
}
static int valid_text(const char *value,int nullable)
{
    if(!value) return nullable;
    size_t length=strlen(value);
    if(!length || length>1024 || !svn_utf__is_valid(value,length)) return 0;
    for(const unsigned char *p=(const unsigned char *)value;*p;++p) if(*p<32 || *p==127) return 0;
    return 1;
}
static svn_error_t *require_repository(pgvn_repository *repository)
{
    if(!repository || repository->context->catalog_version<6)
        return svn_error_create(PGVN_ERR_SCHEMA,NULL,"DAV state requires PgVN catalog schema 6");
    return repository->administrative ? SVN_NO_ERROR : pgvn_policy_revalidate(repository->policy,repository->context,repository->svn->pool);
}
static svn_error_t *arguments(pgvn_dav_alias_kind kind,const char *name,const char *username,int creating)
{
    if(!kind_name(kind) || !valid_text(username,1) ||
        (creating && kind==PGVN_DAV_NATIVE ? name!=NULL : !valid_text(name,0)))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid PgVN DAV state identity");
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_dav_lookup(pgvn_dav_txn_info **output,pgvn_repository *repository,
    pgvn_dav_alias_kind kind,const char *name,const char *username,apr_pool_t *pool)
{
    if(!output) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"DAV state output is required");
    SVN_ERR(arguments(kind,name,username,0)); SVN_ERR(require_repository(repository));
    const char *values[]={repository->repository_id,name,username,kind_name(kind)}; PGresult *result;
    const char *sql=kind==PGVN_DAV_NATIVE ?
        "SELECT d.txn_id,d.creation_phase,t.state,t.committed_rev,d.deleted_at IS NOT NULL "
        "FROM pgvn.dav_transactions d JOIN pgfs.transactions t USING(repository_id,txn_id) "
        "WHERE d.repository_id=$1::uuid AND d.txn_id=$2 AND d.owner_name IS NOT DISTINCT FROM $3 AND $4='native'" :
        "SELECT d.txn_id,d.creation_phase,t.state,t.committed_rev,(d.deleted_at IS NOT NULL OR b.deleted_at IS NOT NULL) "
        "FROM pgvn.dav_bindings b JOIN pgvn.dav_transactions d USING(repository_id,txn_id) "
        "JOIN pgfs.transactions t USING(repository_id,txn_id) "
        "WHERE b.repository_id=$1::uuid AND b.binding_name=$2 AND d.owner_name IS NOT DISTINCT FROM $3 AND b.binding_kind=$4";
    SVN_ERR(pgvn_control_query(&result,repository->context,sql,4,values));
    if(PQntuples(result)!=1) { PQclear(result); return svn_error_create(SVN_ERR_FS_NOT_FOUND,NULL,"PgVN DAV state was not found"); }
    pgvn_dav_txn_info *info=apr_pcalloc(pool,sizeof(*info));
    info->transaction_id=apr_pstrdup(pool,PQgetvalue(result,0,0));
    info->creation_failed=!strcmp(PQgetvalue(result,0,1),"failed");
    info->creation_pending=!strcmp(PQgetvalue(result,0,1),"creating");
    info->committed_revision=PQgetisnull(result,0,3) ? SVN_INVALID_REVNUM : (svn_revnum_t)apr_atoi64(PQgetvalue(result,0,3));
    info->phase=!strcmp(PQgetvalue(result,0,4),"t") ? PGVN_DAV_DELETED :
        !strcmp(PQgetvalue(result,0,2),"committed") ? PGVN_DAV_COMMITTED :
        !strcmp(PQgetvalue(result,0,2),"aborted") || info->creation_failed ? PGVN_DAV_ABORTED :
        info->creation_pending ? PGVN_DAV_CREATING : PGVN_DAV_ACTIVE;
    PQclear(result); *output=info; return SVN_NO_ERROR;
}

typedef struct dav_baton {
    pgvn_repository *repository;
    pgvn_dav_alias_kind kind;
    const char *name,*username,*transaction_id,*generation;
    int leased,recover,failed;
} dav_baton;
static svn_error_t *exec(PGresult **result,PGconn *connection,dav_baton *b,
    const char *sql,int count,const char *const *values)
{
    return pgvn_exec(result,b->repository->context,connection,sql,count,values);
}
static svn_error_t *acquire_creation_lease(PGconn *connection,dav_baton *b,int transaction)
{
    const char *values[]={b->repository->repository_id,b->transaction_id}; PGresult *result;
    SVN_ERR(exec(&result,connection,b,transaction ?
        "SELECT pg_try_advisory_xact_lock(hashtextextended('pgvn-dav-create:'||$1::uuid::text||':'||$2,0))" :
        "SELECT pg_try_advisory_lock(hashtextextended('pgvn-dav-create:'||$1::uuid::text||':'||$2,0))",2,values));
    int acquired=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
    if(!acquired) return svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"PgVN DAV transaction creation is still owned by another request");
    if(!transaction) b->leased=1;
    return SVN_NO_ERROR;
}
static svn_error_t *bind_created(PGconn *connection,svn_fs_txn_t *txn,void *baton,apr_pool_t *pool)
{
    dav_baton *b=baton; SVN_ERR(svn_fs_txn_name(&b->transaction_id,txn,pool));
    SVN_ERR(acquire_creation_lease(connection,b,0));
    const char *values[]={b->repository->repository_id,b->transaction_id,b->username,b->generation,b->name,kind_name(b->kind)};
    PGresult *result;
    SVN_ERR(exec(&result,connection,b,"INSERT INTO pgvn.dav_transactions(repository_id,txn_id,owner_name,creation_generation) "
        "VALUES($1::uuid,$2,$3,$4::bigint)",4,values)); PQclear(result);
    if(b->kind!=PGVN_DAV_NATIVE) {
        SVN_ERR(exec(&result,connection,b,"INSERT INTO pgvn.dav_bindings(repository_id,txn_id,binding_name,binding_kind) "
            "VALUES($1::uuid,$2,$5,$6)",6,values)); PQclear(result);
    }
    return SVN_NO_ERROR;
}
static svn_error_t *release_creation_lease(PGconn *connection,void *baton,apr_pool_t *pool)
{
    dav_baton *b=baton; const char *values[]={b->repository->repository_id,b->transaction_id}; PGresult *result;
    SVN_ERR(exec(&result,connection,b,"SELECT pg_advisory_unlock(hashtextextended('pgvn-dav-create:'||$1::uuid::text||':'||$2,0))",2,values));
    PQclear(result); b->leased=0; return SVN_NO_ERROR;
}
static void release_lease(dav_baton *b,apr_pool_t *pool)
{
    if(b->leased) {
        svn_error_t *error=pgfs_native_atomic(b->repository->fs,release_creation_lease,b,pool);
        if(error) { pgvn_telemetry_event("dav","creation_lease_release_failed",error->apr_err); svn_error_clear(error); }
    }
}
static svn_error_t *activate(PGconn *connection,void *baton,apr_pool_t *pool)
{
    dav_baton *b=baton; const char *values[]={b->repository->repository_id,b->transaction_id,b->username,b->generation}; PGresult *result;
    SVN_ERR(exec(&result,connection,b,"SELECT pgvn.guard_repository($1::uuid,$4::bigint)",4,values));
    int allowed=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
    if(!allowed) return svn_error_create(SVN_ERR_AUTHZ_UNWRITABLE,NULL,"PgVN DAV creation policy changed");
    SVN_ERR(exec(&result,connection,b,"UPDATE pgvn.dav_transactions SET creation_phase='active',updated_at=clock_timestamp() "
        "WHERE repository_id=$1::uuid AND txn_id=$2 AND owner_name IS NOT DISTINCT FROM $3 "
        "AND creation_phase='creating' AND deleted_at IS NULL RETURNING txn_id",3,values));
    int changed=PQntuples(result)==1; PQclear(result);
    return changed ? SVN_NO_ERROR : svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"PgVN DAV creation state changed");
}
static svn_error_t *close_state(PGconn *connection,void *baton,apr_pool_t *pool)
{
    dav_baton *b=baton; const char *values[]={b->repository->repository_id,b->transaction_id,b->username,b->failed ? "failed" : "active"};
    PGresult *result;
    if(b->recover) SVN_ERR(acquire_creation_lease(connection,b,1));
    SVN_ERR(exec(&result,connection,b,"SELECT creation_phase FROM pgvn.dav_transactions "
        "WHERE repository_id=$1::uuid AND txn_id=$2 AND owner_name IS NOT DISTINCT FROM $3 FOR UPDATE",3,values));
    int valid=PQntuples(result)==1 && (!(b->recover || b->failed) || !strcmp(PQgetvalue(result,0,0),"creating"));
    PQclear(result);
    if(!valid) return svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"PgVN DAV closing state changed");
    SVN_ERR(exec(&result,connection,b,"UPDATE pgvn.dav_transactions SET creation_phase=CASE WHEN $4='failed' THEN 'failed' ELSE creation_phase END,"
        "updated_at=clock_timestamp(),deleted_at=COALESCE(deleted_at,clock_timestamp()) "
        "WHERE repository_id=$1::uuid AND txn_id=$2 AND owner_name IS NOT DISTINCT FROM $3",4,values)); PQclear(result);
    SVN_ERR(exec(&result,connection,b,"UPDATE pgvn.dav_bindings SET deleted_at=COALESCE(deleted_at,clock_timestamp()) "
        "WHERE repository_id=$1::uuid AND txn_id=$2",2,values)); PQclear(result);
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_dav_create(const char **transaction_id,pgvn_repository *repository,
    pgvn_dav_alias_kind kind,const char *alias,const char *username,
    svn_revnum_t base,apr_hash_t *revision_properties,apr_pool_t *pool)
{
    if(!transaction_id) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"DAV transaction output is required");
    SVN_ERR(arguments(kind,alias,username,1)); SVN_ERR(require_repository(repository));
    const char *access=username ? repository->policy->authenticated_access : repository->policy->anonymous_access;
    svn_boolean_t writable=FALSE;
    if(access && !strcmp(access,"write")) SVN_ERR(svn_repos_authz_check_access(repository->policy->authz,
        repository->policy->canonical_name,NULL,username,svn_authz_write,&writable,pool));
    if(!writable) return svn_error_create(SVN_ERR_AUTHZ_UNWRITABLE,NULL,"PgVN DAV identity has no repository write access");
    apr_hash_t *props=revision_properties ? apr_hash_copy(pool,revision_properties) : apr_hash_make(pool);
    if(username) apr_hash_set(props,SVN_PROP_REVISION_AUTHOR,APR_HASH_KEY_STRING,svn_string_create(username,pool));
    apr_array_header_t *properties=svn_prop_hash_to_array(props,pool);
    for(int i=0;i<properties->nelts;++i) {
        svn_prop_t *prop=&APR_ARRAY_IDX(properties,i,svn_prop_t);
        SVN_ERR(svn_repos__validate_prop(prop->name,prop->value,pool));
    }
    if(!SVN_IS_VALID_REVNUM(base)) SVN_ERR(svn_fs_youngest_rev(&base,repository->fs,pool));
    char generation[32]; snprintf(generation,sizeof(generation),"%lld",(long long)repository->policy->generation);
    dav_baton b={repository,kind,alias,username,NULL,generation,0,0,0}; svn_fs_txn_t *txn;
    svn_error_t *error=pgfs_native_begin_bound_txn(&txn,repository->fs,base,SVN_FS_TXN_CHECK_LOCKS,properties,bind_created,&b,pool);
    if(error) { release_lease(&b,pool); return error; }
    /* Staging and bindings are durable before the hook runs, matching the
     * repository API's requirement that an external hook can inspect its txn. */
    error=svn_repos__hooks_start_commit(repository->svn,NULL,username,repository->svn->client_capabilities,b.transaction_id,pool);
    if(error) {
        b.failed=1;
        error=svn_error_compose_create(error,pgfs_native_abort_bound_txn(repository->fs,b.transaction_id,close_state,&b,pool));
        release_lease(&b,pool); return error;
    }
    error=pgfs_native_atomic(repository->fs,activate,&b,pool);
    if(error) {
        /* An activation COMMIT acknowledgement may be lost. Confirm the same
         * persistent transaction; never create a replacement or rerun hooks. */
        pgvn_dav_txn_info *info; svn_error_t *lookup=pgvn_dav_lookup(&info,repository,PGVN_DAV_NATIVE,b.transaction_id,username,pool);
        if(!lookup && (info->phase==PGVN_DAV_ACTIVE || info->phase==PGVN_DAV_COMMITTED)) { svn_error_clear(error); error=NULL; }
        else {
            svn_error_clear(lookup);
            if(error->apr_err==SVN_ERR_AUTHZ_UNWRITABLE) {
                b.failed=1; error=svn_error_compose_create(error,
                    pgfs_native_abort_bound_txn(repository->fs,b.transaction_id,close_state,&b,pool));
            }
        }
    }
    release_lease(&b,pool); if(!error) *transaction_id=apr_pstrdup(pool,b.transaction_id); return error;
}

svn_error_t *pgvn_dav_cancel(pgvn_repository *repository,pgvn_dav_alias_kind kind,
    const char *name,const char *username,apr_pool_t *pool)
{
    pgvn_dav_txn_info *info; SVN_ERR(pgvn_dav_lookup(&info,repository,kind,name,username,pool));
    if(info->phase==PGVN_DAV_DELETED) return SVN_NO_ERROR;
    if(info->creation_pending) return svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"PgVN DAV creation has not completed");
    dav_baton b={repository,kind,name,username,info->transaction_id,NULL,0,0,0};
    if(SVN_IS_VALID_REVNUM(info->committed_revision)) return pgfs_native_atomic(repository->fs,close_state,&b,pool);
    svn_error_t *error=pgfs_native_abort_bound_txn(repository->fs,info->transaction_id,close_state,&b,pool);
    if(error && error->apr_err==SVN_ERR_FS_NO_SUCH_TRANSACTION) {
        pgvn_dav_txn_info *latest; svn_error_t *lookup=pgvn_dav_lookup(&latest,repository,kind,name,username,pool);
        if(!lookup && SVN_IS_VALID_REVNUM(latest->committed_revision)) {
            svn_error_clear(error); return pgfs_native_atomic(repository->fs,close_state,&b,pool);
        }
        svn_error_clear(lookup);
    }
    return error;
}
svn_error_t *pgvn_dav_recover_creation(pgvn_repository *repository,const char *transaction_id,
    const char *username,apr_pool_t *pool)
{
    pgvn_dav_txn_info *info; SVN_ERR(pgvn_dav_lookup(&info,repository,PGVN_DAV_NATIVE,transaction_id,username,pool));
    if(!info->creation_pending) return SVN_NO_ERROR;
    dav_baton b={repository,PGVN_DAV_NATIVE,NULL,username,transaction_id,NULL,0,1,1};
    return pgfs_native_abort_bound_txn(repository->fs,transaction_id,close_state,&b,pool);
}
