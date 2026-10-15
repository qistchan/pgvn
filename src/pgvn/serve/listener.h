#ifndef PGVN_LISTENER_H
#define PGVN_LISTENER_H
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include "pgvn.h"

typedef struct pgvn_listener pgvn_listener;
svn_error_t *pgvn_listener_start(pgvn_listener **listener,const char *configuration,HANDLE stop_event,apr_pool_t *pool);
void pgvn_listener_stop(pgvn_listener *listener,void (*progress)(void));
#endif
