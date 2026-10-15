/* Resolve the ambiguous outcome of a lost COMMIT response by locking and
   reading the persisted transaction receipt on a fresh primary connection. */
static PGresult *pgfs_receipt_query(pgfs_data_t *data,PGconn *connection,const char *sql,
                                  int count,const char *const *values)
{
    apr_time_t started=apr_time_now();
    if(data->observer.query_started) data->observer.query_started(data->observer.baton);
    PGresult *result=PQexecParams(connection,sql,count,NULL,values,NULL,NULL,0);
    if(data->observer.query) {
        const char *state=result ? PQresultErrorField(result,PG_DIAG_SQLSTATE) : NULL;
        svn_boolean_t ok=result && (PQresultStatus(result)==PGRES_COMMAND_OK || PQresultStatus(result)==PGRES_TUPLES_OK);
        data->observer.query(data->observer.baton,ok ? "00000" : state ? state : "08006",apr_time_now()-started);
    }
    return result;
}
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
        PGresult *result=pgfs_receipt_query(data,probe,"SET statement_timeout='5s'",0,NULL);
        svn_boolean_t ready=result && PQresultStatus(result)==PGRES_COMMAND_OK;
        if(result) PQclear(result);
        if(ready && !data->directory_adapter) {
            const char *parameters[]={data->repository_id};
            result=pgfs_receipt_query(data,probe,data->exclusive_lease
                ? "SELECT pg_try_advisory_lock(hashtextextended('pgvn-repo:'||$1,0))"
                : "SELECT pg_try_advisory_lock_shared(hashtextextended('pgvn-repo:'||$1,0))",
                1,parameters);
            ready=result && PQresultStatus(result)==PGRES_TUPLES_OK && PQntuples(result)==1 && PQgetvalue(result,0,0)[0]=='t';
            if(result) PQclear(result);
        }
        if(ready) {
            const char *parameters[]={data->repository_id,txn->id};
            /* FOR UPDATE waits for a still-finishing original COMMIT and then
               sees the final row state at READ COMMITTED isolation. */
            result=pgfs_receipt_query(data,probe,
                "SELECT state,committed_rev FROM pgfs.transactions WHERE repository_id=$1::uuid AND txn_id=$2 FOR UPDATE",
                2,parameters);
            if(result && PQresultStatus(result)==PGRES_TUPLES_OK && PQntuples(result)==1 &&
                !strcmp(PQgetvalue(result,0,0),"committed") && !PQgetisnull(result,0,1)) {
                apr_int64_t rev=apr_atoi64(PQgetvalue(result,0,1));
                if(rev>=0 && rev<=LONG_MAX) { *revision=(svn_revnum_t)rev; committed=TRUE; }
            }
            if(result) PQclear(result);
        }
        result=pgfs_receipt_query(data,probe,data->directory_adapter ? "RESET statement_timeout" : "SET statement_timeout='5s'",0,NULL);
        if(result) PQclear(result);
        if(ready && PQstatus(probe)==CONNECTION_OK) {
            PQfinish(data->connection); data->connection=probe; probe=NULL;
        }
    }
    if(probe) PQfinish(probe);
    apr_thread_mutex_unlock(data->mutex);
    return committed;
}
