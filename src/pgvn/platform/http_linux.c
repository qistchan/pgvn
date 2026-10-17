#include "http_linux.h"
#include "pgvn_platform.h"
#include <errno.h>
#include <event2/buffer.h>
#include <event2/event.h>
#include <event2/http.h>
#include <event2/keyvalq_struct.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/queue.h>

#define PGVN_HTTP_HEADER_LIMIT 32768
static const char CSP[] =
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'";

typedef struct pgvn_http_server {
    struct event_base *base;
    struct evhttp *http;
    struct evhttp_bound_socket *bound;
    struct event *drain_timer;
    pgvn_token_store *tokens;
    struct evhttp_request *held;
    char *index_html, *style, *script;
    size_t index_len, style_len, script_len;
    char origin[96];
    int timeout_seconds, queue_limit, drain_ms, test_hooks;
    int inflight, draining;
    unsigned long long sequence;
} pgvn_http_server;

static int ascii_text(const char *text, size_t limit)
{
    size_t i, length;
    if (!text) return 0;
    length = strlen(text);
    if (length >= limit) return 0;
    for (i = 0; i < length; ++i) {
        unsigned char c = (unsigned char)text[i];
        if (c < 32 || c > 126) return 0;
    }
    return 1;
}

static int load_asset(const char *dir, const char *name, char **out, size_t *length)
{
    char path[4096];
    FILE *file;
    long size;
    *out = NULL;
    *length = 0;
    if (!dir || snprintf(path, sizeof(path), "%s/%s", dir, name) >= (int)sizeof(path)) return -1;
    file = fopen(path, "rb");
    if (!file) return -1;
    if (fseek(file, 0, SEEK_END) || (size = ftell(file)) < 0 || size > 262144 || fseek(file, 0, SEEK_SET)) {
        fclose(file);
        return -1;
    }
    *out = malloc((size_t)size + 1);
    if (!*out || (size && fread(*out, 1, (size_t)size, file) != (size_t)size)) {
        free(*out);
        *out = NULL;
        fclose(file);
        return -1;
    }
    (*out)[size] = 0;
    *length = (size_t)size;
    fclose(file);
    return 0;
}

static int load_tokens(pgvn_token_store *store, const char *path)
{
    FILE *file;
    char line[128];
    if (!path) return 0;
    file = fopen(path, "r");
    if (!file) return -1;
    while (fgets(line, sizeof(line), file)) {
        char hex[65];
        unsigned seconds = 0;
        if (line[0] == '#' || line[0] == '\n') continue;
        if (sscanf(line, "%64s %u", hex, &seconds) != 2 || pgvn_token_store_add(store, hex, seconds)) {
            fclose(file);
            return -1;
        }
    }
    fclose(file);
    return 0;
}

static const char *reason_for(int status)
{
    if (status == 200) return "OK";
    if (status == 400) return "Bad Request";
    if (status == 401) return "Unauthorized";
    if (status == 403) return "Forbidden";
    if (status == 404) return "Not Found";
    if (status == 405) return "Method Not Allowed";
    if (status == 409) return "Conflict";
    if (status == 413) return "Payload Too Large";
    if (status == 431) return "Request Header Fields Too Large";
    return "Service Unavailable";
}

static void on_reply_done(struct evhttp_request *req, void *arg)
{
    pgvn_http_server *server = arg;
    (void)req;
    if (server->draining && !server->inflight && server->base) event_base_loopexit(server->base, NULL);
}

static void finish(pgvn_http_server *server, struct evhttp_request *req, int status, const char *type, const char *body)
{
    struct evbuffer *buffer = evbuffer_new();
    struct evkeyvalq *headers = evhttp_request_get_output_headers(req);
    char request_id[40];
    snprintf(request_id, sizeof(request_id), "monitor-%016llx", server->sequence);
    evhttp_add_header(headers, "Content-Type", type);
    evhttp_add_header(headers, "Cache-Control", "no-store");
    evhttp_add_header(headers, "X-Content-Type-Options", "nosniff");
    evhttp_add_header(headers, "Content-Security-Policy", CSP);
    evhttp_add_header(headers, "Referrer-Policy", "no-referrer");
    evhttp_add_header(headers, "X-Frame-Options", "DENY");
    evhttp_add_header(headers, "X-Request-ID", request_id);
    if (status == 405) evhttp_add_header(headers, "Allow", "GET");
    if (status == 401) evhttp_add_header(headers, "WWW-Authenticate", "Bearer realm=\"PgVN monitor\"");
    if (status >= 400) evhttp_add_header(headers, "Connection", "close");
    if (buffer && body) evbuffer_add(buffer, body, strlen(body));
    if (server->inflight > 0) server->inflight--;
    if (server->draining && !server->inflight)
        evhttp_request_set_on_complete_cb(req, on_reply_done, server);
    evhttp_send_reply(req, status, reason_for(status), buffer);
    if (buffer) evbuffer_free(buffer);
}

static void finish_error(pgvn_http_server *server, struct evhttp_request *req, int status, const char *code)
{
    char body[160], request_id[40];
    snprintf(request_id, sizeof(request_id), "monitor-%016llx", server->sequence);
    snprintf(body, sizeof(body), "{\"error\":{\"code\":\"%s\",\"request_id\":\"%s\"}}\n", code, request_id);
    finish(server, req, status, "application/json; charset=utf-8", body);
}

static void count_origin(struct evhttp_request *req, int *count, const char **value)
{
    struct evkeyvalq *headers = evhttp_request_get_input_headers(req);
    struct evkeyval *header;
    *count = 0;
    *value = NULL;
    TAILQ_FOREACH(header, headers, next) {
        if (!strcasecmp(header->key, "Origin")) {
            *value = header->value;
            (*count)++;
        }
    }
}

static void force_drain(evutil_socket_t fd, short what, void *arg)
{
    pgvn_http_server *server = arg;
    (void)fd;
    (void)what;
    if (server->held) {
        struct evhttp_request *held = server->held;
        server->held = NULL;
        finish_error(server, held, 503, "shutting_down");
        return;
    }
    if (server->base) event_base_loopexit(server->base, NULL);
}

static void begin_drain(pgvn_http_server *server)
{
    struct timeval wait;
    if (!server || server->draining) return;
    server->draining = 1;
    if (server->bound) {
        evhttp_del_accept_socket(server->http, server->bound);
        server->bound = NULL;
    }
    if (!server->inflight) {
        event_base_loopexit(server->base, NULL);
        return;
    }
    wait.tv_sec = server->drain_ms / 1000;
    wait.tv_usec = (server->drain_ms % 1000) * 1000;
    server->drain_timer = evtimer_new(server->base, force_drain, server);
    if (server->drain_timer) evtimer_add(server->drain_timer, &wait);
}

static void on_signal(evutil_socket_t fd, short what, void *arg)
{
    (void)fd;
    (void)what;
    begin_drain(arg);
}

static void on_request(struct evhttp_request *req, void *arg)
{
    pgvn_http_server *server = arg;
    const struct evhttp_uri *uri = evhttp_request_get_evhttp_uri(req);
    const char *path = uri ? evhttp_uri_get_path(uri) : NULL;
    const char *query = uri ? evhttp_uri_get_query(uri) : NULL;
    const char *authorization;
    int origin_count = 0, has_body, path_valid, authorized = 0;
    const char *origin = NULL;
    pgvn_http_input input;
    pgvn_http_decision decision;
    char body[64];
    server->sequence++;
    if (server->test_hooks && path && !strcmp(path, "/release")) {
        server->inflight++;
        if (server->held) {
            struct evhttp_request *held = server->held;
            server->held = NULL;
            finish(server, held, 200, "text/plain; charset=utf-8", "released\n");
        }
        finish(server, req, 200, "text/plain; charset=utf-8", "ok\n");
        return;
    }
    if (server->draining || server->inflight >= server->queue_limit) {
        server->inflight++;
        finish_error(server, req, 503, server->draining ? "shutting_down" : "queue_full");
        return;
    }
    server->inflight++;
    if (server->test_hooks && path && !strcmp(path, "/hold")) {
        if (server->held) {
            finish_error(server, req, 409, "hold_busy");
            return;
        }
        server->held = req;
        fprintf(stderr, "pgvn-monitor: holding\n");
        fflush(stderr);
        return;
    }
    has_body = evbuffer_get_length(evhttp_request_get_input_buffer(req)) > 0;
    path_valid = path && ascii_text(path, 256) && (!query || ascii_text(query, 1024));
    count_origin(req, &origin_count, &origin);
    authorization = evhttp_find_header(evhttp_request_get_input_headers(req), "Authorization");
    if (authorization && server->tokens)
        authorized = pgvn_token_authorize(server->tokens, authorization, strlen(authorization));
    memset(&input, 0, sizeof(input));
    input.method_is_get = evhttp_request_get_command(req) == EVHTTP_REQ_GET;
    input.has_body = has_body;
    input.path_valid = path_valid;
    input.path = path;
    input.query_present = query != NULL;
    input.query = query;
    input.origin_count = origin_count;
    input.origin = origin;
    input.expected_origin = server->origin;
    input.authorized = authorized;
    pgvn_http_decide(&input, &decision);
    if (decision.error_code) {
        finish_error(server, req, decision.status, decision.error_code);
        return;
    }
    if (decision.route == PGVN_HTTP_LIVE || decision.route == PGVN_HTTP_READY) {
        int up = decision.route == PGVN_HTTP_LIVE ? !server->draining : server->index_html && !server->draining;
        snprintf(body, sizeof(body), decision.route == PGVN_HTTP_LIVE ? "{\"live\":%s}\n" : "{\"ready\":%s}\n", up ? "true" : "false");
        finish(server, req, up ? 200 : 503, "application/json", body);
        return;
    }
    if (decision.route == PGVN_HTTP_INDEX && server->index_html) {
        finish(server, req, 200, "text/html; charset=utf-8", server->index_html);
        return;
    }
    if (decision.route == PGVN_HTTP_STYLE && server->style) {
        finish(server, req, 200, "text/css; charset=utf-8", server->style);
        return;
    }
    if (decision.route == PGVN_HTTP_SCRIPT && server->script) {
        finish(server, req, 200, "text/javascript; charset=utf-8", server->script);
        return;
    }
    if (decision.route == PGVN_HTTP_API || decision.route == PGVN_HTTP_METRICS)
        finish_error(server, req, 503, "database_unavailable");
    else finish_error(server, req, 404, "not_found");
}

int pgvn_http_serve(const pgvn_http_config *config)
{
    pgvn_http_server server;
    struct event *term_event, *int_event;
    int methods, code = 1;
    if (!config || !config->bind_host || strcmp(config->bind_host, "127.0.0.1")
        || config->port < 1024 || config->port > 65535) return PGVN_PLATFORM_INVALID;
    memset(&server, 0, sizeof(server));
    server.timeout_seconds = config->timeout_seconds > 0 ? config->timeout_seconds : 5;
    server.queue_limit = config->queue_limit > 0 ? config->queue_limit : 64;
    server.drain_ms = config->drain_ms > 0 ? config->drain_ms : 15000;
    server.test_hooks = config->enable_test_hooks;
    snprintf(server.origin, sizeof(server.origin), "http://127.0.0.1:%u", config->port);
    server.tokens = pgvn_token_store_create();
    if (!server.tokens || (config->token_file && load_tokens(server.tokens, config->token_file))) goto done;
    if (config->web_dir) {
        load_asset(config->web_dir, "index.html", &server.index_html, &server.index_len);
        load_asset(config->web_dir, "monitor.css", &server.style, &server.style_len);
        load_asset(config->web_dir, "monitor.js", &server.script, &server.script_len);
    }
    server.base = event_base_new();
    server.http = server.base ? evhttp_new(server.base) : NULL;
    if (!server.http) goto done;
    evhttp_set_default_content_type(server.http, NULL);
    evhttp_set_max_headers_size(server.http, PGVN_HTTP_HEADER_LIMIT);
    evhttp_set_max_body_size(server.http, 65536);
    evhttp_set_timeout(server.http, server.timeout_seconds);
    methods = EVHTTP_REQ_GET | EVHTTP_REQ_POST | EVHTTP_REQ_HEAD | EVHTTP_REQ_PUT | EVHTTP_REQ_DELETE
        | EVHTTP_REQ_OPTIONS | EVHTTP_REQ_TRACE | EVHTTP_REQ_CONNECT | EVHTTP_REQ_PATCH;
    evhttp_set_allowed_methods(server.http, (ev_uint16_t)methods);
    evhttp_set_gencb(server.http, on_request, &server);
    server.bound = evhttp_bind_socket_with_handle(server.http, config->bind_host, (ev_uint16_t)config->port);
    if (!server.bound) goto done;
    term_event = evsignal_new(server.base, SIGTERM, on_signal, &server);
    int_event = evsignal_new(server.base, SIGINT, on_signal, &server);
    if (!term_event || !int_event || evsignal_add(term_event, NULL) || evsignal_add(int_event, NULL)) goto done;
    fprintf(stderr, "pgvn-monitor: listening %s %u\n", config->bind_host, config->port);
    fflush(stderr);
    event_base_dispatch(server.base);
    code = 0;
    event_free(term_event);
    event_free(int_event);
done:
    if (server.http) evhttp_free(server.http);
    if (server.drain_timer) event_free(server.drain_timer);
    if (server.base) event_base_free(server.base);
    free(server.index_html);
    free(server.style);
    free(server.script);
    pgvn_token_store_destroy(server.tokens);
    return code;
}
