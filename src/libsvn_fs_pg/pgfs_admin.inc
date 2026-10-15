/* Repository verification and database-backed administration. */
static svn_error_t *pgfs_verify_node(svn_fs_root_t *root,const pgfs_node_t *node,
    const char *path,apr_hash_t *seen,svn_boolean_t revision_local,
    svn_cancel_func_t cancel,void *baton,apr_pool_t *pool)
{
    if(cancel) SVN_ERR(cancel(baton));
    /* Recheck the revision constraint even when an immutable node was already
       verified through another root. Hash keys must outlive per-revision pools. */
    if(root->is_txn_root && node->owner) {
        if(strcmp(node->owner,root->txn) || node->created_rev!=SVN_INVALID_REVNUM)
            return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"A transaction references another transaction's PgFS node");
    } else if(node->owner || !SVN_IS_VALID_REVNUM(node->created_rev) || node->created_rev>root->rev)
        return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"A revision references an unpublished PgFS node");
    /* Like FSFS, per-revision checks trust older immutable subtrees. The global
       range check is responsible for their complete historical data. */
    if(revision_local && node->created_rev<root->rev) return SVN_NO_ERROR;
    void *state=apr_hash_get(seen,&node->id,sizeof(node->id));
    if(state==(void *)1) return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"Cycle in PgFS version tree");
    if(state==(void *)2) return SVN_NO_ERROR;
    const void *seen_key=apr_pmemdup(apr_hash_pool_get(seen),&node->id,sizeof(node->id));
    apr_hash_set(seen,seen_key,sizeof(node->id),(void *)1);
    const char *v[]={((pgfs_data_t *)root->fs->fsap_data)->repository_id,
        pgfs_number(node->properties,pool),pgfs_number(node->directory,pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,root->fs,
        "SELECT ($2::bigint=0 OR EXISTS(SELECT 1 FROM pgfs.property_sets WHERE repository_id=$1::uuid AND properties_id=$2::bigint)),"
        "($3::bigint=0 OR EXISTS(SELECT 1 FROM pgfs.directories WHERE repository_id=$1::uuid AND directory_id=$3::bigint))",3,v));
    svn_boolean_t valid=PQgetvalue(result,0,0)[0]=='t' && PQgetvalue(result,0,1)[0]=='t'; PQclear(result);
    if(!valid) return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"Missing PgFS property or directory representation");
    if(node->kind==svn_node_file) {
        if(node->directory) return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"File node has a directory representation");
        apr_pool_t *file_pool=svn_pool_create(pool);
        svn_filesize_t expected_length; const char *expected_md5,*expected_sha1;
        SVN_ERR(pgfs_content_metadata(&expected_length,&expected_md5,&expected_sha1,root->fs,node->content,file_pool));
        svn_checksum_ctx_t *md5=svn_checksum_ctx_create(svn_checksum_md5,file_pool),*sha1=svn_checksum_ctx_create(svn_checksum_sha1,file_pool);
        svn_stream_t *stream; SVN_ERR(pgfs_file_contents(&stream,root,path,file_pool));
        char *buffer=apr_palloc(file_pool,65536); svn_filesize_t length=0;
        for(;;) {
            if(cancel) SVN_ERR(cancel(baton));
            apr_size_t amount=65536; SVN_ERR(svn_stream_read_full(stream,buffer,&amount));
            length+=(svn_filesize_t)amount;
            SVN_ERR(svn_checksum_update(md5,buffer,amount)); SVN_ERR(svn_checksum_update(sha1,buffer,amount));
            if(!amount) break;
        }
        SVN_ERR(svn_stream_close(stream));
        svn_checksum_t *actual_md5,*actual_sha1;
        SVN_ERR(svn_checksum_final(&actual_md5,md5,file_pool)); SVN_ERR(svn_checksum_final(&actual_sha1,sha1,file_pool));
        if(length!=expected_length || strcmp(expected_md5,svn_checksum_to_cstring(actual_md5,file_pool)) ||
            strcmp(expected_sha1,svn_checksum_to_cstring(actual_sha1,file_pool)))
            return svn_error_createf(SVN_ERR_CHECKSUM_MISMATCH,NULL,"PgFS fulltext checksum mismatch at '%s'",path);
        svn_pool_destroy(file_pool);
    } else {
        if(node->content) return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"Directory node has file content");
        apr_hash_t *entries; SVN_ERR(pgfs_entries(&entries,root->fs,node->directory,pool));
        for(apr_hash_index_t *hi=apr_hash_first(pool,entries);hi;hi=apr_hash_next(hi)) {
            const void *key; void *value; apr_hash_this(hi,&key,NULL,&value);
            pgfs_node_t *child; SVN_ERR(pgfs_load_node(&child,root->fs,((pgfs_entry_t *)value)->id,pool));
            SVN_ERR(pgfs_verify_node(root,child,pgfs_join(path,key,pool),seen,revision_local,cancel,baton,pool));
        }
    }
    apr_hash_set(seen,seen_key,sizeof(node->id),(void *)2);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_verify_root(svn_fs_root_t *root,apr_pool_t *pool)
{
    pgfs_node_t *node; SVN_ERR(pgfs_lookup(&node,root,"/",FALSE,pool));
    return pgfs_verify_node(root,node,"/",apr_hash_make(pool),!root->is_txn_root,NULL,NULL,pool);
}

static svn_error_t *pgfs_verify_fs(svn_fs_t *fs,const char *path,svn_revnum_t start,svn_revnum_t end,
    svn_fs_progress_notify_func_t notify,void *notify_baton,svn_cancel_func_t cancel,void *cancel_baton,
    svn_mutex__t *common_pool_lock,apr_pool_t *pool,apr_pool_t *common_pool)
{
    SVN_ERR(pgfs_open(fs,path,common_pool_lock,pool,common_pool));
    svn_revnum_t head; SVN_ERR(pgfs_youngest(&head,fs,pool));
    if(!SVN_IS_VALID_REVNUM(start)) start=0;
    if(!SVN_IS_VALID_REVNUM(end)) end=head;
    if(start>end || end>head) return svn_error_create(SVN_ERR_FS_NO_SUCH_REVISION,NULL,"Invalid PgFS verification revision range");
    /* PgFS has no per-revision physical index shards. Announce one repository
       metadata verification pass, following the public callback convention. */
    if(notify) notify(SVN_INVALID_REVNUM,notify_baton,pool);
    /* Committed nodes and fulltexts are immutable. Preserve successful checks
       across ascending revisions while retaining cycle detection within a walk. */
    apr_hash_t *seen=apr_hash_make(pool);
    apr_pool_t *iter=svn_pool_create(pool);
    for(svn_revnum_t rev=start;rev<=end;++rev) {
        svn_pool_clear(iter);
        if(cancel) SVN_ERR(cancel(cancel_baton));
        svn_fs_root_t *root; SVN_ERR(pgfs_revision_root(&root,fs,rev,iter));
        pgfs_node_t *node; SVN_ERR(pgfs_lookup(&node,root,"/",FALSE,iter));
        SVN_ERR(pgfs_verify_node(root,node,"/",seen,FALSE,cancel,cancel_baton,iter));
        if(rev==LONG_MAX) break;
    }
    svn_pool_destroy(iter);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_freeze(svn_fs_t *fs,svn_fs_freeze_func_t callback,void *baton,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(fs));
    svn_revnum_t head;
    svn_error_t *error=pgfs_lock_repository(&head,fs,pool);
    if(!error) error=callback(baton,pool);
    return pgfs_unit_end(fs,error);
}

static svn_error_t *pgfs_recover(svn_fs_t *fs,svn_cancel_func_t cancel,void *baton,apr_pool_t *pool)
{
    if(cancel) SVN_ERR(cancel(baton));
    /* PostgreSQL has already recovered WAL before accepting this connection.
       Validate the published root without discarding persisted SVN staging. */
    svn_revnum_t head; SVN_ERR(pgfs_youngest(&head,fs,pool));
    svn_fs_root_t *root; SVN_ERR(pgfs_revision_root(&root,fs,head,pool));
    pgfs_node_t *node; SVN_ERR(pgfs_lookup(&node,root,"/",FALSE,pool));
    return pgfs_verify_node(root,node,"/",apr_hash_make(pool),FALSE,cancel,baton,pool);
}

static svn_error_t *pgfs_upgrade(svn_fs_t *fs,const char *path,svn_fs_upgrade_notify_t notify,
    void *notify_baton,svn_cancel_func_t cancel,void *cancel_baton,svn_mutex__t *common_pool_lock,
    apr_pool_t *pool,apr_pool_t *common_pool)
{
    if(cancel) SVN_ERR(cancel(cancel_baton));
    /* Format 1 is the only supported format, already current. Database schema
       migrations are installed once per database by the setup script. */
    return pgfs_open(fs,path,common_pool_lock,pool,common_pool);
}

static svn_error_t *pgfs_pack_body(svn_fs_t *fs,svn_cancel_func_t cancel,void *baton,apr_pool_t *pool)
{
    svn_revnum_t head; SVN_ERR(pgfs_lock_repository(&head,fs,pool));
    const char *v[]={((pgfs_data_t *)fs->fsap_data)->repository_id};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,"SELECT DISTINCT n.directory_id FROM pgfs.node_revisions n "
        "JOIN pgfs.directories d USING(repository_id,directory_id) WHERE n.repository_id=$1::uuid "
        "AND n.owner_txn IS NULL AND d.base_directory_id IS NOT NULL",1,v));
    for(int i=0;i<PQntuples(result);++i) {
        svn_error_t *error=cancel ? cancel(baton) : SVN_NO_ERROR;
        if(error) { PQclear(result); return error; }
        PGresult *checkpoint;
        error=pgfs_query(&checkpoint,fs,"INSERT INTO pgfs.directories(repository_id) VALUES($1::uuid) RETURNING directory_id",1,v);
        if(error) { PQclear(result); return error; }
        const char *values[]={v[0],PQgetvalue(result,i,0),apr_pstrdup(pool,PQgetvalue(checkpoint,0,0))};
        PQclear(checkpoint);
        error=pgfs_command(fs,PGFS_DIRECTORY_CHAIN
            ", latest AS (SELECT DISTINCT ON(e.name) e.name,e.child_node_revision_id FROM chain c "
            "JOIN pgfs.directory_entries e ON e.repository_id=$1::uuid AND e.directory_id=c.directory_id "
            "ORDER BY e.name,c.distance) INSERT INTO pgfs.directory_entries(repository_id,directory_id,name,child_node_revision_id) "
            "SELECT $1::uuid,$3::bigint,name,child_node_revision_id FROM latest WHERE child_node_revision_id IS NOT NULL",3,values);
        if(!error) error=pgfs_command(fs,"UPDATE pgfs.node_revisions SET directory_id=$3::bigint "
            "WHERE repository_id=$1::uuid AND directory_id=$2::bigint AND owner_txn IS NULL",3,values);
        if(error) { PQclear(result); return error; }
    }
    PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_pack(svn_fs_t *fs,const char *path,svn_fs_pack_notify_t notify,void *notify_baton,
    svn_cancel_func_t cancel,void *cancel_baton,svn_mutex__t *common_pool_lock,
    apr_pool_t *pool,apr_pool_t *common_pool)
{
    SVN_ERR(pgfs_open(fs,path,common_pool_lock,pool,common_pool));
    SVN_ERR(pgfs_unit_begin(fs));
    /* Compaction changes only physical directory representations. Node IDs,
       revision numbers, history and fulltexts remain unchanged. */
    return pgfs_unit_end(fs,pgfs_pack_body(fs,cancel,cancel_baton,pool));
}

static svn_error_t *pgfs_bdb_logs(apr_array_header_t **files,const char *path,
                                  svn_boolean_t unused,apr_pool_t *pool)
{
    return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"PgFS stores data in PostgreSQL; Berkeley DB log files do not apply");
}

static svn_error_t *pgfs_bdb_errcall(svn_fs_t *fs,void (*handler)(const char *,char *))
{
    return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"Berkeley DB callbacks do not apply to PgFS");
}

static svn_error_t *pgfs_ioctl(svn_fs_t *fs,svn_fs_ioctl_code_t code,void *input,void **output,
    svn_cancel_func_t cancel,void *baton,apr_pool_t *result_pool,apr_pool_t *scratch_pool)
{
    return svn_error_createf(SVN_ERR_FS_UNRECOGNIZED_IOCTL_CODE,NULL,"PgFS does not support filesystem control '%s:%d'",code.fs_type,code.code);
}

static svn_error_t *pgfs_library_ioctl(svn_fs_ioctl_code_t code,void *input,void **output,
    svn_cancel_func_t cancel,void *baton,apr_pool_t *result_pool,apr_pool_t *scratch_pool)
{
    return pgfs_ioctl(NULL,code,input,output,cancel,baton,result_pool,scratch_pool);
}
