#ifndef PGVN_CORE_H
#define PGVN_CORE_H

#include <stddef.h>
#include <apr_pools.h>
#include <apr_time.h>
#include "svn_error.h"
#include "svn_repos.h"

#define PGVN_VERSION "0.1.0-dev"
#define PGVN_MILESTONE "M1"
#define PGVN_SVN_BASE "1.14.5"
#define PGVN_MONITOR_QUEUE_LIMIT 64
#define PGVN_MONITOR_HEADER_LIMIT 32768
#define PGVN_MONITOR_WORKERS 2
#define PGVN_MONITOR_SAMPLE_SECONDS 5
#define PGVN_MONITOR_MAX_AGE_SECONDS 15
#define PGVN_MONITOR_PAGE_DEFAULT 50
#define PGVN_MONITOR_PAGE_MAX 200
#define PGVN_MONITOR_TREND_SECONDS 900
#define PGVN_MONITOR_EVENT_LIMIT 200

/* Stable process exit classes. SVN error codes retain their protocol meaning. */
typedef enum pgvn_exit_code {
    PGVN_EXIT_OK = 0, PGVN_EXIT_FAILURE = 1, PGVN_EXIT_USAGE = 2,
    PGVN_EXIT_DATABASE = 3, PGVN_EXIT_SCHEMA = 4, PGVN_EXIT_DENIED = 5,
    PGVN_EXIT_NOT_FOUND = 6, PGVN_EXIT_CONFLICT = 7
} pgvn_exit_code;

/* Core interfaces are in-process, pool-owned, and private to the pinned build.
 * A binding identifies database storage; it never denotes a local directory.
 * Repository bindings are implemented in M1. Transport and full monitoring
 * capabilities are advertised separately from these in-process interfaces. */
typedef struct pgvn_context pgvn_context;
typedef struct pgvn_repository pgvn_repository;
typedef struct pgvn_policy pgvn_policy;
typedef struct pgvn_context_options {
    const char *connection_string; /* secret: must never be logged */
    int connect_timeout_seconds;
    int statement_timeout_ms;
} pgvn_context_options;

typedef enum pgvn_hook_event {
    PGVN_HOOK_START_COMMIT, PGVN_HOOK_PRE_COMMIT, PGVN_HOOK_POST_COMMIT,
    PGVN_HOOK_PRE_REVPROP, PGVN_HOOK_POST_REVPROP,
    PGVN_HOOK_PRE_LOCK, PGVN_HOOK_POST_LOCK, PGVN_HOOK_PRE_UNLOCK, PGVN_HOOK_POST_UNLOCK
} pgvn_hook_event;
typedef struct pgvn_hook_context {
    const char *repository_id;
    const char *username;
    const char *transaction_id;
    svn_revnum_t revision;
    pgvn_hook_event event;
} pgvn_hook_context;

typedef enum pgvn_freshness {
    PGVN_FRESHNESS_FRESH, PGVN_FRESHNESS_STALE, PGVN_FRESHNESS_UNKNOWN
} pgvn_freshness;
typedef struct pgvn_status {
    const char *node_id;
    const char *instance_id;
    apr_time_t sampled_at;
    apr_interval_time_t age;
    pgvn_freshness freshness;
    svn_boolean_t ready;
    const char *reason; /* stable reason code, never raw database error text */
} pgvn_status;
typedef struct pgvn_metrics {
    apr_int64_t sql_calls;
    apr_int64_t sql_errors;
    apr_int64_t sql_microseconds;
    int connections;
} pgvn_metrics;
typedef struct pgvn_binding {
    const char *repository_id;
    const char *svn_uuid;
    const char *canonical_name;
    const char *in_repository_path;
    long long configuration_version;
} pgvn_binding;

svn_error_t *pgvn_context_create(pgvn_context **context,
                               const pgvn_context_options *options, apr_pool_t *pool);
svn_error_t *pgvn_catalog_resolve(pgvn_binding **binding, pgvn_context *context,
                                const char *url_path, apr_pool_t *pool);
svn_error_t *pgvn_repos_open(pgvn_repository **repository, pgvn_context *context,
                           const char *repository_id, apr_pool_t *pool);
svn_repos_t *pgvn_repo_svn(pgvn_repository *repository);
svn_error_t *pgvn_transaction_receipt(svn_revnum_t *revision,pgvn_repository *repository,
                                    const char *transaction_id,const char *username,apr_pool_t *pool);
svn_error_t *pgvn_fs_bind(svn_fs_t **fs, pgvn_context *context,
                        const char *repository_id, apr_pool_t *pool);
svn_error_t *pgvn_repo_create(pgvn_binding **binding, pgvn_context *context,
                            const char *canonical_name, const char *route_prefix,
                            const char *svn_uuid, apr_pool_t *pool);
svn_error_t *pgvn_policy_acquire(pgvn_policy **policy, pgvn_context *context,
                               const char *repository_id, apr_int64_t generation,
                               apr_pool_t *pool);
svn_error_t *pgvn_policy_revalidate(pgvn_policy *policy, pgvn_context *context,
                                  apr_pool_t *pool);
svn_error_t *pgvn_policy_check(svn_boolean_t *allowed,const pgvn_policy *policy,
                             const char *username,const char *path,
                             svn_repos_authz_access_t required,apr_pool_t *pool);
svn_error_t *pgvn_policy_publish(pgvn_context *context,const char *repository_id,
                               apr_int64_t expected_generation,const char *authz,
                               const char *groups,const char *hooks_json,
                               const char *published_by,apr_pool_t *pool);
svn_error_t *pgvn_hook_dispatch(const pgvn_policy *policy,
                              const pgvn_hook_context *event, apr_pool_t *pool);
svn_error_t *pgvn_status_snapshot(pgvn_status **snapshot, pgvn_context *context,
                                apr_pool_t *pool);
void pgvn_metrics_snapshot(pgvn_metrics *snapshot,pgvn_context *context);

/* No credentials, repository names or local paths are exposed in build info. */
int pgvn_build_json(char *buffer, size_t size);

#endif
