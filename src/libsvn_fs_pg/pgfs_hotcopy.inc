/* Hotcopies retain SVN UUID and history but have independent storage identity.
   Stable object maps also support subsequent incremental native hotcopies. */
static svn_error_t *pgfs_hotcopy_body(svn_fs_t *source,svn_fs_t *target,svn_boolean_t fresh,
    svn_cancel_func_t cancel,void *baton,apr_pool_t *pool)
{
    pgfs_data_t *src=source->fsap_data,*dst=target->fsap_data;
    const char *v[]={src->repository_id,dst->repository_id,pgfs_uuid(pool)};
    if(fresh) SVN_ERR(pgfs_command(source,
        "INSERT INTO pgfs.repositories(repository_id,svn_uuid,instance_id,format_version,hotcopy_source) "
        "SELECT $2::uuid,svn_uuid,$3::uuid,format_version,repository_id FROM pgfs.repositories WHERE repository_id=$1::uuid",3,v));
    PGresult *result;
    SVN_ERR(pgfs_query(&result,source,"SELECT repository_id FROM pgfs.repositories "
        "WHERE repository_id IN($1::uuid,$2::uuid) ORDER BY repository_id FOR UPDATE",2,v));
    svn_boolean_t both=PQntuples(result)==2; PQclear(result);
    if(!both) return svn_error_create(SVN_ERR_FS_GENERAL,NULL,"Hotcopy destination must use the source PostgreSQL database");
    SVN_ERR(pgfs_query(&result,source,
        "SELECT d.hotcopy_source=s.repository_id AND d.svn_uuid=s.svn_uuid AND d.head_rev<=s.head_rev "
        "AND NOT EXISTS(SELECT 1 FROM pgfs.revisions dr LEFT JOIN pgfs.revisions sr "
        "ON sr.repository_id=$1::uuid AND sr.rev=dr.rev WHERE dr.repository_id=$2::uuid "
        "AND (sr.rev IS NULL OR dr.origin_txn_id IS DISTINCT FROM sr.origin_txn_id)) "
        "FROM pgfs.repositories s JOIN pgfs.repositories d ON d.repository_id=$2::uuid WHERE s.repository_id=$1::uuid",2,v));
    svn_boolean_t compatible=PQntuples(result)==1 && PQgetvalue(result,0,0)[0]=='t'; PQclear(result);
    if(!compatible) return svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"Incremental hotcopy destination has diverged or belongs to another source");
    const char *kinds[]={"node","content","properties","directory"};
    const char *tables[]={"node_revisions","contents","property_sets","directories"};
    const char *keys[]={"node_revision_id","content_id","properties_id","directory_id"};
    for(int i=0;i<4;++i) {
        if(cancel) SVN_ERR(cancel(baton));
        const char *filter=i==0 ? " AND owner_txn IS NULL" : i==1 ? " AND ready AND NOT abandoned" : "";
        const char *sql=apr_psprintf(pool,
            "INSERT INTO pgfs.hotcopy_objects(destination_repository_id,source_repository_id,object_kind,source_id,target_id) "
            "SELECT $2::uuid,$1::uuid,'%s',%s,nextval(pg_get_serial_sequence('pgfs.%s','%s')) FROM pgfs.%s "
            "WHERE repository_id=$1::uuid%s ON CONFLICT(destination_repository_id,object_kind,source_id) DO NOTHING",
            kinds[i],keys[i],tables[i],keys[i],tables[i],filter);
        SVN_ERR(pgfs_command(source,sql,2,v));
    }
    const char *statements[]={
        "INSERT INTO pgfs.contents(repository_id,content_id,owner_txn,path,length,md5,sha1,ready,abandoned) "
        "SELECT $2::uuid,m.target_id,c.owner_txn,c.path,c.length,c.md5,c.sha1,true,false FROM pgfs.contents c "
        "JOIN pgfs.hotcopy_objects m ON m.destination_repository_id=$2::uuid AND m.source_repository_id=$1::uuid "
        "AND m.object_kind='content' AND m.source_id=c.content_id WHERE c.repository_id=$1::uuid "
        "ON CONFLICT(content_id) DO NOTHING",

        "INSERT INTO pgfs.content_chunks(repository_id,content_id,chunk_number,data) "
        "SELECT $2::uuid,m.target_id,c.chunk_number,c.data FROM pgfs.content_chunks c JOIN pgfs.hotcopy_objects m "
        "ON m.destination_repository_id=$2::uuid AND m.source_repository_id=$1::uuid AND m.object_kind='content' "
        "AND m.source_id=c.content_id WHERE c.repository_id=$1::uuid ON CONFLICT DO NOTHING",

        "INSERT INTO pgfs.property_sets(repository_id,properties_id) SELECT $2::uuid,m.target_id FROM pgfs.property_sets p "
        "JOIN pgfs.hotcopy_objects m ON m.destination_repository_id=$2::uuid AND m.source_repository_id=$1::uuid "
        "AND m.object_kind='properties' AND m.source_id=p.properties_id WHERE p.repository_id=$1::uuid "
        "ON CONFLICT(properties_id) DO NOTHING",

        "INSERT INTO pgfs.properties(repository_id,properties_id,name,value) SELECT $2::uuid,m.target_id,p.name,p.value "
        "FROM pgfs.properties p JOIN pgfs.hotcopy_objects m ON m.destination_repository_id=$2::uuid "
        "AND m.source_repository_id=$1::uuid AND m.object_kind='properties' AND m.source_id=p.properties_id "
        "WHERE p.repository_id=$1::uuid ON CONFLICT DO NOTHING",

        "INSERT INTO pgfs.directories(repository_id,directory_id,base_directory_id,depth) "
        "SELECT $2::uuid,m.target_id,b.target_id,d.depth FROM pgfs.directories d JOIN pgfs.hotcopy_objects m "
        "ON m.destination_repository_id=$2::uuid AND m.source_repository_id=$1::uuid AND m.object_kind='directory' "
        "AND m.source_id=d.directory_id LEFT JOIN pgfs.hotcopy_objects b ON b.destination_repository_id=$2::uuid "
        "AND b.object_kind='directory' AND b.source_id=d.base_directory_id WHERE d.repository_id=$1::uuid "
        "ON CONFLICT(directory_id) DO NOTHING",

        "INSERT INTO pgfs.node_revisions(repository_id,node_revision_id,node_id,copy_id,predecessor_id,predecessor_count,kind,"
        "created_rev,created_path,copyfrom_rev,copyfrom_path,content_id,properties_id,directory_id) "
        "SELECT $2::uuid,m.target_id,n.node_id,n.copy_id,pred.target_id,n.predecessor_count,n.kind,n.created_rev,n.created_path,"
        "n.copyfrom_rev,n.copyfrom_path,c.target_id,p.target_id,d.target_id FROM pgfs.node_revisions n "
        "JOIN pgfs.hotcopy_objects m ON m.destination_repository_id=$2::uuid AND m.source_repository_id=$1::uuid "
        "AND m.object_kind='node' AND m.source_id=n.node_revision_id "
        "LEFT JOIN pgfs.hotcopy_objects pred ON pred.destination_repository_id=$2::uuid AND pred.object_kind='node' AND pred.source_id=n.predecessor_id "
        "LEFT JOIN pgfs.hotcopy_objects c ON c.destination_repository_id=$2::uuid AND c.object_kind='content' AND c.source_id=n.content_id "
        "LEFT JOIN pgfs.hotcopy_objects p ON p.destination_repository_id=$2::uuid AND p.object_kind='properties' AND p.source_id=n.properties_id "
        "LEFT JOIN pgfs.hotcopy_objects d ON d.destination_repository_id=$2::uuid AND d.object_kind='directory' AND d.source_id=n.directory_id "
        "WHERE n.repository_id=$1::uuid ON CONFLICT(node_revision_id) DO NOTHING",

        "INSERT INTO pgfs.directory_entries(repository_id,directory_id,name,child_node_revision_id) "
        "SELECT $2::uuid,d.target_id,e.name,n.target_id FROM pgfs.directory_entries e JOIN pgfs.hotcopy_objects d "
        "ON d.destination_repository_id=$2::uuid AND d.source_repository_id=$1::uuid AND d.object_kind='directory' AND d.source_id=e.directory_id "
        "LEFT JOIN pgfs.hotcopy_objects n ON n.destination_repository_id=$2::uuid AND n.object_kind='node' AND n.source_id=e.child_node_revision_id "
        "WHERE e.repository_id=$1::uuid AND (e.child_node_revision_id IS NULL OR n.target_id IS NOT NULL) ON CONFLICT DO NOTHING",

        "INSERT INTO pgfs.revisions(repository_id,rev,root_node_revision_id,origin_txn_id) "
        "SELECT $2::uuid,r.rev,m.target_id,r.origin_txn_id FROM pgfs.revisions r JOIN pgfs.hotcopy_objects m "
        "ON m.destination_repository_id=$2::uuid AND m.source_repository_id=$1::uuid AND m.object_kind='node' "
        "AND m.source_id=r.root_node_revision_id WHERE r.repository_id=$1::uuid ON CONFLICT DO NOTHING",

        "DELETE FROM pgfs.revision_properties WHERE repository_id=$2::uuid",
        "INSERT INTO pgfs.revision_properties(repository_id,rev,name,value,generation) SELECT $2::uuid,rev,name,value,generation "
        "FROM pgfs.revision_properties WHERE repository_id=$1::uuid",

        "INSERT INTO pgfs.revision_changes(repository_id,rev,path,node_revision_id,change_kind,node_kind,text_mod,prop_mod,mergeinfo_mod,copyfrom_rev,copyfrom_path) "
        "SELECT $2::uuid,r.rev,r.path,m.target_id,r.change_kind,r.node_kind,r.text_mod,r.prop_mod,r.mergeinfo_mod,r.copyfrom_rev,r.copyfrom_path "
        "FROM pgfs.revision_changes r JOIN pgfs.hotcopy_objects m ON m.destination_repository_id=$2::uuid "
        "AND m.source_repository_id=$1::uuid AND m.object_kind='node' AND m.source_id=r.node_revision_id "
        "WHERE r.repository_id=$1::uuid ON CONFLICT DO NOTHING",

        "DELETE FROM pgfs.locks WHERE repository_id=$2::uuid",
        "INSERT INTO pgfs.locks(repository_id,path,token,owner,comment,is_dav_comment,creation_date,expiration_date) "
        "SELECT $2::uuid,path,token,owner,comment,is_dav_comment,creation_date,expiration_date FROM pgfs.locks WHERE repository_id=$1::uuid",
        "UPDATE pgfs.repositories SET head_rev=(SELECT head_rev FROM pgfs.repositories WHERE repository_id=$1::uuid) WHERE repository_id=$2::uuid"
    };
    for(apr_size_t i=0;i<sizeof(statements)/sizeof(statements[0]);++i) {
        if(cancel) SVN_ERR(cancel(baton));
        SVN_ERR(pgfs_command(source,statements[i],2,v));
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_hotcopy(svn_fs_t *source,svn_fs_t *target,const char *source_path,const char *target_path,
    svn_boolean_t clean,svn_boolean_t incremental,svn_fs_hotcopy_notify_t notify,void *notify_baton,
    svn_cancel_func_t cancel,void *cancel_baton,svn_mutex__t *common_pool_lock,apr_pool_t *pool,apr_pool_t *common_pool)
{
    SVN_ERR(pgfs_open(source,source_path,common_pool_lock,pool,common_pool));
    const char *config=svn_dirent_join(target_path,"pgfs.conf",pool);
    svn_node_kind_t kind; SVN_ERR(svn_io_check_path(config,&kind,pool));
    svn_boolean_t fresh=kind==svn_node_none;
    if(!fresh && !incremental) return svn_error_create(SVN_ERR_FS_ALREADY_OPEN,NULL,"Hotcopy destination already exists; use --incremental");
    if(fresh) {
        SVN_ERR(svn_io_make_dir_recursively(target_path,pool));
        SVN_ERR(pgfs_connect(target,target_path,((pgfs_data_t *)source->fsap_data)->connection_string,pgfs_uuid(pool)));
        target->uuid=apr_pstrdup(target->pool,source->uuid);
    } else {
        SVN_ERR(pgfs_open(target,target_path,common_pool_lock,pool,common_pool));
        if(strcmp(((pgfs_data_t *)source->fsap_data)->connection_string,((pgfs_data_t *)target->fsap_data)->connection_string))
            return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"Hotcopy uses one PostgreSQL database; use dump/load to transfer across databases");
    }
    SVN_ERR(pgfs_unit_begin(source));
    svn_error_t *error=pgfs_hotcopy_body(source,target,fresh,cancel,cancel_baton,pool);
    if(!error && fresh) {
        pgfs_data_t *data=target->fsap_data;
        error=svn_io_file_create(config,apr_psprintf(pool,"[pgfs]\nrepository = %s\nconnection = %s\n",
            data->repository_id,data->connection_string),pool);
    }
    error=pgfs_unit_end(source,error);
    if(error && fresh) error=svn_error_compose_create(error,svn_io_remove_file2(config,TRUE,pool));
    if(error) return error;
    svn_revnum_t head; SVN_ERR(pgfs_youngest(&head,target,pool));
    if(notify) notify(notify_baton,0,head,pool);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_delete_fs_body(svn_fs_t *fs,apr_pool_t *pool)
{
    svn_revnum_t head; SVN_ERR(pgfs_lock_repository(&head,fs,pool));
    const char *v[]={((pgfs_data_t *)fs->fsap_data)->repository_id};
    const char *tables[]={"hotcopy_objects","locks","revision_properties","revision_changes","transaction_properties",
        "transaction_changes","transactions","revisions","directory_entries","node_revisions","properties",
        "property_sets","content_chunks","contents","directories","repositories"};
    for(apr_size_t i=0;i<sizeof(tables)/sizeof(tables[0]);++i) {
        const char *key=i==0 ? "destination_repository_id" : "repository_id";
        SVN_ERR(pgfs_command(fs,apr_psprintf(pool,"DELETE FROM pgfs.%s WHERE %s=$1::uuid",tables[i],key),1,v));
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_delete_fs(const char *path,apr_pool_t *pool)
{
    svn_fs_t *fs=apr_pcalloc(pool,sizeof(*fs)); fs->pool=pool;
    SVN_ERR(pgfs_open(fs,path,NULL,pool,pool));
    SVN_ERR(pgfs_unit_begin(fs));
    SVN_ERR(pgfs_unit_end(fs,pgfs_delete_fs_body(fs,pool)));
    return svn_io_remove_dir2(path,FALSE,NULL,NULL,pool);
}
