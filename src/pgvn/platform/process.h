#ifndef PGVN_PROCESS_H
#define PGVN_PROCESS_H
#include "runtime.h"
#include "svn_error.h"
#define PGVN_PROCESS_OUTPUT_LIMIT 8192
struct pgvn_process_output { char bytes[PGVN_PROCESS_OUTPUT_LIMIT+1]; size_t size; int truncated; };
typedef struct pgvn_process_result {
    int exit_code,cancelled;
    struct pgvn_process_output output,error;
} pgvn_process_result;
/* Launch the packaged supervisor with a private cancellation pipe. NULL input
 * preserves the administrative caller's stdio; non-NULL input captures pipes. */
svn_error_t *pgvn_process_run(pgvn_process_result *result,const char *helper,
    const char *const *arguments,char *const *environment,const char *input,size_t input_size,
    pgvn_cancel_event cancel,apr_pool_t *pool);
const char *pgvn_process_helper(apr_pool_t *pool);
#endif
