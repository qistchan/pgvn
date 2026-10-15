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
    svn_error_t *(*before_mutation)(void *baton, PGconn *connection,
                                   const char *path, apr_pool_t *pool);
    void (*published)(void *baton,svn_boolean_t receipt);
    void (*query_started)(void *baton);
    void (*writer_changed)(void *baton,int change);
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
typedef svn_error_t *(*pgfs_native_atomic_callback)(PGconn *connection,void *baton,apr_pool_t *pool);
svn_error_t *pgfs_native_atomic(svn_fs_t *fs,pgfs_native_atomic_callback callback,void *baton,apr_pool_t *pool);
/* Create the FS transaction, initial properties and caller's persistent binding
 * in one SQL transaction. The repository layer validates properties and runs
 * hooks after this returns, when an external hook can read the staging data.
 * CALLBACK may issue bound SQL on CONNECTION, but must not invoke FS mutation
 * APIs or start/finish a SQL transaction. *TXN is unchanged on any error. */
typedef svn_error_t *(*pgfs_native_txn_binding_callback)(PGconn *connection,
    svn_fs_txn_t *txn,void *baton,apr_pool_t *pool);
svn_error_t *pgfs_native_begin_bound_txn(svn_fs_txn_t **txn,svn_fs_t *fs,
    svn_revnum_t base,apr_uint32_t flags,const apr_array_header_t *properties,
    pgfs_native_txn_binding_callback callback,void *baton,apr_pool_t *pool);
/* Abort using normal FS locking, then let CALLBACK validate/update its binding
 * in the same unit. A callback error rolls the abort back as well. */
svn_error_t *pgfs_native_abort_bound_txn(svn_fs_t *fs,const char *transaction_id,
    pgfs_native_atomic_callback callback,void *baton,apr_pool_t *pool);
svn_error_t *pgfs_native_delete_rows(svn_fs_t *fs,apr_pool_t *pool);
svn_error_t *pgfs_native_exclusive(svn_fs_t *fs,apr_pool_t *pool);
svn_error_t *pgfs_native_hotcopy_rows(svn_fs_t *source,const char *destination_id,
                                    svn_boolean_t fresh,apr_pool_t *pool);
svn_error_t *pgfs_native_verify(svn_fs_t *fs,svn_revnum_t start,svn_revnum_t end,
    svn_fs_progress_notify_func_t notify,void *notify_baton,
    svn_cancel_func_t cancel,void *cancel_baton,apr_pool_t *pool);
#endif
