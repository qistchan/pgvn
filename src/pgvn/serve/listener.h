#ifndef PGVN_LISTENER_H
#define PGVN_LISTENER_H
#include "../platform/runtime.h"
#include "pgvn.h"

typedef struct pgvn_listener pgvn_listener;
svn_error_t *pgvn_listener_start(pgvn_listener **listener,const char *configuration,pgvn_cancel_event stop_event,apr_pool_t *pool);
void pgvn_listener_stop(pgvn_listener *listener,void (*progress)(void));
#endif
