#ifndef PGVN_TELEMETRY_H
#define PGVN_TELEMETRY_H
#include <windows.h>
#include "pgvn.h"
#define PGVN_OPERATION_COUNT 33
#define PGVN_RESULT_COUNT 3
#define PGVN_BUCKET_COUNT 6
#define PGVN_SQL_CLASS_COUNT 11
typedef struct pgvn_process_metrics {
    LONG64 commands[PGVN_OPERATION_COUNT][PGVN_RESULT_COUNT];
    LONG64 latency[PGVN_BUCKET_COUNT];
    LONG64 command_microseconds;
    LONG64 sql_calls,sql_errors,sql_microseconds;
    LONG64 published_revisions,receipt_confirmations;
    LONG64 sessions_started,sessions_rejected;
    LONG sessions_active;
    LONG commands_active,sql_active,connections_active,control_waiters,writers_active;
    LONG64 connection_rejections,connection_failures,bytes_received,bytes_sent;
    LONG64 sql_latency[PGVN_BUCKET_COUNT],sql_classes[PGVN_SQL_CLASS_COUNT];
    LONG64 monitor_requests,monitor_errors,monitor_microseconds;
    LONG monitor_active;
} pgvn_process_metrics;
typedef struct pgvn_monitor_event {
    apr_time_t at;
    char source[16],code[40],operation[32];
    apr_status_t error_code;
    unsigned long long sequence;
    char repository_id[37],request_id[32];
} pgvn_monitor_event;
typedef struct pgvn_event_snapshot {
    pgvn_monitor_event events[PGVN_MONITOR_EVENT_LIMIT];
    unsigned count;
} pgvn_event_snapshot;
const char *pgvn_operation_name(int index);
const char *pgvn_result_name(int index);
apr_time_t pgvn_command_start(void);
void pgvn_command_failure(const svn_error_t *error);
void pgvn_command_finish(const char *operation,apr_time_t start,const svn_error_t *error);
void pgvn_telemetry_query(const char *state,apr_interval_time_t elapsed);
void pgvn_telemetry_publication(svn_boolean_t receipt);
void pgvn_telemetry_session(int change);
void pgvn_process_snapshot(pgvn_process_metrics *result);
void pgvn_telemetry_event(const char *source,const char *code,apr_status_t error_code);
void pgvn_events_snapshot(pgvn_event_snapshot *result);
void pgvn_telemetry_enable(int enabled);
void pgvn_command_repository(const char *repository_id);
void pgvn_telemetry_query_start(void);
void pgvn_telemetry_connection(int change,int rejected,int failed);
void pgvn_telemetry_waiter(int change);
void pgvn_telemetry_writer(int change);
void pgvn_telemetry_io(apr_size_t received,apr_size_t sent);
apr_time_t pgvn_telemetry_http_start(void);
void pgvn_telemetry_http_finish(apr_time_t start,int status,int sent,const char *request_id);
const char *pgvn_sql_class_name(int index);
#endif
