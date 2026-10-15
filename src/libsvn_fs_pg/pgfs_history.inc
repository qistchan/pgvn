/* Copy ancestry and history are reconstructed from immutable copy events and
   node lineage. Unmodified descendants remain shared across a cheap copy. */
static svn_error_t *pgfs_copy_body(svn_fs_root_t *source,const char *source_path,
    svn_fs_root_t *target,const char *target_path,svn_boolean_t history,apr_pool_t *pool)
{
    SVN_ERR(pgfs_lock_txn(target,pool));
    if(source->vtable!=&pgfs_root_vtable) return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"Cannot copy across filesystem providers");

    SVN_ERR(pgfs_check_locks(target,target_path,TRUE,pool));
    pgfs_data_t *a=source->fs->fsap_data,*b=target->fs->fsap_data;
    if(strcmp(a->repository_id,b->repository_id))
        return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"Cannot copy across repositories");
    if(source->is_txn_root) return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"Copy source must be a revision root");
    source_path=pgfs_path(source_path,pool); target_path=pgfs_path(target_path,pool);
    if(!strcmp(target_path,"/")) return svn_error_create(SVN_ERR_FS_ROOT_DIR,NULL,"Cannot replace the root directory by a copy");
    pgfs_node_t *node,*existing;
    SVN_ERR(pgfs_lookup(&node,source,source_path,FALSE,pool));
    SVN_ERR(pgfs_lookup(&existing,target,target_path,TRUE,pool));
    if(existing && existing->id==node->id) return SVN_NO_ERROR;
    const char *remove[]={b->repository_id,target->txn,target_path};
    SVN_ERR(pgfs_command(target->fs,"DELETE FROM pgfs.transaction_changes WHERE repository_id=$1::uuid "
        "AND txn_id=$2 AND (path=$3 OR starts_with(path,$3||'/'))",3,remove));
    apr_int64_t id=node->id;
    if(history) {
        const char *v[]={b->repository_id,pgfs_number(node->id,pool),target->txn,target_path,
            pgfs_number(source->rev,pool),source_path};
        PGresult *result;
        SVN_ERR(pgfs_query(&result,target->fs,
            "INSERT INTO pgfs.node_revisions(repository_id,node_id,copy_id,predecessor_id,predecessor_count,"
            "kind,created_rev,created_path,content_id,properties_id,directory_id,owner_txn,copyfrom_rev,copyfrom_path) "
            "SELECT repository_id,node_id,nextval('pgfs.copy_ids'),node_revision_id,predecessor_count+1,"
            "kind,-1,$4,content_id,properties_id,directory_id,$3,$5::bigint,$6 "
            "FROM pgfs.node_revisions WHERE repository_id=$1::uuid AND node_revision_id=$2::bigint RETURNING node_revision_id",
            6,v));
        id=apr_atoi64(PQgetvalue(result,0,0)); PQclear(result);
    }
    SVN_ERR(pgfs_replace_path(target,target_path,id,pool));
    return pgfs_record_change(target,target_path,FALSE,FALSE,FALSE,
                             source->rev,source_path,pool);
}

static svn_error_t *pgfs_copy(svn_fs_root_t *source,const char *source_path,
    svn_fs_root_t *target,const char *target_path,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(target->fs));
    return pgfs_unit_end(target->fs,pgfs_copy_body(source,source_path,target,target_path,TRUE,pool));
}

static svn_error_t *pgfs_revision_link(svn_fs_root_t *source,svn_fs_root_t *target,const char *path,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(target->fs));
    return pgfs_unit_end(target->fs,pgfs_copy_body(source,path,target,path,FALSE,pool));
}

static svn_error_t *pgfs_copied_from(svn_revnum_t *rev,const char **path,
    svn_fs_root_t *root,const char *node_path,apr_pool_t *pool)
{
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,node_path,FALSE,pool));
    *rev=node->copyfrom_rev; *path=node->copyfrom_path;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_closest_copy(svn_fs_root_t **copy_root,const char **copy_path,
    svn_fs_root_t *root,const char *path,apr_pool_t *pool)
{
    *copy_root=NULL; *copy_path=NULL;
    if(root->is_txn_root) return svn_error_create(SVN_ERR_FS_NOT_REVISION_ROOT,NULL,"Copy history requires a revision root");
    path=pgfs_path(path,pool);
    pgfs_node_t *current;
    SVN_ERR(pgfs_lookup(&current,root,path,FALSE,pool));
    const char *v[]={((pgfs_data_t *)root->fs->fsap_data)->repository_id,pgfs_number(root->rev,pool),path};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,root->fs,
        "SELECT c.rev,c.path FROM pgfs.revision_changes c JOIN pgfs.node_revisions n "
        "ON n.repository_id=c.repository_id AND n.node_revision_id=c.node_revision_id "
        "WHERE c.repository_id=$1::uuid AND c.rev<=$2::bigint AND c.copyfrom_rev IS NOT NULL "
        "AND n.created_rev=c.rev AND n.copyfrom_rev=c.copyfrom_rev AND n.copyfrom_path=c.copyfrom_path "
        "AND ($3=c.path OR starts_with($3,c.path||'/')) ORDER BY c.rev DESC,length(c.path) DESC",3,v));
    for(int i=0;i<PQntuples(result);++i) {
        svn_fs_root_t *candidate;
        svn_error_t *error=pgfs_revision_root(&candidate,root->fs,(svn_revnum_t)apr_atoi64(PQgetvalue(result,i,0)),pool);
        pgfs_node_t *copied=NULL;
        if(!error) error=pgfs_lookup(&copied,candidate,path,TRUE,pool);
        if(error) { PQclear(result); return error; }
        if(!copied || copied->node_id!=current->node_id) continue;
        if(copied->created_rev==candidate->rev && !copied->predecessor) break;
        *copy_root=candidate; *copy_path=apr_pstrdup(pool,PQgetvalue(result,i,1));
        break;
    }
    PQclear(result);
    return SVN_NO_ERROR;
}

typedef struct pgfs_history_data_t {
    svn_fs_t *fs;
    const char *path;
    svn_revnum_t rev;
    svn_boolean_t reported;
} pgfs_history_data_t;

static svn_fs_history_t *pgfs_history(svn_fs_t *fs,const char *path,svn_revnum_t rev,
    svn_boolean_t reported,apr_pool_t *pool)
{
    svn_fs_history_t *history=apr_pcalloc(pool,sizeof(*history));
    pgfs_history_data_t *data=apr_pcalloc(pool,sizeof(*data));
    data->fs=fs; data->path=pgfs_path(path,pool); data->rev=rev; data->reported=reported;
    history->vtable=&pgfs_history_vtable; history->fsap_data=data;
    return history;
}

static svn_error_t *pgfs_node_history(svn_fs_history_t **history,svn_fs_root_t *root,
    const char *path,apr_pool_t *result_pool,apr_pool_t *scratch_pool)
{
    if(root->is_txn_root) return svn_error_create(SVN_ERR_FS_NOT_REVISION_ROOT,NULL,"History requires a revision root");
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,scratch_pool));
    *history=pgfs_history(root->fs,pgfs_path(path,scratch_pool),root->rev,FALSE,result_pool);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_history_location(const char **path,svn_revnum_t *rev,
    svn_fs_history_t *history,apr_pool_t *pool)
{
    pgfs_history_data_t *data=history->fsap_data;
    *path=apr_pstrdup(pool,data->path); *rev=data->rev;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_history_prev(svn_fs_history_t **previous,svn_fs_history_t *history,
    svn_boolean_t cross,apr_pool_t *result_pool,apr_pool_t *scratch_pool)
{
    pgfs_history_data_t *data=history->fsap_data;
    *previous=NULL;
    svn_revnum_t rev=data->rev;
    const char *path=data->path;
    if(!strcmp(path,"/")) {
        if(data->reported) --rev;
        if(SVN_IS_VALID_REVNUM(rev)) *previous=pgfs_history(data->fs,path,rev,TRUE,result_pool);
        return SVN_NO_ERROR;
    }
    svn_fs_root_t *root,*copy_root;
    const char *copy_path;
    SVN_ERR(pgfs_revision_root(&root,data->fs,rev,scratch_pool));
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,scratch_pool));
    SVN_ERR(pgfs_closest_copy(&copy_root,&copy_path,root,path,scratch_pool));
    if(data->reported) {
        if(copy_root && copy_root->rev==rev) {
            if(!cross) return SVN_NO_ERROR;
            svn_revnum_t source_rev; const char *source_path;
            SVN_ERR(pgfs_copied_from(&source_rev,&source_path,copy_root,copy_path,scratch_pool));
            if(!source_path || !SVN_IS_VALID_REVNUM(source_rev)) return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"Copy source metadata is missing");
            path=apr_pstrcat(scratch_pool,source_path,path+strlen(copy_path),NULL);
            rev=source_rev;
            SVN_ERR(pgfs_revision_root(&root,data->fs,rev,scratch_pool));
            SVN_ERR(pgfs_lookup(&node,root,path,FALSE,scratch_pool));
        } else {
            if(rev==0 || !node->predecessor) return SVN_NO_ERROR;
            --rev;
            SVN_ERR(pgfs_revision_root(&root,data->fs,rev,scratch_pool));
            pgfs_node_t *old;
            SVN_ERR(pgfs_lookup(&old,root,path,TRUE,scratch_pool));
            if(!old || old->node_id!=node->node_id) return SVN_NO_ERROR;
            node=old;
        }
        SVN_ERR(pgfs_closest_copy(&copy_root,&copy_path,root,path,scratch_pool));
    }
    svn_revnum_t point=node->created_rev;
    if(copy_root && copy_root->rev>point) point=copy_root->rev;
    *previous=pgfs_history(data->fs,path,point,TRUE,result_pool);
    return SVN_NO_ERROR;
}
