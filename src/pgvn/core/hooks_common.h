#ifndef PGVN_HOOKS_COMMON_H
#define PGVN_HOOKS_COMMON_H

#include "pgvn.h"

svn_error_t *pgvn_hook_event_json(svn_stringbuf_t **output,
    const pgvn_hook_context *event,apr_pool_t *pool);
svn_error_t *pgvn_hook_result(const pgvn_hook_context *event,
    const char *stdout_data,size_t stdout_size,svn_boolean_t stdout_truncated,
    const char *stderr_data,size_t stderr_size,unsigned long exit_code,
    apr_pool_t *pool);

#endif
