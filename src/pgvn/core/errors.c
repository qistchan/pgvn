#include "pgvn.h"

pgvn_exit_code pgvn_error_exit_code(const svn_error_t *error)
{
    pgvn_exit_code code=PGVN_EXIT_FAILURE;
    for(const svn_error_t *item=error;item;item=item->child) {
        apr_status_t n=item->apr_err;
        if(n==PGVN_ERR_DATABASE) return PGVN_EXIT_DATABASE;
        if(n==PGVN_ERR_SCHEMA || n==SVN_ERR_FS_UNSUPPORTED_FORMAT || n==SVN_ERR_VERSION_MISMATCH) code=PGVN_EXIT_SCHEMA;
        else if(n==SVN_ERR_CL_ARG_PARSING_ERROR || n==SVN_ERR_INCORRECT_PARAMS || n==SVN_ERR_REPOS_BAD_ARGS || n==SVN_ERR_FS_PATH_SYNTAX || n==SVN_ERR_AUTHZ_INVALID_CONFIG) code=PGVN_EXIT_USAGE;
        else if(n==SVN_ERR_AUTHZ_UNREADABLE || n==SVN_ERR_AUTHZ_UNWRITABLE || n==SVN_ERR_AUTHN_FAILED || n==SVN_ERR_RA_NOT_AUTHORIZED) code=PGVN_EXIT_DENIED;
        else if(n==SVN_ERR_FS_NOT_FOUND || n==SVN_ERR_FS_NO_SUCH_REVISION || n==SVN_ERR_FS_NO_SUCH_TRANSACTION || APR_STATUS_IS_ENOENT(n)) code=PGVN_EXIT_NOT_FOUND;
        else if(n==SVN_ERR_FS_ALREADY_EXISTS || n==SVN_ERR_FS_TXN_OUT_OF_DATE || n==SVN_ERR_FS_CONFLICT || n==SVN_ERR_FS_REP_BEING_WRITTEN || APR_STATUS_IS_EEXIST(n)) code=PGVN_EXIT_CONFLICT;
    }
    return code;
}
