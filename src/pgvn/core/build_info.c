#include <stdio.h>
#ifdef _WIN32
#include <windows.h>
#endif
#include <apr_version.h>
#include <apu_version.h>
#include <libpq-fe.h>
#include "build_info.h"

int pgvn_build_json(char *buffer, size_t size)
{
#ifdef _WIN32
    unsigned int process_code_page = GetACP();
#else
    /* Windows code pages do not apply to POSIX processes. */
    unsigned int process_code_page = 0;
#endif
    return snprintf(buffer, size,
        PGVN_BUILD_JSON_PREFIX
        ",\"apr_runtime\":\"%s\",\"apu_runtime\":\"%s\",\"libpq_version\":%d,\"process_code_page\":%u,\"ready\":false}\n",
        apr_version_string(), apu_version_string(), PQlibVersion(), process_code_page);
}
