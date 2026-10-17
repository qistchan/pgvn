/* Exercise the production cache, including deliberate heap corruption. */
#include <stdio.h>
#include <apr_general.h>
#include "svn_pools.h"
#include "../../src/pgvn/core/cache.c"
static int assertions;
#define CHECK(value) do { ++assertions; if(!(value)) { fprintf(stderr,"Cache assertion %d failed\n",__LINE__); exit(1); } } while(0)
typedef struct worker { pgvn_cache *cache; apr_pool_t *pool; int index; } worker;
static void *concurrent(void *baton)
{
    worker *work=baton;
    char key[40]; snprintf(key,sizeof(key),"thread-%d",work->index);
    svn_string_t *value=svn_string_create("binary\001value",work->pool);
    for(int i=0;i<100;++i) {
        pgvn_cache_put(work->cache,key,value);
        svn_string_t *read=pgvn_cache_get(work->cache,key,work->pool);
        if(read && (read->len!=value->len || memcmp(read->data,value->data,value->len))) abort();
    }
    return NULL;
}
int main(void)
{
    CHECK(apr_initialize()==APR_SUCCESS);
    apr_pool_t *pool=svn_pool_create(NULL); pgvn_context context={0};svn_config_t *config;
    svn_error_t *error=svn_config_create2(&config,TRUE,TRUE,pool);CHECK(!error);
    svn_config_set(config,"pgvn","cache-mode","local");svn_config_set(config,"pgvn","cache-local-mib","1");
    error=pgvn_cache_configure(&context,config,pool);CHECK(!error && context.cache);
    apr_pool_t *source=svn_pool_create(pool);char *binary=apr_palloc(source,8192);
    for(int i=0;i<8192;++i) binary[i]=(char)i;
    svn_string_t *value=svn_string_ncreate(binary,8192,source);
    pgvn_cache_put(context.cache,"immutable",value);svn_pool_destroy(source);
    svn_string_t *read=pgvn_cache_get(context.cache,"immutable",pool);CHECK(read && read->len==8192);
    for(int i=0;i<8192;++i) CHECK((unsigned char)read->data[i]==(unsigned char)i);
    ((char *)read->data)[0]='x';read=pgvn_cache_get(context.cache,"immutable",pool);CHECK(read && read->data[0]==0);
    context.cache->first->value[7]^=1;CHECK(!pgvn_cache_get(context.cache,"immutable",pool));
    pgvn_cache_stats stats;pgvn_cache_snapshot(&stats);CHECK(stats.errors==1);
    value=svn_string_create("bounded",pool);
    for(int i=0;i<1100;++i) pgvn_cache_put(context.cache,apr_psprintf(pool,"object-%d",i),value);
    CHECK(context.cache->count<=1024 && context.cache->bytes<=1048576);
    CHECK(!pgvn_cache_get(context.cache,"object-0",pool));CHECK(pgvn_cache_get(context.cache,"object-1099",pool));
    pthread_t threads[8];worker workers[8];
    for(int i=0;i<8;++i) { workers[i]=(worker){context.cache,svn_pool_create(pool),i};CHECK(!pthread_create(&threads[i],NULL,concurrent,&workers[i])); }
    for(int i=0;i<8;++i) CHECK(!pthread_join(threads[i],NULL));
    pgvn_context other={0};error=pgvn_cache_configure(&other,config,pool);CHECK(!error);
    CHECK(!pgvn_cache_get(other.cache,"object-1099",pool));
    svn_config_set(config,"pgvn","cache-mode","redis");error=pgvn_cache_configure(&other,config,pool);CHECK(error);svn_error_clear(error);
    svn_pool_destroy(pool);pgvn_cache_snapshot(&stats);CHECK(!stats.bytes && stats.evictions>0);
    apr_terminate();printf("{\"status\":\"PASS\",\"assertions\":%d}\n",assertions);return 0;
}
