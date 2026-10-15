#ifndef PGVN_ADMIN_H
#define PGVN_ADMIN_H
#include <stdint.h>
#include <stdio.h>
#include "internal.h"

typedef enum admin_option {
    OPT_JSON,OPT_CONFIG,OPT_ROUTE,OPT_UUID,OPT_FILE,OPT_REVISION,OPT_TXN,
    OPT_REVPROP,OPT_FULL_PATHS,OPT_INCREMENTAL,OPT_DELTAS,OPT_FORCE_UUID,
    OPT_IGNORE_UUID,OPT_USE_HOOKS,OPT_AUTHZ,OPT_GROUPS,OPT_HOOKS,OPT_GENERATION,
    OPT_CONFIRM_ID,OPT_LABEL,OPT_EXPIRES,OPT_TIMEOUT,OPT_ADMIN_ROLE,
    OPT_SERVICE_ROLE,OPT_MONITOR_ROLE,OPT_LIMIT,OPT_OFFSET,OPT_VERBOSE,
    OPT_HELP,OPT_VERSION,OPT_KEY_FILE,OPT_OLD_KEY_FILE,OPT_SETTINGS,OPT_COUNT
} admin_option;
#define OPTION(n) (UINT64_C(1) << (n))
#define GLOBAL_OPTIONS (OPTION(OPT_JSON)|OPTION(OPT_CONFIG))

typedef struct admin_command {
    int argc;
    const char **argv;
    const char *values[OPT_COUNT];
    uint64_t flags;
    int child_argc;
    const char **child_argv;
    pgvn_context *context;
    const char *credential_key_file;
    const char *old_credential_key_file;
    svn_boolean_t read_only_client;
    apr_pool_t *pool;
} admin_command;

svn_error_t *admin_usage(const char *message);
svn_error_t *admin_options(const admin_command *command,uint64_t allowed,int min_args,int max_args);
svn_error_t *admin_parse(admin_command *command,int argc,const char **argv);
svn_error_t *admin_db(admin_command *command);
svn_error_t *admin_repo(admin_command *command);
svn_error_t *admin_history(admin_command *command);
svn_error_t *admin_policy(admin_command *command);
svn_error_t *admin_tokens(admin_command *command);
svn_error_t *admin_credentials(admin_command *command);
svn_error_t *admin_maintenance(admin_command *command);
svn_error_t *admin_diagnose(admin_command *command);
svn_error_t *admin_dispatch(admin_command *command);
svn_error_t *admin_preflight(admin_command *command);
svn_error_t *admin_emit_query(admin_command *command,const char *sql,int count,const char *const *values);
svn_error_t *admin_read_text(const char **text,const char *path,apr_pool_t *pool);
svn_error_t *admin_open(pgvn_repository **repository,admin_command *command,const char *selector);
svn_error_t *admin_cancel(void *baton);
svn_error_t *admin_number(apr_int64_t *number,const char *value,apr_int64_t minimum,apr_int64_t maximum);
svn_error_t *admin_revision_range(svn_revnum_t *start,svn_revnum_t *end,admin_command *command,svn_fs_t *fs,svn_boolean_t loading);
svn_error_t *admin_db_check(pgvn_context *context,svn_boolean_t require_latest,apr_pool_t *pool);
svn_error_t *admin_db_migrate(pgvn_context *context,apr_pool_t *pool);
void admin_json_string(FILE *file,const char *value,apr_size_t length);
void admin_json_cstr(FILE *file,const char *value);
void admin_ok(const char *action);
const char *admin_env(const wchar_t *name,apr_pool_t *pool);
const char *admin_utf8(const wchar_t *value,apr_pool_t *pool);
void admin_help(void);
int admin_error(svn_error_t *error,svn_boolean_t json);
#endif
