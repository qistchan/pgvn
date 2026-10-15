#include "listener.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <apr_strings.h>
#include <apr_general.h>
#include <apr_poll.h>
#include "svn_pools.h"
#include "svn_delta.h"
#include "svn_dirent_uri.h"
#include "pgvn_protocol.h"
#include "logger.h"
#include "telemetry.h"

#define PGVN_SVN_CONNECTIONS 16
typedef struct pgvn_client_slot {
    struct pgvn_listener *owner;
    HANDLE thread;
    apr_socket_t *socket;
    apr_pool_t *pool;
} pgvn_client_slot;

struct pgvn_listener {
    pgvn_context *context;
    apr_socket_t *socket;
    HANDLE thread;
    HANDLE stop;
    HANDLE io_cancel;
    CRITICAL_SECTION mutex;
    pgvn_client_slot clients[PGVN_SVN_CONNECTIONS];
    serve_params_t parameters;
};

static DWORD WINAPI client_main(void *baton)
{
    pgvn_client_slot *slot=baton;
    pgvn_listener *listener=slot->owner;
    apr_pool_t *pool=slot->pool;
    svn_ra_svn_conn_t *connection=svn_ra_svn_create_conn5(slot->socket,NULL,NULL,
        listener->parameters.compression_level,0,4096,listener->parameters.max_request_size,listener->parameters.max_response_size,pool);
    svn_error_t *error=serve(connection,&listener->parameters,pool);
    if(error) {
        /* Protocol failures are logged without SQL text, DSN or credentials. */
        fprintf(stderr,"PgVN SVN session ended (code %ld)\n",(long)error->apr_err);
        svn_error_clear(error);
    }
    EnterCriticalSection(&listener->mutex);
    slot->socket=NULL;
    LeaveCriticalSection(&listener->mutex);
    svn_pool_destroy(pool);
    pgvn_telemetry_session(-1);
    return 0;
}

static DWORD WINAPI accept_main(void *baton)
{
    pgvn_listener *listener=baton;
    apr_pollfd_t descriptor={0};
    descriptor.desc_type=APR_POLL_SOCKET; descriptor.desc.s=listener->socket; descriptor.reqevents=APR_POLLIN;
    while(WaitForSingleObject(listener->stop,0)==WAIT_TIMEOUT) {
        apr_int32_t ready=0;
        apr_status_t status=apr_poll(&descriptor,1,&ready,APR_USEC_PER_SEC);
        if(APR_STATUS_IS_TIMEUP(status) || APR_STATUS_IS_EINTR(status)) continue;
        if(status || !(descriptor.rtnevents&APR_POLLIN)) { SetEvent(listener->stop); break; }
        apr_pool_t *pool=svn_pool_create(NULL);
        apr_socket_t *socket=NULL;
        status=apr_socket_accept(&socket,listener->socket,pool);
        if(status) {
            svn_pool_destroy(pool);
            if(APR_STATUS_IS_TIMEUP(status) || APR_STATUS_IS_EAGAIN(status) || APR_STATUS_IS_EINTR(status)
                || APR_STATUS_IS_ECONNABORTED(status) || APR_STATUS_IS_ECONNRESET(status)) continue;
            SetEvent(listener->stop); break;
        }
        /* Client callbacks retain nonblocking I/O and implement their own
         * bounded waits, including cancellation after the drain deadline. */
        status=apr_socket_timeout_set(socket,0);
        if(status) { svn_pool_destroy(pool); continue; }
        apr_socket_opt_set(socket,APR_SO_KEEPALIVE,1);
        EnterCriticalSection(&listener->mutex);
        pgvn_client_slot *available=NULL;
        for(int i=0;i<PGVN_SVN_CONNECTIONS;++i) {
            pgvn_client_slot *slot=&listener->clients[i];
            if(slot->thread && WaitForSingleObject(slot->thread,0)==WAIT_OBJECT_0) {
                CloseHandle(slot->thread); slot->thread=NULL;
            }
            if(!slot->thread && !available) available=slot;
        }
        if(available && WaitForSingleObject(listener->stop,0)==WAIT_TIMEOUT) {
            available->owner=listener; available->socket=socket; available->pool=pool;
            pgvn_telemetry_session(1);
            available->thread=CreateThread(NULL,0,client_main,available,0,NULL);
            if(available->thread) pool=NULL;
            else { available->socket=NULL; pgvn_telemetry_session(-1); }
        }
        LeaveCriticalSection(&listener->mutex);
        if(pool) { pgvn_telemetry_session(0); svn_pool_destroy(pool); }
    }
    return 0;
}

static svn_error_t *byte_limit(apr_uint64_t *output,svn_config_t *config,const char *option)
{
    const char *text; svn_config_get(config,&text,"pgvn",option,"67108864");
    if(!text || !*text) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN byte limits require a nonnegative integer");
    apr_uint64_t value=0,maximum=~(apr_uint64_t)0;
    for(const unsigned char *p=(const unsigned char *)text;*p;++p) {
        if(*p<'0' || *p>'9' || value>(maximum-(*p-'0'))/10)
            return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN byte limit is invalid or overflows uint64");
        value=value*10+(*p-'0');
    }
    *output=value; return SVN_NO_ERROR;
}

svn_error_t *pgvn_listener_start(pgvn_listener **output,const char *configuration,HANDLE stop_event,apr_pool_t *pool)
{
    *output=NULL;
    pgvn_listener *listener=apr_pcalloc(pool,sizeof(*listener));
    listener->stop=stop_event;
    svn_config_t *config;
    svn_error_t *error=svn_config_read3(&config,configuration,TRUE,TRUE,TRUE,pool);
    if(error) { svn_error_clear(error); return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid PgVN service configuration"); }
    const char *dsn,*key_path,*previous_key_path,*host,*domain,*port_text;
    svn_config_get(config,&dsn,"pgvn","connection",NULL);
    svn_config_get(config,&key_path,"pgvn","credential-key-file",NULL);
    svn_config_get(config,&previous_key_path,"pgvn","credential-previous-key-file",NULL);
    svn_config_get(config,&host,"pgvn","listen-host","127.0.0.1");
    svn_config_get(config,&port_text,"pgvn","listen-port","3690");
    svn_config_get(config,&domain,"pgvn","identity-domain","pgvn");
    char *end=NULL; long port=strtol(port_text,&end,10);
    if(!*port_text || !end || *end || port<1024 || port>65535 || !*host)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN listener address or port is invalid");
    SVN_ERR(pgvn_credential_identity(domain,"startup-check"));
    SVN_ERR(byte_limit(&listener->parameters.max_request_size,config,"max-request-bytes"));
    SVN_ERR(byte_limit(&listener->parameters.max_response_size,config,"max-response-bytes"));
    pgvn_context_options options={dsn,3,5000};
    SVN_ERR(pgvn_context_create(&listener->context,&options,pool));
    PGresult *result;
    SVN_ERR(pgvn_control_query(&result,listener->context,
        apr_psprintf(pool,"SELECT max(version)=%d FROM pgvn.schema_versions",PGVN_CATALOG_VERSION),0,NULL));
    svn_boolean_t current=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
    if(!current) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"PgVN SVN service requires the current catalog schema; run db migrate");
    listener->context->minimum_catalog_version=PGVN_CATALOG_VERSION;
    svn_config_get(config,&listener->context->hook_root,"pgvn","hook-root",NULL);
    SVN_ERR(svn_dirent_get_absolute(&listener->context->configuration_path,configuration,pool));
    listener->context->cancellation_event=stop_event;
    SVN_ERR(pgvn_keyring_load(&listener->parameters.pgvn_key,key_path,previous_key_path,pool));
    SVN_ERR(svn_config_get_bool(config,&listener->parameters.read_only,"pgvn","read-only",FALSE));
    listener->parameters.pgvn_context=listener->context;
    listener->parameters.pgvn_domain=apr_pstrdup(pool,domain);
    listener->parameters.root="/";
    listener->parameters.username_case=CASE_ASIS;
    listener->parameters.compression_level=SVN_DELTA_COMPRESSION_LEVEL_DEFAULT;
    listener->parameters.error_check_interval=4096;
    apr_sockaddr_t *address;
    apr_status_t status=apr_sockaddr_info_get(&address,host,APR_UNSPEC,(apr_port_t)port,0,pool);
    if(!status) status=apr_socket_create(&listener->socket,address->family,SOCK_STREAM,APR_PROTO_TCP,pool);
    if(!status) status=apr_socket_opt_set(listener->socket,APR_SO_REUSEADDR,0);
    /* SO_RCVTIMEO does not bound Win32 accept(). Poll a nonblocking listener. */
    if(!status) status=apr_socket_timeout_set(listener->socket,0);
    if(!status) status=apr_socket_bind(listener->socket,address);
    if(!status) status=apr_socket_listen(listener->socket,32);
    if(status) return svn_error_wrap_apr(status,"PgVN SVN listener could not bind");
    listener->io_cancel=CreateEventW(NULL,TRUE,FALSE,NULL);
    if(!listener->io_cancel) return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"PgVN I/O cancellation event creation failed");
    pgvn_svn_set_io_cancel(listener->io_cancel);
    InitializeCriticalSection(&listener->mutex);
    listener->thread=CreateThread(NULL,0,accept_main,listener,0,NULL);
    if(!listener->thread) { DeleteCriticalSection(&listener->mutex); pgvn_svn_set_io_cancel(NULL); CloseHandle(listener->io_cancel);
        return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"PgVN listener thread creation failed"); }
    *output=listener; return SVN_NO_ERROR;
}

void pgvn_listener_stop(pgvn_listener *listener,void (*progress)(void))
{
    if(!listener) return;
    SetEvent(listener->stop);
    while(WaitForSingleObject(listener->thread,1000)==WAIT_TIMEOUT) if(progress) progress();
    CloseHandle(listener->thread);
    apr_socket_close(listener->socket);
    /* Stop accepting, allow ten seconds for active commands, then cancel I/O.
     * No forced thread termination: SQL transactions retain normal cleanup. */
    HANDLE threads[PGVN_SVN_CONNECTIONS]; DWORD count=0;
    for(int i=0;i<PGVN_SVN_CONNECTIONS;++i) if(listener->clients[i].thread) threads[count++]=listener->clients[i].thread;
    ULONGLONG deadline=GetTickCount64()+10000;
    DWORD state=WAIT_OBJECT_0;
    while(count && (state=WaitForMultipleObjects(count,threads,TRUE,1000))==WAIT_TIMEOUT) {
        if(progress) progress();
        if(GetTickCount64()>=deadline) break;
    }
    if(count && state==WAIT_TIMEOUT) {
        SetEvent(listener->io_cancel);
        EnterCriticalSection(&listener->mutex);
        for(int i=0;i<PGVN_SVN_CONNECTIONS;++i)
            if(listener->clients[i].socket) apr_socket_shutdown(listener->clients[i].socket,APR_SHUTDOWN_READWRITE);
        LeaveCriticalSection(&listener->mutex);
        while(WaitForMultipleObjects(count,threads,TRUE,1000)==WAIT_TIMEOUT) if(progress) progress();
    }
    for(DWORD i=0;i<count;++i) CloseHandle(threads[i]);
    pgvn_svn_set_io_cancel(NULL); CloseHandle(listener->io_cancel);
    DeleteCriticalSection(&listener->mutex);
}
