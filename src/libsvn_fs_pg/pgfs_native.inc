/* Directory-free storage entry points used by the statically linked core. */
svn_error_t *pgfs_native_create_records(PGconn *connection,const char *id,
    const char *uuid,const char *instance,const char *date)
{
    const char *values[]={id,uuid,instance,date};
    PGresult *result=PQexecParams(connection,
        "WITH repo AS (INSERT INTO pgfs.repositories(repository_id,svn_uuid,instance_id,format_version) "
        "VALUES($1::uuid,$2::uuid,$3::uuid,1) RETURNING repository_id), "
        "node AS (INSERT INTO pgfs.node_revisions(repository_id,node_id,kind,created_rev,created_path) "
        "SELECT repository_id,0,'dir',0,'/' FROM repo RETURNING repository_id,node_revision_id), "
        "rev AS (INSERT INTO pgfs.revisions(repository_id,rev,root_node_revision_id) "
        "SELECT repository_id,0,node_revision_id FROM node RETURNING repository_id,rev) "
        "INSERT INTO pgfs.revision_properties(repository_id,rev,name,value) "
        "SELECT repository_id,rev,'svn:date',convert_to($4,'UTF8') FROM rev",4,NULL,values,NULL,NULL,0);
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
    return pgfs_load_metadata(fs,id);
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
