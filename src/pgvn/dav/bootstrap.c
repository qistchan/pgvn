#include <string.h>
#include "httpd.h"
#include "http_config.h"
#include "http_protocol.h"
#include "http_request.h"
#include "pgvn.h"

/* Shared ABI/build diagnostic handlers. Repository requests and authorization
 * are served by the native providers in the M5 build. */
static int bootstrap_handler(request_rec *request)
{
#ifdef PGVN_AUTHZ_BOOTSTRAP
    const char *handler = "pgvn-authz-bootstrap";
#else
    const char *handler = "pgvn-dav-bootstrap";
#endif
    if (!request->handler || strcmp(request->handler, handler)) return DECLINED;
    char json[PGVN_BUILD_JSON_SIZE];
    pgvn_build_json(json, sizeof(json));
    ap_set_content_type(request, "application/json");
    ap_rputs(json, request);
    return OK;
}

void pgvn_bootstrap_register_hooks(apr_pool_t *pool)
{
    (void)pool;
    ap_hook_handler(bootstrap_handler, NULL, NULL, APR_HOOK_MIDDLE);
}

#ifndef PGVN_REAL_DAV
#ifdef PGVN_AUTHZ_BOOTSTRAP
#define PGVN_MODULE authz_pgvn_module
#else
#define PGVN_MODULE dav_pgvn_module
#endif

module AP_MODULE_DECLARE_DATA PGVN_MODULE = {
    STANDARD20_MODULE_STUFF,
    NULL, NULL, NULL, NULL, NULL, pgvn_bootstrap_register_hooks, 0
};
#endif
