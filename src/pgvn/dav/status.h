#ifndef PGVN_DAV_STATUS_H
#define PGVN_DAV_STATUS_H
#include "httpd.h"
void pgvn_dav_status_start(const char *configuration,apr_pool_t *pool,server_rec *server);
void pgvn_dav_status_hooks(apr_pool_t *pool);
void pgvn_dav_status_generation(apr_int64_t generation);
void pgvn_dav_status_initialized(void);
void pgvn_dav_status_request(request_rec *request);
#endif
