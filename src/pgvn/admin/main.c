#include <stdio.h>
#include <string.h>
#include <apr_general.h>
#include "pgvn.h"

int main(int argc, char **argv)
{
    if (apr_initialize() != APR_SUCCESS) return 1;
    if (argc == 2 && (!strcmp(argv[1], "--version") || !strcmp(argv[1], "diagnose"))) {
        char json[1024];
        pgvn_build_json(json, sizeof(json));
        fputs(json, stdout);
        apr_terminate();
        return 0;
    }
    fputs("M0 bootstrap: use pgvnadmin --version or diagnose. Repository commands follow in M1/M2.\n", stderr);
    apr_terminate();
    return 2;
}
