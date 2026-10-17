#include "pgvn_platform.h"
#include <string.h>

static int origin_ok(const pgvn_http_input *input)
{
    if (!input->origin_count) return 1;
    if (input->origin_count != 1 || !input->origin || !input->expected_origin) return 0;
    return !strcmp(input->origin, input->expected_origin);
}

static int route_of(const char *path, int *route)
{
    if (!path) return 0;
    if (!strcmp(path, "/health/live")) { *route = PGVN_HTTP_LIVE; return 1; }
    if (!strcmp(path, "/health/ready")) { *route = PGVN_HTTP_READY; return 1; }
    if (!strcmp(path, "/") || !strcmp(path, "/monitor/")) { *route = PGVN_HTTP_INDEX; return 1; }
    if (!strcmp(path, "/monitor/monitor.css")) { *route = PGVN_HTTP_STYLE; return 1; }
    if (!strcmp(path, "/monitor/monitor.js")) { *route = PGVN_HTTP_SCRIPT; return 1; }
    if (!strcmp(path, "/metrics")) { *route = PGVN_HTTP_METRICS; return 1; }
    if (!strncmp(path, "/api/monitor/v1/", 16)) { *route = PGVN_HTTP_API; return 1; }
    return 0;
}

void pgvn_http_decide(const pgvn_http_input *input, pgvn_http_decision *decision)
{
    int route = PGVN_HTTP_NONE;
    decision->status = 500;
    decision->error_code = "internal";
    decision->route = PGVN_HTTP_NONE;
    if (!input) return;
    if (input->headers_too_large) {
        decision->status = 431;
        decision->error_code = "headers_too_large";
        return;
    }
    if (!input->method_is_get) {
        decision->status = 405;
        decision->error_code = "read_only";
        return;
    }
    if (input->has_body) {
        decision->status = 413;
        decision->error_code = "request_body_not_supported";
        return;
    }
    if (!input->path_valid || !route_of(input->path, &route)) {
        if (!input->path_valid) {
            decision->status = 400;
            decision->error_code = "invalid_url";
            return;
        }
        decision->status = 404;
        decision->error_code = "not_found";
        return;
    }
    if (route == PGVN_HTTP_API || route == PGVN_HTTP_METRICS) {
        if (!origin_ok(input)) {
            decision->status = 403;
            decision->error_code = "origin_denied";
            return;
        }
        if (!input->authorized) {
            decision->status = 401;
            decision->error_code = "authentication_required";
            return;
        }
        if (route == PGVN_HTTP_METRICS && input->query_present) {
            decision->status = 400;
            decision->error_code = "invalid_query";
            return;
        }
    }
    decision->status = 200;
    decision->error_code = NULL;
    decision->route = route;
}
