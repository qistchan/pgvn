/* Mergeinfo uses Subversion's parser and range algebra; inheritance is a
   property-tree lookup, independent of physical directory representations. */
static svn_error_t *pgfs_parse_mergeinfo(svn_mergeinfo_t *info,const svn_string_t *value,apr_pool_t *pool)
{
    *info=NULL;
    if(!value) return SVN_NO_ERROR;
    svn_error_t *error=svn_mergeinfo_parse(info,value->data,pool);
    if(error && error->apr_err==SVN_ERR_MERGEINFO_PARSE_ERROR) {
        /* FSFS treats syntactically invalid mergeinfo as absent (issue 3896). */
        svn_error_clear(error); *info=NULL; return SVN_NO_ERROR;
    }
    return error;
}

static const char *pgfs_parent(const char *path,apr_pool_t *pool)
{
    if(!strcmp(path,"/")) return NULL;
    const char *end=strrchr(path,'/');
    return end==path ? "/" : apr_pstrndup(pool,path,(apr_size_t)(end-path));
}

static svn_error_t *pgfs_mergeinfo_one(svn_mergeinfo_t *info,svn_fs_root_t *root,
    const char *path,svn_mergeinfo_inheritance_t inherit,svn_boolean_t adjust,apr_pool_t *pool)
{
    *info=NULL;
    pgfs_node_t *node; SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
    const char *cursor=inherit==svn_mergeinfo_nearest_ancestor ? pgfs_parent(path,pool) : path;
    while(cursor) {
        svn_string_t *value; SVN_ERR(pgfs_node_prop(&value,root,cursor,SVN_PROP_MERGEINFO,pool));
        if(value) {
            SVN_ERR(pgfs_parse_mergeinfo(info,value,pool));
            if(*info && adjust && strcmp(cursor,path)) {
                svn_mergeinfo_t inheritable;
                SVN_ERR(svn_mergeinfo_inheritable2(&inheritable,*info,NULL,SVN_INVALID_REVNUM,SVN_INVALID_REVNUM,TRUE,pool,pool));
                svn_mergeinfo_t adjusted=apr_hash_make(pool);
                const char *suffix=path+strlen(cursor)+(strcmp(cursor,"/") ? 1 : 0);
                for(apr_hash_index_t *hi=apr_hash_first(pool,inheritable);hi;hi=apr_hash_next(hi)) {
                    const void *key; void *value; apr_hash_this(hi,&key,NULL,&value);
                    apr_hash_set(adjusted,pgfs_join(key,suffix,pool),APR_HASH_KEY_STRING,value);
                }
                *info=adjusted;
            }
            return SVN_NO_ERROR;
        }
        if(inherit==svn_mergeinfo_explicit) break;
        cursor=pgfs_parent(cursor,pool);
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_mergeinfo_descendants(svn_fs_root_t *root,const pgfs_node_t *node,
    const char *path,svn_fs_mergeinfo_receiver_t receiver,void *baton,apr_hash_t *emitted,apr_pool_t *pool)
{
    if(node->kind!=svn_node_dir) return SVN_NO_ERROR;
    apr_hash_t *entries; SVN_ERR(pgfs_entries(&entries,root->fs,node->directory,pool));
    for(apr_hash_index_t *hi=apr_hash_first(pool,entries);hi;hi=apr_hash_next(hi)) {
        const void *key; void *value; apr_hash_this(hi,&key,NULL,&value);
        const char *child_path=pgfs_join(path,key,pool);
        pgfs_node_t *child; SVN_ERR(pgfs_load_node(&child,root->fs,((pgfs_entry_t *)value)->id,pool));
        apr_hash_t *props; SVN_ERR(pgfs_properties(&props,root->fs,child->properties,pool));
        svn_mergeinfo_t info; SVN_ERR(pgfs_parse_mergeinfo(&info,apr_hash_get(props,SVN_PROP_MERGEINFO,APR_HASH_KEY_STRING),pool));
        if(info && !apr_hash_get(emitted,child_path,APR_HASH_KEY_STRING)) {
            SVN_ERR(receiver(child_path,info,baton,pool));
            apr_hash_set(emitted,child_path,APR_HASH_KEY_STRING,(void *)1);
        }
        SVN_ERR(pgfs_mergeinfo_descendants(root,child,child_path,receiver,baton,emitted,pool));
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_get_mergeinfo(svn_fs_root_t *root,const apr_array_header_t *paths,
    svn_mergeinfo_inheritance_t inherit,svn_boolean_t descendants,svn_boolean_t adjust,
    svn_fs_mergeinfo_receiver_t receiver,void *baton,apr_pool_t *pool)
{
    apr_hash_t *emitted=apr_hash_make(pool);
    for(int i=0;i<paths->nelts;++i) {
        const char *path=pgfs_path(APR_ARRAY_IDX(paths,i,const char *),pool);
        svn_mergeinfo_t info; SVN_ERR(pgfs_mergeinfo_one(&info,root,path,inherit,adjust,pool));
        if(info && !apr_hash_get(emitted,path,APR_HASH_KEY_STRING)) {
            SVN_ERR(receiver(path,info,baton,pool)); apr_hash_set(emitted,path,APR_HASH_KEY_STRING,(void *)1);
        }
        if(descendants) {
            pgfs_node_t *node; SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
            SVN_ERR(pgfs_mergeinfo_descendants(root,node,path,receiver,baton,emitted,pool));
        }
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_merge_body(const char **conflict,svn_fs_root_t *source,const char *source_path,
    svn_fs_root_t *target,const char *target_path,svn_fs_root_t *ancestor,const char *ancestor_path,apr_pool_t *pool)
{
    SVN_ERR(pgfs_lock_txn(target,pool));
    if(source->vtable!=&pgfs_root_vtable || ancestor->vtable!=&pgfs_root_vtable)
        return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"Cannot merge across filesystem providers");

    const char *repository=((pgfs_data_t *)target->fs->fsap_data)->repository_id;
    if(strcmp(repository,((pgfs_data_t *)source->fs->fsap_data)->repository_id) ||
       strcmp(repository,((pgfs_data_t *)ancestor->fs->fsap_data)->repository_id))
        return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"Cannot merge across repositories");
    if(source->is_txn_root || ancestor->is_txn_root) return svn_error_create(SVN_ERR_FS_NOT_REVISION_ROOT,NULL,"Merge inputs must be immutable roots");
    pgfs_node_t *a,*s,*t;
    SVN_ERR(pgfs_lookup(&a,ancestor,ancestor_path,FALSE,pool));
    SVN_ERR(pgfs_lookup(&s,source,source_path,FALSE,pool));
    SVN_ERR(pgfs_lookup(&t,target,target_path,FALSE,pool));
    apr_int64_t merged;
    SVN_ERR(pgfs_merge_nodes(&merged,conflict,target->fs,a->id,s->id,t->id,target->txn,pgfs_path(target_path,pool),pool));
    SVN_ERR(pgfs_replace_path(target,target_path,merged,pool));
    const char *v[]={repository,target->txn,pgfs_number(source->rev,pool)};
    SVN_ERR(pgfs_command(target->fs,"UPDATE pgfs.transactions SET base_rev=$3::bigint "
        "WHERE repository_id=$1::uuid AND txn_id=$2",3,v));
    target->rev=source->rev;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_merge(const char **conflict,svn_fs_root_t *source,const char *source_path,
    svn_fs_root_t *target,const char *target_path,svn_fs_root_t *ancestor,const char *ancestor_path,apr_pool_t *pool)
{
    if(conflict) *conflict=NULL;
    SVN_ERR(pgfs_unit_begin(target->fs));
    return pgfs_unit_end(target->fs,pgfs_merge_body(conflict,source,source_path,target,target_path,ancestor,ancestor_path,pool));
}
