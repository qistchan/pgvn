/* Native alternatives for directory-only test setup; no upstream assertion edits. */
#include <stdio.h>
#include <string.h>
#include <apr_general.h>
#include "svn_test.h"
#include "svn_test_fs.h"
#include "svn_pools.h"
#include "svn_hash.h"
#include "private/svn_fs_private.h"

static svn_error_t *checks(apr_pool_t *pool)
{
    svn_test_opts_t options={0};options.fs_type="pg";
    svn_repos_t *repos;SVN_ERR(svn_test__create_repos(&repos,"native-path-apis",&options,pool));
    const char *(*getters[])(svn_repos_t *,apr_pool_t *)={
        svn_repos_db_env,svn_repos_conf_dir,svn_repos_svnserve_conf,svn_repos_lock_dir,
        svn_repos_db_lockfile,svn_repos_db_logs_lockfile,svn_repos_hook_dir,
        svn_repos_start_commit_hook,svn_repos_pre_commit_hook,svn_repos_pre_lock_hook,
        svn_repos_pre_unlock_hook,svn_repos_post_lock_hook,svn_repos_post_unlock_hook,
        svn_repos_post_commit_hook,svn_repos_pre_revprop_change_hook,svn_repos_post_revprop_change_hook};
    for(size_t i=0;i<sizeof(getters)/sizeof(*getters);++i) SVN_TEST_ASSERT(getters[i](repos,pool)==NULL);
    SVN_TEST_ASSERT(!strncmp(svn_repos_path(repos,pool),"pgvn:",5));
    svn_fs_t *fs=svn_repos_fs(repos);
    int format;svn_version_t *minimum;
    SVN_ERR(svn_fs_info_format(&format,&minimum,fs,pool,pool));SVN_TEST_ASSERT(format==1);
    SVN_ERR(svn_repos_info_format(&format,&minimum,repos,pool,pool));SVN_TEST_ASSERT(format==5);
    apr_hash_t *capabilities;SVN_ERR(svn_repos_capabilities(&capabilities,repos,pool,pool));
    SVN_TEST_ASSERT(svn_hash_gets(capabilities,SVN_REPOS_CAPABILITY_MERGEINFO)!=NULL);
    svn_fs_ioctl_code_t code;code.fs_type="NON-EXISTING";code.code=98765;
    SVN_TEST_ASSERT_ERROR(svn_fs_ioctl(fs,code,NULL,NULL,NULL,NULL,pool,pool),SVN_ERR_FS_UNRECOGNIZED_IOCTL_CODE);
    code.fs_type="pg";
    SVN_TEST_ASSERT_ERROR(svn_fs_ioctl(NULL,code,NULL,NULL,NULL,NULL,pool,pool),SVN_ERR_FS_UNKNOWN_FS_TYPE);
    svn_fs_access_t *access;SVN_ERR(svn_fs_create_access(&access,"jrandom",pool));
    SVN_ERR(svn_fs_access_add_lock_token(access,"opaquelocktoken:abc"));
    SVN_ERR(svn_fs_set_access(fs,access));
    svn_fs_txn_t *txn;svn_fs_root_t *root;svn_revnum_t revision;const char *conflict;
    SVN_ERR(svn_repos_fs_begin_txn_for_commit2(&txn,repos,0,apr_hash_make(pool),pool));
    SVN_ERR(svn_fs_txn_root(&root,txn,pool));SVN_ERR(svn_fs_make_dir(root,"/whatever",pool));
    SVN_ERR(svn_repos_fs_commit_txn(&conflict,repos,&revision,txn,pool));
    SVN_TEST_ASSERT(conflict==NULL && revision==1);
    printf("{\"null_path_accessors\":16,\"storage_format\":1,\"repository_format\":5,\"mergeinfo\":true,\"native_ioctl_contract\":true,\"deprecated_access_commit\":1}\n");
    return SVN_NO_ERROR;
}

int main(void)
{
    if(apr_initialize()!=APR_SUCCESS) return 2;
    apr_pool_t *pool=svn_pool_create(NULL);svn_error_t *error=checks(pool);int status=error ? 1 : 0;
    if(error){svn_handle_error2(error,stderr,FALSE,"native paths: ");svn_error_clear(error);}
    svn_pool_destroy(pool);apr_terminate();return status;
}
