#include <stdio.h>
#include <apr_version.h>
#include <libpq-fe.h>
#include "pgvn.h"

int pgvn_build_json(char *buffer, size_t size)
{
    return snprintf(buffer, size,
        "{\"product\":\"PgVN\",\"version\":\"%s\",\"milestone\":\"%s\","
        "\"svn_base\":\"%s\",\"apr_runtime\":\"%s\",\"libpq_version\":%d,"
        "\"ready\":false,\"capabilities\":{\"repository_by_id\":true,"
        "\"svn_protocol\":false,\"dav_protocol\":false,\"monitor\":\"bootstrap\"}}\n",
        PGVN_VERSION, PGVN_MILESTONE, PGVN_SVN_BASE,
        apr_version_string(), PQlibVersion());
}
