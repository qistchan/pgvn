/* Version-tree primitives. All SQL parameters are bound, including paths. */
typedef struct pgfs_node_t {
    apr_int64_t id, node_id, copy_id, predecessor, predecessor_count;
    apr_int64_t content, properties, directory;
    svn_node_kind_t kind;
    svn_revnum_t created_rev, copyfrom_rev;
    const char *created_path, *copyfrom_path, *owner;
} pgfs_node_t;

typedef struct pgfs_entry_t {
    apr_int64_t id;
    svn_node_kind_t kind;
} pgfs_entry_t;

static const char *pgfs_number(apr_int64_t value, apr_pool_t *pool)
{
    return apr_psprintf(pool, "%" APR_INT64_T_FMT, value);
}

static const char *pgfs_path(const char *path, apr_pool_t *pool)
{
    while (*path == '/') ++path;
    return apr_pstrcat(pool, "/", svn_relpath_canonicalize(path, pool), NULL);
}

static const char *pgfs_join(const char *parent, const char *name, apr_pool_t *pool)
{
    return apr_pstrcat(pool, strcmp(parent, "/") ? parent : "", "/", name, NULL);
}

/* Keep the connection mutex across the entire SQL transaction. Query helpers
   take the same nested mutex, so another APR thread cannot interleave SQL. */
static svn_error_t *pgfs_unit_begin(svn_fs_t *fs)
{
    pgfs_data_t *data = fs->fsap_data;
    apr_status_t status = apr_thread_mutex_lock(data->mutex);
    if (status) return svn_error_wrap_apr(status, "PgFS transaction lock failed");
    PGresult *result;
    svn_error_t *error = pgfs_query(&result, fs, "BEGIN", 0, NULL);
    if (!error) PQclear(result);
    else apr_thread_mutex_unlock(data->mutex);
    return error;
}

static svn_error_t *pgfs_unit_end(svn_fs_t *fs, svn_error_t *error)
{
    pgfs_data_t *data = fs->fsap_data;
    PGresult *result;
    svn_error_t *finish = pgfs_query(&result, fs, error ? "ROLLBACK" : "COMMIT", 0, NULL);
    if (!finish) PQclear(result);
    if (!error && finish) {
        PGresult *rollback;
        svn_error_t *rollback_error = pgfs_query(&rollback, fs, "ROLLBACK", 0, NULL);
        if (!rollback_error) PQclear(rollback);
        finish = svn_error_compose_create(finish, rollback_error);
    }
    error = svn_error_compose_create(error, finish);
    apr_status_t status = apr_thread_mutex_unlock(data->mutex);
    return status ? svn_error_compose_create(error,
        svn_error_wrap_apr(status, "PgFS transaction unlock failed")) : error;
}

static svn_error_t *pgfs_command(svn_fs_t *fs, const char *sql, int count,
                                 const char *const *values)
{
    PGresult *result;
    SVN_ERR(pgfs_query(&result, fs, sql, count, values));
    PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_load_node(pgfs_node_t **node, svn_fs_t *fs,
                                   apr_int64_t id, apr_pool_t *pool)
{
    pgfs_data_t *data = fs->fsap_data;
    const char *values[] = {data->repository_id, pgfs_number(id, pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result, fs,
        "SELECT node_revision_id,node_id,copy_id,COALESCE(predecessor_id,0),predecessor_count,"
        "kind,created_rev,created_path,COALESCE(copyfrom_rev,-1),copyfrom_path,"
        "COALESCE(content_id,0),COALESCE(properties_id,0),COALESCE(directory_id,0),owner_txn "
        "FROM pgfs.node_revisions WHERE repository_id=$1::uuid AND node_revision_id=$2::bigint",
        2, values));
    if (PQntuples(result) != 1) {
        PQclear(result);
        return svn_error_create(SVN_ERR_FS_CORRUPT, NULL, "Missing PgFS node revision");
    }
    pgfs_node_t *n = apr_pcalloc(pool, sizeof(*n));
    n->id = id;
    n->node_id = apr_atoi64(PQgetvalue(result,0,1));
    n->copy_id = apr_atoi64(PQgetvalue(result,0,2));
    n->predecessor = apr_atoi64(PQgetvalue(result,0,3));
    n->predecessor_count = apr_atoi64(PQgetvalue(result,0,4));
    n->kind = !strcmp(PQgetvalue(result,0,5),"dir") ? svn_node_dir : svn_node_file;
    n->created_rev = (svn_revnum_t)apr_atoi64(PQgetvalue(result,0,6));
    n->created_path = apr_pstrdup(pool,PQgetvalue(result,0,7));
    n->copyfrom_rev = (svn_revnum_t)apr_atoi64(PQgetvalue(result,0,8));
    n->copyfrom_path = PQgetisnull(result,0,9) ? NULL : apr_pstrdup(pool,PQgetvalue(result,0,9));
    n->content = apr_atoi64(PQgetvalue(result,0,10));
    n->properties = apr_atoi64(PQgetvalue(result,0,11));
    n->directory = apr_atoi64(PQgetvalue(result,0,12));
    n->owner = PQgetisnull(result,0,13) ? NULL : apr_pstrdup(pool,PQgetvalue(result,0,13));
    PQclear(result);
    *node = n;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_txn_record(PGresult **result, svn_fs_t *fs,
                                    const char *txn, svn_boolean_t lock,
                                    apr_pool_t *pool)
{
    pgfs_data_t *data = fs->fsap_data;
    const char *values[] = {data->repository_id, txn};
    SVN_ERR(pgfs_query(result, fs, lock ?
        "SELECT base_rev,root_node_revision_id,flags,state,committed_rev FROM pgfs.transactions "
        "WHERE repository_id=$1::uuid AND txn_id=$2 FOR UPDATE" :
        "SELECT base_rev,root_node_revision_id,flags,state,committed_rev FROM pgfs.transactions "
        "WHERE repository_id=$1::uuid AND txn_id=$2", 2, values));
    if (PQntuples(*result) != 1) {
        PQclear(*result); *result = NULL;
        return svn_error_createf(SVN_ERR_FS_NO_SUCH_TRANSACTION, NULL,
                                 "No such PgFS transaction '%s'", txn);
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_lock_txn(svn_fs_root_t *root, apr_pool_t *pool)
{
    if (!root->is_txn_root)
        return svn_error_create(SVN_ERR_FS_NOT_MUTABLE, NULL, "Revision roots are immutable");
    PGresult *result;
    SVN_ERR(pgfs_txn_record(&result, root->fs, root->txn, TRUE, pool));
    svn_boolean_t active = !strcmp(PQgetvalue(result,0,3), "active");
    root->rev=(svn_revnum_t)apr_atoi64(PQgetvalue(result,0,0));
    root->txn_flags=(apr_uint32_t)strtoul(PQgetvalue(result,0,2),NULL,10);
    PQclear(result);
    if (!active) return svn_error_create(SVN_ERR_FS_NO_SUCH_TRANSACTION, NULL,
                                         "PgFS transaction is no longer active");
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_root_id(apr_int64_t *id, svn_fs_root_t *root, apr_pool_t *pool)
{
    if (!root->is_txn_root) {
        *id = ((pgfs_root_data_t *)root->fsap_data)->node_revision_id;
        return SVN_NO_ERROR;
    }
    PGresult *result;
    SVN_ERR(pgfs_txn_record(&result, root->fs, root->txn, FALSE, pool));
    svn_boolean_t active = !strcmp(PQgetvalue(result,0,3), "active");
    root->rev=(svn_revnum_t)apr_atoi64(PQgetvalue(result,0,0));
    root->txn_flags=(apr_uint32_t)strtoul(PQgetvalue(result,0,2),NULL,10);
    *id = apr_atoi64(PQgetvalue(result,0,1));
    PQclear(result);
    if (!active) return svn_error_create(SVN_ERR_FS_NO_SUCH_TRANSACTION, NULL,
                                         "PgFS transaction is no longer active");
    return SVN_NO_ERROR;
}

/* Directory overlay depth is bounded. Latest entries and tombstones win. */
#define PGFS_DIRECTORY_CHAIN \
    "WITH RECURSIVE chain AS (SELECT directory_id,base_directory_id,0 AS distance " \
    "FROM pgfs.directories WHERE repository_id=$1::uuid AND directory_id=$2::bigint " \
    "UNION ALL SELECT d.directory_id,d.base_directory_id,c.distance+1 " \
    "FROM chain c JOIN pgfs.directories d ON d.repository_id=$1::uuid " \
    "AND d.directory_id=c.base_directory_id) "

static svn_error_t *pgfs_child(apr_int64_t *child, svn_fs_t *fs,
                               apr_int64_t directory, const char *name, apr_pool_t *pool)
{
    *child = 0;
    if (!directory) return SVN_NO_ERROR;
    pgfs_data_t *data = fs->fsap_data;
    const char *values[] = {data->repository_id,pgfs_number(directory,pool),name};
    PGresult *result;
    SVN_ERR(pgfs_query(&result, fs, PGFS_DIRECTORY_CHAIN
        "SELECT e.child_node_revision_id FROM chain c JOIN pgfs.directory_entries e "
        "ON e.repository_id=$1::uuid AND e.directory_id=c.directory_id "
        "WHERE e.name=$3 ORDER BY c.distance LIMIT 1", 3, values));
    if (PQntuples(result) && !PQgetisnull(result,0,0))
        *child = apr_atoi64(PQgetvalue(result,0,0));
    PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_lookup_at(pgfs_node_t **node, svn_fs_t *fs,
                                   apr_int64_t root_id, const char *path,
                                   svn_boolean_t optional, apr_pool_t *pool)
{
    pgfs_node_t *current;
    SVN_ERR(pgfs_load_node(&current,fs,root_id,pool));
    char *components = apr_pstrdup(pool,pgfs_path(path,pool) + 1);
    char *state;
    for (char *name = apr_strtok(components,"/",&state); name;
         name = apr_strtok(NULL,"/",&state)) {
        if (current->kind != svn_node_dir) {
            if (optional) { *node = NULL; return SVN_NO_ERROR; }
            return svn_error_createf(SVN_ERR_FS_NOT_DIRECTORY,NULL,"Not a directory in '%s'",path);
        }
        apr_int64_t id;
        SVN_ERR(pgfs_child(&id,fs,current->directory,name,pool));
        if (!id) {
            if (optional) { *node = NULL; return SVN_NO_ERROR; }
            return svn_error_createf(SVN_ERR_FS_NOT_FOUND,NULL,"File not found: '%s'",path);
        }
        SVN_ERR(pgfs_load_node(&current,fs,id,pool));
    }
    *node = current;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_lookup(pgfs_node_t **node, svn_fs_root_t *root,
                                const char *path, svn_boolean_t optional, apr_pool_t *pool)
{
    apr_int64_t id;
    SVN_ERR(pgfs_root_id(&id,root,pool));
    return pgfs_lookup_at(node,root->fs,id,path,optional,pool);
}

static svn_error_t *pgfs_entries(apr_hash_t **entries, svn_fs_t *fs,
                                 apr_int64_t directory, apr_pool_t *pool)
{
    *entries = apr_hash_make(pool);
    if (!directory) return SVN_NO_ERROR;
    pgfs_data_t *data = fs->fsap_data;
    const char *values[] = {data->repository_id,pgfs_number(directory,pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs, PGFS_DIRECTORY_CHAIN
        ", latest AS (SELECT DISTINCT ON(e.name) e.name,e.child_node_revision_id "
        "FROM chain c JOIN pgfs.directory_entries e ON e.repository_id=$1::uuid "
        "AND e.directory_id=c.directory_id ORDER BY e.name,c.distance) "
        "SELECT l.name,l.child_node_revision_id,n.kind FROM latest l "
        "JOIN pgfs.node_revisions n ON n.repository_id=$1::uuid AND "
        "n.node_revision_id=l.child_node_revision_id ORDER BY l.name",2,values));
    for (int i=0;i<PQntuples(result);++i) {
        pgfs_entry_t *entry = apr_pcalloc(pool,sizeof(*entry));
        entry->id = apr_atoi64(PQgetvalue(result,i,1));
        entry->kind = !strcmp(PQgetvalue(result,i,2),"dir") ? svn_node_dir : svn_node_file;
        apr_hash_set(*entries,apr_pstrdup(pool,PQgetvalue(result,i,0)),APR_HASH_KEY_STRING,entry);
    }
    PQclear(result);
    return SVN_NO_ERROR;
}

typedef struct pgfs_id_data_t {
    const char *repository, *owner;
    apr_int64_t id, node_id, copy_id;
} pgfs_id_data_t;

static svn_string_t *pgfs_id_unparse(const svn_fs_id_t *id, apr_pool_t *pool)
{
    pgfs_id_data_t *d = id->fsap_data;
    if(d->owner) return svn_string_create(apr_psprintf(pool,"pg:%s:%" APR_INT64_T_FMT ":%" APR_INT64_T_FMT
        ":t%s",d->repository,d->node_id,d->copy_id,d->owner),pool);
    return svn_string_create(apr_psprintf(pool,"pg:%s:%" APR_INT64_T_FMT ":%" APR_INT64_T_FMT
        ":r%" APR_INT64_T_FMT,d->repository,d->node_id,d->copy_id,d->id),pool);
}

static svn_fs_node_relation_t pgfs_id_compare(const svn_fs_id_t *a, const svn_fs_id_t *b)
{
    if (a->vtable != b->vtable) return svn_fs_node_unrelated;
    pgfs_id_data_t *x=a->fsap_data,*y=b->fsap_data;
    if (strcmp(x->repository,y->repository) || x->node_id != y->node_id)
        return svn_fs_node_unrelated;
    if ((!x->owner && !y->owner && x->id == y->id) ||
        (x->owner && y->owner && !strcmp(x->owner,y->owner) && x->copy_id == y->copy_id))
        return svn_fs_node_unchanged;
    return svn_fs_node_common_ancestor;
}

static const id_vtable_t pgfs_id_vtable = {pgfs_id_unparse,pgfs_id_compare};

static const svn_fs_id_t *pgfs_id(svn_fs_t *fs, const pgfs_node_t *node, apr_pool_t *pool)
{
    svn_fs_id_t *id=apr_pcalloc(pool,sizeof(*id));
    pgfs_id_data_t *d=apr_pcalloc(pool,sizeof(*d));
    d->repository=apr_pstrdup(pool,((pgfs_data_t *)fs->fsap_data)->repository_id);
    d->owner=node->owner ? apr_pstrdup(pool,node->owner) : NULL;
    d->id=node->id; d->node_id=node->node_id; d->copy_id=node->copy_id;
    id->vtable=&pgfs_id_vtable; id->fsap_data=d;
    return id;
}

static svn_error_t *pgfs_check_path(svn_node_kind_t *kind, svn_fs_root_t *root,
                                   const char *path, apr_pool_t *pool)
{
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,TRUE,pool));
    *kind=node ? node->kind : svn_node_none;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_node_id(const svn_fs_id_t **id, svn_fs_root_t *root,
                                const char *path, apr_pool_t *pool)
{
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
    if(root->is_txn_root && !strcmp(pgfs_path(path,pool),"/")) node->owner=root->txn;
    *id=pgfs_id(root->fs,node,pool);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_node_relation(svn_fs_node_relation_t *relation,
    svn_fs_root_t *a, const char *pa, svn_fs_root_t *b, const char *pb, apr_pool_t *pool)
{
    if(a->fs!=b->fs) { *relation=svn_fs_node_unrelated; return SVN_NO_ERROR; }
    if((!*pa || !strcmp(pa,"/")) && (!*pb || !strcmp(pb,"/"))) {
        svn_boolean_t different=a->is_txn_root && b->is_txn_root && strcmp(a->txn,b->txn);
        *relation=a->rev==b->rev && a->is_txn_root==b->is_txn_root && !different ?
            svn_fs_node_unchanged : svn_fs_node_common_ancestor;
        return SVN_NO_ERROR;
    }
    pgfs_node_t *x,*y;
    SVN_ERR(pgfs_lookup(&x,a,pa,FALSE,pool));
    SVN_ERR(pgfs_lookup(&y,b,pb,FALSE,pool));
    *relation=pgfs_id_compare(pgfs_id(a->fs,x,pool),pgfs_id(b->fs,y,pool));
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_created_rev(svn_revnum_t *rev, svn_fs_root_t *root,
                                    const char *path, apr_pool_t *pool)
{
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
    *rev=node->created_rev;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_origin_rev(svn_revnum_t *rev, svn_fs_root_t *root,
                                   const char *path, apr_pool_t *pool)
{
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
    while (node->predecessor) SVN_ERR(pgfs_load_node(&node,root->fs,node->predecessor,pool));
    *rev=node->created_rev;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_created_path(const char **created, svn_fs_root_t *root,
                                     const char *path, apr_pool_t *pool)
{
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
    *created=node->created_path;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_properties(apr_hash_t **props, svn_fs_t *fs,
                                   apr_int64_t properties, apr_pool_t *pool)
{
    *props=apr_hash_make(pool);
    if (!properties) return SVN_NO_ERROR;
    pgfs_data_t *data=fs->fsap_data;
    const char *values[]={data->repository_id,pgfs_number(properties,pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,
        "SELECT name,encode(value,'hex') FROM pgfs.properties "
        "WHERE repository_id=$1::uuid AND properties_id=$2::bigint",2,values));
    for(int i=0;i<PQntuples(result);++i)
        apr_hash_set(*props,apr_pstrdup(pool,PQgetvalue(result,i,0)),APR_HASH_KEY_STRING,
                     pgfs_decode_hex(PQgetvalue(result,i,1),pool));
    PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_node_props(apr_hash_t **props, svn_fs_root_t *root,
                                   const char *path, apr_pool_t *pool)
{
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
    return pgfs_properties(props,root->fs,node->properties,pool);
}

static svn_error_t *pgfs_node_prop(svn_string_t **value, svn_fs_root_t *root,
                                  const char *path, const char *name, apr_pool_t *pool)
{
    apr_hash_t *props;
    SVN_ERR(pgfs_node_props(&props,root,path,pool));
    *value=apr_hash_get(props,name,APR_HASH_KEY_STRING);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_node_has_props(svn_boolean_t *has, svn_fs_root_t *root,
                                       const char *path, apr_pool_t *pool)
{
    apr_hash_t *props;
    SVN_ERR(pgfs_node_props(&props,root,path,pool));
    *has=apr_hash_count(props)!=0;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_props_equal(svn_boolean_t *equal, svn_fs_t *fs,
                                     apr_int64_t a, apr_int64_t b, apr_pool_t *pool)
{
    *equal=TRUE;
    if(a==b) return SVN_NO_ERROR;
    apr_hash_t *x,*y;
    SVN_ERR(pgfs_properties(&x,fs,a,pool));
    SVN_ERR(pgfs_properties(&y,fs,b,pool));
    if(apr_hash_count(x)!=apr_hash_count(y)) { *equal=FALSE; return SVN_NO_ERROR; }
    for(apr_hash_index_t *hi=apr_hash_first(pool,x);hi;hi=apr_hash_next(hi)) {
        const void *key; void *value; apr_hash_this(hi,&key,NULL,&value);
        const svn_string_t *v=apr_hash_get(y,key,APR_HASH_KEY_STRING);
        if(!v || !svn_string_compare(value,v)) { *equal=FALSE; break; }
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_props_changed(int *changed, svn_fs_root_t *a,const char *pa,
    svn_fs_root_t *b,const char *pb,svn_boolean_t strict,apr_pool_t *pool)
{
    pgfs_node_t *x,*y;
    SVN_ERR(pgfs_lookup(&x,a,pa,FALSE,pool)); SVN_ERR(pgfs_lookup(&y,b,pb,FALSE,pool));
    if(!strict) *changed=x->properties!=y->properties;
    else {
        svn_boolean_t equal;
        SVN_ERR(pgfs_props_equal(&equal,a->fs,x->properties,y->properties,pool));
        *changed=!equal;
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_dir_entries(apr_hash_t **entries,svn_fs_root_t *root,
                                    const char *path,apr_pool_t *pool)
{
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
    if(node->kind!=svn_node_dir) return svn_error_create(SVN_ERR_FS_NOT_DIRECTORY,NULL,path);
    apr_hash_t *raw;
    SVN_ERR(pgfs_entries(&raw,root->fs,node->directory,pool));
    *entries=apr_hash_make(pool);
    for(apr_hash_index_t *hi=apr_hash_first(pool,raw);hi;hi=apr_hash_next(hi)) {
        const void *key; void *value; apr_hash_this(hi,&key,NULL,&value);
        pgfs_entry_t *entry=value;
        pgfs_node_t *child;
        SVN_ERR(pgfs_load_node(&child,root->fs,entry->id,pool));
        svn_fs_dirent_t *dirent=apr_pcalloc(pool,sizeof(*dirent));
        dirent->name=key; dirent->kind=entry->kind; dirent->id=pgfs_id(root->fs,child,pool);
        apr_hash_set(*entries,key,APR_HASH_KEY_STRING,dirent);
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_dir_order(apr_array_header_t **ordered,svn_fs_root_t *root,
    apr_hash_t *entries,apr_pool_t *result_pool,apr_pool_t *scratch_pool)
{
    *ordered=apr_array_make(result_pool,(int)apr_hash_count(entries),sizeof(svn_fs_dirent_t *));
    for(apr_hash_index_t *hi=apr_hash_first(scratch_pool,entries);hi;hi=apr_hash_next(hi)) {
        void *value; apr_hash_this(hi,NULL,NULL,&value);
        APR_ARRAY_PUSH(*ordered,svn_fs_dirent_t *)=value;
    }
    return SVN_NO_ERROR;
}

/* Copy IDs are inherited lazily from copied ancestors, like FSFS. */
static svn_error_t *pgfs_inherited_copy_id(apr_int64_t *copy,svn_fs_t *fs,
    const char *txn,const char *path,apr_pool_t *pool)
{
    PGresult *result;
    SVN_ERR(pgfs_txn_record(&result,fs,txn,FALSE,pool));
    apr_int64_t id=apr_atoi64(PQgetvalue(result,0,1)); PQclear(result);
    pgfs_node_t *node; SVN_ERR(pgfs_load_node(&node,fs,id,pool));
    if(node->copy_id>*copy) *copy=node->copy_id;
    char *parts=apr_pstrdup(pool,pgfs_path(path,pool)+1),*state;
    for(char *name=apr_strtok(parts,"/",&state);name;name=apr_strtok(NULL,"/",&state)) {
        if(node->kind!=svn_node_dir) break;
        SVN_ERR(pgfs_child(&id,fs,node->directory,name,pool));
        if(!id) break;
        SVN_ERR(pgfs_load_node(&node,fs,id,pool));
        if(node->copy_id>*copy) *copy=node->copy_id;
    }
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_clone(apr_int64_t *id,svn_fs_t *fs,const pgfs_node_t *node,
                              const char *txn,const char *path,apr_pool_t *pool)
{
    pgfs_data_t *data=fs->fsap_data;
    apr_int64_t copy=node->copy_id;
    SVN_ERR(pgfs_inherited_copy_id(&copy,fs,txn,path,pool));
    const char *values[]={data->repository_id,pgfs_number(node->id,pool),txn,path,pgfs_number(copy,pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,
        "INSERT INTO pgfs.node_revisions(repository_id,node_id,copy_id,predecessor_id,"
        "predecessor_count,kind,created_rev,created_path,content_id,properties_id,directory_id,owner_txn,copyfrom_rev,copyfrom_path) "
        "SELECT repository_id,node_id,$5::bigint,CASE WHEN owner_txn=$3 THEN predecessor_id ELSE node_revision_id END,"
        /* Preserve same-txn copy metadata. */
        "predecessor_count+CASE WHEN owner_txn=$3 THEN 0 ELSE 1 END,kind,-1,$4,content_id,properties_id,directory_id,$3,"
        "CASE WHEN owner_txn=$3 THEN copyfrom_rev ELSE NULL END,CASE WHEN owner_txn=$3 THEN copyfrom_path ELSE NULL END "
        "FROM pgfs.node_revisions WHERE repository_id=$1::uuid AND node_revision_id=$2::bigint "
        "RETURNING node_revision_id",5,values));
    *id=apr_atoi64(PQgetvalue(result,0,0)); PQclear(result);
    return SVN_NO_ERROR;
}

/* Create a new directory overlay; compact it before the chain exceeds 32. */
static svn_error_t *pgfs_edit_directory(apr_int64_t *id,svn_fs_t *fs,apr_int64_t base,
    const char *name,apr_int64_t child,apr_pool_t *pool)
{
    pgfs_data_t *data=fs->fsap_data;
    const char *values[]={data->repository_id,pgfs_number(base,pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,
        "INSERT INTO pgfs.directories(repository_id,base_directory_id,depth) "
        "SELECT $1::uuid,CASE WHEN depth<32 THEN directory_id ELSE NULL END,"
        "CASE WHEN depth<32 THEN depth+1 ELSE 0 END FROM pgfs.directories "
        "WHERE repository_id=$1::uuid AND directory_id=$2::bigint "
        "UNION ALL SELECT $1::uuid,NULL,0 WHERE $2::bigint=0 RETURNING directory_id,depth",2,values));
    if(PQntuples(result)!=1) { PQclear(result); return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"Missing directory representation"); }
    *id=apr_atoi64(PQgetvalue(result,0,0));
    svn_boolean_t compact=base && !atoi(PQgetvalue(result,0,1)); PQclear(result);
    const char *v[]={data->repository_id,pgfs_number(base,pool),pgfs_number(*id,pool),name,
                     child ? pgfs_number(child,pool) : NULL};
    if(compact) SVN_ERR(pgfs_command(fs,PGFS_DIRECTORY_CHAIN
        ", latest AS (SELECT DISTINCT ON(e.name) e.name,e.child_node_revision_id FROM chain c "
        "JOIN pgfs.directory_entries e ON e.repository_id=$1::uuid AND e.directory_id=c.directory_id "
        "ORDER BY e.name,c.distance) INSERT INTO pgfs.directory_entries(repository_id,directory_id,name,child_node_revision_id) "
        "SELECT $1::uuid,$3::bigint,name,child_node_revision_id FROM latest WHERE child_node_revision_id IS NOT NULL",3,v));
    SVN_ERR(pgfs_command(fs,
        "INSERT INTO pgfs.directory_entries(repository_id,directory_id,name,child_node_revision_id) "
        "VALUES($1::uuid,$3::bigint,$4,$5::bigint) ON CONFLICT(repository_id,directory_id,name) "
        "DO UPDATE SET child_node_revision_id=EXCLUDED.child_node_revision_id",5,v));
    return SVN_NO_ERROR;
}

/* Replace a leaf and clone every ancestor. Call inside a locked SQL unit. */
static svn_error_t *pgfs_replace_path(svn_fs_root_t *root,const char *path,
                                     apr_int64_t replacement,apr_pool_t *pool)
{
    apr_int64_t root_id;
    SVN_ERR(pgfs_root_id(&root_id,root,pool));
    apr_array_header_t *parents=apr_array_make(pool,8,sizeof(pgfs_node_t *));
    apr_array_header_t *names=apr_array_make(pool,8,sizeof(const char *));
    apr_array_header_t *paths=apr_array_make(pool,8,sizeof(const char *));
    const char *parent_path="/";
    pgfs_node_t *parent;
    SVN_ERR(pgfs_load_node(&parent,root->fs,root_id,pool));
    char *parts=apr_pstrdup(pool,pgfs_path(path,pool)+1),*state;
    for(char *name=apr_strtok(parts,"/",&state);name;) {
        if(parent->kind!=svn_node_dir) return svn_error_create(SVN_ERR_FS_NOT_DIRECTORY,NULL,parent_path);
        APR_ARRAY_PUSH(parents,pgfs_node_t *)=parent;
        APR_ARRAY_PUSH(names,const char *)=name;
        APR_ARRAY_PUSH(paths,const char *)=parent_path;
        char *next=apr_strtok(NULL,"/",&state);
        if(next) {
            apr_int64_t child;
            SVN_ERR(pgfs_child(&child,root->fs,parent->directory,name,pool));
            if(!child) return svn_error_createf(SVN_ERR_FS_NOT_FOUND,NULL,"File not found: '%s'",path);
            SVN_ERR(pgfs_load_node(&parent,root->fs,child,pool));
            parent_path=pgfs_join(parent_path,name,pool);
        }
        name=next;
    }
    pgfs_data_t *data=root->fs->fsap_data;
    for(int i=parents->nelts-1;i>=0;--i) {
        parent=APR_ARRAY_IDX(parents,i,pgfs_node_t *);
        apr_int64_t directory,new_id;
        SVN_ERR(pgfs_edit_directory(&directory,root->fs,parent->directory,
            APR_ARRAY_IDX(names,i,const char *),replacement,pool));
        SVN_ERR(pgfs_clone(&new_id,root->fs,parent,root->txn,APR_ARRAY_IDX(paths,i,const char *),pool));
        const char *v[]={data->repository_id,pgfs_number(new_id,pool),pgfs_number(directory,pool)};
        SVN_ERR(pgfs_command(root->fs,"UPDATE pgfs.node_revisions SET directory_id=$3::bigint "
            "WHERE repository_id=$1::uuid AND node_revision_id=$2::bigint",3,v));
        replacement=new_id;
    }
    const char *values[]={data->repository_id,root->txn,pgfs_number(replacement,pool)};
    SVN_ERR(pgfs_command(root->fs,"UPDATE pgfs.transactions SET root_node_revision_id=$3::bigint "
        "WHERE repository_id=$1::uuid AND txn_id=$2",3,values));
    return SVN_NO_ERROR;
}

/* Collapse repeated edits to one final change per path. Ancestor COW is not
   recorded as a user-visible change. Deleting a subtree removes its edits. */
static svn_error_t *pgfs_record_change(svn_fs_root_t *root,const char *path,
    svn_boolean_t text,svn_boolean_t props,svn_boolean_t mergeinfo,
    svn_revnum_t copyrev,const char *copypath,apr_pool_t *pool)
{
    path=pgfs_path(path,pool);
    svn_fs_root_t *base;
    SVN_ERR(pgfs_revision_root(&base,root->fs,root->rev,pool));
    pgfs_node_t *before,*after;
    SVN_ERR(pgfs_lookup(&before,base,path,TRUE,pool));
    SVN_ERR(pgfs_lookup(&after,root,path,TRUE,pool));
    pgfs_data_t *repo_data=root->fs->fsap_data;
    const char *copy_values[]={repo_data->repository_id,root->txn,path};
    PGresult *copy_result;
    SVN_ERR(pgfs_query(&copy_result,root->fs,
        "SELECT path,copyfrom_rev,copyfrom_path FROM pgfs.transaction_changes WHERE repository_id=$1::uuid "
        "AND txn_id=$2 AND copyfrom_rev IS NOT NULL AND starts_with($3,path||'/') ORDER BY length(path) DESC LIMIT 1",
        3,copy_values));
    if(PQntuples(copy_result)) {
        svn_fs_root_t *copy_source;
        svn_error_t *error=pgfs_revision_root(&copy_source,root->fs,
            (svn_revnum_t)apr_atoi64(PQgetvalue(copy_result,0,1)),pool);
        const char *mapped=apr_pstrcat(pool,PQgetvalue(copy_result,0,2),
            path+strlen(PQgetvalue(copy_result,0,0)),NULL);
        if(!error) error=pgfs_lookup(&before,copy_source,mapped,TRUE,pool);
        if(error) { PQclear(copy_result); return error; }
    }
    PQclear(copy_result);
    pgfs_data_t *data=root->fs->fsap_data;
    const char *v[]={data->repository_id,root->txn,path};
    if(!after) SVN_ERR(pgfs_command(root->fs,
        "DELETE FROM pgfs.transaction_changes WHERE repository_id=$1::uuid AND txn_id=$2 "
        "AND starts_with(path,$3||'/')",3,v));
    if(!before && !after) return pgfs_command(root->fs,
        "DELETE FROM pgfs.transaction_changes WHERE repository_id=$1::uuid AND txn_id=$2 AND path=$3",3,v);
    pgfs_node_t *node=after ? after : before;
    const char *kind=!after ? "delete" : !before ? "add" :
        (before->node_id!=after->node_id || SVN_IS_VALID_REVNUM(copyrev) ||
         (before->copy_id!=after->copy_id && !after->owner)) ? "replace" : "modify";
    const char *values[]={data->repository_id,root->txn,path,pgfs_number(node->id,pool),kind,
        node->kind==svn_node_dir ? "dir" : "file",after && text ? "true" : "false",
        after && props ? "true" : "false",after && mergeinfo ? "true" : "false",
        SVN_IS_VALID_REVNUM(copyrev) ? pgfs_number(copyrev,pool) : NULL,copypath};
    return pgfs_command(root->fs,
        "INSERT INTO pgfs.transaction_changes(repository_id,txn_id,path,node_revision_id,change_kind,node_kind,"
        "text_mod,prop_mod,mergeinfo_mod,copyfrom_rev,copyfrom_path) "
        "VALUES($1::uuid,$2,$3,$4::bigint,$5,$6,$7::boolean,$8::boolean,$9::boolean,$10::bigint,$11) "
        "ON CONFLICT(repository_id,txn_id,path) DO UPDATE SET node_revision_id=EXCLUDED.node_revision_id,"
        "change_kind=CASE WHEN EXCLUDED.change_kind='modify' AND pgfs.transaction_changes.change_kind IN('add','replace') "
        "THEN pgfs.transaction_changes.change_kind ELSE EXCLUDED.change_kind END,node_kind=EXCLUDED.node_kind,"
        "text_mod=CASE WHEN EXCLUDED.change_kind='delete' THEN false ELSE pgfs.transaction_changes.text_mod OR EXCLUDED.text_mod END,"
        "prop_mod=CASE WHEN EXCLUDED.change_kind='delete' THEN false ELSE pgfs.transaction_changes.prop_mod OR EXCLUDED.prop_mod END,"
        "mergeinfo_mod=CASE WHEN EXCLUDED.change_kind='delete' THEN false ELSE pgfs.transaction_changes.mergeinfo_mod OR EXCLUDED.mergeinfo_mod END,"
        "copyfrom_rev=CASE WHEN EXCLUDED.change_kind='delete' THEN NULL ELSE COALESCE(EXCLUDED.copyfrom_rev,pgfs.transaction_changes.copyfrom_rev) END,"
        "copyfrom_path=CASE WHEN EXCLUDED.change_kind='delete' THEN NULL ELSE COALESCE(EXCLUDED.copyfrom_path,pgfs.transaction_changes.copyfrom_path) END",
        11,values);
}

static svn_error_t *pgfs_make_node_body(svn_fs_root_t *root,const char *path,
                                       svn_node_kind_t kind,apr_pool_t *pool)
{
    SVN_ERR(pgfs_lock_txn(root,pool));
    SVN_ERR(pgfs_check_locks(root,path,FALSE,pool));
    path=pgfs_path(path,pool);
    pgfs_node_t *existing;
    SVN_ERR(pgfs_lookup(&existing,root,path,TRUE,pool));
    if(existing) return svn_error_createf(SVN_ERR_FS_ALREADY_EXISTS,NULL,"%s already exists: '%s'",existing->kind==svn_node_file ? "File" : "Directory",path);
    pgfs_data_t *data=root->fs->fsap_data;
    apr_int64_t copy=0;
    SVN_ERR(pgfs_inherited_copy_id(&copy,root->fs,root->txn,path,pool));
    const char *v[]={data->repository_id,root->txn,path,kind==svn_node_dir ? "dir" : "file",pgfs_number(copy,pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,root->fs,
        "INSERT INTO pgfs.node_revisions(repository_id,node_id,kind,created_rev,created_path,owner_txn,copy_id) "
        "VALUES($1::uuid,nextval('pgfs.node_ids'),$4,-1,$3,$2,$5::bigint) RETURNING node_revision_id",5,v));
    apr_int64_t id=apr_atoi64(PQgetvalue(result,0,0)); PQclear(result);
    SVN_ERR(pgfs_replace_path(root,path,id,pool));
    return pgfs_record_change(root,path,kind==svn_node_file,FALSE,FALSE,SVN_INVALID_REVNUM,NULL,pool);
}

static svn_error_t *pgfs_make_dir(svn_fs_root_t *root,const char *path,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(root->fs));
    return pgfs_unit_end(root->fs,pgfs_make_node_body(root,path,svn_node_dir,pool));
}

static svn_error_t *pgfs_make_file(svn_fs_root_t *root,const char *path,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(root->fs));
    return pgfs_unit_end(root->fs,pgfs_make_node_body(root,path,svn_node_file,pool));
}

static svn_error_t *pgfs_delete_body(svn_fs_root_t *root,const char *path,apr_pool_t *pool)
{
    SVN_ERR(pgfs_lock_txn(root,pool));
    SVN_ERR(pgfs_check_locks(root,path,TRUE,pool));
    path=pgfs_path(path,pool);
    if(!strcmp(path,"/")) return svn_error_create(SVN_ERR_FS_ROOT_DIR,NULL,"Cannot delete root directory");
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
    SVN_ERR(pgfs_replace_path(root,path,0,pool));
    return pgfs_record_change(root,path,FALSE,FALSE,FALSE,SVN_INVALID_REVNUM,NULL,pool);
}

static svn_error_t *pgfs_delete_node(svn_fs_root_t *root,const char *path,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(root->fs));
    return pgfs_unit_end(root->fs,pgfs_delete_body(root,path,pool));
}

static svn_error_t *pgfs_change_node_prop_body(svn_fs_root_t *root,const char *path,
    const char *name,const svn_string_t *value,apr_pool_t *pool)
{
    SVN_ERR(pgfs_lock_txn(root,pool));
    SVN_ERR(pgfs_check_locks(root,path,FALSE,pool));
    path=pgfs_path(path,pool);
    pgfs_node_t *node;
    SVN_ERR(pgfs_lookup(&node,root,path,FALSE,pool));
    pgfs_data_t *data=root->fs->fsap_data;
    const char *v[]={data->repository_id,pgfs_number(node->properties,pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,root->fs,"INSERT INTO pgfs.property_sets(repository_id) "
        "VALUES($1::uuid) RETURNING properties_id",1,v));
    apr_int64_t properties=apr_atoi64(PQgetvalue(result,0,0)); PQclear(result);
    const char *copy[]={data->repository_id,pgfs_number(node->properties,pool),pgfs_number(properties,pool),name};
    SVN_ERR(pgfs_command(root->fs,
        "INSERT INTO pgfs.properties(repository_id,properties_id,name,value) "
        "SELECT repository_id,$3::bigint,name,value FROM pgfs.properties "
        "WHERE repository_id=$1::uuid AND properties_id=$2::bigint AND name<>$4",4,copy));
    if(value) {
        if(value->len>INT_MAX) return svn_error_create(SVN_ERR_FS_GENERAL,NULL,"Property exceeds libpq parameter limit");
        const char *values[]={data->repository_id,pgfs_number(properties,pool),name,value->data};
        int lengths[]={0,0,0,(int)value->len},formats[]={0,0,0,1};
        SVN_ERR(pgfs_query_full(&result,root->fs,
            "INSERT INTO pgfs.properties(repository_id,properties_id,name,value) "
            "VALUES($1::uuid,$2::bigint,$3,$4::bytea)",4,values,lengths,formats));
        PQclear(result);
    }
    apr_int64_t new_id;
    SVN_ERR(pgfs_clone(&new_id,root->fs,node,root->txn,path,pool));
    const char *values[]={data->repository_id,pgfs_number(new_id,pool),pgfs_number(properties,pool)};
    SVN_ERR(pgfs_command(root->fs,"UPDATE pgfs.node_revisions SET properties_id=$3::bigint "
        "WHERE repository_id=$1::uuid AND node_revision_id=$2::bigint",3,values));
    SVN_ERR(pgfs_replace_path(root,path,new_id,pool));
    return pgfs_record_change(root,path,FALSE,TRUE,!strcmp(name,SVN_PROP_MERGEINFO),SVN_INVALID_REVNUM,NULL,pool);
}

static svn_error_t *pgfs_change_node_prop(svn_fs_root_t *root,const char *path,
    const char *name,const svn_string_t *value,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(root->fs));
    return pgfs_unit_end(root->fs,pgfs_change_node_prop_body(root,path,name,value,pool));
}

static svn_error_t *pgfs_paths_changed(apr_hash_t **changes,svn_fs_root_t *root,apr_pool_t *pool)
{
    pgfs_data_t *data=root->fs->fsap_data;
    const char *values[]={data->repository_id,root->is_txn_root ? root->txn : pgfs_number(root->rev,pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,root->fs,root->is_txn_root ?
        "SELECT path,node_revision_id,change_kind,node_kind,text_mod,prop_mod,mergeinfo_mod,copyfrom_rev,copyfrom_path "
        "FROM pgfs.transaction_changes WHERE repository_id=$1::uuid AND txn_id=$2" :
        "SELECT path,node_revision_id,change_kind,node_kind,text_mod,prop_mod,mergeinfo_mod,copyfrom_rev,copyfrom_path "
        "FROM pgfs.revision_changes WHERE repository_id=$1::uuid AND rev=$2::bigint",2,values));
    *changes=apr_hash_make(pool);
    for(int i=0;i<PQntuples(result);++i) {
        svn_fs_path_change2_t *change=apr_pcalloc(pool,sizeof(*change));
        pgfs_node_t *node;
        svn_error_t *error=pgfs_load_node(&node,root->fs,apr_atoi64(PQgetvalue(result,i,1)),pool);
        if(error) { PQclear(result); return error; }
        change->node_rev_id=pgfs_id(root->fs,node,pool);
        const char *kind=PQgetvalue(result,i,2);
        change->change_kind=!strcmp(kind,"add") ? svn_fs_path_change_add : !strcmp(kind,"delete") ?
            svn_fs_path_change_delete : !strcmp(kind,"replace") ? svn_fs_path_change_replace : svn_fs_path_change_modify;
        change->node_kind=!strcmp(PQgetvalue(result,i,3),"dir") ? svn_node_dir : svn_node_file;
        change->text_mod=PQgetvalue(result,i,4)[0]=='t';
        change->prop_mod=PQgetvalue(result,i,5)[0]=='t';
        change->mergeinfo_mod=PQgetvalue(result,i,6)[0]=='t' ? svn_tristate_true : svn_tristate_false;
        change->copyfrom_known=TRUE;
        change->copyfrom_rev=PQgetisnull(result,i,7) ? SVN_INVALID_REVNUM : (svn_revnum_t)apr_atoi64(PQgetvalue(result,i,7));
        change->copyfrom_path=PQgetisnull(result,i,8) ? NULL : apr_pstrdup(pool,PQgetvalue(result,i,8));
        apr_hash_set(*changes,apr_pstrdup(pool,PQgetvalue(result,i,0)),APR_HASH_KEY_STRING,change);
    }
    PQclear(result);
    return SVN_NO_ERROR;
}
