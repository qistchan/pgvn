#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include "data.h"
#include <bcrypt.h>
#include <psapi.h>
#include <libpq-fe.h>
#include <apr_strings.h>
#include <apr_uuid.h>
#include "svn_config.h"
#include "svn_pools.h"
#include "svn_string.h"
#include "telemetry.h"
#include "data.h"
#include "pgvn_web.h"

#define COLLECTIONS 6
#define CACHED_ITEMS 1000
#define CACHED_REVISIONS 8000
#define JSON_BUDGET 8388608
#define TOKEN_OFFLINE_SECONDS 900
typedef struct monitor_collection {
    char **items,**names; char (*ids)[37];
    int count,truncated,capacity;
} monitor_collection;
typedef struct monitor_token { unsigned char hash[32]; ULONGLONG expires; } monitor_token;
typedef struct monitor_sample {
    monitor_collection collections[COLLECTIONS];
    monitor_token tokens[CACHED_ITEMS]; int token_count;
    char *database;
    char sampled_at[32];
    ULONGLONG tick;
    int valid,configuration_healthy,reader_restricted;
    LONG64 configuration_generation;
} monitor_sample;
typedef struct monitor_trend {
    char at[32]; LONG64 commands,errors,sql_calls,command_microseconds; double database_ms;
    int database_available;
    LONG64 bytes_received,bytes_sent;
    double rate,error_percent,latency_ms,cpu_percent;
    LONG64 working_set,private_bytes;
    int rate_valid,cpu_valid,activity_valid,resource_valid;
    ULONGLONG tick;
} monitor_trend;
struct pgvn_monitor_data {
    SRWLOCK lock;
    HANDLE thread,stop;
    apr_pool_t *pool;
    const char *dsn,*reader_dsn,*node;
    char instance[37],started_at[32];
    volatile LONG business_ready;
    volatile LONG stopping;
    volatile LONG64 main_tick;
    monitor_sample sample;
    int last_attempt_ok;
    const char *reason;
    unsigned long sequence;
    ULONGLONG started_tick;
    monitor_trend trends[180]; unsigned trend_count,trend_next;
    unsigned sample_failures;
    double sampler_ms;
    int writer_ok,resource_valid,cpu_valid;
    LONG64 dropped_batches,sampler_queries,sampler_query_errors;
    ULONGLONG cpu_ticks,last_cpu_tick,working_set,private_bytes;
    double cpu_percent;
    LONG business_connections,backend_lock_waits;
    char resource_at[32];
};
static const char *collection_names[]={"repositories","nodes","transactions","locks","lock_items","revisions"};
static _Thread_local HANDLE sampler_stop;
static _Thread_local pgvn_monitor_data *sampler_data;
static _Thread_local int sampler_final;

static void timestamp(char out[32])
{
    SYSTEMTIME now; GetSystemTime(&now);
    snprintf(out,32,"%04u-%02u-%02uT%02u:%02u:%02uZ",now.wYear,now.wMonth,now.wDay,now.wHour,now.wMinute,now.wSecond);
}
static void append(svn_stringbuf_t *out,apr_pool_t *pool,const char *format,...)
{
    /* The installed APR was built with MSVC and its formatter does not accept
     * C99 %lld. Use the product's C runtime consistently for our own formats. */
    va_list args,measure; va_start(args,format); va_copy(measure,args);
    int length=vsnprintf(NULL,0,format,measure); va_end(measure);
    if(length>=0 && length<=JSON_BUDGET) {
        char *text=apr_palloc(pool,(apr_size_t)length+1);
        vsnprintf(text,(size_t)length+1,format,args); svn_stringbuf_appendbytes(out,text,(apr_size_t)length);
    }
    va_end(args);
}
static void quote(svn_stringbuf_t *out,const char *text)
{
    static const char hex[]="0123456789abcdef";
    svn_stringbuf_appendbyte(out,'"');
    for(const unsigned char *p=(const unsigned char *)text;*p;++p) {
        if(*p=='"' || *p=='\\') { svn_stringbuf_appendbyte(out,'\\'); svn_stringbuf_appendbyte(out,*p); }
        else if(*p<32) { char escape[]="\\u0000"; escape[4]=hex[*p>>4]; escape[5]=hex[*p&15]; svn_stringbuf_appendbytes(out,escape,6); }
        else svn_stringbuf_appendbyte(out,*p);
    }
    svn_stringbuf_appendbyte(out,'"');
}
static LONG64 total_commands(const pgvn_process_metrics *metrics,int errors)
{
    LONG64 count=0;
    for(int i=0;i<PGVN_OPERATION_COUNT;++i) for(int j=errors ? 1 : 0;j<PGVN_RESULT_COUNT;++j) count+=metrics->commands[i][j];
    return count;
}
static void free_sample(monitor_sample *sample)
{
    for(int c=0;c<COLLECTIONS;++c) {
        monitor_collection *collection=&sample->collections[c];
        for(int i=0;i<collection->count;++i) { free(collection->items[i]); if(collection->names) free(collection->names[i]); }
        free(collection->items); free(collection->names); free(collection->ids);
    }
    free(sample->database); SecureZeroMemory(sample,sizeof(*sample));
}
static PGresult *query(PGconn *connection,const char *sql,int count,const char *const *values)
{
    /* statement_timeout is server-side; a blackholed TCP connection must not
     * trap the sampler or service shutdown in a blocking PQexec. */
    InterlockedIncrement64(&sampler_data->sampler_queries);
    if(!PQsendQueryParams(connection,sql,count,NULL,values,NULL,NULL,0)) { InterlockedIncrement64(&sampler_data->sampler_query_errors); return NULL; }
    ULONGLONG deadline=GetTickCount64()+(sampler_final ? 200 : 2000); PGresult *result=NULL;
    for(;;) {
        if((!sampler_final && WaitForSingleObject(sampler_stop,0)!=WAIT_TIMEOUT) || GetTickCount64()>=deadline) break;
        int pending=PQflush(connection);
        if(pending<0 || !PQconsumeInput(connection)) break;
        if(!pending && !PQisBusy(connection)) {
            PGresult *next=PQgetResult(connection);
            if(!next) {
                if(result && (PQresultStatus(result)==PGRES_TUPLES_OK || PQresultStatus(result)==PGRES_COMMAND_OK)) return result;
                break;
            }
            if(result) PQclear(result); result=next;
            continue;
        }
        WSAPOLLFD descriptor={0}; descriptor.fd=(SOCKET)PQsocket(connection);
        descriptor.events=POLLRDNORM | (pending ? POLLWRNORM : 0);
        int ready=WSAPoll(&descriptor,1,100);
        if(ready==SOCKET_ERROR || (ready>0 && (descriptor.revents&(POLLERR|POLLHUP|POLLNVAL)))) break;
    }
    if(result) PQclear(result); InterlockedIncrement64(&sampler_data->sampler_query_errors); return NULL;
}
static int nibble(char c)
{
    return c>='0' && c<='9' ? c-'0' : c>='a' && c<='f' ? c-'a'+10 : -1;
}
static int hash_decode(unsigned char out[32],const char *text)
{
    if(strlen(text)!=64) return 0;
    for(int i=0;i<32;++i) { int a=nibble(text[2*i]),b=nibble(text[2*i+1]); if(a<0 || b<0) return 0; out[i]=(unsigned char)((a<<4)|b); }
    return 1;
}
static int sha256(unsigned char digest[32],const unsigned char *value,ULONG length)
{
    BCRYPT_ALG_HANDLE algorithm=NULL; BCRYPT_HASH_HANDLE hash=NULL;
    DWORD size=0,received=0; unsigned char *object=NULL;
    NTSTATUS status=BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,NULL,0);
    if(!status) status=BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,(PUCHAR)&size,sizeof(size),&received,0);
    if(!status && (!size || size>1048576)) status=(NTSTATUS)0xc000000d;
    if(!status && !(object=malloc(size))) status=(NTSTATUS)0xc0000017;
    if(!status) status=BCryptCreateHash(algorithm,&hash,object,size,NULL,0,0);
    if(!status) status=BCryptHashData(hash,(PUCHAR)value,length,0);
    if(!status) status=BCryptFinishHash(hash,digest,32,0);
    if(hash) BCryptDestroyHash(hash);
    if(object) { SecureZeroMemory(object,size); free(object); }
    if(algorithm) BCryptCloseAlgorithmProvider(algorithm,0);
    return !status;
}

/* All rows are built by fixed SQL using JSON constructors. Each collection is
 * capped independently; long display text is explicitly marked as truncated. */
static const char sample_sql[]=
    "SELECT 'database',json_build_object('version',current_setting('server_version_num')::integer,"
    "'database_bytes',pg_database_size(current_database()),'reader_read_only',current_setting('transaction_read_only')='on',"
    "'repositories',(SELECT count(*) FROM pgvn.repositories),"
    "'nodes_total',(SELECT count(*) FROM pgvn_monitor.nodes),"
    "'nodes_online',(SELECT count(*) FROM pgvn_monitor.nodes WHERE last_seen_at>=clock_timestamp()-interval '15 seconds' AND metrics->>'stopped' IS DISTINCT FROM 'true'),"
    "'active_transactions',(SELECT count(*) FROM pgvn_monitor.transactions),"
    "'svn_locks',COALESCE((SELECT sum(lock_count) FROM pgvn_monitor.locks),0))::text,''::text,''::text "
    "UNION ALL SELECT 'repositories',row_to_json(q)::text,id::text,name||' '||id::text||' '||svn_uuid::text FROM "
    "(SELECT repository_id AS id,left(canonical_name,256) AS name,length(canonical_name)>256 AS name_truncated,"
    "enabled,generation,head_rev,svn_uuid,last_commit_at,active_transactions,lock_count FROM pgvn_monitor.repositories ORDER BY repository_id LIMIT 1001) q "
    "UNION ALL SELECT 'nodes',row_to_json(q)::text,instance_id::text,'' FROM "
    "(SELECT instance_id,node_id,component,version,process_id,started_at,last_seen_at,"
    "extract(epoch FROM clock_timestamp()-last_seen_at)::double precision AS age_seconds,metrics FROM pgvn_monitor.nodes ORDER BY node_id,instance_id LIMIT 1001) q "
    "UNION ALL SELECT 'transactions',row_to_json(q)::text,repository_id::text,'' FROM "
    "(SELECT repository_id,left(txn_id,256) AS transaction_id,length(txn_id)>256 AS id_truncated,base_rev,created_at "
    "FROM pgvn_monitor.transactions ORDER BY repository_id,txn_id LIMIT 1001) q "
    "UNION ALL SELECT 'locks',row_to_json(q)::text,repository_id::text,'' FROM "
    "(SELECT repository_id,lock_count FROM pgvn_monitor.locks ORDER BY repository_id LIMIT 1001) q "
    "UNION ALL SELECT 'lock_items',row_to_json(q)::text,repository_id::text,'' FROM "
    "(SELECT * FROM pgvn_monitor.lock_items ORDER BY repository_id,path LIMIT 1001) q "
    "UNION ALL SELECT 'revisions',row_to_json(q)::text,repository_id::text,'' FROM "
    "(SELECT r.repository_id,v.rev,v.committed_at FROM (SELECT repository_id FROM pgvn.repositories ORDER BY repository_id LIMIT 1000) r "
    "CROSS JOIN LATERAL(SELECT rev,committed_at FROM pgvn_monitor.recent_revisions v WHERE v.repository_id=r.repository_id ORDER BY rev DESC LIMIT 8) v "
    "ORDER BY r.repository_id,v.rev DESC LIMIT 8001) q";

static int read_sample(PGconn *connection,monitor_sample *sample,const char **reason)
{
    PGresult *result=query(connection,
        "SELECT (SELECT string_agg(version::text,',' ORDER BY version) FROM pgfs.schema_versions),"
        "(SELECT string_agg(version::text,',' ORDER BY version) FROM pgvn.schema_versions),"
        "pg_is_in_recovery(),current_setting('default_transaction_read_only'),"
        "(SELECT string_agg(migration_sha256,',' ORDER BY version) FROM pgvn.storage_migrations),"
        "(SELECT string_agg(migration_sha256,',' ORDER BY version) FROM pgvn.schema_versions),"
        "(SELECT healthy FROM pgvn_monitor.configuration_health),(SELECT max_configuration_generation FROM pgvn_monitor.configuration_health),"
        "NOT has_table_privilege(current_user,'pgvn.credentials','SELECT') AND NOT has_table_privilege(current_user,'pgvn.monitor_tokens','SELECT')",0,NULL);
    if(!result) return 0;
    int valid=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"1,2,3,4")
        && !strcmp(PQgetvalue(result,0,1),"1,2,3,4,5") && !strcmp(PQgetvalue(result,0,2),"f")
        && !strcmp(PQgetvalue(result,0,3),"on") && !strcmp(PQgetvalue(result,0,4),PGVN_STORAGE_HASHES)
        && !strcmp(PQgetvalue(result,0,5),PGVN_CATALOG_HASHES);
    if(valid) { sample->configuration_healthy=!strcmp(PQgetvalue(result,0,6),"t");
        sample->configuration_generation=PQgetisnull(result,0,7) ? -1 : apr_atoi64(PQgetvalue(result,0,7));
        sample->reader_restricted=!strcmp(PQgetvalue(result,0,8),"t"); }
    PQclear(result);
    if(!valid) { *reason="schema_or_primary_invalid"; return 0; }
    if(!sample->reader_restricted) { *reason="reader_role_not_restricted"; return 0; }
    /* A repeatable snapshot keeps counts, pages and token revocations aligned.
     * Readiness uses the primary/default setting above, outside this read-only txn. */
    result=query(connection,"BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",0,NULL);
    if(!result) return 0; PQclear(result);
    result=query(connection,sample_sql,0,NULL);
    if(!result) return 0;
    size_t bytes=0;
    for(int i=0;i<PQntuples(result);++i) {
        const char *kind=PQgetvalue(result,i,0),*json=PQgetvalue(result,i,1);
        size_t length=(size_t)PQgetlength(result,i,1); bytes+=length;
        if(bytes>JSON_BUDGET) { PQclear(result); *reason="snapshot_capacity"; return 0; }
        if(!strcmp(kind,"database")) {
            sample->database=_strdup(json); if(!sample->database) { PQclear(result); return 0; }
        } else for(int c=0;c<COLLECTIONS;++c) if(!strcmp(kind,collection_names[c])) {
            monitor_collection *collection=&sample->collections[c];
            if(!collection->capacity) {
                collection->capacity=c==5 ? CACHED_REVISIONS : CACHED_ITEMS;
                collection->items=calloc((size_t)collection->capacity,sizeof(char *));
                collection->ids=calloc((size_t)collection->capacity,sizeof(*collection->ids));
                if(c==0) collection->names=calloc((size_t)collection->capacity,sizeof(char *));
                if(!collection->items || !collection->ids || (c==0 && !collection->names)) { PQclear(result); return 0; }
            }
            if(collection->count==collection->capacity) collection->truncated=1;
            else {
                int index=collection->count++;
                collection->items[index]=_strdup(json);
                if(c==0) collection->names[index]=_strdup(PQgetvalue(result,i,3));
                snprintf(collection->ids[index],37,"%s",PQgetvalue(result,i,2));
                if(!collection->items[index] || (c==0 && !collection->names[index])) { PQclear(result); return 0; }
            }
            break;
        }
    }
    PQclear(result);
    result=query(connection,"SELECT hash_hex,remaining_seconds FROM pgvn_monitor.token_snapshot()",0,NULL);
    if(!result) return 0;
    if(PQntuples(result)>CACHED_ITEMS) { PQclear(result); *reason="token_capacity"; return 0; }
    ULONGLONG now=GetTickCount64();
    for(int i=0;i<PQntuples(result);++i) {
        double seconds=strtod(PQgetvalue(result,i,1),NULL);
        if(seconds<=0) continue;
        if(seconds>TOKEN_OFFLINE_SECONDS) seconds=TOKEN_OFFLINE_SECONDS;
        monitor_token *token=&sample->tokens[sample->token_count];
        if(!hash_decode(token->hash,PQgetvalue(result,i,0))) { PQclear(result); return 0; }
        token->expires=now+(ULONGLONG)(seconds*1000); sample->token_count++;
    }
    PQclear(result);
    result=query(connection,"COMMIT",0,NULL); if(!result) return 0; PQclear(result);
    timestamp(sample->sampled_at); sample->tick=GetTickCount64(); sample->valid=1;
    return 1;
}
static PGconn *monitor_connect(const char *dsn,int reading)
{
    const char *keys[]={"dbname","connect_timeout","options","application_name","client_encoding",NULL};
    const char *values[]={dsn,"3",reading ? "-c statement_timeout=1000 -c lock_timeout=500 -c default_transaction_read_only=on" :
        "-c statement_timeout=1000 -c lock_timeout=500",reading ? "pgvn-monitor-read" : "pgvn-monitor-write","UTF8",NULL};
    PGconn *connection=PQconnectdbParams(keys,values,1);
    if(connection && (PQstatus(connection)!=CONNECTION_OK || PQsetnonblocking(connection,1))) { PQfinish(connection); connection=NULL; }
    return connection;
}
static int writer_probe(PGconn *connection,LONG *connections,LONG *waits)
{
    PGresult *result=query(connection,
        "SELECT NOT pg_is_in_recovery() AND current_setting('default_transaction_read_only')='off' AND "
        "(SELECT datallowconn FROM pg_database WHERE datname=current_database()),"
        "count(*) FILTER(WHERE application_name='pgvn'),"
        "count(*) FILTER(WHERE application_name='pgvn' AND wait_event_type='Lock') "
        "FROM pg_stat_activity WHERE datname=current_database() AND usename=current_user",0,NULL);
    if(!result) return 0;
    int ready=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t");
    if(ready) { *connections=(LONG)atoi(PQgetvalue(result,0,1)); *waits=(LONG)atoi(PQgetvalue(result,0,2)); }
    PQclear(result); return ready;
}
static void resources(pgvn_monitor_data *data)
{
    PROCESS_MEMORY_COUNTERS_EX memory={0}; memory.cb=sizeof(memory);
    int available=GetProcessMemoryInfo(GetCurrentProcess(),(PROCESS_MEMORY_COUNTERS *)&memory,sizeof(memory))!=0;
    FILETIME created,exited,kernel,user;
    int cpu=GetProcessTimes(GetCurrentProcess(),&created,&exited,&kernel,&user)!=0;
    ULONGLONG now=GetTickCount64(),ticks=0;
    if(cpu) { ULARGE_INTEGER k,u; k.LowPart=kernel.dwLowDateTime; k.HighPart=kernel.dwHighDateTime;
        u.LowPart=user.dwLowDateTime; u.HighPart=user.dwHighDateTime; ticks=k.QuadPart+u.QuadPart; }
    DWORD processors=GetActiveProcessorCount(ALL_PROCESSOR_GROUPS);
    AcquireSRWLockExclusive(&data->lock);
    data->resource_valid=available;
    if(available) { data->working_set=memory.WorkingSetSize; data->private_bytes=memory.PrivateUsage; }
    data->cpu_valid=cpu && processors && data->last_cpu_tick && now>data->last_cpu_tick && ticks>=data->cpu_ticks;
    if(data->cpu_valid) data->cpu_percent=(double)(ticks-data->cpu_ticks)/((now-data->last_cpu_tick)*10000.0*processors)*100.0;
    if(cpu) { data->cpu_ticks=ticks; data->last_cpu_tick=now; }
    timestamp(data->resource_at);
    ReleaseSRWLockExclusive(&data->lock);
}
static int heartbeat(PGconn *connection,pgvn_monitor_data *data,const pgvn_process_metrics *metrics,const monitor_sample *sample,apr_pool_t *pool)
{
    svn_stringbuf_t *body=svn_stringbuf_create_empty(pool);
    append(body,pool,"{\"commands\":%lld,\"errors\":%lld,\"published_revisions\":%lld,\"receipt_confirmations\":%lld,\"sessions_active\":%ld,"
        "\"sql_calls\":%lld,\"connections\":%ld,\"configuration_generation\":",
        (long long)total_commands(metrics,0),(long long)total_commands(metrics,1),(long long)metrics->published_revisions,
        (long long)metrics->receipt_confirmations,(long)metrics->sessions_active,(long long)metrics->sql_calls,(long)metrics->connections_active);
    if(sample->configuration_generation>=0) append(body,pool,"%lld",(long long)sample->configuration_generation); else svn_stringbuf_appendcstr(body,"null");
    append(body,pool,",\"ready\":%s,\"stopped\":%s,\"cpu_percent\":",pgvn_monitor_live(data) && data->writer_ok && sample->configuration_healthy && InterlockedCompareExchange(&data->business_ready,0,0) ? "true" : "false",InterlockedCompareExchange(&data->stopping,0,0) ? "true" : "false");
    AcquireSRWLockShared(&data->lock);
    if(data->cpu_valid) append(body,pool,"%.3f",data->cpu_percent); else svn_stringbuf_appendcstr(body,"null");
    if(data->resource_valid) append(body,pool,",\"working_set_bytes\":%llu,\"private_bytes\":%llu",data->working_set,data->private_bytes);
    else svn_stringbuf_appendcstr(body,",\"working_set_bytes\":null,\"private_bytes\":null");
    ReleaseSRWLockShared(&data->lock);
    svn_stringbuf_appendbyte(body,'}');
    const char *values[]={data->instance,data->node,"svn",PGVN_VERSION "/" PGVN_MILESTONE,apr_psprintf(pool,"%lu",(unsigned long)GetCurrentProcessId()),data->started_at,body->data};
    PGresult *result=query(connection,"SELECT pgvn_monitor.publish($1::uuid,$2,$3,$4,$5::integer,$6::timestamptz,$7::jsonb)",7,values);
    if(!result) return 0; PQclear(result); return 1;
}
static DWORD WINAPI sampler(void *baton)
{
    pgvn_monitor_data *data=baton; sampler_stop=data->stop; sampler_data=data;
    PGconn *reader=NULL,*writer=NULL;
    apr_pool_t *pool=svn_pool_create(NULL);
    while(WaitForSingleObject(data->stop,0)==WAIT_TIMEOUT) {
        ULONGLONG start=GetTickCount64(); const char *reason="database_unavailable";
        monitor_sample *sample=calloc(1,sizeof(*sample));
        if(!reader) reader=monitor_connect(data->reader_dsn,1);
        int ok=sample && reader && read_sample(reader,sample,&reason);
        if(!ok && reader) { PQfinish(reader); reader=NULL; }
        if(!writer && WaitForSingleObject(data->stop,0)==WAIT_TIMEOUT) writer=monitor_connect(data->dsn,0);
        LONG connections=0,waits=0;
        int writable=writer && writer_probe(writer,&connections,&waits);
        if(!writable && writer) { PQfinish(writer); writer=NULL; }
        resources(data);
        pgvn_process_metrics metrics; pgvn_process_snapshot(&metrics);
        AcquireSRWLockExclusive(&data->lock); data->writer_ok=writable; data->business_connections=connections; data->backend_lock_waits=waits; ReleaseSRWLockExclusive(&data->lock);
        if(ok && writable && !heartbeat(writer,data,&metrics,sample,pool)) {
            pgvn_telemetry_event("sampler","heartbeat_unavailable",0);
            AcquireSRWLockExclusive(&data->lock); data->dropped_batches++; ReleaseSRWLockExclusive(&data->lock);
            PQfinish(writer); writer=NULL;
        }
        monitor_trend point={0}; timestamp(point.at); point.tick=GetTickCount64();
        point.command_microseconds=metrics.command_microseconds;
        point.commands=total_commands(&metrics,0); point.errors=total_commands(&metrics,1); point.sql_calls=metrics.sql_calls;
        point.bytes_received=metrics.bytes_received; point.bytes_sent=metrics.bytes_sent;
        point.database_available=ok; point.database_ms=(double)(GetTickCount64()-start);
        AcquireSRWLockExclusive(&data->lock);
        const char *state= !ok ? reason : !writable ? "writer_unavailable" : !sample->configuration_healthy ? "configuration_unavailable" : "ok";
        if(strcmp(data->reason,state)) pgvn_telemetry_event("sampler",state,0);
        if(ok) { monitor_sample old=data->sample; data->sample=*sample; *sample=old; data->sequence++; }
        else if(!strcmp(reason,"token_capacity")) {
            SecureZeroMemory(data->sample.tokens,sizeof(data->sample.tokens)); data->sample.token_count=0;
        }
        data->last_attempt_ok=ok; data->reason=state;
        if(!ok) data->sample_failures++;
        data->sampler_ms=point.database_ms;
        point.cpu_valid=data->cpu_valid; point.cpu_percent=data->cpu_percent; point.resource_valid=data->resource_valid;
        point.working_set=(LONG64)data->working_set; point.private_bytes=(LONG64)data->private_bytes;
        if(data->trend_count) {
            monitor_trend *previous=&data->trends[(data->trend_next+179)%180];
            if(point.tick>previous->tick && point.commands>=previous->commands && point.errors>=previous->errors) {
                point.rate_valid=1; point.rate=(point.commands-previous->commands)*1000.0/(point.tick-previous->tick);
                LONG64 completed=point.commands-previous->commands;
                point.activity_valid=completed>0;
                if(completed) { point.error_percent=(point.errors-previous->errors)*100.0/completed;
                    point.latency_ms=(point.command_microseconds-previous->command_microseconds)/1000.0/completed; }
            }
        }
        data->trends[data->trend_next]=point; data->trend_next=(data->trend_next+1)%180;
        if(data->trend_count<180) data->trend_count++;
        ReleaseSRWLockExclusive(&data->lock);
        if(sample) { free_sample(sample); free(sample); }
        svn_pool_clear(pool);
        ULONGLONG elapsed=GetTickCount64()-start;
        DWORD delay=elapsed<5000 ? (DWORD)(5000-elapsed) : 1;
        if(WaitForSingleObject(data->stop,delay)!=WAIT_TIMEOUT) break;
    }
    /* Best effort final status uses only an existing connection and at most
     * 200 ms of polling. Missing acknowledgement leaves peers to detect loss;
     * it never holds up shutdown or establishes a new database connection. */
    if(writer && PQstatus(writer)==CONNECTION_OK) {
        sampler_final=1; pgvn_process_metrics final; pgvn_process_snapshot(&final);
        (void)heartbeat(writer,data,&final,&data->sample,pool);
    }
    if(reader) PQfinish(reader); if(writer) PQfinish(writer); svn_pool_destroy(pool); return 0;
}

DWORD pgvn_monitor_data_start(pgvn_monitor_data **output,const char *configuration)
{
    *output=NULL; pgvn_monitor_data *data=calloc(1,sizeof(*data));
    if(!data) return ERROR_NOT_ENOUGH_MEMORY;
    InitializeSRWLock(&data->lock); data->pool=svn_pool_create(NULL); data->reason="not_configured";
    apr_uuid_t id; apr_uuid_get(&id); apr_uuid_format(data->instance,&id);
    timestamp(data->started_at); data->started_tick=GetTickCount64(); pgvn_monitor_tick(data);
    data->node="local";
    if(configuration) {
        svn_config_t *config; svn_error_t *error=svn_config_read3(&config,configuration,TRUE,TRUE,TRUE,data->pool);
        if(error) { svn_error_clear(error); pgvn_monitor_data_stop(data); return ERROR_INVALID_PARAMETER; }
        svn_config_get(config,&data->dsn,"pgvn","connection",NULL);
        svn_config_get(config,&data->reader_dsn,"pgvn","monitor-connection",NULL);
        svn_config_get(config,&data->node,"pgvn","node-id",NULL);
        if(!data->node) {
            char host[64]; DWORD length=sizeof(host); const char *port;
            if(!GetComputerNameA(host,&length)) strcpy(host,"node");
            svn_config_get(config,&port,"pgvn","listen-port","3690");
            data->node=apr_pstrcat(data->pool,host,":",port,NULL);
        }
        int valid=data->dsn && *data->dsn && data->reader_dsn && *data->reader_dsn && *data->node && strlen(data->node)<=80;
        for(const unsigned char *p=(const unsigned char *)data->node;*p;++p)
            if(!((*p>='a' && *p<='z') || (*p>='A' && *p<='Z') || (*p>='0' && *p<='9') || strchr("_.:-",*p))) valid=0;
        if(!valid) { pgvn_monitor_data_stop(data); return ERROR_INVALID_PARAMETER; }
        data->reason="awaiting_sample";
        data->stop=CreateEventW(NULL,TRUE,FALSE,NULL);
        if(!data->stop) { DWORD result=GetLastError(); pgvn_monitor_data_stop(data); return result; }
        data->thread=CreateThread(NULL,0,sampler,data,0,NULL);
        if(!data->thread) { DWORD result=GetLastError(); pgvn_monitor_data_stop(data); return result; }
    }
    *output=data; return NO_ERROR;
}
void pgvn_monitor_data_stop(pgvn_monitor_data *data)
{
    if(!data) return;
    InterlockedExchange(&data->stopping,1); InterlockedExchange(&data->business_ready,0);
    if(data->stop) SetEvent(data->stop);
    if(data->thread) { WaitForSingleObject(data->thread,INFINITE); CloseHandle(data->thread); }
    if(data->stop) CloseHandle(data->stop);
    free_sample(&data->sample); if(data->pool) svn_pool_destroy(data->pool); free(data);
}
void pgvn_monitor_business(pgvn_monitor_data *data,int ready) { if(data) InterlockedExchange(&data->business_ready,ready); }
void pgvn_monitor_tick(pgvn_monitor_data *data) { if(data) InterlockedExchange64(&data->main_tick,(LONG64)GetTickCount64()); }
int pgvn_monitor_live(pgvn_monitor_data *data)
{
    return data && GetTickCount64()-(ULONGLONG)InterlockedCompareExchange64(&data->main_tick,0,0)<=15000;
}
int pgvn_monitor_ready(pgvn_monitor_data *data)
{
    if(!pgvn_monitor_live(data)) return 0;
    AcquireSRWLockShared(&data->lock);
    int ready=data->sample.valid && data->last_attempt_ok && data->writer_ok && data->sample.configuration_healthy && GetTickCount64()-data->sample.tick<=15000
        && InterlockedCompareExchange(&data->business_ready,0,0);
    ReleaseSRWLockShared(&data->lock); return ready;
}
int pgvn_monitor_authorize(pgvn_monitor_data *data,const char *header,size_t length)
{
    if(length!=76 || memcmp(header,"Bearer pgvn_",12)) return 0;
    for(size_t i=12;i<length;++i) if(nibble(header[i])<0) return 0;
    unsigned char hash[32]; if(!sha256(hash,(const unsigned char *)header+7,69)) return 0;
    int found=0; ULONGLONG now=GetTickCount64();
    AcquireSRWLockShared(&data->lock);
    for(int i=0;i<data->sample.token_count;++i) {
        unsigned different=0; for(int n=0;n<32;++n) different|=hash[n]^data->sample.tokens[i].hash[n];
        found|=different==0 && now<data->sample.tokens[i].expires;
    }
    ReleaseSRWLockShared(&data->lock); SecureZeroMemory(hash,sizeof(hash)); return found;
}

static void metadata(svn_stringbuf_t *out,pgvn_monitor_data *data,apr_pool_t *pool)
{
    monitor_sample *sample=&data->sample;
    svn_stringbuf_appendcstr(out,"\"sampled_at\":");
    if(sample->valid) quote(out,sample->sampled_at); else svn_stringbuf_appendcstr(out,"null");
    if(sample->valid) append(out,pool,",\"age_seconds\":%.3f",(GetTickCount64()-sample->tick)/1000.0);
    else svn_stringbuf_appendcstr(out,",\"age_seconds\":null");
    const char *freshness=!sample->valid ? "unknown" : GetTickCount64()-sample->tick>15000 ? "stale" : "fresh";
    append(out,pool,",\"freshness\":\"%s\",\"source\":\"postgresql_sample\",\"node_id\":",freshness); quote(out,data->node);
    append(out,pool,",\"instance_id\":\"%s\",\"sample_id\":%lu,\"last_attempt_ok\":%s,\"reason\":\"%s\"",
        data->instance,data->sequence,data->last_attempt_ok ? "true" : "false",data->reason);
}
static int p95_bound(const LONG64 *buckets,LONG64 count)
{
    static const int milliseconds[]={5,20,100,500,2000};
    if(!count) return 0;
    LONG64 threshold=count-count/20;
    for(int i=0;i<5;++i) if(buckets[i]>=threshold) return milliseconds[i];
    return -1;
}
static void process_json(svn_stringbuf_t *out,pgvn_monitor_data *data,apr_pool_t *pool)
{
    pgvn_process_metrics m; pgvn_process_snapshot(&m);
    LONG64 count=total_commands(&m,0); int p95=p95_bound(m.latency,count);
    append(out,pool,"{\"source\":\"process\",\"version\":\"%s/%s\",\"started_at\":\"%s\",\"uptime_seconds\":%.3f,\"pid\":%lu,\"commands\":%lld,\"errors\":%lld,"
        "\"commands_active\":%ld,\"published_revisions\":%lld,\"receipt_confirmations\":%lld,\"sessions_active\":%ld,\"sessions_started\":%lld,\"sessions_rejected\":%lld,"
        "\"sql_calls\":%lld,\"sql_errors\":%lld,\"sql_milliseconds\":%.3f,\"sql_active\":%ld,\"connections_leased\":%ld,\"connection_limit\":32,"
        "\"connection_rejections\":%lld,\"connection_failures\":%lld,\"control_waiters\":%ld,\"writers_active\":%ld,\"bytes_received\":%lld,\"bytes_sent\":%lld,"
        "\"byte_source\":\"svn_ra_svn_socket\",\"monitor_requests\":%lld,\"monitor_errors\":%lld,\"monitor_active\":%ld,\"command_mean_milliseconds\":",
        PGVN_VERSION,PGVN_MILESTONE,data->started_at,(GetTickCount64()-data->started_tick)/1000.0,(unsigned long)GetCurrentProcessId(),(long long)count,(long long)total_commands(&m,1),
        (long)m.commands_active,(long long)m.published_revisions,(long long)m.receipt_confirmations,(long)m.sessions_active,(long long)m.sessions_started,(long long)m.sessions_rejected,
        (long long)m.sql_calls,(long long)m.sql_errors,m.sql_microseconds/1000.0,(long)m.sql_active,(long)m.connections_active,
        (long long)m.connection_rejections,(long long)m.connection_failures,(long)m.control_waiters,(long)m.writers_active,(long long)m.bytes_received,(long long)m.bytes_sent,
        (long long)m.monitor_requests,(long long)m.monitor_errors,(long)m.monitor_active);
    if(count) append(out,pool,"%.3f",m.command_microseconds/1000.0/count); else svn_stringbuf_appendcstr(out,"null");
    svn_stringbuf_appendcstr(out,",\"command_p95_upper_bound_ms\":");
    if(p95>0) append(out,pool,"%d",p95); else svn_stringbuf_appendcstr(out,"null");
    append(out,pool,",\"command_p95_overflow\":%s,\"resource_sampled_at\":",p95<0 ? "true" : "false");
    if(*data->resource_at) quote(out,data->resource_at); else svn_stringbuf_appendcstr(out,"null");
    svn_stringbuf_appendcstr(out,",\"cpu_percent_of_machine\":");
    if(data->cpu_valid) append(out,pool,"%.3f",data->cpu_percent); else svn_stringbuf_appendcstr(out,"null");
    if(data->resource_valid) append(out,pool,",\"working_set_bytes\":%llu,\"private_bytes\":%llu",data->working_set,data->private_bytes);
    else svn_stringbuf_appendcstr(out,",\"working_set_bytes\":null,\"private_bytes\":null");
    svn_stringbuf_appendcstr(out,",\"sqlstate_classes\":{");
    for(int i=0;i<PGVN_SQL_CLASS_COUNT;++i) append(out,pool,"%s\"%s\":%lld",i ? "," : "",pgvn_sql_class_name(i),(long long)m.sql_classes[i]);
    svn_stringbuf_appendcstr(out,"}}");
}
static void database_json(svn_stringbuf_t *out,pgvn_monitor_data *data,apr_pool_t *pool)
{
    if(!data->sample.database) { svn_stringbuf_appendcstr(out,"null"); return; }
    size_t length=strlen(data->sample.database);
    svn_stringbuf_appendbytes(out,data->sample.database,length-1);
    append(out,pool,",\"writable\":%s,\"reader_restricted\":%s,\"configuration_healthy\":%s,\"application_connections_same_role\":",
        data->writer_ok ? "true" : "null",data->sample.reader_restricted ? "true" : "false",data->sample.configuration_healthy ? "true" : "false");
    if(data->writer_ok) append(out,pool,"%ld,\"backend_lock_waits_same_role\":%ld",(long)data->business_connections,(long)data->backend_lock_waits);
    else svn_stringbuf_appendcstr(out,"null,\"backend_lock_waits_same_role\":null");
    svn_stringbuf_appendbyte(out,'}');
}
static int decimal(const char **cursor,unsigned long maximum,unsigned long *value)
{
    const unsigned char *p=(const unsigned char *)*cursor; unsigned long result=0;
    if(*p<'0' || *p>'9') return 0;
    while(*p>='0' && *p<='9') {
        if(result>(maximum-(*p-'0'))/10) return 0;
        result=result*10+(*p++-'0');
    }
    *cursor=(const char *)p; *value=result; return 1;
}
enum { OPT_LIMIT=1,OPT_CURSOR=2,OPT_SEARCH=4,OPT_REPO=8,OPT_METRIC=16,OPT_WINDOW=32,OPT_STEP=64 };
typedef struct api_options { int limit,offset,window,step; unsigned fields; char cursor[96],search[257],repository[37],metric[40]; } api_options;
static int decode_value(char *out,size_t capacity,const char *input,size_t length)
{
    size_t size=0;
    for(size_t i=0;i<length;++i) {
        unsigned char value=(unsigned char)input[i];
        if(value=='%') {
            if(i+2>=length) return 0;
            char a=input[++i],b=input[++i]; if(a>='A' && a<='F') a=(char)(a-'A'+'a'); if(b>='A' && b<='F') b=(char)(b-'A'+'a');
            int high=nibble(a),low=nibble(b); if(high<0 || low<0) return 0; value=(unsigned char)((high<<4)|low);
        } else if(value=='+') value=' ';
        if(value<32 || value==127 || size+1>=capacity) return 0;
        out[size++]=(char)value;
    }
    out[size]=0;
    return !size || MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,out,(int)size,NULL,0)>0;
}
static int parse_options(api_options *options,const char *query_text,unsigned allowed)
{
    memset(options,0,sizeof(*options)); options->limit=50; options->window=900; options->step=5;
    strcpy(options->metric,"commands_per_second");
    while(query_text && *query_text) {
        const char *end=strchr(query_text,'&'); if(!end) end=query_text+strlen(query_text);
        const char *equals=memchr(query_text,'=',(size_t)(end-query_text)); if(!equals) return 0;
        char key[24],value[257]; size_t key_length=(size_t)(equals-query_text);
        if(!key_length || key_length>=sizeof(key)) return 0;
        memcpy(key,query_text,key_length); key[key_length]=0;
        if(!decode_value(value,sizeof(value),equals+1,(size_t)(end-equals-1))) return 0;
        unsigned flag=!strcmp(key,"limit") ? OPT_LIMIT : !strcmp(key,"cursor") ? OPT_CURSOR : !strcmp(key,"q") ? OPT_SEARCH :
            !strcmp(key,"repo") ? OPT_REPO : !strcmp(key,"metric") ? OPT_METRIC : !strcmp(key,"window") ? OPT_WINDOW : !strcmp(key,"step") ? OPT_STEP : 0;
        if(!flag || !(flag&allowed) || (options->fields&flag)) return 0;
        options->fields|=flag;
        if(flag==OPT_CURSOR) { if(strlen(value)>=sizeof(options->cursor)) return 0; strcpy(options->cursor,value); }
        else if(flag==OPT_SEARCH) strcpy(options->search,value);
        else if(flag==OPT_REPO) { apr_uuid_t uuid; if(apr_uuid_parse(&uuid,value)!=APR_SUCCESS) return 0; apr_uuid_format(options->repository,&uuid); }
        else if(flag==OPT_METRIC) { if(strlen(value)>=sizeof(options->metric)) return 0; strcpy(options->metric,value); }
        else {
            unsigned long number; const char *p=value;
            if(!decimal(&p,flag==OPT_LIMIT ? 200 : 900,&number) || *p || !number) return 0;
            if(flag==OPT_LIMIT) options->limit=(int)number;
            if(flag==OPT_WINDOW) { if(number!=60 && number!=300 && number!=900) return 0; options->window=(int)number; }
            if(flag==OPT_STEP) { if(number!=5 && number!=15 && number!=30 && number!=60) return 0; options->step=(int)number; }
        }
        query_text=*end ? end+1 : end; if(*end && !*query_text) return 0;
    }
    return 1;
}
static int page_offset(api_options *options,unsigned long sequence)
{
    if(!(options->fields&OPT_CURSOR)) return 1;
    const char *p=options->cursor; unsigned long sample,offset;
    if(!decimal(&p,0xffffffffUL,&sample) || *p++!=':' || !decimal(&p,CACHED_REVISIONS,&offset) || *p) return 0;
    if(sample!=sequence) return -1;
    options->offset=(int)offset; return 1;
}
static unsigned char folded(unsigned char value) { return value>='A' && value<='Z' ? (unsigned char)(value-'A'+'a') : value; }
static int contains(const char *text,const char *term)
{
    if(!*term) return 1;
    for(;*text;++text) { const unsigned char *a=(const unsigned char *)text,*b=(const unsigned char *)term;
        while(*a && *b && folded(*a)==folded(*b)) { a++; b++; } if(!*b) return 1; }
    return 0;
}
static int row_matches(monitor_collection *collection,int index,const api_options *options)
{
    return (!*options->repository || !strcmp(options->repository,collection->ids[index])) &&
        (!*options->search || (collection->names && contains(collection->names[index],options->search)));
}
static void collection_json(svn_stringbuf_t *out,monitor_collection *collection,const api_options *options,unsigned long sequence,apr_pool_t *pool)
{
    int matched=0,returned=0,more=0;
    svn_stringbuf_appendcstr(out,"\"items\":[");
    for(int i=0;i<collection->count;++i) if(row_matches(collection,i,options)) {
        if(matched++<options->offset) continue;
        if(returned==options->limit) { more=1; break; }
        if(returned++) svn_stringbuf_appendbyte(out,','); svn_stringbuf_appendcstr(out,collection->items[i]);
    }
    append(out,pool,"],\"limit\":%d,\"cached_count\":%d,\"truncated\":%s,\"next_cursor\":",options->limit,collection->count,collection->truncated ? "true" : "false");
    if(more) append(out,pool,"\"%lu:%d\"",sequence,options->offset+returned); else svn_stringbuf_appendcstr(out,"null");
}
static void event_json(svn_stringbuf_t *out,const pgvn_monitor_event *event,apr_pool_t *pool)
{
    apr_time_exp_t stamp; apr_time_exp_gmt(&stamp,event->at);
    append(out,pool,"{\"sampled_at\":\"%04d-%02d-%02dT%02d:%02d:%02dZ\",\"source\":\"%s\",\"code\":\"%s\",\"operation\":\"%s\",\"error_code\":%ld,"
        "\"repository_id\":\"%s\",\"request_id\":\"%s\",\"sequence\":%llu}",stamp.tm_year+1900,stamp.tm_mon+1,stamp.tm_mday,stamp.tm_hour,stamp.tm_min,stamp.tm_sec,
        event->source,event->code,event->operation,(long)event->error_code,event->repository_id,event->request_id,event->sequence);
}
static int decimal64(const char *text,unsigned long long *result)
{
    if(!*text) return 0; *result=0;
    for(;*text;++text) { if(*text<'0' || *text>'9' || *result>(~0ULL-(unsigned)(*text-'0'))/10) return 0; *result=*result*10+(unsigned)(*text-'0'); }
    return 1;
}
static int events_json(svn_stringbuf_t *out,const api_options *options,apr_pool_t *pool)
{
    pgvn_event_snapshot *snapshot=apr_palloc(pool,sizeof(*snapshot)); pgvn_events_snapshot(snapshot);
    unsigned long long before=~0ULL;
    if(options->fields&OPT_CURSOR) {
        if(options->cursor[0]!='e' || !decimal64(options->cursor+1,&before)) return 400;
        if(snapshot->count && (before<=snapshot->events[0].sequence || before>snapshot->events[snapshot->count-1].sequence+1)) return 409;
    }
    unsigned returned=0; unsigned long long last=0; int more=0;
    svn_stringbuf_appendcstr(out,"\"event_source\":\"process\",\"items\":[");
    for(unsigned i=snapshot->count;i>0;--i) {
        pgvn_monitor_event *event=&snapshot->events[i-1];
        if(event->sequence>=before || (*options->repository && strcmp(options->repository,event->repository_id))) continue;
        if(returned==(unsigned)options->limit) { more=1; break; }
        if(returned++) svn_stringbuf_appendbyte(out,','); event_json(out,event,pool); last=event->sequence;
    }
    append(out,pool,"],\"limit\":%d,\"capacity\":200,\"next_cursor\":",options->limit);
    if(more) append(out,pool,"\"e%llu\"",last); else svn_stringbuf_appendcstr(out,"null");
    return 200;
}
static int series_metric(const monitor_trend *point,const char *metric,double *value)
{
    if(!strcmp(metric,"commands_per_second")) { *value=point->rate; return point->rate_valid; }
    if(!strcmp(metric,"error_percent")) { *value=point->error_percent; return point->activity_valid; }
    if(!strcmp(metric,"mean_latency_ms")) { *value=point->latency_ms; return point->activity_valid; }
    if(!strcmp(metric,"cpu_percent")) { *value=point->cpu_percent; return point->cpu_valid; }
    if(!strcmp(metric,"working_set_bytes")) { *value=(double)point->working_set; return point->resource_valid; }
    if(!strcmp(metric,"private_bytes")) { *value=(double)point->private_bytes; return point->resource_valid; }
    if(!strcmp(metric,"bytes_received")) { *value=(double)point->bytes_received; return 1; }
    if(!strcmp(metric,"bytes_sent")) { *value=(double)point->bytes_sent; return 1; }
    if(!strcmp(metric,"sql_calls")) { *value=(double)point->sql_calls; return 1; }
    if(!strcmp(metric,"sampler_ms")) { *value=point->database_ms; return 1; }
    return -1;
}
static int series_json(svn_stringbuf_t *out,pgvn_monitor_data *data,const api_options *options,apr_pool_t *pool)
{
    monitor_trend zero={0}; double probe;
    if(series_metric(&zero,options->metric,&probe)<0) return 400;
    ULONGLONG now=GetTickCount64(); int last_bucket=-1,count=0,emitted=0; double sum=0,last_value=0; const char *stamp=NULL;
    int cumulative=!strcmp(options->metric,"bytes_received") || !strcmp(options->metric,"bytes_sent") || !strcmp(options->metric,"sql_calls");
    append(out,pool,"\"series_source\":\"process\",\"metric\":\"%s\",\"window_seconds\":%d,\"step_seconds\":%d,\"aggregation\":\"%s\",\"items\":[",options->metric,options->window,options->step,cumulative ? "last" : "mean");
    for(unsigned i=0;i<=data->trend_count;++i) {
        monitor_trend *point=i<data->trend_count ? &data->trends[(data->trend_next+180-data->trend_count+i)%180] : NULL;
        if(point && now-point->tick>(ULONGLONG)options->window*1000) continue;
        int bucket=point ? (int)((now-point->tick)/(1000*(unsigned)options->step)) : -2;
        if(stamp && bucket!=last_bucket) {
            append(out,pool,"%s{\"sampled_at\":\"%s\",\"value\":",emitted++ ? "," : "",stamp);
            if(count) append(out,pool,"%.6f",cumulative ? last_value : sum/count); else svn_stringbuf_appendcstr(out,"null");
            svn_stringbuf_appendbyte(out,'}'); stamp=NULL; count=0; sum=0;
        }
        if(point) { double value; int valid=series_metric(point,options->metric,&value); stamp=point->at; last_bucket=bucket;
            if(valid>0) { sum+=value; last_value=value; count++; } }
    }
    svn_stringbuf_appendcstr(out,"],\"next_cursor\":null"); return 200;
}
const char *pgvn_monitor_api(pgvn_monitor_data *data,const char *path,const char *query_text,int *status,apr_pool_t *pool)
{
    const char *name=path+strlen("/api/monitor/v1/");
    if(!strcmp(name,"repos")) name="repositories";
    int detail=!strncmp(name,"repos/",6);
    if(detail) { apr_uuid_t uuid; if(strlen(name+6)!=36 || apr_uuid_parse(&uuid,name+6)!=APR_SUCCESS) { *status=400; return "invalid_repository_id"; } }
    int collection=-1; for(int i=0;i<4;++i) if(!strcmp(name,collection_names[i])) collection=i;
    int series=!strcmp(name,"series"),events=!strcmp(name,"events");
    if(collection<0 && !detail && !series && !events && strcmp(name,"overview") && strcmp(name,"trends") && strcmp(name,"requests")) { *status=404; return "not_found"; }
    unsigned allowed=detail || !strcmp(name,"overview") || !strcmp(name,"requests") ? 0 : collection>=0 ? OPT_LIMIT|OPT_CURSOR : events ? OPT_LIMIT|OPT_CURSOR|OPT_REPO : series ? OPT_METRIC|OPT_WINDOW|OPT_STEP : 0;
    if(collection==0) allowed|=OPT_SEARCH; if(collection==2 || collection==3) allowed|=OPT_REPO;
    api_options options;
    if(!parse_options(&options,query_text,allowed)) { *status=400; return "invalid_query"; }
    svn_stringbuf_t *out=svn_stringbuf_create("{",pool);
    AcquireSRWLockShared(&data->lock);
    if(collection>=0) { int parsed=page_offset(&options,data->sequence); if(parsed!=1) { ReleaseSRWLockShared(&data->lock); *status=parsed<0 ? 409 : 400; return parsed<0 ? "cursor_expired" : "invalid_query"; } }
    metadata(out,data,pool);
    if(!strcmp(name,"overview")) {
        int ready=pgvn_monitor_live(data) && data->sample.valid && data->last_attempt_ok && data->writer_ok && data->sample.configuration_healthy && GetTickCount64()-data->sample.tick<=15000
            && InterlockedCompareExchange(&data->business_ready,0,0);
        append(out,pool,",\"ready\":%s,\"sampler_milliseconds\":%.3f,\"sample_failures\":%u,\"sampler_sql_calls\":%lld,\"sampler_sql_errors\":%lld,\"dropped_telemetry_batches\":%lld,\"database\":",
            ready ? "true" : "false",data->sampler_ms,data->sample_failures,(long long)InterlockedCompareExchange64(&data->sampler_queries,0,0),
            (long long)InterlockedCompareExchange64(&data->sampler_query_errors,0,0),(long long)data->dropped_batches);
        database_json(out,data,pool); svn_stringbuf_appendcstr(out,",\"process\":"); process_json(out,data,pool);
    } else if(collection>=0) { svn_stringbuf_appendbyte(out,','); collection_json(out,&data->sample.collections[collection],&options,data->sequence,pool); }
    else if(detail) {
        apr_uuid_t uuid; apr_uuid_parse(&uuid,name+6); char id[37]; apr_uuid_format(id,&uuid);
        monitor_collection *repos=&data->sample.collections[0]; int found=-1;
        for(int i=0;i<repos->count;++i) if(!strcmp(repos->ids[i],id)) { found=i; break; }
        if(found<0) { ReleaseSRWLockShared(&data->lock); *status=repos->truncated || !data->sample.valid ? 503 : 404; return *status==503 ? "repository_not_in_snapshot" : "not_found"; }
        append(out,pool,",\"repository\":%s",repos->items[found]); strcpy(options.repository,id);
        const char *keys[]={"transactions","locks","recent_revisions"}; const int indices[]={2,4,5};
        for(int c=0;c<3;++c) { append(out,pool,",\"%s\":{",keys[c]); collection_json(out,&data->sample.collections[indices[c]],&options,data->sequence,pool); svn_stringbuf_appendbyte(out,'}'); }
        svn_stringbuf_appendcstr(out,",\"recent_errors\":{"); int code=events_json(out,&options,pool); (void)code; svn_stringbuf_appendbyte(out,'}');
    } else if(series) {
        svn_stringbuf_appendbyte(out,','); int code=series_json(out,data,&options,pool);
        if(code!=200) { ReleaseSRWLockShared(&data->lock); *status=400; return "invalid_query"; }
    } else if(!strcmp(name,"trends")) {
        svn_stringbuf_appendcstr(out,",\"window_seconds\":900,\"items\":[");
        int emitted=0; ULONGLONG now=GetTickCount64();
        for(unsigned i=0;i<data->trend_count;++i) {
            monitor_trend *point=&data->trends[(data->trend_next+180-data->trend_count+i)%180];
            if(now-point->tick>900000) continue;
            append(out,pool,"%s{\"sampled_at\":\"%s\",\"commands\":%lld,\"errors\":%lld,\"sql_calls\":%lld,\"database_available\":%s,\"sampler_milliseconds\":%.3f,\"commands_per_second\":",
                emitted++ ? "," : "",point->at,(long long)point->commands,(long long)point->errors,(long long)point->sql_calls,point->database_available ? "true" : "false",point->database_ms);
            if(point->rate_valid) append(out,pool,"%.6f",point->rate); else svn_stringbuf_appendcstr(out,"null");
            svn_stringbuf_appendcstr(out,",\"error_percent\":"); if(point->activity_valid) append(out,pool,"%.6f",point->error_percent); else svn_stringbuf_appendcstr(out,"null");
            svn_stringbuf_appendcstr(out,",\"mean_latency_ms\":"); if(point->activity_valid) append(out,pool,"%.6f",point->latency_ms); else svn_stringbuf_appendcstr(out,"null");
            svn_stringbuf_appendbyte(out,'}');
        }
        svn_stringbuf_appendcstr(out,"],\"next_cursor\":null");
    } else if(!strcmp(name,"requests")) {
        pgvn_process_metrics metrics; pgvn_process_snapshot(&metrics); svn_stringbuf_appendcstr(out,",\"counter_source\":\"process\",\"items\":[");
        for(int i=0;i<PGVN_OPERATION_COUNT;++i) append(out,pool,"%s{\"operation\":\"%s\",\"success\":%lld,\"denied\":%lld,\"error\":%lld}",i ? "," : "",pgvn_operation_name(i),(long long)metrics.commands[i][0],(long long)metrics.commands[i][1],(long long)metrics.commands[i][2]);
        svn_stringbuf_appendcstr(out,"],\"next_cursor\":null");
    } else {
        svn_stringbuf_appendbyte(out,','); int code=events_json(out,&options,pool);
        if(code!=200) { ReleaseSRWLockShared(&data->lock); *status=code; return code==409 ? "cursor_expired" : "invalid_query"; }
    }
    svn_stringbuf_appendcstr(out,"}\n"); ReleaseSRWLockShared(&data->lock); *status=200; return out->data;
}
static void metric_type(svn_stringbuf_t *out,const char *name,const char *type,const char *help,apr_pool_t *pool)
{
    append(out,pool,"# HELP %s %s\n# TYPE %s %s\n",name,help,name,type);
}
const char *pgvn_monitor_metrics(pgvn_monitor_data *data,apr_pool_t *pool)
{
    pgvn_process_metrics m; pgvn_process_snapshot(&m); svn_stringbuf_t *out=svn_stringbuf_create_empty(pool);
    metric_type(out,"pgvn_commands_total","counter","Completed top-level SVN commands by operation and result.",pool);
    for(int i=0;i<PGVN_OPERATION_COUNT;++i) for(int r=0;r<PGVN_RESULT_COUNT;++r)
        append(out,pool,"pgvn_commands_total{component=\"svn\",instance_id=\"%s\",operation=\"%s\",result=\"%s\"} %lld\n",data->instance,pgvn_operation_name(i),pgvn_result_name(r),(long long)m.commands[i][r]);
    metric_type(out,"pgvn_published_revisions_total","counter","New revisions confirmed by this process.",pool);
    append(out,pool,"pgvn_published_revisions_total{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.published_revisions);
    metric_type(out,"pgvn_receipt_confirmations_total","counter","Confirmed transaction receipts, distinct from new revisions.",pool);
    append(out,pool,"pgvn_receipt_confirmations_total{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.receipt_confirmations);
    metric_type(out,"pgvn_sql_calls_total","counter","Completed business libpq execution calls, excluding sampler queries.",pool);
    append(out,pool,"pgvn_sql_calls_total{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.sql_calls);
    metric_type(out,"pgvn_sql_errors_total","counter","Failed business libpq calls.",pool);
    append(out,pool,"pgvn_sql_errors_total{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.sql_errors);
    metric_type(out,"pgvn_sessions_active","gauge","Active SVN transport sessions.",pool);
    append(out,pool,"pgvn_sessions_active{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.sessions_active);
    metric_type(out,"pgvn_commands_active","gauge","SVN commands in progress.",pool);
    append(out,pool,"pgvn_commands_active{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.commands_active);
    metric_type(out,"pgvn_sql_active","gauge","Business libpq execution calls in progress.",pool);
    append(out,pool,"pgvn_sql_active{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.sql_active);
    metric_type(out,"pgvn_connections_leased","gauge","Business connection leases, including connection attempts.",pool);
    append(out,pool,"pgvn_connections_leased{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.connections_active);
    metric_type(out,"pgvn_control_waiters","gauge","Threads acquiring the business control mutex.",pool);
    append(out,pool,"pgvn_control_waiters{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.control_waiters);
    metric_type(out,"pgvn_writers_active","gauge","Open SVN file text write streams.",pool);
    append(out,pool,"pgvn_writers_active{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.writers_active);
    metric_type(out,"pgvn_connection_rejections_total","counter","Connection lease capacity rejections.",pool);
    append(out,pool,"pgvn_connection_rejections_total{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.connection_rejections);
    metric_type(out,"pgvn_connection_failures_total","counter","Failed business database connection attempts.",pool);
    append(out,pool,"pgvn_connection_failures_total{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.connection_failures);
    metric_type(out,"pgvn_monitor_requests_total","counter","Completed monitor HTTP requests.",pool);
    append(out,pool,"pgvn_monitor_requests_total{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.monitor_requests);
    metric_type(out,"pgvn_monitor_errors_total","counter","Monitor HTTP error responses or cancelled sends.",pool);
    append(out,pool,"pgvn_monitor_errors_total{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.monitor_errors);
    metric_type(out,"pgvn_monitor_requests_active","gauge","Monitor HTTP requests in progress.",pool);
    append(out,pool,"pgvn_monitor_requests_active{instance_id=\"%s\"} %lld\n",data->instance,(long long)m.monitor_active);
    metric_type(out,"pgvn_svn_bytes_total","counter","Bytes transferred by the RA SVN socket callbacks, including protocol framing.",pool);
    append(out,pool,"pgvn_svn_bytes_total{instance_id=\"%s\",direction=\"received\"} %lld\npgvn_svn_bytes_total{instance_id=\"%s\",direction=\"sent\"} %lld\n",data->instance,(long long)m.bytes_received,data->instance,(long long)m.bytes_sent);
    metric_type(out,"pgvn_sql_results_total","counter","Business libpq call results by bounded SQLSTATE class.",pool);
    for(int i=0;i<PGVN_SQL_CLASS_COUNT;++i) append(out,pool,"pgvn_sql_results_total{instance_id=\"%s\",sqlstate_class=\"%s\"} %lld\n",data->instance,pgvn_sql_class_name(i),(long long)m.sql_classes[i]);
    static const char *bounds[]={"0.005","0.02","0.1","0.5","2","+Inf"};
    const char *names[]={"pgvn_command_duration_seconds","pgvn_sql_duration_seconds"};
    const LONG64 *counts[]={m.latency,m.sql_latency};
    for(int n=0;n<2;++n) {
        metric_type(out,names[n],"histogram",n==0 ? "Completed SVN command elapsed time." : "Completed business libpq call elapsed time.",pool);
        for(int i=0;i<PGVN_BUCKET_COUNT;++i) append(out,pool,"%s_bucket{instance_id=\"%s\",le=\"%s\"} %lld\n",names[n],data->instance,bounds[i],(long long)counts[n][i]);
        append(out,pool,"%s_sum{instance_id=\"%s\"} %.6f\n%s_count{instance_id=\"%s\"} %lld\n",names[n],data->instance,(n==0 ? m.command_microseconds : m.sql_microseconds)/1000000.0,names[n],data->instance,(long long)(n==0 ? total_commands(&m,0) : m.sql_calls));
    }
    AcquireSRWLockShared(&data->lock);
    metric_type(out,"pgvn_sampler_sql_calls_total","counter","Completed or attempted sampler libpq calls, separate from business calls.",pool);
    append(out,pool,"pgvn_sampler_sql_calls_total{instance_id=\"%s\"} %lld\n",data->instance,(long long)InterlockedCompareExchange64(&data->sampler_queries,0,0));
    metric_type(out,"pgvn_dropped_telemetry_batches_total","counter","Telemetry publication batches that failed.",pool);
    append(out,pool,"pgvn_dropped_telemetry_batches_total{instance_id=\"%s\"} %lld\n",data->instance,(long long)data->dropped_batches);
    if(data->last_cpu_tick) {
        metric_type(out,"pgvn_process_cpu_seconds_total","counter","Process kernel and user CPU seconds from Windows.",pool);
        append(out,pool,"pgvn_process_cpu_seconds_total{instance_id=\"%s\"} %.6f\n",data->instance,data->cpu_ticks/10000000.0);
    }
    if(data->resource_valid) {
        metric_type(out,"pgvn_process_working_set_bytes","gauge","Windows working set bytes, sampled independently of database success.",pool);
        append(out,pool,"pgvn_process_working_set_bytes{instance_id=\"%s\"} %llu\n",data->instance,data->working_set);
        metric_type(out,"pgvn_process_private_bytes","gauge","Windows process private committed bytes.",pool);
        append(out,pool,"pgvn_process_private_bytes{instance_id=\"%s\"} %llu\n",data->instance,data->private_bytes);
    }
    if(data->sample.valid) {
        metric_type(out,"pgvn_database_sample_age_seconds","gauge","Age of the last successful database sample.",pool);
        append(out,pool,"pgvn_database_sample_age_seconds{instance_id=\"%s\"} %.3f\n",data->instance,(GetTickCount64()-data->sample.tick)/1000.0);
    }
    ReleaseSRWLockShared(&data->lock);
    metric_type(out,"pgvn_ready","gauge","One when the local business listener and validated dependencies are ready.",pool);
    append(out,pool,"pgvn_ready{instance_id=\"%s\"} %d\n",data->instance,pgvn_monitor_ready(data));
    return out->data;
}
