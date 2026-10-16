#ifndef PGVN_HTTP_ADAPTER_H
#define PGVN_HTTP_ADAPTER_H
#include "httpd.h"
#include "http_config.h"
#include "mod_dav.h"
#include "pgvn.h"
#include "dav_state.h"
#include "dav_svn.h"

#ifdef PGVN_DAV_BUILD
#define PGVN_HTTP_API __declspec(dllexport)
#else
#define PGVN_HTTP_API __declspec(dllimport)
#endif
PGVN_HTTP_API svn_error_t *pgvn_http_resolve(pgvn_binding **binding,request_rec *r,const char *uri,apr_pool_t *pool);
dav_error *pgvn_http_repository_error(svn_error_t *error,request_rec *r);
PGVN_HTTP_API int pgvn_http_error_response(request_rec *r,dav_error *error);
PGVN_HTTP_API svn_error_t *pgvn_http_policy(svn_authz_t **authz,request_rec *r,apr_pool_t *pool);
PGVN_HTTP_API const char *pgvn_http_mount(request_rec *r);
PGVN_HTTP_API svn_error_t *pgvn_http_check_access(request_rec *r,const char *repository_name,
    const char *path,const char *user,svn_repos_authz_access_t required,svn_boolean_t *allowed,apr_pool_t *pool);
const char *pgvn_http_config(cmd_parms *cmd,void *unused,const char *path);
void pgvn_http_child_init(apr_pool_t *pool,server_rec *server);
svn_error_t *pgvn_http_open(dav_svn_repos *repos,request_rec *r);
const char *pgvn_http_get_txn(const dav_svn_repos *repos,pgvn_dav_alias_kind kind,const char *name);
dav_error *pgvn_http_create_txn(dav_svn_repos *repos,const char **name,apr_hash_t *properties,
    pgvn_dav_alias_kind kind,const char *alias,apr_pool_t *pool);
dav_error *pgvn_http_cancel(const dav_svn_repos *repos,pgvn_dav_alias_kind kind,const char *name);
dav_error *pgvn_http_validate_txn(dav_svn_repos *repos,const char *name,svn_boolean_t receipt_allowed);
svn_error_t *pgvn_http_commit(const char **conflict,svn_revnum_t *revision,dav_svn_repos *repos,
    const char *name,svn_fs_txn_t **txn,apr_pool_t *pool);
dav_error *pgvn_http_pending(const dav_svn_repos *repos);
void pgvn_bootstrap_register_hooks(apr_pool_t *pool);
#endif
