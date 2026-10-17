/* Optional, process-local immutable fulltext cache. A context is its epoch:
 * nothing survives node restart/restore. PG metadata is always read first. */
#include "internal.h"
#include "cache.h"
#include "../platform/runtime.h"
#include "../platform/credentials.h"
#include <apr_strings.h>
typedef struct cache_entry {
    struct cache_entry *previous,*next;
    size_t size,cost;
    unsigned char digest[32];
    char *key;
    unsigned char *value;
} cache_entry;
struct pgvn_cache { pgvn_rwlock lock; size_t bytes,limit,count; cache_entry *first,*last; };
static volatile pgvn_atomic64 hits,misses,evictions,bytes,errors;
static void remove_entry(pgvn_cache *cache,cache_entry *entry)
{
    if(entry->previous) entry->previous->next=entry->next; else cache->first=entry->next;
    if(entry->next) entry->next->previous=entry->previous; else cache->last=entry->previous;
    --cache->count; cache->bytes-=entry->cost; pgvn_atomic_add64(&bytes,-(int64_t)entry->cost);
    free(entry->key); free(entry->value); free(entry);
}
static apr_status_t destroy(void *baton)
{
    pgvn_cache *cache=baton;
    while(cache->first) remove_entry(cache,cache->first);
    pgvn_rwlock_destroy(&cache->lock); return APR_SUCCESS;
}
svn_error_t *pgvn_cache_configure(pgvn_context *context,svn_config_t *config,apr_pool_t *pool)
{
    if(!config) return SVN_NO_ERROR;
    const char *mode,*text;
    svn_config_get(config,&mode,"pgvn","cache-mode","off");
    if(strcmp(mode,"off") && strcmp(mode,"local"))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"cache-mode must be off or local; Redis is not enabled");
    svn_config_get(config,&text,"pgvn","cache-local-mib","16");
    char *end=NULL; long mib=strtol(text,&end,10);
    if(!*text || *end || mib<1 || mib>256)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"cache-local-mib must be 1..256");
    if(!strcmp(mode,"off")) return SVN_NO_ERROR;
    pgvn_cache *cache=apr_pcalloc(pool,sizeof(*cache));
    cache->limit=(size_t)mib*1048576; pgvn_rwlock_init(&cache->lock);
    apr_pool_cleanup_register(pool,cache,destroy,apr_pool_cleanup_null);
    context->cache=cache; return SVN_NO_ERROR;
}
svn_string_t *pgvn_cache_get(pgvn_cache *cache,const char *key,apr_pool_t *pool)
{
    if(!cache) return NULL;
    svn_string_t *value=NULL; pgvn_write_lock(&cache->lock);
    for(cache_entry *entry=cache->first;entry;entry=entry->next) if(!strcmp(entry->key,key)) {
        unsigned char digest[32];
        if(!pgvn_sha256(digest,entry->value,entry->size) || memcmp(digest,entry->digest,32)) {
            pgvn_atomic_increment64(&errors); remove_entry(cache,entry); break;
        }
        value=svn_string_ncreate((char *)entry->value,entry->size,pool);
        if(entry!=cache->first) {
            entry->previous->next=entry->next;
            if(entry->next) entry->next->previous=entry->previous; else cache->last=entry->previous;
            entry->previous=NULL; entry->next=cache->first; cache->first->previous=entry; cache->first=entry;
        }
        break;
    }
    pgvn_write_unlock(&cache->lock);
    pgvn_atomic_increment64(value ? &hits : &misses); return value;
}
void pgvn_cache_put(pgvn_cache *cache,const char *key,const svn_string_t *value)
{
    if(!cache || value->len>1048576 || strlen(key)>256) return;
    size_t cost=sizeof(cache_entry)+strlen(key)+1+value->len;
    if(cost>cache->limit) return;
    cache_entry *entry=calloc(1,sizeof(*entry));
    if(!entry) return;
    entry->key=pgvn_strdup(key); entry->value=malloc(value->len ? value->len : 1);
    if(!entry->key || !entry->value) { free(entry->key); free(entry->value); free(entry); return; }
    memcpy(entry->value,value->data,value->len); entry->size=value->len; entry->cost=cost;
    if(!pgvn_sha256(entry->digest,entry->value,entry->size)) { free(entry->key); free(entry->value); free(entry); return; }
    pgvn_write_lock(&cache->lock);
    for(cache_entry *old=cache->first;old;old=old->next) if(!strcmp(old->key,key)) { remove_entry(cache,old); break; }
    while(cache->last && (cache->bytes+cost>cache->limit || cache->count>=1024)) { remove_entry(cache,cache->last); pgvn_atomic_increment64(&evictions); }
    entry->next=cache->first;
    if(cache->first) cache->first->previous=entry; else cache->last=entry;
    cache->first=entry; ++cache->count; cache->bytes+=cost; pgvn_atomic_add64(&bytes,(int64_t)cost);
    pgvn_write_unlock(&cache->lock);
}
void pgvn_cache_snapshot(pgvn_cache_stats *stats)
{
    stats->hits=pgvn_atomic_read64(&hits); stats->misses=pgvn_atomic_read64(&misses);
    stats->evictions=pgvn_atomic_read64(&evictions); stats->bytes=pgvn_atomic_read64(&bytes); stats->errors=pgvn_atomic_read64(&errors);
}
