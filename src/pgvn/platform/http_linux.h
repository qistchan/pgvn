#ifndef PGVN_HTTP_LINUX_H
#define PGVN_HTTP_LINUX_H
typedef struct pgvn_http_config {
    const char *bind_host;
    unsigned port;
    const char *web_dir;
    const char *token_file;
    int timeout_seconds;
    int queue_limit;
    int drain_ms;
    int enable_test_hooks;
} pgvn_http_config;
int pgvn_http_serve(const pgvn_http_config *config);
#endif
