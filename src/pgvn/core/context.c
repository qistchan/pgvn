#include <stdio.h>
#include <string.h>
#include "internal.h"
#include <apr_strings.h>
#include <apr_uuid.h>

void pgvn_metric_query(void *baton,const char *state,apr_interval_time_t elapsed)
{
    pgvn_context *context=baton;
    InterlockedIncrement64(&context->sql_calls);
    if(strcmp(state,"00000")) InterlockedIncrement64(&context->sql_errors);
    InterlockedAdd64(&context->sql_microseconds,elapsed);
}

void pgvn_metric_closed(void *baton)
{
    pgvn_context *context=baton;
    InterlockedDecrement(&context->leases);
}

svn_error_t *pgvn_exec(PGresult **result,pgvn_context *context,PGconn *connection,
    const char *sql,int count,const char *const *values)
{
    apr_time_t start=apr_time_now();
    Oid types[32];
    if(count<0 || count>32) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN parameter limit exceeded");
    for(int i=0;i<count;++i) types[i]=25;
    *result=PQexecParams(connection,sql,count,types,values,NULL,NULL,0);
    if(!*result || (PQresultStatus(*result)!=PGRES_COMMAND_OK && PQresultStatus(*result)!=PGRES_TUPLES_OK)) {
        const char *state=*result ? PQresultErrorField(*result,PG_DIAG_SQLSTATE) : NULL;
        if(!state) state="08006";
        pgvn_metric_query(context,state,apr_time_now()-start);
        apr_status_t code=!strcmp(state,"23505") ? SVN_ERR_FS_ALREADY_EXISTS :
            !strcmp(state,"40001") ? SVN_ERR_FS_TXN_OUT_OF_DATE : SVN_ERR_FS_GENERAL;
        svn_error_t *error=svn_error_createf(code,NULL,"PgVN database operation failed (SQLSTATE %s)",state);
        if(*result) PQclear(*result);
        *result=NULL;
        return error;
    }
    pgvn_metric_query(context,"00000",apr_time_now()-start);
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_connect(PGconn **connection,pgvn_context *context)
{
    if(InterlockedIncrement(&context->leases)>32) {
        pgvn_metric_closed(context);
        return svn_error_create(SVN_ERR_FS_GENERAL,NULL,"PgVN connection limit reached");
    }
    char timeout[16],options[64];
    snprintf(timeout,sizeof(timeout),"%d",context->connect_timeout_seconds);
    snprintf(options,sizeof(options),"-c statement_timeout=%d",context->statement_timeout_ms);
    const char *keywords[]={"dbname","connect_timeout","options","application_name",NULL};
    const char *values[]={context->connection_string,timeout,options,"pgvn",NULL};
    *connection=PQconnectdbParams(keywords,values,1);
    if(!*connection || PQstatus(*connection)!=CONNECTION_OK) {
        if(*connection) PQfinish(*connection);
        *connection=NULL;
        pgvn_metric_closed(context);
        return svn_error_create(SVN_ERR_FS_GENERAL,NULL,"PgVN database connection unavailable");
    }
    if(PQsetClientEncoding(*connection,"UTF8")) {
        PQfinish(*connection); *connection=NULL; pgvn_metric_closed(context);
        return svn_error_create(SVN_ERR_FS_GENERAL,NULL,"PgVN requires UTF-8 database communication");
    }
    return SVN_NO_ERROR;
}

static apr_status_t cleanup_context(void *baton)
{
    pgvn_context *context=baton;
    if(context->control) {
        PQfinish(context->control); context->control=NULL; pgvn_metric_closed(context);
    }
    return APR_SUCCESS;
}

svn_error_t *pgvn_control_query(PGresult **result,pgvn_context *context,
    const char *sql,int count,const char *const *values)
{
    apr_status_t status=apr_thread_mutex_lock(context->control_mutex);
    if(status) return svn_error_wrap_apr(status,"PgVN control lock failed");
    svn_error_t *error=pgvn_exec(result,context,context->control,sql,count,values);
    apr_thread_mutex_unlock(context->control_mutex);
    return error;
}

svn_error_t *pgvn_control_begin(pgvn_context *context)
{
    apr_status_t status=apr_thread_mutex_lock(context->control_mutex);
    if(status) return svn_error_wrap_apr(status,"PgVN control lock failed");
    PGresult *result;
    svn_error_t *error=pgvn_exec(&result,context,context->control,"BEGIN",0,NULL);
    if(result) PQclear(result);
    if(error) apr_thread_mutex_unlock(context->control_mutex);
    return error;
}

svn_error_t *pgvn_control_end(pgvn_context *context,svn_error_t *error)
{
    PGresult *result;
    svn_error_t *end=pgvn_exec(&result,context,context->control,error ? "ROLLBACK" : "COMMIT",0,NULL);
    if(result) PQclear(result);
    apr_thread_mutex_unlock(context->control_mutex);
    return svn_error_compose_create(error,end);
}

svn_error_t *pgvn_context_create(pgvn_context **output,const pgvn_context_options *options,apr_pool_t *pool)
{
    *output=NULL;
    if(!options || !options->connection_string || !*options->connection_string)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN connection configuration is required");
    pgvn_context *context=apr_pcalloc(pool,sizeof(*context));
    context->pool=pool;
    context->connection_string=apr_pstrdup(pool,options->connection_string);
    context->connect_timeout_seconds=options->connect_timeout_seconds ? options->connect_timeout_seconds : 3;
    context->statement_timeout_ms=options->statement_timeout_ms ? options->statement_timeout_ms : 5000;
    if(context->connect_timeout_seconds<1 || context->connect_timeout_seconds>30
       || context->statement_timeout_ms<100 || context->statement_timeout_ms>60000)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN database timeouts exceed supported limits");
    apr_status_t status=apr_thread_mutex_create(&context->control_mutex,APR_THREAD_MUTEX_NESTED,pool);
    if(status) return svn_error_wrap_apr(status,"PgVN control mutex initialization failed");
    SVN_ERR(pgvn_connect(&context->control,context));
    apr_pool_cleanup_register(pool,context,cleanup_context,apr_pool_cleanup_null);
    PGresult *result;
    SVN_ERR(pgvn_control_query(&result,context,
        "SELECT (SELECT max(version) FROM pgfs.schema_versions),"
        "(SELECT max(version) FROM pgvn.schema_versions),pg_is_in_recovery()",0,NULL));
    svn_boolean_t compatible=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"4")
        && !strcmp(PQgetvalue(result,0,1),"1") && !strcmp(PQgetvalue(result,0,2),"f");
    PQclear(result);
    if(!compatible) return svn_error_create(SVN_ERR_FS_UNSUPPORTED_FORMAT,NULL,"PgVN requires pgfs schema 4, pgvn schema 1 and a writable database");
    context->schema_checked_at=apr_time_now();
    apr_uuid_t uuid; char buffer[APR_UUID_FORMATTED_LENGTH+1];
    apr_uuid_get(&uuid); apr_uuid_format(buffer,&uuid);
    context->instance_id=apr_pstrdup(pool,buffer);
    *output=context;
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_status_snapshot(pgvn_status **output,pgvn_context *context,apr_pool_t *pool)
{
    pgvn_status *snapshot=apr_pcalloc(pool,sizeof(*snapshot));
    snapshot->instance_id=apr_pstrdup(pool,context->instance_id);
    snapshot->sampled_at=context->schema_checked_at;
    snapshot->age=apr_time_now()-snapshot->sampled_at;
    snapshot->freshness=snapshot->age<=apr_time_from_sec(PGVN_MONITOR_MAX_AGE_SECONDS)
        ? PGVN_FRESHNESS_FRESH : PGVN_FRESHNESS_STALE;
    /* A schema check alone does not make the unfinished protocol service ready. */
    snapshot->ready=FALSE;
    snapshot->reason="protocol_not_initialized";
    *output=snapshot;
    return SVN_NO_ERROR;
}

void pgvn_metrics_snapshot(pgvn_metrics *output,pgvn_context *context)
{
    output->sql_calls=InterlockedCompareExchange64(&context->sql_calls,0,0);
    output->sql_errors=InterlockedCompareExchange64(&context->sql_errors,0,0);
    output->sql_microseconds=InterlockedCompareExchange64(&context->sql_microseconds,0,0);
    output->connections=InterlockedCompareExchange(&context->leases,0,0);
}
