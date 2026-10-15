/* Directory-free construction fixture for unchanged Apache native tests.
 * Administrative bindings test FS/repos semantics; transport policy remains
 * covered by the protocol suites. Never opens a local repository shell. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
#include "internal.h"
#include "svn_test.h"
#include "svn_test_fs.h"
#include "svn_checksum.h"
#include "svn_dirent_uri.h"
#include "svn_pools.h"
#include <apr_strings.h>

static svn_error_t *context(pgvn_context **output,apr_pool_t *pool)
{
    const char *database=getenv("PGVN_NATIVE_TEST_DATABASE");
    const char *dsn=getenv("PGVN_DSN");
    if(GetACP()!=CP_UTF8 || !database || strncmp(database,"pgvn_m6_native_",15) || !dsn)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Native PgVN test guard is required");
    pgvn_context_options options={dsn,3,5000};
    SVN_ERR(pgvn_context_create(output,&options,pool));
    if(strcmp(PQdb((*output)->control),database))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Native test database does not match guard");
    return SVN_NO_ERROR;
}

static svn_error_t *alias_name(const char **output,const char *name,apr_pool_t *pool)
{
    const char *absolute,*base=getenv("PGVN_NATIVE_TEST_ROOT");
    SVN_ERR(svn_dirent_get_absolute(&absolute,name,pool));
    if(!base || !svn_dirent_is_ancestor(base,absolute))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Native fixture alias escaped test root");
    svn_checksum_t *hash;
    SVN_ERR(svn_checksum(&hash,svn_checksum_sha1,absolute,strlen(absolute),pool));
    *output=apr_pstrcat(pool,"native-",svn_checksum_to_cstring(hash,pool),NULL);
    return SVN_NO_ERROR;
}

static svn_error_t *ledger(const char *operation,const char *alias,const char *id,apr_pool_t *pool)
{
    if(strncmp(alias,"pgvn:",5)) SVN_ERR(svn_dirent_get_absolute(&alias,alias,pool));
    const char *path=getenv("PGVN_NATIVE_TEST_LEDGER");
    if(!path) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Native fixture ledger is required");
    FILE *file=fopen(path,"ab");
    if(!file) return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"Cannot write native fixture ledger");
    fprintf(file,"{\"operation\":\"%s\",\"repository_id\":\"%s\",\"alias_hex\":\"",operation,id);
    for(const unsigned char *p=(const unsigned char *)alias;*p;++p) fprintf(file,"%02x",*p);
    fprintf(file,"\",\"process_code_page\":%u}\n",GetACP());
    int error=ferror(file);if(fclose(file)) error=1;
    return error ? svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"Native fixture ledger write failed") : SVN_NO_ERROR;
}

static svn_error_t *create(svn_repos_t **repos,const char *name,const svn_test_opts_t *opts,apr_pool_t *pool)
{
    if(strcmp(opts->fs_type,"pg"))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Native fixture only accepts the PgVN PG backend");
    pgvn_context *ctx;const char *canonical;pgvn_binding *binding;
    SVN_ERR(context(&ctx,pool));SVN_ERR(alias_name(&canonical,name,pool));
    svn_error_t *error=pgvn_repository_lookup(&binding,ctx,apr_pstrcat(pool,"name:",canonical,NULL),pool);
    if(error) {
        if(pgvn_error_exit_code(error)!=PGVN_EXIT_NOT_FOUND) return error;
        svn_error_clear(error);
    } else {
        SVN_ERR(ledger("purge",name,binding->repository_id,pool));
        SVN_ERR(pgvn_repo_purge(ctx,binding->repository_id,pool));
    }
    SVN_ERR(pgvn_repo_create(&binding,ctx,canonical,apr_pstrcat(pool,"/",canonical,NULL),NULL,pool));
    pgvn_repository *repository;
    SVN_ERR(pgvn_repos_open_admin(&repository,ctx,binding->repository_id,pool));
    *repos=pgvn_repo_svn(repository);
    return ledger("create",name,binding->repository_id,pool);
}

svn_error_t *svn_test__create_fs2(svn_fs_t **fs,const char *name,const svn_test_opts_t *opts,
    apr_hash_t *config,apr_pool_t *pool)
{
    if(config && apr_hash_count(config))
        return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"Per-directory FS configuration is not a native PgVN fixture");
    svn_repos_t *repos;SVN_ERR(create(&repos,name,opts,pool));*fs=svn_repos_fs(repos);return SVN_NO_ERROR;
}
svn_error_t *svn_test__create_fs(svn_fs_t **fs,const char *name,const svn_test_opts_t *opts,apr_pool_t *pool)
{ return svn_test__create_fs2(fs,name,opts,NULL,pool); }
svn_error_t *svn_test__create_repos2(svn_repos_t **repos,const char **url,const char **dirent,
    const char *name,const svn_test_opts_t *opts,apr_pool_t *result_pool,apr_pool_t *scratch_pool)
{
    if(url) return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"Local file-RA URLs are not a PgVN native fixture");
    svn_repos_t *created;SVN_ERR(create(&created,name,opts,result_pool));
    if(repos) *repos=created;
    if(dirent) *dirent=svn_repos_path(created,result_pool);
    return SVN_NO_ERROR;
}
svn_error_t *svn_test__create_repos(svn_repos_t **repos,const char *name,const svn_test_opts_t *opts,apr_pool_t *pool)
{ return svn_test__create_repos2(repos,NULL,NULL,name,opts,pool,pool); }

svn_error_t *pgvn_native_repos_open3(svn_repos_t **repos,const char *path,apr_hash_t *config,
    apr_pool_t *result_pool,apr_pool_t *scratch_pool)
{
    pgvn_context *ctx;pgvn_binding *binding;const char *selector;
    SVN_ERR(context(&ctx,result_pool));
    if(!strncmp(path,"pgvn:",5)) selector=apr_pstrcat(result_pool,"id:",path+5,NULL);
    else {const char *canonical;SVN_ERR(alias_name(&canonical,path,result_pool));selector=apr_pstrcat(result_pool,"name:",canonical,NULL);}
    SVN_ERR(pgvn_repository_lookup(&binding,ctx,selector,result_pool));
    pgvn_repository *repository;SVN_ERR(pgvn_repos_open_admin(&repository,ctx,binding->repository_id,result_pool));
    *repos=pgvn_repo_svn(repository);return ledger("open",path,binding->repository_id,result_pool);
}
svn_error_t *pgvn_native_fs_open2(svn_fs_t **fs,const char *path,apr_hash_t *config,
    apr_pool_t *result_pool,apr_pool_t *scratch_pool)
{
    svn_repos_t *repos;SVN_ERR(pgvn_native_repos_open3(&repos,path,config,result_pool,scratch_pool));
    *fs=svn_repos_fs(repos);return SVN_NO_ERROR;
}
svn_error_t *pgvn_native_fs_open(svn_fs_t **fs,const char *path,apr_hash_t *config,apr_pool_t *pool)
{return pgvn_native_fs_open2(fs,path,config,pool,pool);}
