#ifndef PGVN_DAV_STATE_H
#define PGVN_DAV_STATE_H
#include "pgvn.h"
/* Private protocol adapters. HTTP identity comes from the Apache authn layer. */
typedef enum pgvn_dav_alias_kind {
    PGVN_DAV_NATIVE, PGVN_DAV_ACTIVITY, PGVN_DAV_VIRTUAL
} pgvn_dav_alias_kind;
typedef enum pgvn_dav_phase {
    PGVN_DAV_CREATING, PGVN_DAV_ACTIVE, PGVN_DAV_COMMITTED,
    PGVN_DAV_ABORTED, PGVN_DAV_DELETED
} pgvn_dav_phase;
typedef struct pgvn_dav_txn_info {
    const char *transaction_id;
    pgvn_dav_phase phase;
    svn_revnum_t committed_revision;
    svn_boolean_t creation_failed;
    svn_boolean_t creation_pending;
} pgvn_dav_txn_info;
svn_error_t *pgvn_dav_create(const char **transaction_id,pgvn_repository *repository,
    pgvn_dav_alias_kind kind,const char *alias,const char *username,
    svn_revnum_t base,apr_hash_t *revision_properties,apr_pool_t *pool);
svn_error_t *pgvn_dav_lookup(pgvn_dav_txn_info **info,pgvn_repository *repository,
    pgvn_dav_alias_kind kind,const char *name,const char *username,apr_pool_t *pool);
svn_error_t *pgvn_dav_cancel(pgvn_repository *repository,pgvn_dav_alias_kind kind,
    const char *name,const char *username,apr_pool_t *pool);
/* Recover only a proven-unowned creating intent; never age-delete active work. */
svn_error_t *pgvn_dav_recover_creation(pgvn_repository *repository,
    const char *transaction_id,const char *username,apr_pool_t *pool);
#endif
