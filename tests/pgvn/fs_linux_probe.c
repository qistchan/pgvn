/* Real ELF FS driver. All SQL is delegated to libpq; the interposer measures
 * returned column payloads (not TCP protocol overhead) without changing data. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <apr_general.h>
#include <apr_strings.h>
#include <libpq-fe.h>
#include "svn_checksum.h"
#include "svn_fs.h"
#include "svn_pools.h"
#include "svn_props.h"
#include "svn_version.h"
#include "fs-loader.h"
#include "pgfs_native.h"
#include "build_info.h"

static unsigned long long queries, payload;
static int assertions;
static const apr_int64_t sizes[]={0,1,1048575,1048576,1048577,3145759,8388639};
#define CHECK(c) do { ++assertions; if(!(c)) return svn_error_createf(SVN_ERR_TEST_FAILED,NULL,"FS assertion at line %d",__LINE__); } while(0)
extern svn_error_t *svn_fs_pg__init(const svn_version_t *,fs_library_vtable_t **,apr_pool_t *);
typedef struct init_probe { apr_pool_t *pool; fs_library_vtable_t *table; int failed; } init_probe;
static void *initialize_thread(void *baton)
{
    init_probe *probe=baton; const svn_version_t version={1,14,5,""};
    for(int i=0;i<100;++i) {
        svn_error_t *error=svn_fs_pg__init(&version,&probe->table,probe->pool);
        if(error) { probe->failed=1; svn_error_clear(error); break; }
    }
    return NULL;
}

static svn_error_t *check_initializer(apr_pool_t *pool)
{
    const svn_version_t wrong={1,14,3,""}; fs_library_vtable_t *table=NULL;
    svn_error_t *error=svn_fs_pg__init(&wrong,&table,pool);
    CHECK(error && error->apr_err==SVN_ERR_VERSION_MISMATCH && !table); svn_error_clear(error);
    pthread_t threads[8]; init_probe probes[8]={0};
    for(int i=0;i<8;++i) { probes[i].pool=svn_pool_create(pool); CHECK(!pthread_create(&threads[i],NULL,initialize_thread,&probes[i])); }
    for(int i=0;i<8;++i) CHECK(!pthread_join(threads[i],NULL));
    for(int i=0;i<8;++i) { CHECK(!probes[i].failed && probes[i].table && probes[i].table==probes[0].table); svn_pool_destroy(probes[i].pool); }
    return SVN_NO_ERROR;
}

static int content_query;
int PQsendQueryParams(PGconn *connection,const char *sql,int count,const Oid *types,
    const char *const *values,const int *lengths,const int *formats,int result_format)
{
    typedef int (*execute)(PGconn *,const char *,int,const Oid *,const char *const *,const int *,const int *,int);
    static execute original;
    if(!original) { original=(execute)dlsym(RTLD_NEXT,"PQsendQueryParams"); if(!original) abort(); }
    ++queries; content_query=strstr(sql,"FROM pgfs.content_chunks")!=NULL;
    return original(connection,sql,count,types,values,lengths,formats,result_format);
}
PGresult *PQgetResult(PGconn *connection)
{
    typedef PGresult *(*receive)(PGconn *);
    static receive original;
    if(!original) { original=(receive)dlsym(RTLD_NEXT,"PQgetResult"); if(!original) abort(); }
    PGresult *result=original(connection);
    if(result && content_query)
        for(int row=0;row<PQntuples(result);++row)
            for(int col=0;col<PQnfields(result);++col) payload+=(unsigned)PQgetlength(result,row,col);
    if(!result) content_query=0;
    return result;
}

static unsigned char pattern(apr_int64_t index)
{
    unsigned x=(unsigned)index+0x73b198acU;
    x^=x>>16; x*=0x7feb352dU; x^=x>>15; x*=0x846ca68bU; x^=x>>16;
    return (unsigned char)x;
}

static svn_error_t *write_pattern(svn_fs_root_t *root,const char *path,apr_int64_t size,apr_pool_t *pool)
{
    SVN_ERR(svn_fs_make_file(root,path,pool));
    svn_stream_t *stream; SVN_ERR(svn_fs_apply_text(&stream,root,path,NULL,pool));
    char buffer[65536];
    for(apr_int64_t position=0;position<size;) {
        apr_size_t amount=(apr_size_t)(size-position<65536 ? size-position : 65536),written=amount;
        for(apr_size_t i=0;i<amount;++i) buffer[i]=(char)pattern(position+(apr_int64_t)i);
        SVN_ERR(svn_stream_write(stream,buffer,&written)); CHECK(written==amount); position+=(apr_int64_t)amount;
    }
    return svn_stream_close(stream);
}

static svn_error_t *prepare(svn_fs_t *fs,apr_pool_t *pool)
{
    svn_fs_txn_t *txn; svn_fs_root_t *root;
    SVN_ERR(svn_fs_begin_txn2(&txn,fs,0,0,pool)); SVN_ERR(svn_fs_txn_root(&root,txn,pool));
    SVN_ERR(svn_fs_make_dir(root,"数据",pool));
    for(int i=0;i<7;++i) SVN_ERR(write_pattern(root,apr_psprintf(pool,"数据/f%d",i),sizes[i],pool));
    SVN_ERR(svn_fs_make_dir(root,"大目录",pool));
    for(int i=0;i<256;++i) SVN_ERR(svn_fs_make_file(root,apr_psprintf(pool,"大目录/file-%04d",i),pool));
    const char raw[]={0,'x',(char)0xff};
    SVN_ERR(svn_fs_change_node_prop(root,"数据/f6","test:binary",svn_string_ncreate(raw,sizeof(raw),pool),pool));
    svn_revnum_t rev; const char *conflict;
    SVN_ERR(svn_fs_commit_txn(&conflict,&rev,txn,pool)); CHECK(rev==1);
    return SVN_NO_ERROR;
}

static svn_error_t *check_stream(svn_stream_t *stream,apr_int64_t size,apr_pool_t *pool)
{
    char buffer[65537]; apr_int64_t position=0;
    while(position<size) {
        apr_size_t amount=sizeof(buffer);
        SVN_ERR(svn_stream_read_full(stream,buffer,&amount));
        CHECK(amount>0 && (apr_int64_t)amount<=size-position);
        int equal=1;
        for(apr_size_t i=0;i<amount;++i) if((unsigned char)buffer[i]!=pattern(position+(apr_int64_t)i)) equal=0;
        CHECK(equal); position+=(apr_int64_t)amount;
    }
    apr_size_t amount=1; SVN_ERR(svn_stream_read_full(stream,buffer,&amount)); CHECK(amount==0);
    SVN_ERR(svn_stream_seek(stream,NULL));
    amount=size>100 ? 100 : (apr_size_t)size;
    SVN_ERR(svn_stream_read_full(stream,buffer,&amount));
    svn_stream_mark_t *mark; SVN_ERR(svn_stream_mark(stream,&mark,pool));
    if(size>100) {
        amount=sizeof(buffer); SVN_ERR(svn_stream_read_full(stream,buffer,&amount));
        SVN_ERR(svn_stream_seek(stream,mark));
        amount=sizeof(buffer); SVN_ERR(svn_stream_read_full(stream,buffer,&amount));
        int equal=1;
        for(apr_size_t i=0;i<amount;++i) if((unsigned char)buffer[i]!=pattern(100+(apr_int64_t)i)) equal=0;
        CHECK(equal);
    }
    return svn_stream_close(stream);
}

static svn_error_t *check_fs(svn_fs_t *fs,apr_pool_t *pool)
{
    svn_fs_root_t *root; SVN_ERR(svn_fs_revision_root(&root,fs,1,pool));
    for(int i=0;i<7;++i) {
        apr_pool_t *scratch=svn_pool_create(pool); svn_stream_t *stream;
        SVN_ERR(svn_fs_file_contents(&stream,root,apr_psprintf(scratch,"数据/f%d",i),scratch));
        SVN_ERR(check_stream(stream,sizes[i],scratch)); svn_pool_destroy(scratch);
    }
    apr_hash_t *entries; SVN_ERR(svn_fs_dir_entries(&entries,root,"数据",pool)); CHECK(apr_hash_count(entries)==7);
    for(int i=0;i<7;++i) {
        const char *name=apr_psprintf(pool,"f%d",i); svn_fs_dirent_t *entry=apr_hash_get(entries,name,APR_HASH_KEY_STRING);
        const svn_fs_id_t *id; SVN_ERR(svn_fs_node_id(&id,root,apr_pstrcat(pool,"数据/",name,NULL),pool));
        CHECK(entry && entry->kind==svn_node_file && svn_fs_compare_ids(entry->id,id)==0);
    }
    svn_string_t *property; SVN_ERR(svn_fs_node_prop(&property,root,"数据/f6","test:binary",pool));
    CHECK(property && property->len==3 && property->data[0]==0 && (unsigned char)property->data[2]==255);
    SVN_ERR(svn_fs_verify_root(root,pool));
    svn_fs_txn_t *txn; svn_fs_root_t *mutable;
    SVN_ERR(svn_fs_begin_txn2(&txn,fs,1,0,pool)); SVN_ERR(svn_fs_txn_root(&mutable,txn,pool));
    SVN_ERR(svn_fs_copy(root,"数据",mutable,"copy",pool)); SVN_ERR(svn_fs_delete(mutable,"copy/f0",pool));
    SVN_ERR(svn_fs_dir_entries(&entries,mutable,"copy",pool)); CHECK(apr_hash_count(entries)==6);
    SVN_ERR(svn_fs_dir_entries(&entries,root,"数据",pool)); CHECK(apr_hash_count(entries)==7);
    SVN_ERR(svn_fs_abort_txn(txn,pool));
    return SVN_NO_ERROR;
}

static double milliseconds(clockid_t clock)
{
    struct timespec value; if(clock_gettime(clock,&value)) abort();
    return value.tv_sec*1000.0+value.tv_nsec/1000000.0;
}

static svn_error_t *directory_benchmark(svn_fs_t *fs,int repetitions,apr_pool_t *pool)
{
    svn_fs_root_t *root; SVN_ERR(svn_fs_revision_root(&root,fs,1,pool));
    for(int repeat=0;repeat<repetitions;++repeat) {
        apr_pool_t *scratch=svn_pool_create(pool); queries=0;
        double start=milliseconds(CLOCK_MONOTONIC),cpu=milliseconds(CLOCK_PROCESS_CPUTIME_ID);
        apr_hash_t *entries; SVN_ERR(svn_fs_dir_entries(&entries,root,"大目录",scratch));
        double elapsed=milliseconds(CLOCK_MONOTONIC)-start,cpu_ms=milliseconds(CLOCK_PROCESS_CPUTIME_ID)-cpu;
        CHECK(apr_hash_count(entries)==256);
        for(int i=0;i<256;++i) {
            const char *name=apr_psprintf(scratch,"file-%04d",i);
            svn_fs_dirent_t *entry=apr_hash_get(entries,name,APR_HASH_KEY_STRING);
            CHECK(entry && entry->kind==svn_node_file && entry->id);
        }
        printf("{\"elapsed_ms\":%.6f,\"cpu_ms\":%.6f,\"queries\":%llu,\"entries\":256,\"chunk_column_bytes\":0}\n",elapsed,cpu_ms,queries);
        fflush(stdout); svn_pool_destroy(scratch);
        if(!repeat && getenv("PGVN_FS_BENCH_SYNC")) CHECK(getchar()=='G');
    }
    return SVN_NO_ERROR;
}

static svn_error_t *benchmark(svn_fs_t *fs,int repetitions,apr_pool_t *pool)
{
    svn_fs_root_t *root; SVN_ERR(svn_fs_revision_root(&root,fs,1,pool));
    svn_checksum_t *expected; SVN_ERR(svn_fs_file_checksum(&expected,svn_checksum_sha1,root,"数据/f6",TRUE,pool));
    for(int i=0;i<repetitions;++i) {
        apr_pool_t *scratch=svn_pool_create(pool); queries=payload=0;
        double start=milliseconds(CLOCK_MONOTONIC),cpu=milliseconds(CLOCK_PROCESS_CPUTIME_ID);
        svn_stream_t *stream; SVN_ERR(svn_fs_file_contents(&stream,root,"数据/f6",scratch));
        svn_checksum_ctx_t *hash=svn_checksum_ctx_create(svn_checksum_sha1,scratch);
        char buffer[65536]; apr_int64_t total=0;
        for(;;) {
            apr_size_t amount=sizeof(buffer); SVN_ERR(svn_stream_read_full(stream,buffer,&amount));
            SVN_ERR(svn_checksum_update(hash,buffer,amount)); total+=(apr_int64_t)amount;
            if(!amount) break;
        }
        SVN_ERR(svn_stream_close(stream)); svn_checksum_t *actual; SVN_ERR(svn_checksum_final(&actual,hash,scratch));
        double cpu_ms=milliseconds(CLOCK_PROCESS_CPUTIME_ID)-cpu,elapsed=milliseconds(CLOCK_MONOTONIC)-start;
        CHECK(total==sizes[6] && svn_checksum_match(actual,expected));
        printf("{\"elapsed_ms\":%.6f,\"cpu_ms\":%.6f,\"queries\":%llu,\"chunk_column_bytes\":%llu,\"content_bytes\":%lld,\"sha1\":\"%s\"}\n",
            elapsed,cpu_ms,queries,payload,(long long)total,svn_checksum_to_cstring(actual,scratch));
        svn_pool_destroy(scratch);
        if(i==0 && getenv("PGVN_FS_BENCH_SYNC")) {
            fflush(stdout);
            CHECK(getchar()=='G');
        }
    }
    return SVN_NO_ERROR;
}

static svn_error_t *run(int argc,char **argv,apr_pool_t *pool)
{
    if(argc<2) return svn_error_create(SVN_ERR_CL_ARG_PARSING_ERROR,NULL,"FS operation required");
    SVN_ERR(check_initializer(pool));
    const char *dsn=getenv("PGVN_FS_TEST_DSN"),*id=getenv("PGVN_FS_TEST_REPOSITORY");
    CHECK(dsn && id);
    PGconn *connection=PQconnectdb(dsn); CHECK(connection && PQstatus(connection)==CONNECTION_OK);
    if(!strcmp(argv[1],"prepare")) SVN_ERR(pgfs_native_create_records(connection,id,id,id,"2026-10-17T00:00:00.000000Z"));
    /* Directory-free native binding uses the same allocation entry as core. */
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
    svn_fs_t *fs=svn_fs_new(NULL,pool);
#pragma GCC diagnostic pop
    SVN_ERR(pgfs_native_bind(fs,connection,dsn,id,NULL,pool));
    if(!strcmp(argv[1],"prepare")) SVN_ERR(prepare(fs,pool));
    else if(!strcmp(argv[1],"check")) SVN_ERR(check_fs(fs,pool));
    else if(!strcmp(argv[1],"dirbench")) return directory_benchmark(fs,argc>2 ? atoi(argv[2]) : 1,pool);
    else if(!strcmp(argv[1],"bench")) return benchmark(fs,argc>2 ? atoi(argv[2]) : 1,pool);
    else if(!strcmp(argv[1],"read")) {
        svn_fs_root_t *root; svn_stream_t *stream; SVN_ERR(svn_fs_revision_root(&root,fs,1,pool));
        SVN_ERR(svn_fs_file_contents(&stream,root,"数据/f2",pool)); SVN_ERR(check_stream(stream,sizes[2],pool));
    } else return svn_error_create(SVN_ERR_CL_ARG_PARSING_ERROR,NULL,"Unknown FS operation");
    printf("{\"assertions\":%d,\"status\":\"PASS\"}\n",assertions);
    return SVN_NO_ERROR;
}

int main(int argc,char **argv)
{
    if(argc==2 && !strcmp(argv[1],"--version")) { char json[PGVN_BUILD_JSON_SIZE]; pgvn_build_json(json,sizeof(json)); puts(json); return 0; }
    if(apr_initialize()!=APR_SUCCESS) return 2;
    apr_pool_t *pool=svn_pool_create(NULL); svn_error_t *error=svn_fs_initialize(pool);
    if(!error) error=run(argc,argv,pool);
    int code=0;
    if(error) { svn_handle_error2(error,stderr,FALSE,"fs_linux_probe: "); svn_error_clear(error); code=1; }
    svn_pool_destroy(pool); apr_terminate(); return code;
}
