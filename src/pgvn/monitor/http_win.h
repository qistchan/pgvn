#ifndef PGVN_MONITOR_HTTP_WIN_H
#define PGVN_MONITOR_HTTP_WIN_H
#include <winsock2.h>
#include <windows.h>
#include <http.h>
#include "pgvn.h"
#include "data.h"

typedef struct pgvn_monitor pgvn_monitor;
typedef struct pgvn_monitor_worker {
    HANDLE thread;
    unsigned char *buffer;
    pgvn_monitor *owner;
    volatile LONG64 request_id;
} pgvn_monitor_worker;

struct pgvn_monitor {
    HANDLE queue;
    HTTP_SERVER_SESSION_ID session;
    HTTP_URL_GROUP_ID group;
    pgvn_monitor_worker workers[PGVN_MONITOR_WORKERS];
    int initialized;
    HANDLE stop;
    pgvn_monitor_data *data;
    char origin[96];
    volatile LONG64 request_sequence;
};

ULONG pgvn_monitor_start(pgvn_monitor *monitor, const wchar_t *url,const char *configuration);
void pgvn_monitor_stop(pgvn_monitor *monitor);
int pgvn_monitor_url_valid(const wchar_t *url);
#endif
