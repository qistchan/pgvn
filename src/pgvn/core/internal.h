#ifndef PGVN_INTERNAL_H
#define PGVN_INTERNAL_H
#include <windows.h>
#include <apr_thread_mutex.h>
#include <libpq-fe.h>
#include "pgvn.h"
#include "telemetry.h"

struct pgvn_context {
    const char *connection_string;
    int connect_timeout_seconds;
    int statement_timeout_ms;
    PGconn *control;
    apr_thread_mutex_t *control_mutex;
    svn_boolean_t control_transaction;
    int minimum_catalog_version;
    int catalog_version;
    const char *hook_root;
    const char *configuration_path;
    HANDLE cancellation_event;
    volatile LONG leases;
    volatile LONG64 sql_calls;
    volatile LONG64 sql_errors;
    volatile LONG64 sql_microseconds;
    apr_time_t schema_checked_at;
    const char *instance_id;
    apr_pool_t *pool;
};

typedef struct pgvn_hook_handler {
    const char *executable;
    apr_array_header_t *arguments;
    unsigned timeout_seconds;
} pgvn_hook_handler;

struct pgvn_policy {
    pgvn_context *context;
    const char *repository_id;
    const char *canonical_name;
    apr_int64_t generation;
    svn_authz_t *authz;
    const char *hook_handlers[9];
    pgvn_hook_handler *external_hooks[9];
    const char *identity_domain;
    const char *anonymous_access;
    const char *authenticated_access;
};

struct pgvn_repository {
    pgvn_context *context;
    const char *repository_id;
    pgvn_policy *policy;
    svn_fs_t *fs;
    svn_repos_t *svn;
    svn_boolean_t administrative;
    const char *credential_domain;
    const char *credential_user;
    apr_int64_t credential_generation;
};

svn_error_t *pgvn_database_open(pgvn_context **context,const pgvn_context_options *options,apr_pool_t *pool);
svn_error_t *pgvn_database_validate(pgvn_context *context);
int pgvn_catalog_version(const char *version_list);
svn_error_t *pgvn_policy_load_protocol(pgvn_policy *policy,apr_pool_t *pool);
svn_error_t *pgvn_external_hook(const pgvn_policy *policy,const pgvn_hook_context *event,apr_pool_t *pool);
svn_error_t *pgvn_require_admin(pgvn_context *context);
svn_error_t *pgvn_policy_acquire_internal(pgvn_policy **policy,pgvn_context *context,
    const char *repository_id,apr_int64_t generation,svn_boolean_t allow_disabled,apr_pool_t *pool);
svn_error_t *pgvn_exec(PGresult **result, pgvn_context *context, PGconn *connection,
                       const char *sql, int count, const char *const *values);
svn_error_t *pgvn_connect(PGconn **connection, pgvn_context *context);
void pgvn_metric_query(void *baton, const char *sqlstate, apr_interval_time_t elapsed);
void pgvn_metric_closed(void *baton);
svn_error_t *pgvn_control_begin(pgvn_context *context);
svn_error_t *pgvn_control_end(pgvn_context *context, svn_error_t *error);
svn_error_t *pgvn_control_query(PGresult **result, pgvn_context *context,
                               const char *sql, int count, const char *const *values);
svn_error_t *pgvn_repos_hook(void *repository, const char *event, const char *user,
                            const char *txn, svn_revnum_t rev, apr_pool_t *pool);
svn_error_t *pgvn_repos_hook_context(void *repository,const char *event,const char *user,
    const char *txn,svn_revnum_t rev,const pgvn_hook_context *details,apr_pool_t *pool);
const char *pgvn_hook_name(pgvn_hook_event event);
svn_error_t *pgvn_guard_publish(void *baton, PGconn *connection,
                              const char *txn, apr_pool_t *pool);
svn_error_t *pgvn_guard_mutation(void *baton,PGconn *connection,const char *path,apr_pool_t *pool);
svn_error_t *pgvn_validate_path(const char *path);
svn_error_t *pgvn_validate_fspath(const char *path);
svn_error_t *pgvn_validate_name(const char *name);
const char *pgvn_new_uuid(apr_pool_t *pool);
#endif
