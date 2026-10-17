#ifndef PGVN_PLATFORM_CREDENTIALS_H
#define PGVN_PLATFORM_CREDENTIALS_H

#include <stddef.h>
#include <apr_pools.h>
#include "svn_error.h"

/* Internal platform boundary. Wire format, AAD construction, identity checks,
 * key parsing, SQL and generation semantics remain in core/credentials.c. */
void pgvn_secure_zero(void *data,size_t size);
int pgvn_sha256(unsigned char digest[32],const unsigned char *value,size_t length);
int pgvn_secure_random(unsigned char *data,size_t size);
int pgvn_utf8_valid(const char *text);

/* AES-256-GCM, 12-byte nonce, 16-byte tag; failure clears all output bytes.
 * TAG is produced on encryption and authenticated on decryption. */
int pgvn_credential_cipher(int encrypt,const unsigned char key[32],
    const unsigned char *aad,size_t aad_size,const unsigned char nonce[12],
    unsigned char tag[16],const unsigned char *input,size_t size,
    unsigned char *output,apr_pool_t *pool);

svn_error_t *pgvn_keyfile_write(const char *path,const char *data,size_t size,apr_pool_t *pool);
svn_error_t *pgvn_keyfile_read(const char *path,char *data,size_t capacity,size_t *size,apr_pool_t *pool);

static inline svn_error_t *pgvn_keyfile_error(void)
{
    return svn_error_create(SVN_ERR_AUTHN_FAILED,NULL,
        "PgVN credential key is unavailable, invalid or insufficiently protected");
}

#endif
