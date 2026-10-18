#ifndef PGVN_MONITOR_DATA_H
#define PGVN_MONITOR_DATA_H
#ifdef _WIN32
#include <winsock2.h>
#include <windows.h>
#include <stdint.h>
#include "../platform/platform.h"
typedef DWORD pgvn_monitor_status;
#else
#include <stdint.h>
#include "../platform/platform.h"
typedef int pgvn_monitor_status;
#endif
#include "pgvn.h"
typedef struct pgvn_monitor_data pgvn_monitor_data;
pgvn_monitor_status pgvn_monitor_data_start(pgvn_monitor_data **data,const char *configuration);
void pgvn_monitor_data_stop(pgvn_monitor_data *data);
void pgvn_monitor_business(pgvn_monitor_data *data,int ready);
void pgvn_monitor_tick(pgvn_monitor_data *data);
int pgvn_monitor_live(pgvn_monitor_data *data);
int pgvn_monitor_ready(pgvn_monitor_data *data);
int pgvn_monitor_authorize(pgvn_monitor_data *data,const char *header,size_t length);
const char *pgvn_monitor_api(pgvn_monitor_data *data,const char *path,const char *query,
                           int *status,apr_pool_t *pool);
const char *pgvn_monitor_metrics(pgvn_monitor_data *data,apr_pool_t *pool);
#endif
