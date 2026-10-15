/* PgFS: PostgreSQL native filesystem provider for Subversion 1.14.5. */
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <apr_hash.h>
#include <apr_strings.h>
#include <apr_thread_mutex.h>
#include <apr_uuid.h>
#include <libpq-fe.h>
#include "svn_config.h"
#include "svn_checksum.h"
#include "svn_delta.h"
#include "svn_dirent_uri.h"
#include "svn_error.h"
#include "svn_io.h"
#include "svn_pools.h"
#include "svn_props.h"
#include "svn_time.h"
#include "svn_version.h"
#include "fs-loader.h"
#include "pgfs_native.h"

typedef struct pgfs_data_t {
    PGconn *connection;
    apr_thread_mutex_t *mutex;
    const char *repository_id;
    const char *connection_string;
    apr_hash_t *active_writers;
    svn_boolean_t directory_adapter;
    svn_boolean_t exclusive_lease;
    pgfs_observer observer;
} pgfs_data_t;

typedef struct pgfs_root_data_t {
    apr_int64_t node_revision_id;
} pgfs_root_data_t;

static svn_error_t *pgfs_unsupported(const char *operation)
{
    return svn_error_createf(SVN_ERR_UNSUPPORTED_FEATURE, NULL,
                            "PgFS operation not yet implemented: %s", operation);
}

#include "pgfs_stubs.h"

static apr_status_t pgfs_cleanup(void *baton)
{
    pgfs_data_t *data = baton;
    if (data->connection) {
        PQfinish(data->connection); data->connection = NULL;
        if (data->observer.closed) data->observer.closed(data->observer.baton);
    }
    return APR_SUCCESS;
}

static svn_error_t *pgfs_query_full(PGresult **result, svn_fs_t *fs, const char *sql,
                                   int count, const char *const *values,
                                   const int *lengths, const int *formats)
{
    pgfs_data_t *data = fs->fsap_data;
    apr_status_t status = apr_thread_mutex_lock(data->mutex);
    if (status) return svn_error_wrap_apr(status, "PgFS connection lock failed");
    /* Give even intentionally unused parameters a type. Text input is cast
       explicitly in SQL; binary inputs are bytea (PostgreSQL OIDs 25 / 17). */
    Oid *types = count ? malloc((size_t)count * sizeof(*types)) : NULL;
    if (count && !types) {
        apr_thread_mutex_unlock(data->mutex);
        return svn_error_create(SVN_ERR_FS_GENERAL, NULL, "PgFS parameter allocation failed");
    }
    for (int i = 0; i < count; ++i) types[i] = formats && formats[i] ? 17 : 25;
    apr_time_t started = data->observer.query ? apr_time_now() : 0;
    if(data->observer.query_started) data->observer.query_started(data->observer.baton);
    *result = PQexecParams(data->connection, sql, count, types, values, lengths, formats, 0);
    free(types);
    svn_error_t *error = NULL;
    char sqlstate[6] = "00000";
    if (!*result || (PQresultStatus(*result) != PGRES_TUPLES_OK &&
                    PQresultStatus(*result) != PGRES_COMMAND_OK)) {
        const char *state = *result ? PQresultErrorField(*result, PG_DIAG_SQLSTATE) : NULL;
        memcpy(sqlstate, state && strlen(state) == 5 ? state : "08006", 5);
        error = svn_error_createf(SVN_ERR_FS_GENERAL, NULL,
                                  "PgFS database operation failed (SQLSTATE %s)", sqlstate);
        if (*result) PQclear(*result);
        *result = NULL;
    }
    status = apr_thread_mutex_unlock(data->mutex);
    if (data->observer.query)
        data->observer.query(data->observer.baton, sqlstate, apr_time_now() - started);
    if (status && !error) error = svn_error_wrap_apr(status, "PgFS connection unlock failed");
    return error;
}

static svn_error_t *pgfs_query(PGresult **result, svn_fs_t *fs, const char *sql,
                              int count, const char *const *values)
{
    return pgfs_query_full(result, fs, sql, count, values, NULL, NULL);
}

static svn_error_t *pgfs_attach(svn_fs_t *fs, const char *path, const char *dsn,
                               const char *repository_id, PGconn *connection,
                               const pgfs_observer *observer)
{
    pgfs_data_t *data = apr_pcalloc(fs->pool, sizeof(*data));
    fs->path = apr_pstrdup(fs->pool, path);
    fs->fsap_data = data;
    fs->vtable = &pgfs_fs_vtable;
    data->repository_id = apr_pstrdup(fs->pool, repository_id);
    data->connection_string = apr_pstrdup(fs->pool, dsn);
    data->directory_adapter = TRUE;
    if (observer) data->observer = *observer;
    data->active_writers = apr_hash_make(fs->pool);
    data->connection = connection;
    apr_pool_cleanup_register(fs->pool, data, pgfs_cleanup, apr_pool_cleanup_null);
    apr_status_t status = apr_thread_mutex_create(&data->mutex,
                                                 APR_THREAD_MUTEX_NESTED, fs->pool);
    if (status) return svn_error_wrap_apr(status, "PgFS mutex creation failed");
    if (!data->connection || PQstatus(data->connection) != CONNECTION_OK)
        return svn_error_create(SVN_ERR_FS_GENERAL, NULL, "PgFS database connection failed");
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_connect(svn_fs_t *fs, const char *path, const char *dsn,
                                const char *repository_id)
{
    return pgfs_attach(fs, path, dsn, repository_id, PQconnectdb(dsn), NULL);
}

static const char *pgfs_uuid(apr_pool_t *pool)
{
    apr_uuid_t uuid;
    char buffer[APR_UUID_FORMATTED_LENGTH + 1];
    apr_uuid_get(&uuid);
    apr_uuid_format(buffer, &uuid);
    return apr_pstrdup(pool, buffer);
}

static svn_error_t *pgfs_create(svn_fs_t *fs, const char *path,
                               svn_mutex__t *common_pool_lock,
                               apr_pool_t *scratch_pool, apr_pool_t *common_pool)
{
    const char *dsn = getenv("PGFS_DSN");
    if (!dsn || !*dsn || strchr(dsn, '\n') || strchr(dsn, '\r'))
        return svn_error_create(SVN_ERR_FS_GENERAL, NULL,
                                "Set PGFS_DSN to a single-line libpq connection string before creation");
    const char *repository_id = pgfs_uuid(fs->pool);
    const char *instance_id = pgfs_uuid(fs->pool);
    SVN_ERR(pgfs_connect(fs, path, dsn, repository_id));
    fs->uuid = repository_id;
    SVN_ERR(pgfs_native_create_records(((pgfs_data_t *)fs->fsap_data)->connection,
        repository_id, repository_id, instance_id, svn_time_to_cstring(apr_time_now(), scratch_pool)));
    const char *config = apr_psprintf(scratch_pool,
        "[pgfs]\nrepository = %s\nconnection = %s\n", repository_id, dsn);
    SVN_ERR(svn_io_file_create(svn_dirent_join(path, "pgfs.conf", scratch_pool),
                               config, scratch_pool));
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_load_metadata(svn_fs_t *fs, const char *repository_id)
{
    const char *values[] = {repository_id};
    PGresult *result;
    SVN_ERR(pgfs_query(&result, fs,
        "SELECT svn_uuid::text,format_version FROM pgfs.repositories WHERE repository_id=$1::uuid",
        1, values));
    if (PQntuples(result) != 1) {
        PQclear(result);
        return svn_error_create(SVN_ERR_FS_CORRUPT, NULL, "PgFS repository record does not exist");
    }
    fs->uuid = apr_pstrdup(fs->pool, PQgetvalue(result, 0, 0));
    int format = atoi(PQgetvalue(result, 0, 1));
    PQclear(result);
    if (format != 1) return svn_error_create(SVN_ERR_FS_UNSUPPORTED_FORMAT, NULL,
                                            "Unsupported PgFS storage format");
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_open(svn_fs_t *fs, const char *path,
                             svn_mutex__t *common_pool_lock,
                             apr_pool_t *scratch_pool, apr_pool_t *common_pool)
{
    svn_config_t *config;
    const char *dsn, *repository_id;
    SVN_ERR(svn_config_read3(&config, svn_dirent_join(path, "pgfs.conf", scratch_pool),
                             TRUE, TRUE, TRUE, scratch_pool));
    svn_config_get(config, &dsn, "pgfs", "connection", NULL);
    svn_config_get(config, &repository_id, "pgfs", "repository", NULL);
    if (!dsn || !repository_id)
        return svn_error_create(SVN_ERR_FS_CORRUPT, NULL, "PgFS repository configuration is incomplete");
    SVN_ERR(pgfs_connect(fs, path, dsn, repository_id));
    return pgfs_load_metadata(fs, repository_id);
}

static svn_error_t *pgfs_youngest(svn_revnum_t *rev, svn_fs_t *fs, apr_pool_t *pool)
{
    pgfs_data_t *data = fs->fsap_data;
    const char *values[] = {data->repository_id};
    PGresult *result;
    SVN_ERR(pgfs_query(&result, fs,
        "SELECT head_rev FROM pgfs.repositories WHERE repository_id=$1::uuid", 1, values));
    if (PQntuples(result) != 1) {
        PQclear(result);
        return svn_error_create(SVN_ERR_FS_CORRUPT, NULL, "PgFS repository record disappeared");
    }
    apr_int64_t value = apr_atoi64(PQgetvalue(result, 0, 0));
    PQclear(result);
    if (value < 0 || value > LONG_MAX)
        return svn_error_create(SVN_ERR_FS_CORRUPT, NULL, "PgFS HEAD exceeds the native revision range");
    *rev = (svn_revnum_t)value;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_set_uuid(svn_fs_t *fs, const char *uuid, apr_pool_t *pool)
{
    pgfs_data_t *data = fs->fsap_data;
    if (!uuid) uuid = pgfs_uuid(pool);
    const char *values[] = {data->repository_id, uuid};
    PGresult *result;
    SVN_ERR(pgfs_query(&result, fs,
        "UPDATE pgfs.repositories SET svn_uuid=$2::uuid WHERE repository_id=$1::uuid", 2, values));
    PQclear(result);
    fs->uuid = apr_pstrdup(fs->pool, uuid);
    return SVN_NO_ERROR;
}

static svn_string_t *pgfs_decode_hex(const char *hex, apr_pool_t *pool)
{
    size_t length = strlen(hex) / 2;
    char *bytes = apr_palloc(pool, length + 1);
    for (size_t i = 0; i < length; ++i) {
        char pair[] = {hex[2*i], hex[2*i+1], 0};
        bytes[i] = (char)strtoul(pair, NULL, 16);
    }
    bytes[length] = 0;
    return svn_string_ncreate(bytes, length, pool);
}

static svn_error_t *pgfs_revprops(apr_hash_t **props, svn_fs_t *fs, svn_revnum_t rev,
                                 svn_boolean_t refresh, apr_pool_t *result_pool,
                                 apr_pool_t *scratch_pool)
{
    pgfs_data_t *data = fs->fsap_data;
    const char *values[] = {data->repository_id, apr_psprintf(scratch_pool, "%ld", rev)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result, fs,
        "SELECT p.name,encode(p.value,'hex') FROM pgfs.revisions r "
        "LEFT JOIN pgfs.revision_properties p USING(repository_id,rev) "
        "WHERE r.repository_id=$1::uuid AND r.rev=$2::bigint", 2, values));
    if (!PQntuples(result)) {
        PQclear(result);
        return svn_error_createf(SVN_ERR_FS_NO_SUCH_REVISION, NULL, "No such revision %ld", rev);
    }
    *props = apr_hash_make(result_pool);
    for (int i = 0; i < PQntuples(result); ++i) {
        if (!PQgetisnull(result, i, 0))
            apr_hash_set(*props, apr_pstrdup(result_pool, PQgetvalue(result, i, 0)),
                         APR_HASH_KEY_STRING, pgfs_decode_hex(PQgetvalue(result, i, 1), result_pool));
    }
    PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_revprop(svn_string_t **value, svn_fs_t *fs, svn_revnum_t rev,
                                const char *name, svn_boolean_t refresh,
                                apr_pool_t *result_pool, apr_pool_t *scratch_pool)
{
    apr_hash_t *props;
    SVN_ERR(pgfs_revprops(&props, fs, rev, refresh, result_pool, scratch_pool));
    *value = apr_hash_get(props, name, APR_HASH_KEY_STRING);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_refresh_revprops(svn_fs_t *fs, apr_pool_t *pool)
{
    /* Revision properties are read from the primary on each request. */
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_revision_root(svn_fs_root_t **root, svn_fs_t *fs,
                                      svn_revnum_t rev, apr_pool_t *pool)
{
    pgfs_data_t *data = fs->fsap_data;
    const char *values[] = {data->repository_id, apr_psprintf(pool, "%ld", rev)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result, fs,
        "SELECT root_node_revision_id FROM pgfs.revisions "
        "WHERE repository_id=$1::uuid AND rev=$2::bigint", 2, values));
    if (PQntuples(result) != 1) {
        PQclear(result);
        return svn_error_createf(SVN_ERR_FS_NO_SUCH_REVISION, NULL, "No such revision %ld", rev);
    }
    apr_pool_t *root_pool = svn_pool_create(pool);
    *root = apr_pcalloc(root_pool, sizeof(**root));
    (*root)->pool = root_pool;
    (*root)->fs = fs;
    (*root)->rev = rev;
    (*root)->vtable = &pgfs_root_vtable;
    pgfs_root_data_t *root_data = apr_pcalloc(root_pool, sizeof(*root_data));
    root_data->node_revision_id = apr_atoi64(PQgetvalue(result, 0, 0));
    (*root)->fsap_data = root_data;
    PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_check_locks(svn_fs_root_t *,const char *,svn_boolean_t,apr_pool_t *);
static svn_error_t *pgfs_check_commit_locks(svn_fs_txn_t *,apr_pool_t *);
#include "pgfs_tree.inc"
#include "pgfs_streams.inc"
#include "pgfs_receipts.inc"
#include "pgfs_transactions.inc"
#include "pgfs_history.inc"
#include "pgfs_locks.inc"
#include "pgfs_mergeinfo.inc"
#include "pgfs_admin.inc"
#include "pgfs_hotcopy.inc"

static svn_error_t *pgfs_format(int *format, svn_version_t **version, svn_fs_t *fs,
                               apr_pool_t *result_pool, apr_pool_t *scratch_pool)
{
    /* The info API reports a minimum minor release; its patch must be zero. */
    static const svn_version_t supported = {1, 14, 0, ""};
    *format = 1;
    *version = apr_pmemdup(result_pool, &supported, sizeof(supported));
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_config_files(apr_array_header_t **files, svn_fs_t *fs,
                                     apr_pool_t *result_pool, apr_pool_t *scratch_pool)
{
    *files = apr_array_make(result_pool, 1, sizeof(const char *));
    if (((pgfs_data_t *)fs->fsap_data)->directory_adapter)
        APR_ARRAY_PUSH(*files, const char *) = svn_dirent_join(fs->path, "pgfs.conf", result_pool);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_info(const void **info, svn_fs_t *fs,
                             apr_pool_t *result_pool, apr_pool_t *scratch_pool)
{
    svn_fs_info_placeholder_t *value = apr_pcalloc(result_pool, sizeof(*value));
    value->fs_type = "pg";
    *info = value;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_set_open(svn_fs_t *fs,
    svn_error_t *(*open_function)(svn_fs_t **, const char *, apr_hash_t *, apr_pool_t *, apr_pool_t *))
{
    /* PgFS does not need to reopen another backend for its current operations. */
    return SVN_NO_ERROR;
}

static const svn_version_t *pgfs_version(void)
{
    static const svn_version_t version = {1, 14, 5, ""};
    return &version;
}

static const char *pgfs_description(void)
{
    return "PostgreSQL filesystem provider (PgFS), storage format 1.";
}

static INIT_ONCE pgfs_vtables_once = INIT_ONCE_STATIC_INIT;

static BOOL CALLBACK pgfs_initialize_vtables(PINIT_ONCE once, PVOID parameter, PVOID *context)
{
    pgfs_initialize_fs_library_stubs();
    pgfs_initialize_fs_stubs();
    pgfs_initialize_txn_stubs();
    pgfs_initialize_root_stubs();
    pgfs_initialize_history_stubs();
    pgfs_fs_library_vtable.get_version = pgfs_version;
    pgfs_fs_library_vtable.get_description = pgfs_description;
    pgfs_fs_library_vtable.create = pgfs_create;
    pgfs_fs_library_vtable.open_fs = pgfs_open;
    pgfs_fs_library_vtable.open_fs_for_recovery = pgfs_open;
    pgfs_fs_library_vtable.set_svn_fs_open = pgfs_set_open;
    pgfs_fs_vtable.youngest_rev = pgfs_youngest;
    pgfs_fs_vtable.refresh_revprops = pgfs_refresh_revprops;
    pgfs_fs_vtable.revision_prop = pgfs_revprop;
    pgfs_fs_vtable.revision_proplist = pgfs_revprops;
    pgfs_fs_vtable.set_uuid = pgfs_set_uuid;
    pgfs_fs_vtable.change_rev_prop = pgfs_change_rev_prop;
    pgfs_fs_vtable.begin_txn = pgfs_begin_txn;
    pgfs_fs_vtable.open_txn = pgfs_open_txn;
    pgfs_fs_vtable.purge_txn = pgfs_purge_txn;
    pgfs_fs_vtable.list_transactions = pgfs_list_txns;
    pgfs_fs_vtable.deltify = pgfs_deltify;
    pgfs_fs_vtable.revision_root = pgfs_revision_root;
    pgfs_fs_vtable.info_format = pgfs_format;
    pgfs_fs_vtable.info_config_files = pgfs_config_files;
    pgfs_fs_vtable.info_fsap = pgfs_info;
    pgfs_root_vtable.check_path = pgfs_check_path;
    pgfs_root_vtable.node_prop = pgfs_node_prop;
    pgfs_root_vtable.node_proplist = pgfs_node_props;
    pgfs_root_vtable.node_has_props = pgfs_node_has_props;
    pgfs_root_vtable.node_created_rev = pgfs_created_rev;
    pgfs_root_vtable.node_origin_rev = pgfs_origin_rev;
    pgfs_root_vtable.node_created_path = pgfs_created_path;
    pgfs_root_vtable.dir_entries = pgfs_dir_entries;
    pgfs_root_vtable.paths_changed = pgfs_paths_changed;
    pgfs_root_vtable.report_changes = NULL; /* Loader emulates the iterator. */
    pgfs_root_vtable.node_id = pgfs_node_id;
    pgfs_root_vtable.node_relation = pgfs_node_relation;
    pgfs_root_vtable.dir_optimal_order = pgfs_dir_order;
    pgfs_root_vtable.make_dir = pgfs_make_dir;
    pgfs_root_vtable.make_file = pgfs_make_file;
    pgfs_root_vtable.delete_node = pgfs_delete_node;
    pgfs_root_vtable.change_node_prop = pgfs_change_node_prop;
    pgfs_root_vtable.props_changed = pgfs_props_changed;
    pgfs_root_vtable.file_contents = pgfs_file_contents;
    pgfs_root_vtable.file_length = pgfs_file_length;
    pgfs_root_vtable.file_checksum = pgfs_file_checksum;
    pgfs_root_vtable.contents_changed = pgfs_contents_changed;
    pgfs_root_vtable.apply_text = pgfs_apply_text;
    pgfs_root_vtable.apply_textdelta = pgfs_apply_textdelta;
    pgfs_root_vtable.get_file_delta_stream = pgfs_delta_stream;
    pgfs_root_vtable.try_process_file_contents = pgfs_try_contents;
    pgfs_txn_vtable.commit = pgfs_commit_txn;
    pgfs_txn_vtable.abort = pgfs_abort_txn;
    pgfs_txn_vtable.root = pgfs_txn_root;
    pgfs_txn_vtable.get_prop = pgfs_txn_prop;
    pgfs_txn_vtable.get_proplist = pgfs_txn_props;
    pgfs_txn_vtable.change_prop = pgfs_change_txn_prop;
    pgfs_txn_vtable.change_props = pgfs_change_txn_props;
    pgfs_root_vtable.copy = pgfs_copy;
    pgfs_root_vtable.revision_link = pgfs_revision_link;
    pgfs_root_vtable.copied_from = pgfs_copied_from;
    pgfs_root_vtable.closest_copy = pgfs_closest_copy;
    pgfs_root_vtable.node_history = pgfs_node_history;
    pgfs_history_vtable.prev = pgfs_history_prev;
    pgfs_history_vtable.location = pgfs_history_location;
    pgfs_fs_vtable.lock = pgfs_lock_many;
    pgfs_fs_vtable.unlock = pgfs_unlock_many;
    pgfs_fs_vtable.get_lock = pgfs_get_lock;
    pgfs_fs_vtable.get_locks = pgfs_get_locks;
    pgfs_fs_vtable.generate_lock_token = pgfs_generate_token;
    pgfs_root_vtable.get_mergeinfo = pgfs_get_mergeinfo;
    pgfs_root_vtable.merge = pgfs_merge;
    pgfs_fs_vtable.verify_root = pgfs_verify_root;
    pgfs_fs_vtable.freeze = pgfs_freeze;
    pgfs_fs_vtable.bdb_set_errcall = pgfs_bdb_errcall;
    pgfs_fs_vtable.ioctl = pgfs_ioctl;
    pgfs_fs_library_vtable.verify_fs = pgfs_verify_fs;
    pgfs_fs_library_vtable.recover = pgfs_recover;
    pgfs_fs_library_vtable.upgrade_fs = pgfs_upgrade;
    pgfs_fs_library_vtable.pack_fs = pgfs_pack;
    pgfs_fs_library_vtable.bdb_logfiles = pgfs_bdb_logs;
    pgfs_fs_library_vtable.ioctl = pgfs_library_ioctl;
    pgfs_fs_library_vtable.hotcopy = pgfs_hotcopy;
    pgfs_fs_library_vtable.delete_fs = pgfs_delete_fs;
    return TRUE;
}

#ifndef PGVN_STATIC
__declspec(dllexport)
#endif
svn_error_t *svn_fs_pg__init(const svn_version_t *loader_version,
                            fs_library_vtable_t **vtable, apr_pool_t *common_pool)
{
    if (loader_version->major != 1 || loader_version->minor != 14 || loader_version->patch != 5)
        return svn_error_create(SVN_ERR_VERSION_MISMATCH, NULL,
                                "PgFS was built for Subversion 1.14.5");
    if (!InitOnceExecuteOnce(&pgfs_vtables_once, pgfs_initialize_vtables, NULL, NULL))
        return svn_error_wrap_apr(GetLastError(), "PgFS vtable initialization failed");
    *vtable = &pgfs_fs_library_vtable;
    return SVN_NO_ERROR;
}

#include "pgfs_native.inc"
