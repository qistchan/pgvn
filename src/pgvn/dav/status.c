#include "../platform/runtime.h"
#include "../platform/credentials.h"
#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <sys/select.h>
#include <unistd.h>
#endif
#include <libpq-fe.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <apr_uuid.h>
#include "httpd.h"
#include "http_config.h"
#include "http_protocol.h"
#include "http_request.h"
#include "http_log.h"
#include "mod_dav.h"
#include "svn_config.h"
#include "svn_pools.h"
#include "svn_time.h"
#include "telemetry.h"
#include "status.h"

#define DAV_METHOD_COUNT 17
#define RECENT_ERRORS 5
static const char *methods[DAV_METHOD_COUNT]={"OPTIONS","GET","HEAD","PROPFIND","REPORT","POST","MKACTIVITY",
    "CHECKOUT","PROPPATCH","PUT","COPY","MERGE","DELETE","MKCOL","LOCK","UNLOCK","other"};
typedef struct request_error { char id[32];int status;apr_time_t at; } request_error;
typedef struct dav_status {
    pgvn_rwlock lock;pgvn_cancel_event stop; pgvn_thread thread;const char *dsn,*node,*started;
    char instance[37];volatile pgvn_atomic32 initialized;volatile pgvn_atomic64 generation,sequence;
    pgvn_atomic64 counts[DAV_METHOD_COUNT],errors[DAV_METHOD_COUNT],durations[DAV_METHOD_COUNT],latency[6],cancelled;
    pgvn_atomic32 active;request_error recent[RECENT_ERRORS];unsigned error_next,error_count;
    unsigned long long telemetry_queries,telemetry_failures;
} dav_status;
typedef struct request_status { dav_status *data;volatile pgvn_atomic32 finished;int method;apr_time_t start;char id[32]; } request_status;
static dav_status *worker;

void pgvn_dav_status_generation(apr_int64_t generation)
{
    if(!worker) return;
    pgvn_atomic64 before=pgvn_compare64(&worker->generation,0,0);
    while(generation>before) { pgvn_atomic64 actual=pgvn_compare64(&worker->generation,generation,before);
        if(actual==before) break;before=actual; }
}
void pgvn_dav_status_initialized(void) { if(worker) pgvn_exchange32(&worker->initialized,1); }

static void finish(request_status *request,int status)
{
    if(pgvn_compare32(&request->finished,1,0)) return;
    dav_status *data=request->data;apr_time_t now=apr_time_now();apr_interval_time_t elapsed=now-request->start;
    if(elapsed<0) elapsed=0;
    pgvn_write_lock(&data->lock);
    data->active--;data->counts[request->method]++;data->durations[request->method]+=elapsed;
    static const apr_interval_time_t limits[]={5000,20000,100000,500000,2000000};
    for(int i=0;i<6;++i) if(i==5 || elapsed<=limits[i]) data->latency[i]++;
    if(!status || status>=400) {
        data->errors[request->method]++;if(!status) data->cancelled++;
        request_error *error=&data->recent[data->error_next];
        snprintf(error->id,sizeof(error->id),"%s",request->id);error->status=status;error->at=now;
        data->error_next=(data->error_next+1)%RECENT_ERRORS;if(data->error_count<RECENT_ERRORS) data->error_count++;
    }
    pgvn_write_unlock(&data->lock);
    if(!status || status>=400) pgvn_telemetry_event("dav",!status ? "request_cancelled" : status>=500 ? "http_5xx" : "http_4xx",status);
    pgvn_telemetry_scope(NULL);
}
static apr_status_t cancelled(void *baton) { finish(baton,0);return APR_SUCCESS; }
static int begin_request(request_rec *r)
{
    const char *provider=dav_get_provider_name(r);
    if(!worker || r->main || r->prev || !provider || strcmp(provider,"pgvn")) return DECLINED;
    void *existing=NULL;apr_pool_userdata_get(&existing,"pgvn-dav-request",r->pool);
    if(existing) return DECLINED;
    request_status *request=apr_pcalloc(r->pool,sizeof(*request));request->data=worker;request->start=apr_time_now();request->method=DAV_METHOD_COUNT-1;
    for(int i=0;i<DAV_METHOD_COUNT-1;++i) if(!strcmp(r->method,methods[i])) { request->method=i;break; }
    snprintf(request->id,sizeof(request->id),"%.8s-%llu",worker->instance,(unsigned long long)pgvn_atomic_increment64(&worker->sequence));
    pgvn_telemetry_scope(request->id);
    pgvn_write_lock(&worker->lock);worker->active++;pgvn_write_unlock(&worker->lock);
    apr_pool_userdata_setn(request,"pgvn-dav-request",NULL,r->pool);
    apr_pool_cleanup_register(r->pool,request,cancelled,apr_pool_cleanup_null);
    apr_table_setn(r->err_headers_out,"X-PgVN-Request-Id",request->id);
    return DECLINED;
}
void pgvn_dav_status_request(request_rec *request) { (void)begin_request(request); }
static int end_request(request_rec *r)
{
    void *value=NULL;apr_pool_userdata_get(&value,"pgvn-dav-request",r->pool);
    if(value) finish(value,r->connection->aborted ? 0 : r->status);
    return DECLINED;
}
void pgvn_dav_status_hooks(apr_pool_t *pool)
{
    ap_hook_header_parser(begin_request,NULL,NULL,APR_HOOK_FIRST);
    ap_hook_log_transaction(end_request,NULL,NULL,APR_HOOK_LAST);
}

static int poll_socket(PGconn *connection,int write,pgvn_cancel_event stop,uint64_t deadline,int final)
{
    if((!final && pgvn_wait_stop(stop,0)) || pgvn_tick()>=deadline) return 0;
    return pgvn_socket_poll(PQsocket(connection),write,50)>=0;
}
static PGconn *connect_writer(dav_status *data)
{
    const char *keys[]={"dbname","connect_timeout","application_name","options",NULL};
    const char *values[]={data->dsn,"3","pgvn-dav-telemetry","-c statement_timeout=1000 -c lock_timeout=500",NULL};
    PGconn *connection=PQconnectStartParams(keys,values,1);
    if(!connection) return NULL;
    uint64_t deadline=pgvn_tick()+3000;
    for(;;) {
        PostgresPollingStatusType state=PQconnectPoll(connection);
        if(state==PGRES_POLLING_OK) { if(PQsetnonblocking(connection,1)==0) return connection;break; }
        if(state==PGRES_POLLING_FAILED || !poll_socket(connection,state==PGRES_POLLING_WRITING,data->stop,deadline,0)) break;
    }
    PQfinish(connection);return NULL;
}
static PGresult *query(PGconn *connection,dav_status *data,const char *sql,int count,const char *const *values,int final)
{
    data->telemetry_queries++;
    if(!PQsendQueryParams(connection,sql,count,NULL,values,NULL,NULL,0)) return NULL;
    uint64_t deadline=pgvn_tick()+(final ? 200 : 2000);PGresult *result=NULL;
    for(;;) {
        if((!final && pgvn_wait_stop(data->stop,0)) || pgvn_tick()>=deadline) break;
        int pending=PQflush(connection);
        if(pending<0 || !PQconsumeInput(connection)) break;
        if(!pending && !PQisBusy(connection)) {
            PGresult *next=PQgetResult(connection);
            if(!next) {
                if(result && PQresultStatus(result)==PGRES_TUPLES_OK) return result;
                break;
            }
            if(result) PQclear(result);result=next;continue;
        }
        if(!poll_socket(connection,pending,data->stop,deadline,final)) break;
    }
    if(result) PQclear(result);return NULL;
}
static void append(char *body,size_t *length,const char *format,...)
{
    if(*length>=4096) return;
    va_list args;va_start(args,format);int size=vsnprintf(body+*length,4096-*length,format,args);va_end(args);
    if(size<0 || (size_t)size>=4096-*length) { *length=4096;return; }
    *length+=(size_t)size;
}
static int publish(PGconn *connection,dav_status *data,int ready,int final)
{
    pgvn_process_metrics core;pgvn_process_snapshot(&core);
    char body[4096],pid[24];size_t length=0;pgvn_atomic64 total=0,errors=0,duration=0;
    pgvn_read_lock(&data->lock);
    for(int i=0;i<DAV_METHOD_COUNT;++i) { total+=data->counts[i];errors+=data->errors[i];duration+=data->durations[i]; }
    append(body,&length,"{\"ready\":%s,\"ready_basis\":\"catalog_primary_probe\",\"stopped\":%s,\"requests\":%lld,\"request_errors\":%lld,"
        "\"requests_active\":%ld,\"request_microseconds\":%lld,\"cancelled\":%lld,\"methods\":[",
        ready && pgvn_compare32(&data->initialized,0,0) && !final ? "true" : "false",final ? "true" : "false",
        (long long)total,(long long)errors,(long)data->active,(long long)duration,(long long)data->cancelled);
    for(int i=0;i<DAV_METHOD_COUNT;++i) append(body,&length,"%s{\"method\":\"%s\",\"requests\":%lld,\"errors\":%lld,\"microseconds\":%lld}",
        i ? "," : "",methods[i],(long long)data->counts[i],(long long)data->errors[i],(long long)data->durations[i]);
    append(body,&length,"],\"request_latency_cumulative\":[");
    for(int i=0;i<6;++i) append(body,&length,"%s%lld",i ? "," : "",(long long)data->latency[i]);
    append(body,&length,"],\"recent_errors\":[");
    for(unsigned i=0;i<data->error_count;++i) {
        request_error *error=&data->recent[(data->error_next+RECENT_ERRORS-data->error_count+i)%RECENT_ERRORS];
        append(body,&length,"%s{\"request_id\":\"%s\",\"status\":%d,\"at_unix_microseconds\":%lld}",i ? "," : "",error->id,error->status,(long long)error->at);
    }
    pgvn_read_unlock(&data->lock);
    append(body,&length,"],\"connections\":%ld,\"writers_active\":%ld,\"sql_calls\":%lld,\"sql_errors\":%lld,\"sql_microseconds\":%lld,"
        "\"published_revisions\":%lld,\"receipt_confirmations\":%lld,\"configuration_generation\":%lld,\"telemetry_queries\":%llu,\"telemetry_failures\":%llu",
        (long)core.connections_active,(long)core.writers_active,(long long)core.sql_calls,(long long)core.sql_errors,(long long)core.sql_microseconds,
        (long long)core.published_revisions,(long long)core.receipt_confirmations,(long long)pgvn_compare64(&data->generation,0,0),
        data->telemetry_queries,data->telemetry_failures);
    uint64_t working_set=0,private_bytes=0,ticks=0; unsigned processors=0;
    if(pgvn_process_resources(&working_set,&private_bytes,&ticks,&processors))
        append(body,&length,",\"working_set_bytes\":%llu,\"private_bytes\":%llu",(unsigned long long)working_set,(unsigned long long)private_bytes);
    append(body,&length,"}");if(length>=4096) return 0;
    snprintf(pid,sizeof(pid),"%lu",(unsigned long)pgvn_process_id());
    const char *values[]={data->instance,data->node,"dav",PGVN_VERSION "/" PGVN_MILESTONE,pid,data->started,body};
    PGresult *result=query(connection,data,"SELECT pgvn_monitor.publish($1::uuid,$2,$3,$4,$5::integer,$6::timestamptz,$7::jsonb)",7,values,final);
    if(!result) return 0;PQclear(result);return 1;
}
static PGVN_THREAD_PROC sampler(void *baton)
{
    dav_status *data=baton;PGconn *connection=NULL;char version[16];
    snprintf(version,sizeof(version),"%d",PGVN_CATALOG_VERSION);const char *expected[]={version};
    while(!pgvn_wait_stop(data->stop,0)) {
        uint64_t start=pgvn_tick();
        if(!connection) connection=connect_writer(data);
        PGresult *result=connection ? query(connection,data,
            "SELECT NOT pg_is_in_recovery() AND current_setting('transaction_read_only')='off' "
            "AND (SELECT max(version) FROM pgvn.schema_versions)=$1::integer "
            "AND to_regclass('pgvn.dav_transactions') IS NOT NULL AND to_regclass('pgvn.dav_bindings') IS NOT NULL",1,expected,0) : NULL;
        int ready=result && PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t");if(result) PQclear(result);
        if(!connection || !ready || !publish(connection,data,ready,0)) {
            data->telemetry_failures++;if(connection) { PQfinish(connection);connection=NULL; }
        }
        uint64_t elapsed=pgvn_tick()-start;
        if(pgvn_wait_stop(data->stop,elapsed<5000 ? (uint32_t)(5000-elapsed) : 1)) break;
    }
    if(connection) { (void)publish(connection,data,0,1);PQfinish(connection); }
    return 0;
}
static apr_status_t stop(void *baton)
{
    dav_status *data=baton;pgvn_cancel_signal(data->stop);
    if(data->thread) { pgvn_thread_wait(data->thread,UINT32_MAX);pgvn_thread_close(data->thread); }
    pgvn_event_close(data->stop);pgvn_rwlock_destroy(&data->lock);return APR_SUCCESS;
}
void pgvn_dav_status_start(const char *configuration,apr_pool_t *pool,server_rec *server)
{
    worker=NULL;if(!configuration) return;
    svn_config_t *config;svn_error_t *error=svn_config_read3(&config,configuration,TRUE,TRUE,TRUE,pool);
    if(error) { svn_error_clear(error);ap_log_error(APLOG_MARK,APLOG_ERR,0,server,"PgVN DAV telemetry configuration unavailable");return; }
    dav_status *data=apr_pcalloc(pool,sizeof(*data));pgvn_rwlock_init(&data->lock);
    svn_config_get(config,&data->dsn,"pgvn","connection",NULL);svn_config_get(config,&data->node,"pgvn","node-id","dav");
    int valid=data->dsn && *data->dsn && *data->node && strlen(data->node)<=80;
    for(const unsigned char *p=(const unsigned char *)data->node;*p;++p)
        if(!((*p>='a' && *p<='z') || (*p>='A' && *p<='Z') || (*p>='0' && *p<='9') || strchr("_.:-",*p))) valid=0;
    if(!valid) { ap_log_error(APLOG_MARK,APLOG_ERR,0,server,"PgVN DAV telemetry requires a connection and a valid node-id");return; }
    apr_uuid_t id;apr_uuid_get(&id);apr_uuid_format(data->instance,&id);data->started=svn_time_to_cstring(apr_time_now(),pool);
    data->stop=pgvn_event_create();
    if(!data->stop) return;
    data->thread=pgvn_thread_create(sampler,data);
    if(!data->thread) { pgvn_event_close(data->stop);return; }
    worker=data;apr_pool_cleanup_register(pool,data,stop,apr_pool_cleanup_null);
}
