/* Private in-process entry points; no repository directory is accepted. */
#ifndef PGFS_NATIVE_H
#define PGFS_NATIVE_H
#include <libpq-fe.h>
#include "svn_fs.h"
#include "svn_error.h"

typedef struct pgfs_observer {
    void (*query)(void *baton, const char *sqlstate, apr_interval_time_t elapsed);
    void (*closed)(void *baton);
    svn_error_t *(*before_publish)(void *baton, PGconn *connection,
                                  const char *transaction_id, apr_pool_t *pool);
    void *baton;
} pgfs_observer;

svn_error_t *pgfs_native_bind(svn_fs_t *fs, PGconn *connection,
                            const char *connection_string, const char *repository_id,
                            const pgfs_observer *observer, apr_pool_t *pool);
svn_error_t *pgfs_native_create_records(PGconn *connection, const char *repository_id,
                                      const char *svn_uuid, const char *instance_id,
                                      const char *date);
svn_error_t *pgfs_native_pack(svn_fs_t *fs, apr_pool_t *pool);
svn_error_t *pgfs_native_recover(svn_fs_t *fs, apr_pool_t *pool);
svn_error_t *pgfs_native_delete(svn_fs_t *fs, apr_pool_t *pool);
svn_error_t *pgfs_native_hotcopy(svn_fs_t *source, svn_fs_t *target,
                               svn_boolean_t fresh, apr_pool_t *pool);
#endif
