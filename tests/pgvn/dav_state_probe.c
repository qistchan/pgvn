/* Native M5 foundation probe; it does not claim to implement HTTP DAV. */
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdlib.h>
#include <fcntl.h>
#include <io.h>
#include <apr_general.h>
#include "internal.h"
#include "pgfs_native.h"
#include "svn_pools.h"
#include "svn_props.h"

typedef struct binding_baton {
    pgvn_context *context;
    svn_fs_t *fs;
    const char *repository_id,*binding,*mode,*created_id;
    const apr_array_header_t *properties;
    svn_fs_txn_t *result;
    svn_revnum_t base;
} binding_baton;

static svn_error_t *save_binding(PGconn *connection,svn_fs_txn_t *txn,void *baton,apr_pool_t *pool)
{
    binding_baton *b=baton;
    SVN_ERR(svn_fs_txn_name(&b->created_id,txn,pool));
    const char *values[]={b->repository_id,b->binding,b->created_id}; PGresult *result;
    SVN_ERR(pgvn_exec(&result,b->context,connection,
        "INSERT INTO pgvn.m5_binding_probe(repository_id,binding,txn_id) VALUES($1::uuid,$2,$3)",3,values));
    PQclear(result);
    if(!strcmp(b->mode,"hold")) {
        puts("READY"); fflush(stdout);
        if(getchar()==EOF) return svn_error_create(SVN_ERR_CANCELLED,NULL,"Probe input closed");
    }
    if(!strcmp(b->mode,"rollback")) return svn_error_create(SVN_ERR_TEST_FAILED,NULL,"Probe rejects the binding after insertion");
    return SVN_NO_ERROR;
}

static svn_error_t *nested_attempt(PGconn *connection,void *baton,apr_pool_t *pool)
{
    binding_baton *b=baton;
    return pgfs_native_begin_bound_txn(&b->result,b->fs,b->base,SVN_FS_TXN_CHECK_LOCKS,
        b->properties,save_binding,b,pool);
}

static svn_error_t *run(binding_baton *b,const char *repository_id,apr_pool_t *pool)
{
    pgvn_context_options options={getenv("PGVN_DSN"),3,5000}; pgvn_repository *repository;
    SVN_ERR(pgvn_context_create(&b->context,&options,pool));
    SVN_ERR(pgvn_repos_open(&repository,b->context,repository_id,pool));
    b->repository_id=repository->repository_id; b->fs=svn_repos_fs(pgvn_repo_svn(repository));
    svn_fs_access_t *access; SVN_ERR(svn_fs_create_access(&access,"writer",pool));
    SVN_ERR(svn_fs_set_access(b->fs,access));
    SVN_ERR(svn_fs_youngest_rev(&b->base,b->fs,pool));
    apr_array_header_t *properties=apr_array_make(pool,2,sizeof(svn_prop_t));
    svn_prop_t author={SVN_PROP_REVISION_AUTHOR,svn_string_create("writer",pool)};
    const char binary[]={'b',0,(char)0xff,'y'};
    svn_prop_t custom={"pgvn:binary",svn_string_ncreate(binary,sizeof(binary),pool)};
    APR_ARRAY_PUSH(properties,svn_prop_t)=author; APR_ARRAY_PUSH(properties,svn_prop_t)=custom;
    b->properties=properties;
    if(!strcmp(b->mode,"nested")) return pgfs_native_atomic(b->fs,nested_attempt,b,pool);
    return pgfs_native_begin_bound_txn(&b->result,b->fs,b->base,SVN_FS_TXN_CHECK_LOCKS,
        b->properties,save_binding,b,pool);
}

int wmain(int argc,wchar_t **wide)
{
    _setmode(_fileno(stdout),_O_BINARY); _setmode(_fileno(stdin),_O_BINARY);
    if(argc!=4 || apr_initialize()!=APR_SUCCESS) return 2;
    apr_pool_t *pool=svn_pool_create(NULL); const char *args[4];
    for(int i=1;i<4;++i) {
        int length=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,wide[i],-1,NULL,0,NULL,NULL);
        if(!length) { svn_pool_destroy(pool); apr_terminate(); return 2; }
        char *text=apr_palloc(pool,length);
        WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,wide[i],-1,text,length,NULL,NULL); args[i]=text;
    }
    binding_baton baton={0}; baton.binding=args[2]; baton.mode=args[3];
    baton.result=(svn_fs_txn_t *)(uintptr_t)1;
    svn_error_t *error=run(&baton,args[1],pool);
    int unchanged=baton.result==(svn_fs_txn_t *)(uintptr_t)1;
    const char *id=""; if(!error) { svn_error_t *name_error=svn_fs_txn_name(&id,baton.result,pool); if(name_error) error=name_error; }
    printf("{\"ok\":%s,\"transaction_id\":\"%s\",\"unchanged_on_error\":%s,\"error_code\":%ld}\n",
        error ? "false" : "true",id,unchanged ? "true" : "false",(long)(error ? error->apr_err : 0));
    int code=error ? 1 : 0;
    if(error) { svn_handle_error2(error,stderr,FALSE,"PgVN probe: "); svn_error_clear(error); }
    svn_pool_destroy(pool); apr_terminate(); return code;
}
