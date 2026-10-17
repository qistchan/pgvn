/* Component probe: real PgVN policy/protocol code + real PostgreSQL guards.
 * Repository FS binding, access identities and credential lookup are adapters;
 * this does not exercise the SVN wire protocol, crypto, or a complete product. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <apr_general.h>
#include <apr_strings.h>
#include <apr_version.h>
#include <apu_version.h>
#include "svn_version.h"
#include "pgvn_protocol.h"

static const char *id="11111111-1111-4111-8111-111111111111";
static const char *author="alice";
static unsigned checks;
static int fs_marker;
static volatile pgvn_atomic32 platform_leases;
static volatile pgvn_atomic64 platform_counter;
static pgvn_cancel_state platform_cancel;

static void *counter_worker(void *baton)
{
    (void)baton;
    for(int i=0;i<50000;++i) {
        pgvn_atomic_increment32(&platform_leases);
        pgvn_atomic_increment64(&platform_counter);
        pgvn_atomic_add64(&platform_counter,2);
        pgvn_atomic_decrement32(&platform_leases);
    }
    pgvn_cancel_signal(&platform_cancel);
    return NULL;
}

static void *cancel_worker(void *baton)
{
    apr_sleep(100000);
    pgvn_cancel_signal((pgvn_cancel_event)baton);
    return NULL;
}


static void require(int condition,const char *name)
{
    if(!condition) { fprintf(stderr,"FAIL: %s\n",name); exit(1); }
    ++checks;
}

static void success(svn_error_t *error,const char *name)
{
    if(error) { fprintf(stderr,"FAIL: %s (%ld)\n",name,(long)error->apr_err); svn_error_clear(error); exit(1); }
    ++checks;
}

static void denied(svn_error_t *error,apr_status_t code,const char *name)
{
    require(error && error->apr_err==code,name);
    svn_error_clear(error);
}

static void sql(PGconn *connection,const char *text)
{
    PGresult *result=PQexec(connection,text);
    require(result && (PQresultStatus(result)==PGRES_COMMAND_OK || PQresultStatus(result)==PGRES_TUPLES_OK),"fixture SQL");
    PQclear(result);
}

/* Telemetry sinks are outside this component; the production context and SQL
 * driver below still maintain their real atomic counters and APR mutex. */
void pgvn_telemetry_query(const char *state,apr_interval_time_t elapsed) { (void)state; (void)elapsed; }
void pgvn_telemetry_query_start(void) { }
void pgvn_telemetry_connection(int change,int rejected,int failed) { (void)change; (void)rejected; (void)failed; }
void pgvn_telemetry_waiter(int change) { (void)change; }
void pgvn_telemetry_io(apr_size_t received,apr_size_t sent) { (void)received; (void)sent; }

svn_error_t *pgvn_credential_guard(pgvn_context *context,PGconn *connection,const char *domain,
    const char *user,apr_int64_t generation,apr_pool_t *pool)
{
    const char *values[]={domain,user,apr_psprintf(pool,"%" APR_INT64_T_FMT,generation)};
    PGresult *result;
    SVN_ERR(pgvn_exec(&result,context,connection,"SELECT pgvn.guard_credential($1,$2,$3::bigint)",3,values));
    int valid=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t");
    PQclear(result);
    return valid ? SVN_NO_ERROR : svn_error_create(SVN_ERR_AUTHN_FAILED,NULL,"Fixture credential is stale");
}

svn_error_t *pgvn_credential_check(pgvn_context *context,const char *domain,
    const char *user,apr_int64_t generation,apr_pool_t *pool)
{ return pgvn_credential_guard(context,context->control,domain,user,generation,pool); }

svn_error_t *svn_fs_get_access(svn_fs_access_t **access,svn_fs_t *fs)
{ (void)fs; *access=(svn_fs_access_t *)&fs_marker; return SVN_NO_ERROR; }

svn_error_t *svn_fs_access_get_username(const char **username,svn_fs_access_t *access)
{ (void)access; *username=author; return SVN_NO_ERROR; }

svn_fs_t *svn_repos_fs(svn_repos_t *repos)
{ (void)repos; return (svn_fs_t *)&fs_marker; }

svn_error_t *svn_repos_remember_client_capabilities(svn_repos_t *repos,const apr_array_header_t *capabilities)
{ (void)repos; (void)capabilities; return SVN_NO_ERROR; }

svn_error_t *pgvn_repos_open_svn(pgvn_repository **output,pgvn_context *context,const char *repository_id,apr_pool_t *pool)
{
    pgvn_repository *repository=apr_pcalloc(pool,sizeof(*repository));
    repository->context=context; repository->repository_id=repository_id;
    repository->native_svn=TRUE; repository->svn=(svn_repos_t *)&fs_marker;
    SVN_ERR(pgvn_policy_acquire(&repository->policy,context,repository_id,0,pool));
    *output=repository; return SVN_NO_ERROR;
}

svn_repos_t *pgvn_repo_svn(pgvn_repository *repository) { return repository->svn; }
void pgvn_command_repository(const char *repository_id) { (void)repository_id; }
svn_error_t *pgvn_external_hook(const pgvn_policy *policy,const pgvn_hook_context *event,apr_pool_t *pool)
{ (void)policy; (void)event; (void)pool; return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,"External hook execution is outside this component"); }

int main(void)
{
    require(apr_initialize()==APR_SUCCESS,"APR initialization");
    apr_pool_t *pool; require(apr_pool_create(&pool,NULL)==APR_SUCCESS,"pool");
    uint64_t started,finished;
    require(!pgvn_monotonic_milliseconds(&started),"monotonic clock initialized");
    require(!pgvn_cancel_is_set(NULL) && !pgvn_cancel_is_set(&platform_cancel),"null and unset cancellation");
    pthread_t workers[4];
    for(int i=0;i<4;++i) require(!pthread_create(&workers[i],NULL,counter_worker,NULL),"counter worker starts");
    for(int i=0;i<4;++i) require(!pthread_join(workers[i],NULL),"counter worker joins");
    require(pgvn_atomic_read32(&platform_leases)==0 && pgvn_atomic_read64(&platform_counter)==600000,"POSIX atomic counters survive contention");
    require(pgvn_cancel_is_set(&platform_cancel),"cross-thread cancellation observed");
    require(!pgvn_monotonic_milliseconds(&finished) && finished>=started,"monotonic clock does not go backwards");
    apr_sockaddr_t *address;
    apr_socket_t *listener,*client,*server;
    require(apr_sockaddr_info_get(&address,"127.0.0.1",APR_INET,0,0,pool)==APR_SUCCESS,"loopback address");
    require(apr_socket_create(&listener,APR_INET,SOCK_STREAM,APR_PROTO_TCP,pool)==APR_SUCCESS,"loopback listener");
    require(apr_socket_bind(listener,address)==APR_SUCCESS && apr_socket_listen(listener,1)==APR_SUCCESS,"ephemeral isolated listener");
    require(apr_socket_addr_get(&address,APR_LOCAL,listener)==APR_SUCCESS,"ephemeral port");
    require(apr_socket_create(&client,APR_INET,SOCK_STREAM,APR_PROTO_TCP,pool)==APR_SUCCESS,"loopback client");
    require(apr_socket_connect(client,address)==APR_SUCCESS && apr_socket_accept(&server,listener,pool)==APR_SUCCESS,"loopback connection");
    pgvn_cancel_state io_cancel={0}; pgvn_svn_set_io_cancel(&io_cancel);
    apr_size_t length=5;
    success(pgvn_svn_socket_write(client,pool,"hello",&length,TRUE),"native POSIX socket write");
    char payload[8]={0}; length=5;
    success(pgvn_svn_socket_read(server,pool,payload,&length),"native POSIX socket read");
    require(length==5 && !memcmp(payload,"hello",5),"native socket bytes preserved");
    pthread_t canceller;
    require(!pthread_create(&canceller,NULL,cancel_worker,&io_cancel),"I/O cancellation worker");
    require(!pgvn_monotonic_milliseconds(&started),"I/O cancellation start clock");
    length=1;
    denied(pgvn_svn_socket_read(server,pool,payload,&length),SVN_ERR_CANCELLED,"blocked native read observes cancellation");
    require(!pthread_join(canceller,NULL),"I/O cancellation worker joins");
    require(!pgvn_monotonic_milliseconds(&finished) && finished-started<5000 && length==0,"cancelled read is bounded and reports no bytes");
    length=5;
    denied(pgvn_svn_socket_write(client,pool,"hello",&length,TRUE),SVN_ERR_CANCELLED,"cancelled write refused");
    require(length==0,"cancelled write reports no bytes");
    pgvn_svn_set_io_cancel(NULL);
    apr_socket_close(server); apr_socket_close(client); apr_socket_close(listener);
    pgvn_context context={0}; context.catalog_version=7;
    context.pool=pool; context.minimum_catalog_version=PGVN_CATALOG_VERSION;
    context.connection_string="host=127.0.0.1 dbname=pgvn_component user=pgvn_fixture_owner sslmode=disable";
    context.connect_timeout_seconds=3; context.statement_timeout_ms=5000;
    require(apr_thread_mutex_create(&context.control_mutex,APR_THREAD_MUTEX_NESTED,pool)==APR_SUCCESS,"production control mutex");
    context.control=PQconnectdb("host=127.0.0.1 dbname=pgvn_component user=pgvn_fixture_owner sslmode=disable");
    require(PQstatus(context.control)==CONNECTION_OK,"isolated PostgreSQL connection");
    context.leases=1;
    success(pgvn_database_validate(&context),"production schema 7 validation on Linux");
    pgvn_cancel_state cancelled={0}; context.cancellation_event=&cancelled; pgvn_cancel_signal(&cancelled);
    PGconn *cancelled_connection=NULL;
    denied(pgvn_connect(&cancelled_connection,&context),SVN_ERR_CANCELLED,"cancelled new connection refused");
    require(cancelled_connection==NULL && pgvn_atomic_read32(&context.leases)==1,"cancel does not leak a connection lease");
    context.cancellation_event=NULL;
    pgvn_policy *policy;
    success(pgvn_policy_acquire(&policy,&context,id,0,pool),"load mode, groups and authz");
    require(policy->svn_authz_bypass,"bypass policy loaded");
    svn_boolean_t allowed;
    success(pgvn_policy_check(&allowed,policy,"alice","/hidden",svn_authz_write,pool),"public policy check");
    require(!allowed,"public/DAV/admin policy still denies");
    pgvn_repository repository={0}; repository.context=&context; repository.repository_id=id; repository.policy=policy;
    repository.native_svn=TRUE; repository.credential_domain="pgvn"; repository.credential_user="alice"; repository.credential_generation=1;
    sql(context.control,"BEGIN");
    success(pgvn_guard_mutation(&repository,context.control,"/hidden",pool),"native bypass mutation");
    success(pgvn_guard_publish(&repository,context.control,"empty",pool),"native bypass empty commit");
    denied(pgvn_guard_mutation(&repository,context.control,"/../bad",pool),SVN_ERR_FS_PATH_SYNTAX,"bypass keeps canonical path validation");
    sql(context.control,"ROLLBACK");
    repository.native_svn=FALSE;
    sql(context.control,"BEGIN");
    denied(pgvn_guard_mutation(&repository,context.control,"/hidden",pool),SVN_ERR_AUTHZ_UNWRITABLE,"DAV mutation denied");
    denied(pgvn_guard_publish(&repository,context.control,"empty",pool),SVN_ERR_AUTHZ_UNWRITABLE,"DAV empty commit denied");
    sql(context.control,"ROLLBACK");
    repository.native_svn=TRUE; repository.administrative=TRUE;
    require(!pgvn_repository_svn_bypass(&repository),"administrative context never gains native bypass");
    repository.administrative=FALSE; policy->svn_authz_bypass=FALSE;
    sql(context.control,"BEGIN");
    denied(pgvn_guard_mutation(&repository,context.control,"/hidden",pool),SVN_ERR_AUTHZ_UNWRITABLE,"enforce still denies");
    sql(context.control,"ROLLBACK"); policy->svn_authz_bypass=TRUE;
    repository.credential_user=NULL;
    denied(pgvn_guard_publish(&repository,context.control,"empty",pool),SVN_ERR_AUTHN_FAILED,"anonymous bypass denied");
    repository.credential_user="alice"; author="mallory";
    sql(context.control,"BEGIN");
    denied(pgvn_guard_publish(&repository,context.control,"empty",pool),SVN_ERR_AUTHN_FAILED,"spoofed commit author denied");
    sql(context.control,"ROLLBACK"); author="alice";
    sql(context.control,"UPDATE pgvn.credentials SET generation=2");
    denied(pgvn_guard_publish(&repository,context.control,"empty",pool),SVN_ERR_AUTHN_FAILED,"stale credential denied");
    sql(context.control,"UPDATE pgvn.credentials SET generation=1,enabled=false");
    denied(pgvn_guard_mutation(&repository,context.control,"/hidden",pool),SVN_ERR_AUTHN_FAILED,"disabled credential denied");
    sql(context.control,"UPDATE pgvn.credentials SET enabled=true; UPDATE pgvn.repositories SET generation=2");
    denied(pgvn_guard_publish(&repository,context.control,"empty",pool),SVN_ERR_AUTHZ_UNWRITABLE,"stale repository generation denied");
    sql(context.control,"UPDATE pgvn.repositories SET generation=1,enabled=false");
    denied(pgvn_guard_publish(&repository,context.control,"empty",pool),SVN_ERR_AUTHZ_UNWRITABLE,"disabled repository denied");
    sql(context.control,"UPDATE pgvn.repositories SET enabled=true");
    PGconn *other=PQconnectdb("host=127.0.0.1 dbname=pgvn_component user=pgvn_fixture_owner sslmode=disable");
    require(PQstatus(other)==CONNECTION_OK,"second isolated connection");
    sql(other,"SET lock_timeout='100ms'"); sql(context.control,"BEGIN");
    success(pgvn_guard_publish(&repository,context.control,"empty",pool),"publication locks acquired");
    const char *updates[]={"UPDATE pgvn.repositories SET enabled=false","UPDATE pgvn.credentials SET generation=2"};
    for(unsigned i=0;i<2;++i) {
        PGresult *result=PQexec(other,updates[i]);
        const char *state=PQresultErrorField(result,PG_DIAG_SQLSTATE);
        require(state && !strcmp(state,"55P03"),"credential/config updates wait for publication transaction"); PQclear(result);
    }
    sql(context.control,"ROLLBACK"); sql(other,"UPDATE pgvn.credentials SET generation=2");
    denied(pgvn_guard_publish(&repository,context.control,"empty",pool),SVN_ERR_AUTHN_FAILED,"revocation effective after guard releases locks");
    sql(other,"UPDATE pgvn.credentials SET generation=1"); PQfinish(other);
    pgvn_hook_context event={0}; event.event=PGVN_HOOK_PRE_COMMIT;
    policy->hook_handlers[event.event]="reject";
    denied(pgvn_hook_dispatch(policy,&event,pool),SVN_ERR_REPOS_HOOK_FAILURE,"hook rejection retained");
    policy->hook_handlers[event.event]=NULL;
    repository_t protocol={0}; protocol.pgvn_context=&context; protocol.pgvn_domain="pgvn";
    protocol.capabilities=apr_array_make(pool,0,sizeof(const char *));
    success(pgvn_svn_find(&protocol,"svn://localhost/test/hidden",TRUE,pool,pool),"native readonly routing");
    require(protocol.anon_access==NO_ACCESS && protocol.auth_access==READ_ACCESS && protocol.authzdb==NULL,"bypass requires authentication and retains readonly");
    protocol.pgvn_auth_generation=1;
    denied(pgvn_svn_command(&protocol,NULL,pool),SVN_ERR_AUTHN_FAILED,"unauthenticated command denied");
    success(pgvn_svn_authenticated(&protocol,"alice",pool),"identity bound after authentication");
    success(pgvn_svn_command(&protocol,"alice",pool),"authenticated command allowed");
    denied(pgvn_svn_command(&protocol,"mallory",pool),SVN_ERR_AUTHN_FAILED,"command identity substitution denied");
    success(pgvn_svn_find(&protocol,"svn://localhost/test/hidden",FALSE,pool,pool),"native writable routing");
    require(protocol.auth_access==WRITE_ACCESS && protocol.anon_access==NO_ACCESS,"authenticated none overridden only for native bypass");
    context.catalog_version=6;
    denied(pgvn_policy_acquire(&policy,&context,id,0,pool),SVN_ERR_AUTHZ_INVALID_CONFIG,"old validator rejects the new field");
    context.catalog_version=7;
    success(pgvn_policy_acquire(&policy,&context,id,0,pool),"new policy snapshot");
    sql(context.control,"DELETE FROM pgvn.configurations WHERE generation=2");
    denied(pgvn_policy_publish_settings(&context,id,1,"invalid authz",NULL,"{}","{\"svn_authz_mode\":\"bypass\"}","invalid",pool),SVN_ERR_AUTHZ_INVALID_CONFIG,"bypass still validates stored authz");
    success(pgvn_policy_publish_settings(&context,id,1,"[/]\n* =\n",NULL,"{}","{\"svn_authz_mode\":\"enforce\"}","component",pool),"mode publish increments generation");
    denied(pgvn_policy_publish_settings(&context,id,1,"[/]\n* = rw\n",NULL,"{}","{}","stale",pool),SVN_ERR_FS_TXN_OUT_OF_DATE,"stale publisher rejected");
    denied(pgvn_svn_command(&protocol,"alice",pool),SVN_ERR_AUTHZ_UNWRITABLE,"mode switch invalidates old command snapshot");
    success(pgvn_policy_acquire(&policy,&context,id,0,pool),"reload enforce");
    require(!policy->svn_authz_bypass && policy->generation==2,"enforce restored at new generation");
    require(PQstatus(context.control)==CONNECTION_OK,"database remains available");
    pgvn_metrics metrics; pgvn_metrics_snapshot(&metrics,&context);
    require(metrics.sql_calls>0 && metrics.sql_errors==0 && metrics.connections==1,"production atomic SQL metrics snapshot");
    PQfinish(context.control); apr_pool_destroy(pool); apr_terminate();
    const svn_version_t *version=svn_subr_version();
    printf("{\"status\":\"PASS\",\"checks\":%u,\"wire_protocol_exercised\":false,"
           "\"apr_runtime\":\"%s\",\"apu_runtime\":\"%s\",\"libpq_version\":%d,\"svn_runtime\":\"%d.%d.%d\"}\n",
           checks,apr_version_string(),apu_version_string(),PQlibVersion(),version->major,version->minor,version->patch);
    return 0;
}
