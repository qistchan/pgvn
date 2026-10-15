/* Test-constructor routing only. Upstream sources and assertions are unchanged. */
#ifndef PGVN_NATIVE_REDIRECT_H
#define PGVN_NATIVE_REDIRECT_H
#include "svn_fs.h"
#include "svn_repos.h"
svn_error_t *pgvn_native_fs_open2(svn_fs_t **,const char *,apr_hash_t *,apr_pool_t *,apr_pool_t *);
svn_error_t *pgvn_native_fs_open(svn_fs_t **,const char *,apr_hash_t *,apr_pool_t *);
svn_error_t *pgvn_native_repos_open3(svn_repos_t **,const char *,apr_hash_t *,apr_pool_t *,apr_pool_t *);
#define svn_fs_open2 pgvn_native_fs_open2
#define svn_fs_open pgvn_native_fs_open
#define svn_repos_open3 pgvn_native_repos_open3
#endif
