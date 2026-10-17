#include "pgvn_platform.h"
#include <stdlib.h>
#include <string.h>

struct pgvn_token_store {
    unsigned char hash[64][32];
    uint64_t expires[64];
    int count;
};

pgvn_token_store *pgvn_token_store_create(void)
{
    return calloc(1, sizeof(pgvn_token_store));
}

void pgvn_token_store_destroy(pgvn_token_store *store)
{
    if (!store) return;
    pgvn_secure_zero(store, sizeof(*store));
    free(store);
}

static int nibble(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

static int hash_decode(unsigned char out[32], const char *text)
{
    int i;
    if (!text || strlen(text) != 64) return 0;
    for (i = 0; i < 32; ++i) {
        int high = nibble(text[2 * i]), low = nibble(text[2 * i + 1]);
        if (high < 0 || low < 0) return 0;
        out[i] = (unsigned char)((high << 4) | low);
    }
    return 1;
}

int pgvn_token_store_add(pgvn_token_store *store, const char *hash_hex, unsigned remaining_seconds)
{
    unsigned char hash[32];
    if (!store || store->count >= 64 || !hash_decode(hash, hash_hex)) return PGVN_PLATFORM_INVALID;
    if (remaining_seconds > 366u * 24u * 60u * 60u) return PGVN_PLATFORM_LIMIT;
    memcpy(store->hash[store->count], hash, 32);
    store->expires[store->count] = pgvn_monotonic_ms() + (uint64_t)remaining_seconds * 1000u;
    store->count++;
    pgvn_secure_zero(hash, sizeof(hash));
    return PGVN_PLATFORM_OK;
}

int pgvn_token_authorize(const pgvn_token_store *store, const char *header, size_t length)
{
    unsigned char digest[32];
    unsigned different_all = 1;
    int i, n;
    uint64_t now;
    if (!store || !header || length != 76 || memcmp(header, "Bearer pgvn_", 12)) return 0;
    for (i = 12; i < 76; ++i) if (nibble(header[i]) < 0) return 0;
    if (pgvn_sha256(digest, header + 7, 69)) return 0;
    now = pgvn_monotonic_ms();
    for (i = 0; i < store->count; ++i) {
        unsigned different = 0;
        for (n = 0; n < 32; ++n) different |= (unsigned)(digest[n] ^ store->hash[i][n]);
        if (!different && now < store->expires[i]) different_all = 0;
    }
    pgvn_secure_zero(digest, sizeof(digest));
    return different_all == 0;
}
