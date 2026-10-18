#include <string.h>
#include <stdio.h>
#include "telemetry.h"
static pgvn_rwlock metrics_lock=PGVN_RWLOCK_INITIALIZER;
static pgvn_process_metrics totals;
static pgvn_monitor_event events[PGVN_MONITOR_EVENT_LIMIT];
static unsigned event_count,event_next;
static _Thread_local int encoded_failure;
static _Thread_local char command_repository[37],command_request[32];
static volatile pgvn_atomic32 telemetry_enabled=1;
static volatile pgvn_atomic64 request_sequence;
static unsigned long long event_sequence;
static const apr_interval_time_t bounds[]={5000,20000,100000,500000,2000000};
static const char *sql_classes[]={"00","08","22","23","25","40","42","53","55","57","other"};
void pgvn_telemetry_enable(int enabled) { pgvn_atomic_exchange32(&telemetry_enabled,enabled); }
#define DISABLED() (!pgvn_atomic_read32(&telemetry_enabled))
const char *pgvn_sql_class_name(int index) { return sql_classes[index]; }

static const char *operations[PGVN_OPERATION_COUNT]={
    "reparent","get-latest-rev","get-dated-rev","change-rev-prop","change-rev-prop2",
    "rev-proplist","rev-prop","commit","get-file","get-dir","update","switch",
    "status","diff","get-mergeinfo","log","check-path","stat","get-locations",
    "get-location-segments","get-file-revs","lock","lock-many","unlock","unlock-many",
    "get-lock","get-locks","replay","replay-range","get-deleted-rev","get-iprops","list","other"};
static void event_locked(const char *source,const char *code,const char *operation,apr_status_t error_code)
{
    pgvn_monitor_event *event=&events[event_next]; memset(event,0,sizeof(*event));
    event->at=apr_time_now(); event->error_code=error_code; event->sequence=++event_sequence;
    snprintf(event->repository_id,sizeof(event->repository_id),"%s",command_repository);
    snprintf(event->request_id,sizeof(event->request_id),"%s",command_request);
    snprintf(event->source,sizeof(event->source),"%s",source);
    snprintf(event->code,sizeof(event->code),"%s",code);
    snprintf(event->operation,sizeof(event->operation),"%s",operation);
    event_next=(event_next+1)%PGVN_MONITOR_EVENT_LIMIT;
    if(event_count<PGVN_MONITOR_EVENT_LIMIT) event_count++;
}
const char *pgvn_operation_name(int index) { return operations[index]; }
const char *pgvn_result_name(int index) { static const char *names[]={"success","denied","error"}; return names[index]; }
apr_time_t pgvn_command_start(void)
{
    encoded_failure=0; command_repository[0]=0;
    if(!DISABLED()) {
        snprintf(command_request,sizeof(command_request),"svn-%016llx",(unsigned long long)pgvn_atomic_increment64(&request_sequence));
        pgvn_rwlock_write_lock(&metrics_lock); totals.commands_active++; pgvn_rwlock_write_unlock(&metrics_lock);
    }
    return apr_time_now();
}
void pgvn_command_repository(const char *id) { snprintf(command_repository,sizeof(command_repository),"%s",id ? id : ""); }
void pgvn_telemetry_scope(const char *id)
{
    encoded_failure=0;command_repository[0]=0;
    snprintf(command_request,sizeof(command_request),"%s",id ? id : "");
}
void pgvn_command_failure(const svn_error_t *error) { encoded_failure=pgvn_error_exit_code(error)==PGVN_EXIT_DENIED ? 1 : 2; }
void pgvn_command_finish(const char *operation,apr_time_t start,const svn_error_t *error)
{
    if(DISABLED()) return;
    int index=PGVN_OPERATION_COUNT-1,result=error ? 2 : encoded_failure;
    for(int i=0;i<PGVN_OPERATION_COUNT-1;++i) if(!strcmp(operation,operations[i])) { index=i; break; }
    if(error && pgvn_error_exit_code(error)==PGVN_EXIT_DENIED) result=1;
    apr_interval_time_t elapsed=apr_time_now()-start; if(elapsed<0) elapsed=0;
    pgvn_rwlock_write_lock(&metrics_lock);
    totals.commands_active--;
    totals.commands[index][result]++; totals.command_microseconds+=elapsed;
    for(int i=0;i<PGVN_BUCKET_COUNT;++i) if(i==PGVN_BUCKET_COUNT-1 || elapsed<=bounds[i]) totals.latency[i]++;
    if(result) event_locked("svn",result==1 ? "command_denied" : "command_failed",operations[index],error ? error->apr_err : 0);
    pgvn_rwlock_write_unlock(&metrics_lock);
    command_request[0]=0; command_repository[0]=0;
}
void pgvn_telemetry_query(const char *state,apr_interval_time_t elapsed)
{
    if(DISABLED()) return;
    pgvn_rwlock_write_lock(&metrics_lock);
    totals.sql_calls++; totals.sql_errors+=strcmp(state,"00000")!=0; totals.sql_microseconds+=elapsed<0 ? 0 : elapsed;
    totals.sql_active--;
    int category=PGVN_SQL_CLASS_COUNT-1;
    for(int i=0;i<PGVN_SQL_CLASS_COUNT-1;++i) if(!strncmp(state,sql_classes[i],2)) { category=i; break; }
    totals.sql_classes[category]++;
    for(int i=0;i<PGVN_BUCKET_COUNT;++i) if(i==PGVN_BUCKET_COUNT-1 || elapsed<=bounds[i]) totals.sql_latency[i]++;
    if(strcmp(state,"00000")) event_locked("sql","query_failed","",0);
    pgvn_rwlock_write_unlock(&metrics_lock);
}
void pgvn_telemetry_publication(svn_boolean_t receipt)
{
    if(DISABLED()) return;
    pgvn_rwlock_write_lock(&metrics_lock);
    if(receipt) totals.receipt_confirmations++; else totals.published_revisions++;
    pgvn_rwlock_write_unlock(&metrics_lock);
}
void pgvn_telemetry_session(int change)
{
    if(DISABLED()) return;
    pgvn_rwlock_write_lock(&metrics_lock);
    if(change==1) { totals.sessions_active++; totals.sessions_started++; }
    else if(change==-1) totals.sessions_active--;
    else totals.sessions_rejected++;
    pgvn_rwlock_write_unlock(&metrics_lock);
}
void pgvn_telemetry_session_limit(unsigned limit)
{
    if(DISABLED()) return;
    pgvn_rwlock_write_lock(&metrics_lock);
    totals.sessions_limit=(pgvn_atomic32)limit;
    pgvn_rwlock_write_unlock(&metrics_lock);
}
void pgvn_process_snapshot(pgvn_process_metrics *result)
{
    pgvn_rwlock_read_lock(&metrics_lock); *result=totals; pgvn_rwlock_read_unlock(&metrics_lock);
}
void pgvn_telemetry_event(const char *source,const char *code,apr_status_t error_code)
{
    if(DISABLED()) return;
    pgvn_rwlock_write_lock(&metrics_lock); event_locked(source,code,"",error_code); pgvn_rwlock_write_unlock(&metrics_lock);
}
void pgvn_events_snapshot(pgvn_event_snapshot *result)
{
    pgvn_rwlock_read_lock(&metrics_lock); result->count=event_count;
    for(unsigned i=0;i<event_count;++i) result->events[i]=events[(event_next+PGVN_MONITOR_EVENT_LIMIT-event_count+i)%PGVN_MONITOR_EVENT_LIMIT];
    pgvn_rwlock_read_unlock(&metrics_lock);
}

void pgvn_telemetry_query_start(void)
{
    if(DISABLED()) return;
    pgvn_rwlock_write_lock(&metrics_lock); totals.sql_active++; pgvn_rwlock_write_unlock(&metrics_lock);
}
void pgvn_telemetry_connection(int change,int rejected,int failed)
{
    if(DISABLED()) return;
    pgvn_rwlock_write_lock(&metrics_lock); totals.connections_active+=change;
    totals.connection_rejections+=rejected; totals.connection_failures+=failed; pgvn_rwlock_write_unlock(&metrics_lock);
}
void pgvn_telemetry_waiter(int change)
{
    if(DISABLED()) return;
    pgvn_rwlock_write_lock(&metrics_lock); totals.control_waiters+=change; pgvn_rwlock_write_unlock(&metrics_lock);
}
void pgvn_telemetry_writer(int change)
{
    if(DISABLED()) return;
    pgvn_rwlock_write_lock(&metrics_lock); totals.writers_active+=change; pgvn_rwlock_write_unlock(&metrics_lock);
}
void pgvn_telemetry_io(apr_size_t received,apr_size_t sent)
{
    if(DISABLED()) return;
    pgvn_rwlock_write_lock(&metrics_lock); totals.bytes_received+=(pgvn_atomic64)received; totals.bytes_sent+=(pgvn_atomic64)sent; pgvn_rwlock_write_unlock(&metrics_lock);
}
apr_time_t pgvn_telemetry_http_start(void)
{
    if(!DISABLED()) { pgvn_rwlock_write_lock(&metrics_lock); totals.monitor_active++; pgvn_rwlock_write_unlock(&metrics_lock); }
    return apr_time_now();
}
void pgvn_telemetry_http_finish(apr_time_t start,int status,int sent,const char *request_id)
{
    if(DISABLED()) return;
    apr_interval_time_t elapsed=apr_time_now()-start;
    pgvn_rwlock_write_lock(&metrics_lock); totals.monitor_active--; totals.monitor_requests++;
    totals.monitor_microseconds+=elapsed<0 ? 0 : elapsed;
    if(status>=400 || !sent) {
        totals.monitor_errors++;
        event_locked("monitor",!sent ? "http_cancelled" : status>=500 ? "http_5xx" : "http_4xx","",status);
        pgvn_monitor_event *event=&events[(event_next+PGVN_MONITOR_EVENT_LIMIT-1)%PGVN_MONITOR_EVENT_LIMIT];
        snprintf(event->request_id,sizeof(event->request_id),"%s",request_id); event->repository_id[0]=0;
    }
    pgvn_rwlock_write_unlock(&metrics_lock);
}
