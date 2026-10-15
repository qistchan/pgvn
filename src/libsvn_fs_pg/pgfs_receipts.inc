/* Resolve the ambiguous outcome of a lost COMMIT response by locking and
   reading the persisted transaction receipt on a fresh primary connection. */
static svn_boolean_t pgfs_resolve_commit_receipt(svn_revnum_t *revision,svn_fs_txn_t *txn)
{
    pgfs_data_t *data=txn->fs->fsap_data;
    if(apr_thread_mutex_lock(data->mutex)) return FALSE;
    if(PQstatus(data->connection)==CONNECTION_OK) {
        apr_thread_mutex_unlock(data->mutex); return FALSE;
    }
    const char *keywords[]={"dbname","connect_timeout",NULL};
    const char *values[]={data->connection_string,"5",NULL};
    PGconn *probe=PQconnectdbParams(keywords,values,1);
    svn_boolean_t committed=FALSE;
    if(PQstatus(probe)==CONNECTION_OK) {
        PGresult *result=PQexec(probe,"SET statement_timeout='5s'");
        svn_boolean_t ready=result && PQresultStatus(result)==PGRES_COMMAND_OK;
        if(result) PQclear(result);
        if(ready) {
            const char *parameters[]={data->repository_id,txn->id};
            /* FOR UPDATE waits for a still-finishing original COMMIT and then
               sees the final row state at READ COMMITTED isolation. */
            result=PQexecParams(probe,
                "SELECT state,committed_rev FROM pgfs.transactions WHERE repository_id=$1::uuid AND txn_id=$2 FOR UPDATE",
                2,NULL,parameters,NULL,NULL,0);
            if(result && PQresultStatus(result)==PGRES_TUPLES_OK && PQntuples(result)==1 &&
                !strcmp(PQgetvalue(result,0,0),"committed") && !PQgetisnull(result,0,1)) {
                apr_int64_t rev=apr_atoi64(PQgetvalue(result,0,1));
                if(rev>=0 && rev<=LONG_MAX) { *revision=(svn_revnum_t)rev; committed=TRUE; }
            }
            if(result) PQclear(result);
        }
        result=PQexec(probe,"RESET statement_timeout");
        if(result) PQclear(result);
        if(PQstatus(probe)==CONNECTION_OK) {
            PQfinish(data->connection); data->connection=probe; probe=NULL;
        }
    }
    if(probe) PQfinish(probe);
    apr_thread_mutex_unlock(data->mutex);
    return committed;
}
