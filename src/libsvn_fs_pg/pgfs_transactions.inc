/* Persisted SVN transactions. Staging survives process exit. Only publication
   takes the repository row lock; unrelated transactions can stage in parallel. */
static svn_fs_txn_t *pgfs_txn_object(svn_fs_t *fs,const char *name,svn_revnum_t base,apr_pool_t *pool)
{
    svn_fs_txn_t *txn=apr_pcalloc(pool,sizeof(*txn));
    txn->fs=fs; txn->id=apr_pstrdup(pool,name); txn->base_rev=base; txn->vtable=&pgfs_txn_vtable;
    return txn;
}

static svn_error_t *pgfs_open_txn(svn_fs_txn_t **txn,svn_fs_t *fs,const char *name,apr_pool_t *pool)
{
    PGresult *result;
    SVN_ERR(pgfs_txn_record(&result,fs,name,FALSE,pool));
    svn_revnum_t base=(svn_revnum_t)apr_atoi64(PQgetvalue(result,0,0));
    svn_boolean_t active=!strcmp(PQgetvalue(result,0,3),"active"); PQclear(result);
    if(!active) return svn_error_createf(SVN_ERR_FS_NO_SUCH_TRANSACTION,NULL,"No active transaction '%s'",name);
    *txn=pgfs_txn_object(fs,name,base,pool);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_begin_body(svn_fs_txn_t **txn,svn_fs_t *fs,svn_revnum_t rev,
                                   apr_uint32_t flags,apr_pool_t *pool)
{
    pgfs_data_t *data=fs->fsap_data;
    const char *name=pgfs_uuid(pool);
    const char *values[]={data->repository_id,pgfs_number(rev,pool),name,pgfs_number(flags,pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,
        "INSERT INTO pgfs.transactions(repository_id,txn_id,base_rev,root_node_revision_id,flags) "
        "SELECT repository_id,$3,rev,root_node_revision_id,$4::integer FROM pgfs.revisions "
        "WHERE repository_id=$1::uuid AND rev=$2::bigint RETURNING txn_id",4,values));
    svn_boolean_t exists=PQntuples(result)==1; PQclear(result);
    if(!exists) return svn_error_createf(SVN_ERR_FS_NO_SUCH_REVISION,NULL,"No such revision %ld",rev);
    const char *v[]={data->repository_id,name,svn_time_to_cstring(apr_time_now(),pool),
                    pgfs_number(flags,pool)};
    SVN_ERR(pgfs_command(fs,
        "INSERT INTO pgfs.transaction_properties(repository_id,txn_id,name,value) "
        "SELECT $1::uuid,$2,'svn:date',convert_to($3,'UTF8') UNION ALL "
        "SELECT $1::uuid,$2,'svn:check-ood',convert_to('true','UTF8') WHERE ($4::integer & 1)<>0 UNION ALL "
        "SELECT $1::uuid,$2,'svn:check-locks',convert_to('true','UTF8') WHERE ($4::integer & 2)<>0 UNION ALL "
        "SELECT $1::uuid,$2,'svn:client-date',convert_to('0','UTF8') WHERE ($4::integer & 4)<>0",4,v));
    *txn=pgfs_txn_object(fs,name,rev,pool);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_begin_txn(svn_fs_txn_t **txn,svn_fs_t *fs,svn_revnum_t rev,
                                  apr_uint32_t flags,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(fs));
    return pgfs_unit_end(fs,pgfs_begin_body(txn,fs,rev,flags,pool));
}

static svn_error_t *pgfs_txn_root(svn_fs_root_t **root,svn_fs_txn_t *txn,apr_pool_t *pool)
{
    PGresult *result;
    SVN_ERR(pgfs_txn_record(&result,txn->fs,txn->id,FALSE,pool));
    svn_boolean_t active=!strcmp(PQgetvalue(result,0,3),"active");
    apr_uint32_t flags=(apr_uint32_t)strtoul(PQgetvalue(result,0,2),NULL,10); PQclear(result);
    if(!active) return svn_error_create(SVN_ERR_FS_NO_SUCH_TRANSACTION,NULL,"Transaction is no longer active");
    apr_pool_t *root_pool=svn_pool_create(pool);
    *root=apr_pcalloc(root_pool,sizeof(**root));
    (*root)->pool=root_pool; (*root)->fs=txn->fs; (*root)->is_txn_root=TRUE;
    (*root)->txn=apr_pstrdup(root_pool,txn->id); (*root)->txn_flags=flags;
    (*root)->rev=txn->base_rev; (*root)->vtable=&pgfs_root_vtable;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_txn_props(apr_hash_t **props,svn_fs_txn_t *txn,apr_pool_t *pool)
{
    PGresult *record;
    SVN_ERR(pgfs_txn_record(&record,txn->fs,txn->id,FALSE,pool));
    svn_boolean_t active=!strcmp(PQgetvalue(record,0,3),"active"); PQclear(record);
    if(!active) return svn_error_create(SVN_ERR_FS_NO_SUCH_TRANSACTION,NULL,"Transaction is no longer active");
    pgfs_data_t *data=txn->fs->fsap_data;
    const char *values[]={data->repository_id,txn->id};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,txn->fs,"SELECT name,encode(value,'hex') FROM pgfs.transaction_properties "
        "WHERE repository_id=$1::uuid AND txn_id=$2",2,values));
    *props=apr_hash_make(pool);
    for(int i=0;i<PQntuples(result);++i)
        apr_hash_set(*props,apr_pstrdup(pool,PQgetvalue(result,i,0)),APR_HASH_KEY_STRING,
                     pgfs_decode_hex(PQgetvalue(result,i,1),pool));
    PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_txn_prop(svn_string_t **value,svn_fs_txn_t *txn,const char *name,apr_pool_t *pool)
{
    apr_hash_t *props;
    SVN_ERR(pgfs_txn_props(&props,txn,pool));
    *value=apr_hash_get(props,name,APR_HASH_KEY_STRING);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_txn_props_body(svn_fs_txn_t *txn,const apr_array_header_t *props,apr_pool_t *pool)
{
    svn_fs_root_t *root;
    SVN_ERR(pgfs_txn_root(&root,txn,pool));
    SVN_ERR(pgfs_lock_txn(root,pool));
    pgfs_data_t *data=txn->fs->fsap_data;
    for(int i=0;i<props->nelts;++i) {
        svn_prop_t *prop=&APR_ARRAY_IDX(props,i,svn_prop_t);
        if(!strcmp(prop->name,SVN_PROP_REVISION_DATE)) {
            const char *v[]={data->repository_id,txn->id};
            SVN_ERR(pgfs_command(txn->fs,"UPDATE pgfs.transaction_properties SET value=convert_to('1','UTF8') "
                "WHERE repository_id=$1::uuid AND txn_id=$2 AND name='svn:client-date'",2,v));
        }
        const char *values[]={data->repository_id,txn->id,prop->name,prop->value ? prop->value->data : NULL};
        if(!prop->value) SVN_ERR(pgfs_command(txn->fs,"DELETE FROM pgfs.transaction_properties "
            "WHERE repository_id=$1::uuid AND txn_id=$2 AND name=$3",3,values));
        else {
            if(prop->value->len>INT_MAX) return svn_error_create(SVN_ERR_FS_GENERAL,NULL,"Transaction property too large");
            int lengths[]={0,0,0,(int)prop->value->len},formats[]={0,0,0,1};
            PGresult *result;
            SVN_ERR(pgfs_query_full(&result,txn->fs,
                "INSERT INTO pgfs.transaction_properties(repository_id,txn_id,name,value) "
                "VALUES($1::uuid,$2,$3,$4::bytea) ON CONFLICT(repository_id,txn_id,name) DO UPDATE SET value=EXCLUDED.value",
                4,values,lengths,formats,0));
            PQclear(result);
        }
    }
    const char *flag_values[]={data->repository_id,txn->id};
    SVN_ERR(pgfs_command(txn->fs,"UPDATE pgfs.transactions SET flags="
        "(CASE WHEN EXISTS(SELECT 1 FROM pgfs.transaction_properties p WHERE p.repository_id=$1::uuid AND p.txn_id=$2 AND p.name='svn:check-ood') THEN 1 ELSE 0 END) + "
        "(CASE WHEN EXISTS(SELECT 1 FROM pgfs.transaction_properties p WHERE p.repository_id=$1::uuid AND p.txn_id=$2 AND p.name='svn:check-locks') THEN 2 ELSE 0 END) + "
        "(CASE WHEN EXISTS(SELECT 1 FROM pgfs.transaction_properties p WHERE p.repository_id=$1::uuid AND p.txn_id=$2 AND p.name='svn:client-date') THEN 4 ELSE 0 END) "
        "WHERE repository_id=$1::uuid AND txn_id=$2",2,flag_values));
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_change_txn_props(svn_fs_txn_t *txn,const apr_array_header_t *props,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(txn->fs));
    return pgfs_unit_end(txn->fs,pgfs_txn_props_body(txn,props,pool));
}

static svn_error_t *pgfs_change_txn_prop(svn_fs_txn_t *txn,const char *name,
                                       const svn_string_t *value,apr_pool_t *pool)
{
    apr_array_header_t *props=apr_array_make(pool,1,sizeof(svn_prop_t));
    svn_prop_t prop={name,value}; APR_ARRAY_PUSH(props,svn_prop_t)=prop;
    return pgfs_change_txn_props(txn,props,pool);
}

static svn_error_t *pgfs_abort_body(svn_fs_t *fs,const char *name,apr_pool_t *pool)
{
    PGresult *result;
    SVN_ERR(pgfs_txn_record(&result,fs,name,TRUE,pool));
    svn_boolean_t committed=!strcmp(PQgetvalue(result,0,3),"committed"); PQclear(result);
    if(committed) return svn_error_create(SVN_ERR_FS_NO_SUCH_TRANSACTION,NULL,"Committed transaction cannot be aborted");
    const char *values[]={((pgfs_data_t *)fs->fsap_data)->repository_id,name};
    return pgfs_command(fs,"UPDATE pgfs.transactions SET state='aborted' "
        "WHERE repository_id=$1::uuid AND txn_id=$2",2,values);
}

static svn_error_t *pgfs_purge_txn(svn_fs_t *fs,const char *name,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(fs));
    return pgfs_unit_end(fs,pgfs_abort_body(fs,name,pool));
}

static svn_error_t *pgfs_abort_txn(svn_fs_txn_t *txn,apr_pool_t *pool)
{
    return pgfs_purge_txn(txn->fs,txn->id,pool);
}

static svn_error_t *pgfs_list_txns(apr_array_header_t **names,svn_fs_t *fs,apr_pool_t *pool)
{
    const char *values[]={((pgfs_data_t *)fs->fsap_data)->repository_id};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,"SELECT txn_id FROM pgfs.transactions "
        "WHERE repository_id=$1::uuid AND state='active' ORDER BY created_at,txn_id",1,values));
    *names=apr_array_make(pool,PQntuples(result),sizeof(const char *));
    for(int i=0;i<PQntuples(result);++i) APR_ARRAY_PUSH(*names,const char *)=apr_pstrdup(pool,PQgetvalue(result,i,0));
    PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_conflict(const char **conflict,const char *path,apr_pool_t *pool)
{
    if(conflict) *conflict=apr_pstrdup(pool,path);
    return svn_error_createf(SVN_ERR_FS_CONFLICT,NULL,"Conflict at '%s'",path);
}

/* Mirror FSFS's tree merge rules: distinct directory entries can merge;
   concurrent file edits, replacements and double deletion conflict. */
static svn_error_t *pgfs_merge_nodes(apr_int64_t *merged,const char **conflict,svn_fs_t *fs,
    apr_int64_t base_id,apr_int64_t source_id,apr_int64_t target_id,
    const char *txn,const char *path,apr_pool_t *pool)
{
    if(source_id==base_id || source_id==target_id) { *merged=target_id; return SVN_NO_ERROR; }
    if(target_id==base_id) { *merged=source_id; return SVN_NO_ERROR; }
    if(!base_id || !source_id || !target_id) return pgfs_conflict(conflict,path,pool);
    pgfs_node_t *base,*source,*target;
    SVN_ERR(pgfs_load_node(&base,fs,base_id,pool)); SVN_ERR(pgfs_load_node(&source,fs,source_id,pool));
    SVN_ERR(pgfs_load_node(&target,fs,target_id,pool));
    if(base->kind!=svn_node_dir || source->kind!=svn_node_dir || target->kind!=svn_node_dir ||
       base->node_id!=source->node_id || base->node_id!=target->node_id ||
       base->copy_id!=source->copy_id || base->copy_id!=target->copy_id)
        return pgfs_conflict(conflict,path,pool);
    svn_boolean_t same;
    SVN_ERR(pgfs_props_equal(&same,fs,source->properties,base->properties,pool));
    if(!same) return pgfs_conflict(conflict,path,pool);
    apr_hash_t *a,*s,*t;
    SVN_ERR(pgfs_entries(&a,fs,base->directory,pool)); SVN_ERR(pgfs_entries(&s,fs,source->directory,pool));
    SVN_ERR(pgfs_entries(&t,fs,target->directory,pool));
    SVN_ERR(pgfs_props_equal(&same,fs,target->properties,base->properties,pool));
    if(!same) {
        if(apr_hash_count(a)!=apr_hash_count(s)) return pgfs_conflict(conflict,path,pool);
        for(apr_hash_index_t *hi=apr_hash_first(pool,a);hi;hi=apr_hash_next(hi)) {
            const void *key; void *value; apr_hash_this(hi,&key,NULL,&value);
            pgfs_entry_t *entry=apr_hash_get(s,key,APR_HASH_KEY_STRING);
            if(!entry || entry->kind!=((pgfs_entry_t *)value)->kind) return pgfs_conflict(conflict,path,pool);
        }
    }
    apr_hash_t *names=apr_hash_make(pool);
    apr_hash_t *sets[]={a,s,t};
    for(int i=0;i<3;++i) for(apr_hash_index_t *hi=apr_hash_first(pool,sets[i]);hi;hi=apr_hash_next(hi)) {
        const void *key; apr_hash_this(hi,&key,NULL,NULL); apr_hash_set(names,key,APR_HASH_KEY_STRING,(void *)1);
    }
    apr_int64_t directory=target->directory;
    for(apr_hash_index_t *hi=apr_hash_first(pool,names);hi;hi=apr_hash_next(hi)) {
        const void *key; apr_hash_this(hi,&key,NULL,NULL);
        pgfs_entry_t *ae=apr_hash_get(a,key,APR_HASH_KEY_STRING),*se=apr_hash_get(s,key,APR_HASH_KEY_STRING),
                     *te=apr_hash_get(t,key,APR_HASH_KEY_STRING);
        apr_int64_t result;
        /* Two independent deletions are a conflict (not a shared zero ID). */
        if(ae && !se && !te) return pgfs_conflict(conflict,pgfs_join(path,key,pool),pool);
        SVN_ERR(pgfs_merge_nodes(&result,conflict,fs,ae ? ae->id : 0,se ? se->id : 0,te ? te->id : 0,
                                 txn,pgfs_join(path,key,pool),pool));
        if(result!=(te ? te->id : 0)) SVN_ERR(pgfs_edit_directory(&directory,fs,directory,key,result,pool));
    }
    SVN_ERR(pgfs_clone(merged,fs,target,txn,path,pool));
    const char *v[]={((pgfs_data_t *)fs->fsap_data)->repository_id,pgfs_number(*merged,pool),
        pgfs_number(directory,pool),pgfs_number(source_id,pool),pgfs_number(source->predecessor_count+1,pool)};
    return pgfs_command(fs,"UPDATE pgfs.node_revisions SET directory_id=$3::bigint,"
        "predecessor_id=$4::bigint,predecessor_count=$5::bigint WHERE repository_id=$1::uuid "
        "AND node_revision_id=$2::bigint",5,v);
}

static svn_error_t *pgfs_commit_body(const char **conflict,svn_revnum_t *new_rev,
                                    svn_fs_txn_t *txn,svn_boolean_t *receipt,apr_pool_t *pool)
{
    pgfs_data_t *data=txn->fs->fsap_data;
    const char *v[]={data->repository_id,txn->id};
    PGresult *result;
    /* Lock ordering: repository, then transaction. Staging takes only txn. */
    SVN_ERR(pgfs_query(&result,txn->fs,"SELECT head_rev FROM pgfs.repositories "
        "WHERE repository_id=$1::uuid FOR UPDATE",1,v));
    if(PQntuples(result)!=1) { PQclear(result); return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"Missing repository"); }
    svn_revnum_t head=(svn_revnum_t)apr_atoi64(PQgetvalue(result,0,0)); PQclear(result);
    if(head==LONG_MAX) return svn_error_create(SVN_ERR_FS_GENERAL,NULL,"Native SVN revision range exhausted");
    SVN_ERR(pgfs_txn_record(&result,txn->fs,txn->id,TRUE,pool));
    if(!strcmp(PQgetvalue(result,0,3),"committed")) {
        *receipt=TRUE; *new_rev=(svn_revnum_t)apr_atoi64(PQgetvalue(result,0,4)); PQclear(result); return SVN_NO_ERROR;
    }
    svn_boolean_t active=!strcmp(PQgetvalue(result,0,3),"active");
    txn->base_rev=(svn_revnum_t)apr_atoi64(PQgetvalue(result,0,0));
    apr_int64_t target=apr_atoi64(PQgetvalue(result,0,1)); PQclear(result);
    if(!active) return svn_error_create(SVN_ERR_FS_NO_SUCH_TRANSACTION,NULL,"Transaction has been aborted");
    if(data->observer.before_publish)
        SVN_ERR(data->observer.before_publish(data->observer.baton,data->connection,txn->id,pool));
    if(apr_hash_get(data->active_writers,txn->id,APR_HASH_KEY_STRING))
        return svn_error_create(SVN_ERR_FS_REP_BEING_WRITTEN,NULL,"A PgFS file text stream is still open");
    SVN_ERR(pgfs_query(&result,txn->fs,
        "SELECT pg_try_advisory_xact_lock(hashtextextended($1||':'||$2,0))",2,v));
    svn_boolean_t writer_free=PQgetvalue(result,0,0)[0]=='t'; PQclear(result);
    if(!writer_free) return svn_error_create(SVN_ERR_FS_REP_BEING_WRITTEN,NULL,"A PgFS file text stream is still open");
    SVN_ERR(pgfs_command(txn->fs,"UPDATE pgfs.contents SET abandoned=true WHERE repository_id=$1::uuid "
        "AND owner_txn=$2 AND NOT ready AND NOT abandoned",2,v));
    SVN_ERR(pgfs_check_commit_locks(txn,pool));
    if(head!=txn->base_rev) {
        svn_fs_root_t *base,*source;
        SVN_ERR(pgfs_revision_root(&base,txn->fs,txn->base_rev,pool));
        SVN_ERR(pgfs_revision_root(&source,txn->fs,head,pool));
        SVN_ERR(pgfs_merge_nodes(&target,conflict,txn->fs,
            ((pgfs_root_data_t *)base->fsap_data)->node_revision_id,
            ((pgfs_root_data_t *)source->fsap_data)->node_revision_id,target,txn->id,"/",pool));
    }
    /* Refresh change IDs after a directory merge; deleted paths retain their
       original ID. No incoming changes are added to this transaction's log. */
    SVN_ERR(pgfs_query(&result,txn->fs,"SELECT path FROM pgfs.transaction_changes "
        "WHERE repository_id=$1::uuid AND txn_id=$2 AND change_kind<>'delete'",2,v));
    for(int i=0;i<PQntuples(result);++i) {
        pgfs_node_t *node;
        svn_error_t *error=pgfs_lookup_at(&node,txn->fs,target,PQgetvalue(result,i,0),FALSE,pool);
        if(!error) {
            const char *update[]={data->repository_id,txn->id,PQgetvalue(result,i,0),pgfs_number(node->id,pool)};
            error=pgfs_command(txn->fs,"UPDATE pgfs.transaction_changes SET node_revision_id=$4::bigint "
                "WHERE repository_id=$1::uuid AND txn_id=$2 AND path=$3",4,update);
        }
        if(error) { PQclear(result); return error; }
    }
    PQclear(result);
    /* Every revision owns its root, even without any tree edit. */
    pgfs_node_t *root_node;
    SVN_ERR(pgfs_load_node(&root_node,txn->fs,target,pool));
    if(!root_node->owner || strcmp(root_node->owner,txn->id))
        SVN_ERR(pgfs_clone(&target,txn->fs,root_node,txn->id,"/",pool));
    svn_revnum_t revision=head+1;
    const char *values[]={data->repository_id,txn->id,pgfs_number(revision,pool),pgfs_number(target,pool),
        svn_time_to_cstring(apr_time_now(),pool)};
    SVN_ERR(pgfs_command(txn->fs,"UPDATE pgfs.node_revisions SET created_rev=$3::bigint,owner_txn=NULL "
        "WHERE repository_id=$1::uuid AND owner_txn=$2",3,values));
    SVN_ERR(pgfs_command(txn->fs,"INSERT INTO pgfs.revisions(repository_id,rev,root_node_revision_id,origin_txn_id) "
        "VALUES($1::uuid,$3::bigint,$4::bigint,$2)",4,values));
    SVN_ERR(pgfs_command(txn->fs,
        "INSERT INTO pgfs.revision_properties(repository_id,rev,name,value) "
        "SELECT repository_id,$3::bigint,name,value FROM pgfs.transaction_properties "
        "WHERE repository_id=$1::uuid AND txn_id=$2 "
        "AND name NOT IN ('svn:check-ood','svn:check-locks','svn:client-date')",3,values));
    SVN_ERR(pgfs_command(txn->fs,
        "INSERT INTO pgfs.revision_properties(repository_id,rev,name,value) "
        "SELECT $1::uuid,$3::bigint,'svn:date',convert_to($5,'UTF8') WHERE NOT EXISTS "
        "(SELECT 1 FROM pgfs.transaction_properties WHERE repository_id=$1::uuid "
        "AND txn_id=$2 AND name='svn:client-date' AND value=convert_to('1','UTF8')) "
        "ON CONFLICT(repository_id,rev,name) DO UPDATE SET value=EXCLUDED.value",5,values));
    SVN_ERR(pgfs_command(txn->fs,
        "INSERT INTO pgfs.revision_changes(repository_id,rev,path,node_revision_id,change_kind,node_kind,"
        "text_mod,prop_mod,mergeinfo_mod,copyfrom_rev,copyfrom_path) "
        "SELECT repository_id,$3::bigint,path,node_revision_id,change_kind,node_kind,text_mod,prop_mod,"
        "mergeinfo_mod,copyfrom_rev,copyfrom_path FROM pgfs.transaction_changes "
        "WHERE repository_id=$1::uuid AND txn_id=$2",3,values));
    SVN_ERR(pgfs_command(txn->fs,"UPDATE pgfs.transactions SET root_node_revision_id=$4::bigint,"
        "state='committed',committed_rev=$3::bigint WHERE repository_id=$1::uuid AND txn_id=$2",4,values));
    SVN_ERR(pgfs_command(txn->fs,"UPDATE pgfs.repositories SET head_rev=$3::bigint "
        "WHERE repository_id=$1::uuid",3,values));
    *new_rev=revision;
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_commit_txn(const char **conflict,svn_revnum_t *new_rev,svn_fs_txn_t *txn,apr_pool_t *pool)
{
    if(conflict) *conflict=NULL; *new_rev=SVN_INVALID_REVNUM;
    SVN_ERR(pgfs_unit_begin(txn->fs));
    svn_boolean_t receipt=FALSE,reconciled=FALSE;
    svn_error_t *error=pgfs_commit_body(conflict,new_rev,txn,&receipt,pool);
    svn_boolean_t attempted_new=!receipt && SVN_IS_VALID_REVNUM(*new_rev);
    error=pgfs_unit_end(txn->fs,error);
    /* A valid revision must never escape a failed publication. The persistent
       origin transaction lets a caller resolve an uncertain COMMIT outcome. */
    if(error) {
        *new_rev=SVN_INVALID_REVNUM;
        if(pgfs_resolve_commit_receipt(new_rev,txn)) {
            svn_error_clear(error); error=SVN_NO_ERROR; reconciled=TRUE;
            if(!attempted_new) receipt=TRUE;
        }
    }
    pgfs_data_t *data=txn->fs->fsap_data;
    if(!error && SVN_IS_VALID_REVNUM(*new_rev) && data->observer.published) {
        data->observer.published(data->observer.baton,receipt);
        if(reconciled && !receipt) data->observer.published(data->observer.baton,TRUE);
    }
    return error;
}

static svn_error_t *pgfs_deltify(svn_fs_t *fs,svn_revnum_t rev,apr_pool_t *pool)
{
    svn_fs_root_t *root;
    /* Fulltext storage has no post-commit deltification work. */
    return pgfs_revision_root(&root,fs,rev,pool);
}

static svn_error_t *pgfs_change_rev_prop_body(svn_fs_t *fs,svn_revnum_t rev,const char *name,
    const svn_string_t *const *expected,const svn_string_t *value,apr_pool_t *pool)
{
    pgfs_data_t *data=fs->fsap_data;
    const char *v[]={data->repository_id,pgfs_number(rev,pool),name};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,"SELECT head_rev FROM pgfs.repositories "
        "WHERE repository_id=$1::uuid FOR UPDATE",1,v));
    PQclear(result);

    if(data->observer.before_mutation)
        SVN_ERR(data->observer.before_mutation(data->observer.baton,data->connection,NULL,pool));

    SVN_ERR(pgfs_query(&result,fs,"SELECT 1 FROM pgfs.revisions "
        "WHERE repository_id=$1::uuid AND rev=$2::bigint FOR UPDATE",2,v));
    svn_boolean_t exists=PQntuples(result)==1; PQclear(result);
    if(!exists) return svn_error_createf(SVN_ERR_FS_NO_SUCH_REVISION,NULL,"No such revision %ld",rev);
    if(expected) {
        svn_string_t *actual;
        SVN_ERR(pgfs_revprop(&actual,fs,rev,name,TRUE,pool,pool));
        if((actual==NULL)!=(*expected==NULL) || (actual && !svn_string_compare(actual,*expected)))
            return svn_error_create(SVN_ERR_FS_PROP_BASEVALUE_MISMATCH,NULL,"Revision property base value mismatch");
    }
    if(!value) return pgfs_command(fs,"DELETE FROM pgfs.revision_properties "
        "WHERE repository_id=$1::uuid AND rev=$2::bigint AND name=$3",3,v);
    if(value->len>INT_MAX) return svn_error_create(SVN_ERR_FS_GENERAL,NULL,"Revision property too large");
    const char *values[]={v[0],v[1],v[2],value->data};
    int lengths[]={0,0,0,(int)value->len},formats[]={0,0,0,1};
    SVN_ERR(pgfs_query_full(&result,fs,
        "INSERT INTO pgfs.revision_properties(repository_id,rev,name,value) VALUES($1::uuid,$2::bigint,$3,$4::bytea) "
        "ON CONFLICT(repository_id,rev,name) DO UPDATE SET value=EXCLUDED.value,generation=pgfs.revision_properties.generation+1",
        4,values,lengths,formats,0));
    PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_change_rev_prop(svn_fs_t *fs,svn_revnum_t rev,const char *name,
    const svn_string_t *const *expected,const svn_string_t *value,apr_pool_t *pool)
{
    SVN_ERR(pgfs_unit_begin(fs));
    return pgfs_unit_end(fs,pgfs_change_rev_prop_body(fs,rev,name,expected,value,pool));
}
