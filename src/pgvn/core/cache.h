#ifndef PGVN_CACHE_H
#define PGVN_CACHE_H
#include <stdint.h>
#include "pgvn.h"
#include "svn_config.h"
typedef struct pgvn_cache pgvn_cache;
svn_error_t *pgvn_cache_configure(pgvn_context *context,svn_config_t *config,apr_pool_t *pool);
svn_string_t *pgvn_cache_get(pgvn_cache *cache,const char *key,apr_pool_t *pool);
void pgvn_cache_put(pgvn_cache *cache,const char *key,const svn_string_t *value);
typedef struct pgvn_cache_stats { int64_t hits,misses,evictions,bytes,errors; } pgvn_cache_stats;
void pgvn_cache_snapshot(pgvn_cache_stats *stats);
#endif
