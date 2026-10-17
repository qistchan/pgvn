#include "http_linux.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void usage(void)
{
    fprintf(stderr, "usage: pgvn-monitor --port PORT [--bind 127.0.0.1] [--web DIR] [--tokens FILE] "
                    "[--timeout SEC] [--queue N] [--drain-ms MS] [--enable-test-hooks]\n");
}

int main(int argc, char **argv)
{
    pgvn_http_config config;
    int i;
    memset(&config, 0, sizeof(config));
    config.bind_host = "127.0.0.1";
    config.timeout_seconds = 5;
    config.queue_limit = 64;
    config.drain_ms = 15000;
    if (argc == 2 && !strcmp(argv[1], "--limits")) {
        printf("header_limit=32768\nqueue_limit=64\ntimeout_seconds=5\n");
        return 0;
    }
    for (i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--enable-test-hooks")) config.enable_test_hooks = 1;
        else if (i + 1 >= argc) { usage(); return 2; }
        else if (!strcmp(argv[i], "--bind")) config.bind_host = argv[++i];
        else if (!strcmp(argv[i], "--port")) config.port = (unsigned)atoi(argv[++i]);
        else if (!strcmp(argv[i], "--web")) config.web_dir = argv[++i];
        else if (!strcmp(argv[i], "--tokens")) config.token_file = argv[++i];
        else if (!strcmp(argv[i], "--timeout")) config.timeout_seconds = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--queue")) config.queue_limit = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--drain-ms")) config.drain_ms = atoi(argv[++i]);
        else { usage(); return 2; }
    }
    if (!config.port) { usage(); return 2; }
    return pgvn_http_serve(&config) ? 1 : 0;
}
