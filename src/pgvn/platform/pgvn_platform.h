#ifndef PGVN_PLATFORM_H
#define PGVN_PLATFORM_H
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <stddef.h>
#include <stdint.h>
#include <signal.h>
#include <sys/types.h>
#include <sys/stat.h>

#define PGVN_PLATFORM_OK 0
#define PGVN_PLATFORM_INVALID 1
#define PGVN_PLATFORM_DENIED 2
#define PGVN_PLATFORM_IO 3
#define PGVN_PLATFORM_EXISTS 4
#define PGVN_PLATFORM_NOT_FOUND 5
#define PGVN_PLATFORM_TIMEOUT 6
#define PGVN_PLATFORM_CANCELLED 7
#define PGVN_PLATFORM_LIMIT 8
#define PGVN_PLATFORM_DESCENDANT 9
#define PGVN_PLATFORM_CRYPTO 10

#define PGVN_AES_NONCE 12
#define PGVN_AES_TAG 16
#define PGVN_AES_KEY 32
#define PGVN_HOOK_OUTPUT_MAX 8192
#define PGVN_HOOK_ARGUMENTS_MAX 16

uint64_t pgvn_monotonic_ms(void);
unsigned long pgvn_process_id(void);
void pgvn_secure_zero(void *data, size_t size);
int pgvn_random_bytes(void *data, size_t size);
int pgvn_sha256(unsigned char out[32], const void *data, size_t size);
int pgvn_utf8_valid(const void *data, size_t size);
int pgvn_identity_valid(const char *value, size_t max_bytes);
int pgvn_aes256gcm_encrypt(const unsigned char key[PGVN_AES_KEY], const unsigned char nonce[PGVN_AES_NONCE],
    const void *aad, size_t aad_len, const void *plain, size_t plain_len,
    unsigned char *cipher, unsigned char tag[PGVN_AES_TAG]);
int pgvn_aes256gcm_decrypt(const unsigned char key[PGVN_AES_KEY], const unsigned char nonce[PGVN_AES_NONCE],
    const void *aad, size_t aad_len, const unsigned char *cipher, size_t cipher_len,
    const unsigned char tag[PGVN_AES_TAG], unsigned char *plain);

typedef struct pgvn_loaded_key {
    char id[65];
    unsigned char secret[PGVN_AES_KEY];
} pgvn_loaded_key;

int pgvn_key_stat_ok(const struct stat *info, uid_t owner);
int pgvn_key_create(const char *path, const char *key_id);
int pgvn_key_load(pgvn_loaded_key *key, const char *path);

typedef struct pgvn_spawn_request {
    const char *root;
    const char *executable;
    const char *const *arguments;
    const char *stdin_data;
    size_t stdin_len;
    unsigned timeout_seconds;
    size_t output_limit;
    volatile sig_atomic_t *cancel;
} pgvn_spawn_request;

typedef struct pgvn_spawn_result {
    int code;
    int exit_code;
    int truncated_stdout;
    int truncated_stderr;
    char stdout_buf[PGVN_HOOK_OUTPUT_MAX + 1];
    size_t stdout_len;
    char stderr_buf[PGVN_HOOK_OUTPUT_MAX + 1];
    size_t stderr_len;
} pgvn_spawn_result;

int pgvn_spawn_capture(const pgvn_spawn_request *request, pgvn_spawn_result *result);
int pgvn_hook_files_ready(const char *root, const char *const *names, int count);

typedef struct pgvn_lru pgvn_lru;
pgvn_lru *pgvn_lru_create(size_t max_bytes, size_t max_entries, uint32_t format, uint64_t epoch);
void pgvn_lru_destroy(pgvn_lru *cache);
void pgvn_lru_set_epoch(pgvn_lru *cache, uint64_t epoch);
int pgvn_lru_put(pgvn_lru *cache, const void *key, size_t key_len, const void *value, size_t value_len, int immutable);
int pgvn_lru_get(pgvn_lru *cache, const void *key, size_t key_len, void *value, size_t *value_len);
void pgvn_lru_test_corrupt(pgvn_lru *cache, const void *key, size_t key_len);

typedef struct pgvn_token_store pgvn_token_store;
pgvn_token_store *pgvn_token_store_create(void);
void pgvn_token_store_destroy(pgvn_token_store *store);
int pgvn_token_store_add(pgvn_token_store *store, const char *hash_hex, unsigned remaining_seconds);
int pgvn_token_authorize(const pgvn_token_store *store, const char *header, size_t length);

typedef struct pgvn_http_input {
    int headers_too_large;
    int method_is_get;
    int has_body;
    int path_valid;
    const char *path;
    int query_present;
    const char *query;
    int origin_count;
    const char *origin;
    const char *expected_origin;
    int authorized;
} pgvn_http_input;

enum pgvn_http_route {
    PGVN_HTTP_NONE = 0,
    PGVN_HTTP_LIVE,
    PGVN_HTTP_READY,
    PGVN_HTTP_INDEX,
    PGVN_HTTP_STYLE,
    PGVN_HTTP_SCRIPT,
    PGVN_HTTP_API,
    PGVN_HTTP_METRICS
};

typedef struct pgvn_http_decision {
    int status;
    const char *error_code;
    int route;
} pgvn_http_decision;

void pgvn_http_decide(const pgvn_http_input *input, pgvn_http_decision *decision);
#endif
