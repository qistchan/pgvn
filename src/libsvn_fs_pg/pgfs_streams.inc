#include "chunk.h"
/* Fulltexts are chunked in PostgreSQL. Streaming never buffers an attachment
   in its entirety; textdelta windows use Subversion's own delta engine. */
#define PGFS_CHUNK_SIZE 1048576

typedef struct pgfs_reader_t {
    svn_fs_t *fs;
    apr_int64_t content, length, position, cached_chunk;
    svn_string_t *bytes;
    apr_pool_t *scratch;
} pgfs_reader_t;

static svn_error_t *pgfs_content_metadata(svn_filesize_t *length,const char **md5,
    const char **sha1,svn_fs_t *fs,apr_int64_t content,apr_pool_t *pool)
{
    if(!content) {
        *length=0;
        *md5=svn_checksum_to_cstring(svn_checksum_empty_checksum(svn_checksum_md5,pool),pool);
        *sha1=svn_checksum_to_cstring(svn_checksum_empty_checksum(svn_checksum_sha1,pool),pool);
        return SVN_NO_ERROR;
    }
    pgfs_data_t *data=fs->fsap_data;
    const char *values[]={data->repository_id,pgfs_number(content,pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,
        "SELECT length,md5,sha1 FROM pgfs.contents WHERE repository_id=$1::uuid "
        "AND content_id=$2::bigint AND ready AND NOT abandoned",2,values));
    if(PQntuples(result)!=1) { PQclear(result); return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"Incomplete PgFS file representation"); }
    *length=apr_atoi64(PQgetvalue(result,0,0));
    *md5=apr_pstrdup(pool,PQgetvalue(result,0,1));
    *sha1=apr_pstrdup(pool,PQgetvalue(result,0,2));
    PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_read_full(void *baton,char *buffer,apr_size_t *length)
{
    pgfs_reader_t *reader=baton;
    apr_size_t requested=*length,done=0;
    *length=0;
    while(done<requested && reader->position<reader->length) {
        apr_int64_t chunk=reader->position/PGFS_CHUNK_SIZE;
        if(chunk!=reader->cached_chunk) {
            svn_pool_clear(reader->scratch);
            pgfs_data_t *data=reader->fs->fsap_data;
            const char *values[]={data->repository_id,pgfs_number(reader->content,reader->scratch),
                                 pgfs_number(chunk,reader->scratch)};
            PGresult *result;
            SVN_ERR(pgfs_query_binary(&result,reader->fs,
                "SELECT data FROM pgfs.content_chunks WHERE repository_id=$1::uuid "
                "AND content_id=$2::bigint AND chunk_number=$3::bigint",3,values));
            if(PQntuples(result)!=1 || PQgetisnull(result,0,0)
                || !pgvn_chunk_length_ok(0,PQfformat(result,0)==1,PQgetlength(result,0,0),PGFS_CHUNK_SIZE)) {
                PQclear(result); return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"Missing PgFS content chunk");
            }
            {
                const char *raw=PQgetvalue(result,0,0);
                reader->bytes=svn_string_ncreate(raw ? raw : "", (apr_size_t)PQgetlength(result,0,0), reader->scratch);
            }
            PQclear(result);
            reader->cached_chunk=chunk;
        }
        apr_size_t offset=(apr_size_t)(reader->position%PGFS_CHUNK_SIZE);
        if(offset>=reader->bytes->len) return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"Truncated PgFS content chunk");
        apr_size_t take=reader->bytes->len-offset;
        if(take>requested-done) take=requested-done;
        if((apr_int64_t)take>reader->length-reader->position) take=(apr_size_t)(reader->length-reader->position);
        memcpy(buffer+done,reader->bytes->data+offset,take);
        done+=take; reader->position+=(apr_int64_t)take; *length=done;
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_read_mark(void *baton,svn_stream_mark_t **mark,apr_pool_t *pool)
{
    apr_int64_t *position=apr_palloc(pool,sizeof(*position));
    *position=((pgfs_reader_t *)baton)->position;
    *mark=(svn_stream_mark_t *)position;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_read_seek(void *baton,const svn_stream_mark_t *mark)
{
    ((pgfs_reader_t *)baton)->position=mark ? *(const apr_int64_t *)mark : 0;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_file_contents(svn_stream_t **stream,svn_fs_root_t *root,
                                      const char *path,apr_pool_t *pool)
{
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
    if(node->kind!=svn_node_file) return svn_error_create(SVN_ERR_FS_NOT_FILE,NULL,path);
    pgfs_reader_t *reader=apr_pcalloc(pool,sizeof(*reader));
    reader->fs=root->fs; reader->content=node->content; reader->cached_chunk=-1;
    const char *md5,*sha1;
    SVN_ERR(pgfs_content_metadata(&reader->length,&md5,&sha1,root->fs,node->content,pool));
    reader->scratch=svn_pool_create(pool);
    *stream=svn_stream_create(reader,pool);
    svn_stream_set_read2(*stream,pgfs_read_full,pgfs_read_full);
    svn_stream_set_mark(*stream,pgfs_read_mark);
    svn_stream_set_seek(*stream,pgfs_read_seek);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_file_length(svn_filesize_t *length,svn_fs_root_t *root,
                                    const char *path,apr_pool_t *pool)
{
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
    if(node->kind!=svn_node_file) return svn_error_create(SVN_ERR_FS_NOT_FILE,NULL,path);
    const char *md5,*sha1;
    return pgfs_content_metadata(length,&md5,&sha1,root->fs,node->content,pool);
}

static svn_error_t *pgfs_file_checksum(svn_checksum_t **checksum,svn_checksum_kind_t kind,
    svn_fs_root_t *root,const char *path,apr_pool_t *pool)
{
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
    if(node->kind!=svn_node_file) return svn_error_create(SVN_ERR_FS_NOT_FILE,NULL,path);
    if(kind!=svn_checksum_md5 && kind!=svn_checksum_sha1) { *checksum=NULL; return SVN_NO_ERROR; }
    svn_filesize_t length; const char *md5,*sha1;
    SVN_ERR(pgfs_content_metadata(&length,&md5,&sha1,root->fs,node->content,pool));
    return svn_checksum_parse_hex(checksum,kind,kind==svn_checksum_md5 ? md5 : sha1,pool);
}

typedef struct pgfs_writer_t {
    svn_fs_root_t *root;
    svn_fs_t *fs; /* Pool cleanup must not dereference a destroyed root pool. */
    const char *path;
    apr_int64_t content,base_content,node_id,copy_id,length,chunk;
    const char *content_string;
    const char *txn_name;
    svn_boolean_t leased;
    apr_size_t buffered;
    char *buffer;
    svn_boolean_t closed,failed;
    svn_checksum_ctx_t *md5,*sha1;
    svn_checksum_t *expected;
    apr_pool_t *pool,*scratch;
} pgfs_writer_t;

/* Session advisory locks provide the same one-writer-per-transaction
   guarantee as FSFS. They disappear automatically when a process/connection
   dies, so persisted staging can resume without a stale writer lease. */
static svn_error_t *pgfs_release_writer(pgfs_writer_t *writer)
{
    if(!writer->leased) return SVN_NO_ERROR;
    pgfs_data_t *data=writer->fs->fsap_data;
    apr_status_t status=apr_thread_mutex_lock(data->mutex);
    if(status) return svn_error_wrap_apr(status,"PgFS writer release lock failed");
    if(apr_hash_get(data->active_writers,writer->txn_name,APR_HASH_KEY_STRING)==writer)
        apr_hash_set(data->active_writers,writer->txn_name,APR_HASH_KEY_STRING,NULL);
    writer->leased=FALSE;
    if(data->observer.writer_changed) data->observer.writer_changed(data->observer.baton,-1);
    svn_error_t *error=SVN_NO_ERROR;
    if(data->connection && PQstatus(data->connection)==CONNECTION_OK) {
        const char *values[]={data->repository_id,writer->txn_name};
        PGresult *result;
        error=pgfs_query(&result,writer->fs,
            "SELECT pg_advisory_unlock(hashtextextended($1||':'||$2,0))",2,values);
        if(!error) PQclear(result);
    }
    status=apr_thread_mutex_unlock(data->mutex);
    return status ? svn_error_compose_create(error,svn_error_wrap_apr(status,"PgFS writer release unlock failed")) : error;
}

static svn_error_t *pgfs_abandon_writer(pgfs_writer_t *writer)
{
    pgfs_data_t *data=writer->fs->fsap_data;
    if(!data->connection || !writer->content_string) return SVN_NO_ERROR;
    const char *v[]={data->repository_id,writer->content_string};
    return pgfs_command(writer->fs,"UPDATE pgfs.contents SET abandoned=true "
        "WHERE repository_id=$1::uuid AND content_id=$2::bigint AND NOT ready",2,v);
}

static apr_status_t pgfs_writer_cleanup(void *baton)
{
    pgfs_writer_t *writer=baton;
    if(!writer->closed) {
        svn_error_t *error=pgfs_abandon_writer(writer);
        error=svn_error_compose_create(error,pgfs_release_writer(writer));
        if(error) svn_error_clear(error);
    }
    return APR_SUCCESS;
}

static svn_error_t *pgfs_writer_available(pgfs_writer_t *writer)
{
    SVN_ERR(pgfs_lock_txn(writer->root,writer->scratch));
    pgfs_data_t *data=writer->root->fs->fsap_data;
    const char *v[]={data->repository_id,pgfs_number(writer->content,writer->scratch)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,writer->root->fs,
        "SELECT 1 FROM pgfs.contents WHERE repository_id=$1::uuid AND content_id=$2::bigint "
        "AND NOT ready AND NOT abandoned",2,v));
    svn_boolean_t available=PQntuples(result)==1; PQclear(result);
    if(!available) return svn_error_create(SVN_ERR_FS_NOT_MUTABLE,NULL,"File writer was closed or superseded");
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_flush_body(pgfs_writer_t *writer)
{
    SVN_ERR(pgfs_writer_available(writer));
    pgfs_data_t *data=writer->root->fs->fsap_data;
    const char *v[]={data->repository_id,pgfs_number(writer->content,writer->scratch),
        pgfs_number(writer->chunk,writer->scratch),writer->buffer};
    int lengths[]={0,0,0,(int)writer->buffered},formats[]={0,0,0,1};
    PGresult *result;
    SVN_ERR(pgfs_query_full(&result,writer->root->fs,
        "INSERT INTO pgfs.content_chunks(repository_id,content_id,chunk_number,data) "
        "VALUES($1::uuid,$2::bigint,$3::bigint,$4::bytea)",4,v,lengths,formats,0));
    PQclear(result);
    ++writer->chunk; writer->buffered=0;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_write(void *baton,const char *bytes,apr_size_t *length)
{
    pgfs_writer_t *writer=baton;
    if(writer->closed || writer->failed) return svn_error_create(SVN_ERR_FS_NOT_MUTABLE,NULL,"File writer is not active");
    apr_size_t requested=*length,done=0; *length=0;
    SVN_ERR(svn_checksum_update(writer->md5,bytes,requested));
    SVN_ERR(svn_checksum_update(writer->sha1,bytes,requested));
    while(done<requested) {
        apr_size_t take=PGFS_CHUNK_SIZE-writer->buffered;
        if(take>requested-done) take=requested-done;
        memcpy(writer->buffer+writer->buffered,bytes+done,take);
        writer->buffered+=take; writer->length+=(apr_int64_t)take; done+=take; *length=done;
        if(writer->buffered==PGFS_CHUNK_SIZE) {
            svn_pool_clear(writer->scratch);
            svn_error_t *error=pgfs_unit_begin(writer->root->fs);
            if(!error) error=pgfs_unit_end(writer->root->fs,pgfs_flush_body(writer));
            if(error) { writer->failed=TRUE; return error; }
        }
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_close_body(pgfs_writer_t *writer,svn_checksum_t *md5,svn_checksum_t *sha1)
{
    SVN_ERR(pgfs_writer_available(writer));
    if(writer->buffered) SVN_ERR(pgfs_flush_body(writer));
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,writer->root,writer->path,FALSE,writer->scratch));
    if(node->node_id!=writer->node_id || node->copy_id!=writer->copy_id || node->content!=writer->base_content)
        return svn_error_create(SVN_ERR_FS_CONFLICT,NULL,"File changed while its text stream was open");
    pgfs_data_t *data=writer->root->fs->fsap_data;
    const char *v[]={data->repository_id,pgfs_number(writer->content,writer->scratch),
        pgfs_number(writer->length,writer->scratch),svn_checksum_to_cstring(md5,writer->scratch),
        svn_checksum_to_cstring(sha1,writer->scratch)};
    SVN_ERR(pgfs_command(writer->root->fs,
        "UPDATE pgfs.contents SET length=$3::bigint,md5=$4,sha1=$5,ready=true "
        "WHERE repository_id=$1::uuid AND content_id=$2::bigint",5,v));
    apr_int64_t id;
    SVN_ERR(pgfs_clone(&id,writer->root->fs,node,writer->root->txn,writer->path,writer->scratch));
    const char *values[]={data->repository_id,pgfs_number(id,writer->scratch),pgfs_number(writer->content,writer->scratch)};
    SVN_ERR(pgfs_command(writer->root->fs,"UPDATE pgfs.node_revisions SET content_id=$3::bigint "
        "WHERE repository_id=$1::uuid AND node_revision_id=$2::bigint",3,values));
    SVN_ERR(pgfs_replace_path(writer->root,writer->path,id,writer->scratch));
    return pgfs_record_change(writer->root,writer->path,TRUE,FALSE,FALSE,SVN_INVALID_REVNUM,NULL,writer->scratch);
}

static svn_error_t *pgfs_write_close(void *baton)
{
    pgfs_writer_t *writer=baton;
    if(writer->closed) return SVN_NO_ERROR;
    svn_pool_clear(writer->scratch);
    svn_checksum_t *md5,*sha1;
    svn_error_t *error=writer->failed ? svn_error_create(SVN_ERR_FS_GENERAL,NULL,"File stream had a write failure") : SVN_NO_ERROR;
    if(!error) error=svn_checksum_final(&md5,writer->md5,writer->scratch);
    if(!error) error=svn_checksum_final(&sha1,writer->sha1,writer->scratch);
    if(!error && writer->expected && !svn_checksum_match(writer->expected,
        writer->expected->kind==svn_checksum_md5 ? md5 : sha1))
        error=svn_error_createf(SVN_ERR_CHECKSUM_MISMATCH,NULL,"Result checksum mismatch for '%s'",writer->path);
    if(!error) {
        error=pgfs_unit_begin(writer->root->fs);
        if(!error) error=pgfs_unit_end(writer->root->fs,pgfs_close_body(writer,md5,sha1));
    }
    if(error) error=svn_error_compose_create(error,pgfs_abandon_writer(writer));
    error=svn_error_compose_create(error,pgfs_release_writer(writer));
    writer->closed=TRUE;
    return error;
}

static svn_error_t *pgfs_apply_text_body(pgfs_writer_t *writer)
{
    SVN_ERR(pgfs_lock_txn(writer->root,writer->scratch));
    SVN_ERR(pgfs_check_locks(writer->root,writer->path,FALSE,writer->scratch));
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,writer->root,writer->path,FALSE,writer->scratch));
    if(node->kind!=svn_node_file) return svn_error_create(SVN_ERR_FS_NOT_FILE,NULL,writer->path);
    writer->node_id=node->node_id; writer->copy_id=node->copy_id; writer->base_content=node->content;
    pgfs_data_t *data=writer->root->fs->fsap_data;
    const char *v[]={data->repository_id,writer->root->txn,writer->path};
    if(apr_hash_get(data->active_writers,writer->txn_name,APR_HASH_KEY_STRING))
        return svn_error_create(SVN_ERR_FS_REP_BEING_WRITTEN,NULL,"A text representation is already being written in this transaction");
    PGresult *result;
    SVN_ERR(pgfs_query(&result,writer->fs,
        "SELECT pg_try_advisory_lock(hashtextextended($1||':'||$2,0))",2,v));
    svn_boolean_t acquired=PQgetvalue(result,0,0)[0]=='t'; PQclear(result);
    if(!acquired) return svn_error_create(SVN_ERR_FS_REP_BEING_WRITTEN,NULL,"A text representation is already being written in this transaction");
    writer->leased=TRUE;
    if(data->observer.writer_changed) data->observer.writer_changed(data->observer.baton,1);
    apr_hash_set(data->active_writers,apr_pstrdup(writer->fs->pool,writer->txn_name),APR_HASH_KEY_STRING,writer);
    /* Holding the lease proves any unfinished old representation came from
       a departed connection. Its partial bytes must never become visible. */
    SVN_ERR(pgfs_command(writer->fs,"UPDATE pgfs.contents SET abandoned=true "
        "WHERE repository_id=$1::uuid AND owner_txn=$2 AND NOT ready AND NOT abandoned",2,v));
    SVN_ERR(pgfs_query(&result,writer->root->fs,
        "INSERT INTO pgfs.contents(repository_id,owner_txn,path) VALUES($1::uuid,$2,$3) RETURNING content_id",3,v));
    writer->content=apr_atoi64(PQgetvalue(result,0,0)); PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_apply_text(svn_stream_t **stream,svn_fs_root_t *root,const char *path,
                                    svn_checksum_t *expected,apr_pool_t *pool)
{
    pgfs_writer_t *writer=apr_pcalloc(pool,sizeof(*writer));
    writer->root=root; writer->fs=root->fs; writer->path=pgfs_path(path,pool); writer->pool=pool;
    writer->txn_name=apr_pstrdup(pool,root->txn ? root->txn : "");
    writer->scratch=svn_pool_create(pool); writer->expected=svn_checksum_dup(expected,pool);
    writer->buffer=apr_palloc(pool,PGFS_CHUNK_SIZE);
    writer->md5=svn_checksum_ctx_create(svn_checksum_md5,pool);
    writer->sha1=svn_checksum_ctx_create(svn_checksum_sha1,pool);
    apr_pool_cleanup_register(pool,writer,pgfs_writer_cleanup,apr_pool_cleanup_null);
    SVN_ERR(pgfs_unit_begin(root->fs));
    svn_error_t *error=pgfs_unit_end(root->fs,pgfs_apply_text_body(writer));
    if(error) return svn_error_compose_create(error,pgfs_release_writer(writer));
    writer->content_string=pgfs_number(writer->content,pool);
    *stream=svn_stream_create(writer,pool);
    svn_stream_set_write(*stream,pgfs_write); svn_stream_set_close(*stream,pgfs_write_close);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_apply_textdelta(svn_txdelta_window_handler_t *handler,void **baton,
    svn_fs_root_t *root,const char *path,svn_checksum_t *base,svn_checksum_t *result,apr_pool_t *pool)
{
    if(base) {
        svn_checksum_t *actual;
        SVN_ERR(pgfs_file_checksum(&actual,base->kind,root,path,pool));
        if(!svn_checksum_match(base,actual)) return svn_error_createf(SVN_ERR_CHECKSUM_MISMATCH,NULL,
            "Base checksum mismatch for '%s'",path);
    }
    svn_stream_t *source,*target;
    SVN_ERR(pgfs_file_contents(&source,root,path,pool));
    SVN_ERR(pgfs_apply_text(&target,root,path,result,pool));
    svn_txdelta_apply(source,target,NULL,path,pool,handler,baton);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_contents_changed(int *changed,svn_fs_root_t *a,const char *pa,
    svn_fs_root_t *b,const char *pb,svn_boolean_t strict,apr_pool_t *pool)
{
    pgfs_node_t *x,*y;
    SVN_ERR(pgfs_lookup(&x,a,pa,FALSE,pool)); SVN_ERR(pgfs_lookup(&y,b,pb,FALSE,pool));
    if(x->kind!=svn_node_file || y->kind!=svn_node_file) return svn_error_create(SVN_ERR_FS_NOT_FILE,NULL,"Expected files");
    *changed=x->content!=y->content;
    if(strict && *changed) {
        svn_filesize_t lx,ly; const char *mx,*sx,*my,*sy;
        SVN_ERR(pgfs_content_metadata(&lx,&mx,&sx,a->fs,x->content,pool));
        SVN_ERR(pgfs_content_metadata(&ly,&my,&sy,b->fs,y->content,pool));
        *changed=lx!=ly || strcmp(mx,my) || strcmp(sx,sy);
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_try_contents(svn_boolean_t *success,svn_fs_root_t *root,const char *path,
    svn_fs_process_contents_func_t processor,void *baton,apr_pool_t *pool)
{
    /* The optional direct-buffer fast path does not fit chunked fulltexts. */
    *success=FALSE;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_delta_stream(svn_txdelta_stream_t **stream,svn_fs_root_t *source,
    const char *source_path,svn_fs_root_t *target,const char *target_path,apr_pool_t *pool)
{
    svn_stream_t *a=svn_stream_empty(pool),*b;
    if(source) SVN_ERR(pgfs_file_contents(&a,source,source_path,pool));
    SVN_ERR(pgfs_file_contents(&b,target,target_path,pool));
    svn_txdelta2(stream,a,b,TRUE,pool);
    return SVN_NO_ERROR;
}
