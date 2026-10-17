/* Directory-free storage entry points used by the statically linked core. */
svn_error_t *pgfs_native_create_records(PGconn *connection,const char *id,
    const char *uuid,const char *instance,const char *date)
{
    const char *values[]={id,uuid,instance,date};
    PGresult *result=pgfs_pq_exchange(connection,
        "WITH repo AS (INSERT INTO pgfs.repositories(repository_id,svn_uuid,instance_id,format_version) "
        "VALUES($1::uuid,$2::uuid,$3::uuid,1) RETURNING repository_id), "
        "node AS (INSERT INTO pgfs.node_revisions(repository_id,node_id,kind,created_rev,created_path) "
        "SELECT repository_id,0,'dir',0,'/' FROM repo RETURNING repository_id,node_revision_id), "
        "rev AS (INSERT INTO pgfs.revisions(repository_id,rev,root_node_revision_id) "
        "SELECT repository_id,0,node_revision_id FROM node RETURNING repository_id,rev) "
        "INSERT INTO pgfs.revision_properties(repository_id,rev,name,value) "
        "SELECT repository_id,rev,'svn:date',convert_to($4,'UTF8') FROM rev",4,NULL,values,NULL,NULL,0,6000,NULL,NULL,0);
    if(!result || PQresultStatus(result)!=PGRES_COMMAND_OK) {
        const char *state=result ? PQresultErrorField(result,PG_DIAG_SQLSTATE) : NULL;
        svn_error_t *error=svn_error_createf(SVN_ERR_FS_GENERAL,NULL,
            "PgFS repository creation failed (SQLSTATE %s)",state ? state : "08006");
        if(result) PQclear(result);
        return error;
    }
    PQclear(result);
    return SVN_NO_ERROR;
}

svn_error_t *pgfs_native_bind(svn_fs_t *fs,PGconn *connection,const char *dsn,
    const char *id,const pgfs_observer *observer,apr_pool_t *pool)
{
    /* Transfer connection ownership even when later validation fails. */
    SVN_ERR(pgfs_attach(fs,apr_pstrcat(pool,"pgvn:",id,NULL),dsn,id,connection,observer));
    ((pgfs_data_t *)fs->fsap_data)->directory_adapter=FALSE;
    fs_library_vtable_t *unused;
    const svn_version_t version={1,14,5,""};
    SVN_ERR(svn_fs_pg__init(&version,&unused,pool));
    SVN_ERR(pgfs_load_metadata(fs,id));
    const char *values[]={id};
    /* Session lease: purge/incremental replacement must prove no other
     * process still owns a database FS handle before deleting its rows. */
    return pgfs_command(fs,"SELECT pg_advisory_lock_shared(hashtextextended('pgvn-repo:'||$1,0))",1,values);
}

svn_error_t *pgfs_native_pack(svn_fs_t *fs,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(fs));
    return pgfs_unit_end(fs,pgfs_pack_body(fs,NULL,NULL,pool));
}

svn_error_t *pgfs_native_recover(svn_fs_t *fs,apr_pool_t *pool)
{
    return pgfs_recover(fs,NULL,NULL,pool);
}

svn_error_t *pgfs_native_delete(svn_fs_t *fs,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(fs));
    return pgfs_unit_end(fs,pgfs_delete_fs_body(fs,pool));
}

svn_error_t *pgfs_native_hotcopy(svn_fs_t *source,svn_fs_t *target,
    svn_boolean_t fresh,apr_pool_t *pool)
{
    pgfs_data_t *a=source->fsap_data,*b=target->fsap_data;
    if(strcmp(a->connection_string,b->connection_string))
        return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"Hotcopy requires the same PostgreSQL database");
    if(!strcmp(a->repository_id,b->repository_id))
        return svn_error_create(SVN_ERR_FS_ALREADY_EXISTS,NULL,"Hotcopy source and destination must differ");
    SVN_ERR(pgfs_unit_begin(source));
    return pgfs_unit_end(source,pgfs_hotcopy_body(source,target,fresh,NULL,NULL,pool));
}

svn_error_t *pgfs_native_atomic(svn_fs_t *fs,pgfs_native_atomic_callback callback,void *baton,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(fs));
    return pgfs_unit_end(fs,callback(((pgfs_data_t *)fs->fsap_data)->connection,baton,pool));
}

static svn_error_t *pgfs_native_bound_unit_begin(svn_fs_t *fs)
{
    if(!fs || fs->vtable!=&pgfs_fs_vtable || !fs->fsap_data)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgFS native binding requires a PgFS handle");
    pgfs_data_t *data=fs->fsap_data;
    apr_status_t status=apr_thread_mutex_lock(data->mutex);
    if(status) return svn_error_wrap_apr(status,"PgFS bound transaction lock failed");
    svn_error_t *error=NULL;
    /* The ordinary unit helper does not implement SQL nesting. Check while
     * owning the same nested mutex used by every operation on this PGconn. */
    if(!data->connection || PQstatus(data->connection)!=CONNECTION_OK)
        error=svn_error_create(SVN_ERR_FS_GENERAL,NULL,"PgFS bound transaction database connection is unavailable");
    else if(PQtransactionStatus(data->connection)!=PQTRANS_IDLE)
        error=svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgFS bound transaction requires an idle connection");
    else error=pgfs_unit_begin(fs);
    if(error) {
        status=apr_thread_mutex_unlock(data->mutex);
        if(status) error=svn_error_compose_create(error,svn_error_wrap_apr(status,"PgFS bound transaction unlock failed"));
    }
    return error;
}
static svn_error_t *pgfs_native_bound_unit_end(svn_fs_t *fs,svn_error_t *error)
{
    error=pgfs_unit_end(fs,error);
    apr_status_t status=apr_thread_mutex_unlock(((pgfs_data_t *)fs->fsap_data)->mutex);
    return status ? svn_error_compose_create(error,svn_error_wrap_apr(status,"PgFS bound transaction unlock failed")) : error;
}

svn_error_t *pgfs_native_begin_bound_txn(svn_fs_txn_t **txn,svn_fs_t *fs,
    svn_revnum_t base,apr_uint32_t flags,const apr_array_header_t *properties,
    pgfs_native_txn_binding_callback callback,void *baton,apr_pool_t *pool)
{
    if(!txn || !callback || !SVN_IS_VALID_REVNUM(base) || (properties &&
        (properties->elt_size!=(int)sizeof(svn_prop_t) || properties->nelts<0 || (properties->nelts && !properties->elts))))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid PgFS bound transaction creation");
    if(properties) for(int i=0;i<properties->nelts;++i) {
        const svn_prop_t *prop=&APR_ARRAY_IDX(properties,i,svn_prop_t);
        if(!prop->name || !*prop->name)
            return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid PgFS transaction property name");
    }
    SVN_ERR(pgfs_native_bound_unit_begin(fs));
    svn_fs_txn_t *created=NULL;
    svn_error_t *error=pgfs_begin_body(&created,fs,base,flags,pool);
    if(!error && properties) error=pgfs_txn_props_body(created,properties,pool);
    if(!error) error=callback(((pgfs_data_t *)fs->fsap_data)->connection,created,baton,pool);
    error=pgfs_native_bound_unit_end(fs,error);
    if(!error) *txn=created;
    return error;
}

svn_error_t *pgfs_native_abort_bound_txn(svn_fs_t *fs,const char *transaction_id,
    pgfs_native_atomic_callback callback,void *baton,apr_pool_t *pool)
{
    if(!transaction_id || !*transaction_id || !callback)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid PgFS bound transaction abort");
    SVN_ERR(pgfs_native_bound_unit_begin(fs));
    svn_error_t *error=pgfs_abort_body(fs,transaction_id,pool);
    if(!error) error=callback(((pgfs_data_t *)fs->fsap_data)->connection,baton,pool);
    return pgfs_native_bound_unit_end(fs,error);
}

svn_error_t *pgfs_native_delete_rows(svn_fs_t *fs,apr_pool_t *pool)
{
    return pgfs_delete_fs_body(fs,pool);
}

svn_error_t *pgfs_native_exclusive(svn_fs_t *fs,apr_pool_t *pool)
{
    pgfs_data_t *data=fs->fsap_data;
    const char *values[]={data->repository_id}; PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,"SELECT pg_try_advisory_lock(hashtextextended('pgvn-repo:'||$1,0))",1,values));
    svn_boolean_t locked=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
    if(!locked) return svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"Repository still has open handles");
    data->exclusive_lease=TRUE;
    return SVN_NO_ERROR;
}

svn_error_t *pgfs_native_hotcopy_rows(svn_fs_t *source,const char *destination,
    svn_boolean_t fresh,apr_pool_t *pool)
{
    pgfs_data_t data;
    svn_fs_t target;
    memset(&data,0,sizeof(data)); memset(&target,0,sizeof(target));
    data.repository_id=destination;
    target.fsap_data=&data;
    target.uuid=source->uuid;
    return pgfs_hotcopy_body(source,&target,fresh,NULL,NULL,pool);
}

svn_error_t *pgfs_native_verify(svn_fs_t *fs,svn_revnum_t start,svn_revnum_t end,
    svn_fs_progress_notify_func_t notify,void *notify_baton,
    svn_cancel_func_t cancel,void *cancel_baton,apr_pool_t *pool)
{
    return pgfs_verify_bound(fs,start,end,notify,notify_baton,cancel,cancel_baton,pool);
}
