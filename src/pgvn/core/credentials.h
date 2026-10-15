#ifndef PGVN_CREDENTIALS_H
#define PGVN_CREDENTIALS_H
#include "internal.h"

typedef struct pgvn_credential_key {
    char id[65];
    unsigned char secret[32];
    struct pgvn_credential_key *previous;
} pgvn_credential_key;

svn_error_t *pgvn_key_create(const char *path,const char *key_id,apr_pool_t *pool);
svn_error_t *pgvn_key_load(pgvn_credential_key **key,const char *path,apr_pool_t *pool);
svn_error_t *pgvn_keyring_load(pgvn_credential_key **keys,const char *current,const char *previous,apr_pool_t *pool);
svn_error_t *pgvn_credential_rekey(pgvn_context *context,const pgvn_credential_key *old_key,
    const pgvn_credential_key *new_key,const char *domain,const char *user,apr_pool_t *pool);
svn_error_t *pgvn_credential_put(pgvn_context *context,const pgvn_credential_key *key,
    const char *domain,const char *user,const char *password,apr_pool_t *pool);
svn_error_t *pgvn_credential_get(const char **password,apr_int64_t *generation,
    pgvn_context *context,const pgvn_credential_key *key,const char *domain,
    const char *user,apr_pool_t *pool);
svn_error_t *pgvn_credential_check(pgvn_context *context,const char *domain,
    const char *user,apr_int64_t generation,apr_pool_t *pool);
svn_error_t *pgvn_credential_guard(pgvn_context *context,PGconn *connection,
    const char *domain,const char *user,apr_int64_t generation,apr_pool_t *pool);
svn_error_t *pgvn_credential_identity(const char *domain,const char *user);
#endif
