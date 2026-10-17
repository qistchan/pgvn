#ifndef PGVN_HTTP_POSIX_H
#define PGVN_HTTP_POSIX_H
#include "../platform/runtime.h"
#include "data.h"
typedef struct pgvn_monitor_connection pgvn_monitor_connection;
typedef struct pgvn_monitor {
    struct event_base *base;
    struct evhttp *http;
    struct evhttp_bound_socket *bound;
    struct event *timer;
    pgvn_thread thread;
    pgvn_cancel_event stop;
    pgvn_monitor_data *data;
    pgvn_monitor_connection *connections;
    unsigned active,responses;
    uint64_t sequence;
    char origin[96],authority[64];
} pgvn_monitor;
int pgvn_monitor_url_valid(const char *url);
int pgvn_monitor_start(pgvn_monitor *monitor,const char *url,const char *configuration);
void pgvn_monitor_stop(pgvn_monitor *monitor);
#endif
