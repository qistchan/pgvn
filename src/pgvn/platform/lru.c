#include "pgvn_platform.h"
#include <stdlib.h>
#include <string.h>

typedef struct pgvn_lru_entry {
    struct pgvn_lru_entry *newer, *older;
    size_t key_len, value_len;
    unsigned char sum[32];
    unsigned char data[];
} pgvn_lru_entry;

struct pgvn_lru {
    pgvn_lru_entry *newest, *oldest;
    size_t bytes, max_bytes, count, max_entries;
    uint32_t format;
    uint64_t epoch;
};

static void unlink_entry(pgvn_lru *cache, pgvn_lru_entry *entry)
{
    if (entry->newer) entry->newer->older = entry->older;
    else cache->newest = entry->older;
    if (entry->older) entry->older->newer = entry->newer;
    else cache->oldest = entry->newer;
    cache->bytes -= entry->value_len;
    cache->count--;
}

static void free_entry(pgvn_lru *cache, pgvn_lru_entry *entry)
{
    unlink_entry(cache, entry);
    pgvn_secure_zero(entry->data, entry->key_len + entry->value_len);
    free(entry);
}

pgvn_lru *pgvn_lru_create(size_t max_bytes, size_t max_entries, uint32_t format, uint64_t epoch)
{
    pgvn_lru *cache;
    if (!max_bytes || !max_entries || max_entries > 4096 || !format) return NULL;
    cache = calloc(1, sizeof(*cache));
    if (!cache) return NULL;
    cache->max_bytes = max_bytes;
    cache->max_entries = max_entries;
    cache->format = format;
    cache->epoch = epoch;
    return cache;
}

void pgvn_lru_destroy(pgvn_lru *cache)
{
    if (!cache) return;
    while (cache->newest) free_entry(cache, cache->newest);
    pgvn_secure_zero(cache, sizeof(*cache));
    free(cache);
}

void pgvn_lru_set_epoch(pgvn_lru *cache, uint64_t epoch)
{
    if (!cache || cache->epoch == epoch) return;
    while (cache->newest) free_entry(cache, cache->newest);
    cache->epoch = epoch;
}

static pgvn_lru_entry *find_entry(pgvn_lru *cache, const void *key, size_t key_len)
{
    pgvn_lru_entry *entry;
    for (entry = cache->newest; entry; entry = entry->older)
        if (entry->key_len == key_len && !memcmp(entry->data, key, key_len)) return entry;
    return NULL;
}

int pgvn_lru_put(pgvn_lru *cache, const void *key, size_t key_len, const void *value, size_t value_len, int immutable)
{
    pgvn_lru_entry *entry;
    unsigned char sum[32];
    if (!cache || !key || !key_len || key_len > 1024 || (!value && value_len)) return PGVN_PLATFORM_INVALID;
    if (!immutable) return PGVN_PLATFORM_DENIED;
    if (value_len > cache->max_bytes) return PGVN_PLATFORM_LIMIT;
    if (pgvn_sha256(sum, value, value_len)) return PGVN_PLATFORM_CRYPTO;
    entry = find_entry(cache, key, key_len);
    if (entry) free_entry(cache, entry);
    while (cache->count && (cache->count >= cache->max_entries || cache->bytes + value_len > cache->max_bytes))
        free_entry(cache, cache->oldest);
    entry = calloc(1, sizeof(*entry) + key_len + value_len);
    if (!entry) return PGVN_PLATFORM_IO;
    entry->key_len = key_len;
    entry->value_len = value_len;
    memcpy(entry->sum, sum, sizeof(sum));
    memcpy(entry->data, key, key_len);
    if (value_len) memcpy(entry->data + key_len, value, value_len);
    entry->older = cache->newest;
    if (cache->newest) cache->newest->newer = entry;
    cache->newest = entry;
    if (!cache->oldest) cache->oldest = entry;
    cache->bytes += value_len;
    cache->count++;
    return PGVN_PLATFORM_OK;
}

int pgvn_lru_get(pgvn_lru *cache, const void *key, size_t key_len, void *value, size_t *value_len)
{
    pgvn_lru_entry *entry;
    unsigned char sum[32];
    if (!cache || !key || !key_len || !value_len) return PGVN_PLATFORM_INVALID;
    entry = find_entry(cache, key, key_len);
    if (!entry) return PGVN_PLATFORM_NOT_FOUND;
    if (pgvn_sha256(sum, entry->data + entry->key_len, entry->value_len) || memcmp(sum, entry->sum, sizeof(sum))) {
        free_entry(cache, entry);
        return PGVN_PLATFORM_NOT_FOUND;
    }
    if (*value_len < entry->value_len || (!value && entry->value_len)) return PGVN_PLATFORM_LIMIT;
    if (entry->value_len) memcpy(value, entry->data + entry->key_len, entry->value_len);
    *value_len = entry->value_len;
    if (entry != cache->newest) {
        unlink_entry(cache, entry);
        entry->newer = NULL;
        entry->older = cache->newest;
        if (cache->newest) cache->newest->newer = entry;
        cache->newest = entry;
        if (!cache->oldest) cache->oldest = entry;
        cache->bytes += entry->value_len;
        cache->count++;
    }
    return PGVN_PLATFORM_OK;
}

void pgvn_lru_test_corrupt(pgvn_lru *cache, const void *key, size_t key_len)
{
    pgvn_lru_entry *entry = cache && key ? find_entry(cache, key, key_len) : NULL;
    if (entry) entry->sum[0] ^= 0xff;
}
