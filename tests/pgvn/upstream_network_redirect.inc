/* Test-only constructor/path adapters. Command bodies remain upstream code. */
#ifndef PGVN_UPSTREAM_NETWORK_REDIRECT_H
#define PGVN_UPSTREAM_NETWORK_REDIRECT_H
#include "svn_repos.h"
svn_error_t *pgvn_network_repos_open3(svn_repos_t **,const char *,apr_hash_t *,apr_pool_t *,apr_pool_t *);
svn_error_t *pgvn_network_repos_create(svn_repos_t **,const char *,const char *,const char *,apr_hash_t *,apr_hash_t *,apr_pool_t *);
svn_error_t *pgvn_network_repos_hotcopy3(const char *,const char *,svn_boolean_t,svn_boolean_t,
    svn_repos_notify_func_t,void *,svn_cancel_func_t,void *,apr_pool_t *);
svn_error_t *pgvn_network_repos_recover4(const char *,svn_boolean_t,svn_repos_notify_func_t,void *,svn_cancel_func_t,void *,apr_pool_t *);
svn_error_t *pgvn_network_repos_upgrade2(const char *,svn_boolean_t,svn_repos_notify_func_t,void *,apr_pool_t *);
svn_error_t *pgvn_network_repos_freeze(const apr_array_header_t *,svn_repos_freeze_func_t,void *,apr_pool_t *);
svn_error_t *pgvn_network_repos_pack(svn_repos_t *,svn_repos_notify_func_t,void *,svn_cancel_func_t,void *,apr_pool_t *);
svn_error_t *pgvn_network_fs_open2(svn_fs_t **,const char *,apr_hash_t *,apr_pool_t *,apr_pool_t *);
#define svn_repos_open3 pgvn_network_repos_open3
#define svn_repos_create pgvn_network_repos_create
#define svn_repos_hotcopy3 pgvn_network_repos_hotcopy3
#define svn_repos_recover4 pgvn_network_repos_recover4
#define svn_repos_upgrade2 pgvn_network_repos_upgrade2
#define svn_repos_freeze pgvn_network_repos_freeze
#define svn_repos_fs_pack2 pgvn_network_repos_pack
#define svn_fs_open2 pgvn_network_fs_open2
#endif
