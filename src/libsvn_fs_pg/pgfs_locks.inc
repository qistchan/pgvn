/* SVN locks are independent of version-tree nodes. Lock/unlock and revision
   publication serialize on the same repository row to close enforcement races. */
static svn_lock_t *pgfs_lock_value(PGresult *result,int row,apr_pool_t *pool)
{
    svn_lock_t *lock=svn_lock_create(pool);
    lock->path=apr_pstrdup(pool,PQgetvalue(result,row,0));
    lock->token=apr_pstrdup(pool,PQgetvalue(result,row,1));
    lock->owner=apr_pstrdup(pool,PQgetvalue(result,row,2));
    lock->comment=PQgetisnull(result,row,3) ? NULL : apr_pstrdup(pool,PQgetvalue(result,row,3));
    lock->is_dav_comment=PQgetvalue(result,row,4)[0]=='t';
    lock->creation_date=apr_atoi64(PQgetvalue(result,row,5));
    lock->expiration_date=apr_atoi64(PQgetvalue(result,row,6));
    return lock;
}

#define PGFS_LOCK_COLUMNS "path,token,owner,comment,is_dav_comment,creation_date,expiration_date"

static svn_error_t *pgfs_generate_token(const char **token,svn_fs_t *fs,apr_pool_t *pool)
{
    *token=apr_pstrcat(pool,"opaquelocktoken:",pgfs_uuid(pool),NULL);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_lock_repository(svn_revnum_t *head,svn_fs_t *fs,apr_pool_t *pool)
{
    const char *v[]={((pgfs_data_t *)fs->fsap_data)->repository_id};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,"SELECT head_rev FROM pgfs.repositories WHERE repository_id=$1::uuid FOR UPDATE",1,v));
    if(PQntuples(result)!=1) { PQclear(result); return svn_error_create(SVN_ERR_FS_CORRUPT,NULL,"Repository record is missing"); }
    *head=(svn_revnum_t)apr_atoi64(PQgetvalue(result,0,0)); PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_get_lock(svn_lock_t **lock,svn_fs_t *fs,const char *path,apr_pool_t *pool)
{
    const char *v[]={((pgfs_data_t *)fs->fsap_data)->repository_id,pgfs_path(path,pool),pgfs_number(apr_time_now(),pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,"SELECT " PGFS_LOCK_COLUMNS " FROM pgfs.locks WHERE repository_id=$1::uuid "
        "AND path=$2 AND (expiration_date=0 OR expiration_date>$3::bigint)",3,v));
    *lock=PQntuples(result) ? pgfs_lock_value(result,0,pool) : NULL; PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_verify_lock(svn_fs_t *fs,const svn_lock_t *lock)
{
    if(!fs->access_ctx || !fs->access_ctx->username)
        return svn_error_createf(SVN_ERR_FS_NO_USER,NULL,"No user is available for locked path '%s'",lock->path);
    if(strcmp(fs->access_ctx->username,lock->owner))
        return svn_error_createf(SVN_ERR_FS_LOCK_OWNER_MISMATCH,NULL,"User '%s' does not own lock on path '%s' (currently locked by '%s')",fs->access_ctx->username,lock->path,lock->owner);
    if(!apr_hash_get(fs->access_ctx->lock_tokens,lock->token,APR_HASH_KEY_STRING))
        return svn_error_createf(SVN_ERR_FS_BAD_LOCK_TOKEN,NULL,"Cannot verify lock on path '%s'; no matching lock-token available",lock->path);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_check_locks(svn_fs_root_t *root,const char *path,
                                     svn_boolean_t recursive,apr_pool_t *pool)
{
    if(!(root->txn_flags & SVN_FS_TXN_CHECK_LOCKS)) return SVN_NO_ERROR;
    path=pgfs_path(path,pool);
    const char *v[]={((pgfs_data_t *)root->fs->fsap_data)->repository_id,path,pgfs_number(apr_time_now(),pool),
        recursive ? "true" : "false"};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,root->fs,"SELECT " PGFS_LOCK_COLUMNS " FROM pgfs.locks "
        "WHERE repository_id=$1::uuid AND (expiration_date=0 OR expiration_date>$3::bigint) "
        "AND (path=$2 OR ($4::boolean AND ($2='/' OR starts_with(path,$2||'/')))) ORDER BY path",4,v));
    for(int i=0;i<PQntuples(result);++i) {
        svn_error_t *error=pgfs_verify_lock(root->fs,pgfs_lock_value(result,i,pool));
        if(error) { PQclear(result); return error; }
    }
    PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_check_commit_locks(svn_fs_txn_t *txn,apr_pool_t *pool)
{
    svn_fs_root_t *root;
    SVN_ERR(pgfs_txn_root(&root,txn,pool));
    if(!(root->txn_flags & SVN_FS_TXN_CHECK_LOCKS)) return SVN_NO_ERROR;
    const char *v[]={((pgfs_data_t *)txn->fs->fsap_data)->repository_id,txn->id,pgfs_number(apr_time_now(),pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,txn->fs,
        "SELECT DISTINCT l.path,l.token,l.owner,l.comment,l.is_dav_comment,l.creation_date,l.expiration_date "
        "FROM pgfs.locks l JOIN pgfs.transaction_changes c ON c.repository_id=l.repository_id "
        "AND (l.path=c.path OR (c.change_kind IN('delete','replace','add') AND "
        "(c.path='/' OR starts_with(l.path,c.path||'/')))) WHERE l.repository_id=$1::uuid "
        "AND c.txn_id=$2 AND (l.expiration_date=0 OR l.expiration_date>$3::bigint) ORDER BY l.path",3,v));
    for(int i=0;i<PQntuples(result);++i) {
        svn_error_t *error=pgfs_verify_lock(txn->fs,pgfs_lock_value(result,i,pool));
        if(error) { PQclear(result); return error; }
    }
    PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_lock_one(svn_lock_t **lock,svn_fs_t *fs,const char *path,
    const svn_fs_lock_target_t *target,const char *comment,svn_boolean_t dav,
    apr_time_t expiration,svn_boolean_t steal,apr_pool_t *result_pool,apr_pool_t *pool)
{
    svn_revnum_t head; SVN_ERR(pgfs_lock_repository(&head,fs,pool));
    svn_fs_root_t *root; SVN_ERR(pgfs_revision_root(&root,fs,head,pool));
    pgfs_node_t *node; SVN_ERR(pgfs_lookup(&node,root,path,TRUE,pool));
    if(!node) return svn_error_createf(SVN_IS_VALID_REVNUM(target->current_rev) ? SVN_ERR_FS_OUT_OF_DATE : SVN_ERR_FS_NOT_FOUND,NULL,"Path '%s' doesn't exist in HEAD revision",path);
    if(node->kind!=svn_node_file) return svn_error_create(SVN_ERR_FS_NOT_FILE,NULL,path);
    if(SVN_IS_VALID_REVNUM(target->current_rev)) {
        if(target->current_rev>head) return svn_error_create(SVN_ERR_FS_NO_SUCH_REVISION,NULL,"Lock revision is newer than HEAD");
        if(target->current_rev<node->created_rev) return svn_error_createf(SVN_ERR_FS_OUT_OF_DATE,NULL,"Lock failed: newer version of '%s' exists",path);
    }
    svn_lock_t *existing; SVN_ERR(pgfs_get_lock(&existing,fs,path,pool));
    if(existing && !steal) return svn_error_createf(SVN_ERR_FS_PATH_ALREADY_LOCKED,NULL,"Path '%s' is already locked by user '%s' in filesystem '%s'",path,existing->owner,fs->path);
    const char *token=target->token;
    if(!token) SVN_ERR(pgfs_generate_token(&token,fs,pool));
    pgfs_data_t *data=fs->fsap_data;
    const char *v[]={data->repository_id,path,token,fs->access_ctx->username,comment,
        dav ? "true" : "false",pgfs_number(apr_time_now(),pool),pgfs_number(expiration,pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,"SELECT path FROM pgfs.locks WHERE repository_id=$1::uuid AND token=$3 "
        "AND path<>$2 AND (expiration_date=0 OR expiration_date>$7::bigint)",7,v));
    svn_boolean_t duplicate=PQntuples(result)!=0; PQclear(result);
    if(duplicate) return svn_error_create(SVN_ERR_FS_BAD_LOCK_TOKEN,NULL,"Lock token belongs to another path");
    SVN_ERR(pgfs_command(fs,"DELETE FROM pgfs.locks WHERE repository_id=$1::uuid "
        "AND expiration_date<>0 AND expiration_date<=$7::bigint",7,v));
    SVN_ERR(pgfs_query(&result,fs,"INSERT INTO pgfs.locks(repository_id,path,token,owner,comment,is_dav_comment,creation_date,expiration_date) "
        "VALUES($1::uuid,$2,$3,$4,$5,$6::boolean,$7::bigint,$8::bigint) ON CONFLICT(repository_id,path) DO UPDATE SET "
        "token=EXCLUDED.token,owner=EXCLUDED.owner,comment=EXCLUDED.comment,is_dav_comment=EXCLUDED.is_dav_comment,"
        "creation_date=EXCLUDED.creation_date,expiration_date=EXCLUDED.expiration_date RETURNING " PGFS_LOCK_COLUMNS,8,v));
    *lock=pgfs_lock_value(result,0,result_pool); PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_lock_many(svn_fs_t *fs,apr_hash_t *targets,const char *comment,
    svn_boolean_t dav,apr_time_t expiration,svn_boolean_t steal,svn_fs_lock_callback_t callback,
    void *baton,apr_pool_t *result_pool,apr_pool_t *scratch_pool)
{
    if(!fs->access_ctx || !fs->access_ctx->username) return svn_error_create(SVN_ERR_FS_NO_USER,NULL,"Lock creation requires a user");
    apr_pool_t *iter=svn_pool_create(scratch_pool);
    for(apr_hash_index_t *hi=apr_hash_first(scratch_pool,targets);hi;hi=apr_hash_next(hi)) {
        svn_pool_clear(iter);
        const void *key; void *value; apr_hash_this(hi,&key,NULL,&value);
        const char *path=pgfs_path(key,result_pool);
        svn_lock_t *lock=NULL;
        svn_error_t *error=pgfs_unit_begin(fs);
        if(error) return error;
        error=pgfs_unit_end(fs,pgfs_lock_one(&lock,fs,path,value,comment,dav,expiration,steal,result_pool,iter));
        svn_error_t *callback_error=callback ? callback(baton,path,error ? NULL : lock,error,iter) : SVN_NO_ERROR;
        if(callback_error==error) error=NULL;
        svn_error_clear(error);
        if(callback_error) return callback_error;
    }
    svn_pool_destroy(iter);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_unlock_one(svn_fs_t *fs,const char *path,const char *token,
                                   svn_boolean_t force,apr_pool_t *pool)
{
    svn_revnum_t head; SVN_ERR(pgfs_lock_repository(&head,fs,pool));
    const char *v[]={((pgfs_data_t *)fs->fsap_data)->repository_id,path};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,"SELECT " PGFS_LOCK_COLUMNS " FROM pgfs.locks WHERE repository_id=$1::uuid AND path=$2",2,v));
    svn_lock_t *lock=PQntuples(result) ? pgfs_lock_value(result,0,pool) : NULL; PQclear(result);
    if(!lock) return svn_error_createf(SVN_ERR_FS_NO_SUCH_LOCK,NULL,"No lock on path '%s' in filesystem '%s'",path,fs->path);
    if(lock->expiration_date && lock->expiration_date<=apr_time_now()) return svn_error_create(SVN_ERR_FS_LOCK_EXPIRED,NULL,path);
    if(!force) {
        if(!token || strcmp(token,lock->token)) return svn_error_createf(SVN_ERR_FS_NO_SUCH_LOCK,NULL,"No lock on path '%s' in filesystem '%s'",path,fs->path);
        if(strcmp(fs->access_ctx->username,lock->owner)) return svn_error_createf(SVN_ERR_FS_LOCK_OWNER_MISMATCH,NULL,"User '%s' is trying to use a lock owned by '%s' in filesystem '%s'",fs->access_ctx->username,lock->owner,fs->path);
    }
    return pgfs_command(fs,"DELETE FROM pgfs.locks WHERE repository_id=$1::uuid AND path=$2",2,v);
}

static svn_error_t *pgfs_unlock_many(svn_fs_t *fs,apr_hash_t *targets,svn_boolean_t force,
    svn_fs_lock_callback_t callback,void *baton,apr_pool_t *result_pool,apr_pool_t *scratch_pool)
{
    if(!force && (!fs->access_ctx || !fs->access_ctx->username)) return svn_error_create(SVN_ERR_FS_NO_USER,NULL,"Unlock requires a user");
    apr_pool_t *iter=svn_pool_create(scratch_pool);
    for(apr_hash_index_t *hi=apr_hash_first(scratch_pool,targets);hi;hi=apr_hash_next(hi)) {
        svn_pool_clear(iter);
        const void *key; void *value; apr_hash_this(hi,&key,NULL,&value);
        const char *path=pgfs_path(key,result_pool);
        SVN_ERR(pgfs_unit_begin(fs));
        svn_error_t *error=pgfs_unit_end(fs,pgfs_unlock_one(fs,path,value,force,iter));
        svn_error_t *callback_error=callback ? callback(baton,path,NULL,error,iter) : SVN_NO_ERROR;
        if(callback_error==error) error=NULL;
        svn_error_clear(error);
        if(callback_error) return callback_error;
    }
    svn_pool_destroy(iter);
    return SVN_NO_ERROR;
}

static svn_error_t *pgfs_get_locks(svn_fs_t *fs,const char *path,svn_depth_t depth,
    svn_fs_get_locks_callback_t callback,void *baton,apr_pool_t *pool)
{
    path=pgfs_path(path,pool);
    const char *v[]={((pgfs_data_t *)fs->fsap_data)->repository_id,path,pgfs_number(apr_time_now(),pool)};
    PGresult *result;
    SVN_ERR(pgfs_query(&result,fs,"SELECT " PGFS_LOCK_COLUMNS " FROM pgfs.locks WHERE repository_id=$1::uuid "
        "AND (path=$2 OR $2='/' OR starts_with(path,$2||'/')) AND (expiration_date=0 OR expiration_date>$3::bigint) ORDER BY path",3,v));
    svn_error_t *error=SVN_NO_ERROR;
    for(int i=0;!error && i<PQntuples(result);++i) {
        const char *locked=PQgetvalue(result,i,0);
        if(strcmp(locked,path)) {
            if(depth==svn_depth_empty) continue;
            const char *suffix=locked+strlen(path)+(strcmp(path,"/") ? 1 : 0);
            if(depth!=svn_depth_infinity && strchr(suffix,'/')) continue;
        }
        if(callback) error=callback(baton,pgfs_lock_value(result,i,pool),pool);
    }
    PQclear(result);
    return error;
}
