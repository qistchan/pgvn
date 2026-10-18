#ifndef _WIN32
#include "listener.h"
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <apr_general.h>
#include <apr_network_io.h>
#include <apr_poll.h>
#include <apr_strings.h>
#include "credentials.h"
#include "internal.h"
#include "logger.h"
#include "../platform/credentials.h"
#include "pgvn_protocol.h"
#include "svn_delta.h"
#include "svn_dirent_uri.h"
#include "svn_io.h"
#include "svn_pools.h"
#include "svn_utf.h"
#include "private/svn_utf_private.h"
#include "telemetry.h"

#define PGVN_SVN_MAX_CONNECTIONS 64
#define PGVN_SHUTDOWN_GRACE_MS 10000

typedef struct pgvn_client_slot {
    struct pgvn_listener *owner;
    pthread_t thread;
    apr_socket_t *socket;
    apr_pool_t *pool;
    int in_use;
    int done;
} pgvn_client_slot;

struct pgvn_listener {
    pgvn_context *context;
    apr_socket_t *socket;
    pthread_t accept_thread;
    pgvn_cancel_event stop_event;
    pgvn_cancel_state io_cancel;
    pthread_mutex_t mutex;
    pthread_cond_t changed;
    unsigned connection_limit;
    int stopping;
    int accept_failed;
    int accept_started;
    int mutex_ready;
    int cond_ready;
    pgvn_client_slot clients[PGVN_SVN_MAX_CONNECTIONS];
    serve_params_t parameters;
};

static int listener_stopping(pgvn_listener *listener)
{
    int stopping;
    pthread_mutex_lock(&listener->mutex);
    stopping=listener->stopping || pgvn_cancel_is_set(listener->stop_event);
    pthread_mutex_unlock(&listener->mutex);
    return stopping;
}

static void mark_accept_failure(pgvn_listener *listener)
{
    pthread_mutex_lock(&listener->mutex);
    listener->accept_failed=1;
    listener->stopping=1;
    pgvn_cancel_signal(listener->stop_event);
    pthread_cond_broadcast(&listener->changed);
    pthread_mutex_unlock(&listener->mutex);
}

static void *client_main(void *baton)
{
    pgvn_client_slot *slot=baton;
    pgvn_listener *listener=slot->owner;
    apr_pool_t *pool=slot->pool;
    svn_ra_svn_conn_t *connection=svn_ra_svn_create_conn5(slot->socket,NULL,NULL,
        listener->parameters.compression_level,0,4096,listener->parameters.max_request_size,
        listener->parameters.max_response_size,pool);
    svn_error_t *error=serve(connection,&listener->parameters,pool);
    if(error) {
        fprintf(stderr,"PgVN SVN session ended (code %ld)\n",(long)error->apr_err);
        svn_error_clear(error);
    }
    pthread_mutex_lock(&listener->mutex);
    slot->socket=NULL;
    pthread_mutex_unlock(&listener->mutex);
    svn_pool_destroy(pool);
    pgvn_telemetry_session(-1);
    pthread_mutex_lock(&listener->mutex);
    slot->done=1;
    pthread_cond_broadcast(&listener->changed);
    pthread_mutex_unlock(&listener->mutex);
    return NULL;
}

static void reap_completed(pgvn_listener *listener)
{
    for(unsigned i=0;i<listener->connection_limit;++i) {
        pthread_t thread;
        int join=0;
        pthread_mutex_lock(&listener->mutex);
        pgvn_client_slot *slot=&listener->clients[i];
        if(slot->in_use && slot->done) {
            thread=slot->thread;
            join=1;
        }
        pthread_mutex_unlock(&listener->mutex);
        if(!join) continue;
        pthread_join(thread,NULL);
        pthread_mutex_lock(&listener->mutex);
        slot->in_use=0;
        slot->done=0;
        slot->pool=NULL;
        pthread_mutex_unlock(&listener->mutex);
    }
}

static void reject_socket(apr_socket_t *socket,apr_pool_t *pool)
{
    if(socket) apr_socket_close(socket);
    if(pool) svn_pool_destroy(pool);
    pgvn_telemetry_session(0);
}

static void *accept_main(void *baton)
{
    pgvn_listener *listener=baton;
    apr_pollfd_t descriptor={0};
    descriptor.desc_type=APR_POLL_SOCKET;
    descriptor.desc.s=listener->socket;
    descriptor.reqevents=APR_POLLIN;
    while(!listener_stopping(listener)) {
        apr_int32_t ready=0;
        apr_status_t status=apr_poll(&descriptor,1,&ready,100000);
        if(APR_STATUS_IS_TIMEUP(status) || APR_STATUS_IS_EINTR(status)) {
            reap_completed(listener);
            continue;
        }
        if(status || !(descriptor.rtnevents&APR_POLLIN)) {
            if(!listener_stopping(listener)) mark_accept_failure(listener);
            break;
        }
        apr_pool_t *pool=svn_pool_create(NULL);
        apr_socket_t *socket=NULL;
        status=apr_socket_accept(&socket,listener->socket,pool);
        if(status) {
            svn_pool_destroy(pool);
            if(APR_STATUS_IS_EAGAIN(status) || APR_STATUS_IS_TIMEUP(status) || APR_STATUS_IS_EINTR(status)
                || APR_STATUS_IS_ECONNABORTED(status) || APR_STATUS_IS_ECONNRESET(status)) continue;
            if(!listener_stopping(listener)) mark_accept_failure(listener);
            break;
        }
        status=apr_socket_timeout_set(socket,0);
        if(status) {
            reject_socket(socket,pool);
            continue;
        }
        (void)apr_socket_opt_set(socket,APR_SO_KEEPALIVE,1);
        reap_completed(listener);
        pthread_mutex_lock(&listener->mutex);
        pgvn_client_slot *available=NULL;
        if(!listener->stopping && !pgvn_cancel_is_set(listener->stop_event)) {
            for(unsigned i=0;i<listener->connection_limit;++i)
                if(!listener->clients[i].in_use) { available=&listener->clients[i]; break; }
        }
        if(available) {
            available->owner=listener;
            available->socket=socket;
            available->pool=pool;
            available->done=0;
            available->in_use=1;
            pgvn_telemetry_session(1);
            int error=pthread_create(&available->thread,NULL,client_main,available);
            if(error) {
                available->socket=NULL;
                available->pool=NULL;
                available->in_use=0;
                pgvn_telemetry_session(-1);
                pthread_mutex_unlock(&listener->mutex);
                reject_socket(socket,pool);
                continue;
            }
            pthread_mutex_unlock(&listener->mutex);
        } else {
            pthread_mutex_unlock(&listener->mutex);
            reject_socket(socket,pool);
        }
    }
    reap_completed(listener);
    return NULL;
}

static svn_error_t *connection_limit(unsigned *output,svn_config_t *config)
{
    const char *text;
    svn_config_get(config,&text,"pgvn","max-connections","64");
    unsigned value=0;
    if(!text || !*text) goto invalid;
    for(const unsigned char *p=(const unsigned char *)text;*p;++p) {
        if(*p<'0' || *p>'9' || value>((unsigned)PGVN_SVN_MAX_CONNECTIONS-(unsigned)(*p-'0'))/10) goto invalid;
        value=value*10+(unsigned)(*p-'0');
    }
    if(!value) goto invalid;
    *output=value;
    return SVN_NO_ERROR;
invalid:
    return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,
        "PgVN SVN max-connections must be an integer between 1 and 64");
}

static svn_error_t *byte_limit(apr_uint64_t *output,svn_config_t *config,const char *option)
{
    const char *text;
    svn_config_get(config,&text,"pgvn",option,"67108864");
    if(!text || !*text)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN byte limits require a nonnegative integer");
    apr_uint64_t value=0,maximum=~(apr_uint64_t)0;
    for(const unsigned char *p=(const unsigned char *)text;*p;++p) {
        if(*p<'0' || *p>'9' || value>(maximum-(*p-'0'))/10)
            return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN byte limit is invalid or overflows uint64");
        value=value*10+(*p-'0');
    }
    *output=value;
    return SVN_NO_ERROR;
}

static svn_error_t *listener_error(apr_status_t status)
{
    if(APR_TO_OS_ERROR(status)==EADDRINUSE)
        return svn_error_create(SVN_ERR_FS_ALREADY_EXISTS,NULL,"PgVN SVN listen address is already in use");
    return svn_error_wrap_apr(status,"PgVN SVN listener could not bind");
}

static void listener_partial_destroy(pgvn_listener *listener)
{
    if(listener->cond_ready) pthread_cond_destroy(&listener->changed);
    if(listener->mutex_ready) pthread_mutex_destroy(&listener->mutex);
}

svn_error_t *pgvn_listener_start(pgvn_listener **output,const char *configuration,
    pgvn_cancel_event stop_event,apr_pool_t *pool)
{
    *output=NULL;
    if(!configuration || !pgvn_utf8_valid(configuration))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid UTF-8 PgVN configuration path");
    pgvn_listener *listener=apr_pcalloc(pool,sizeof(*listener));
    listener->stop_event=stop_event;
    if(pthread_mutex_init(&listener->mutex,NULL))
        return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"PgVN listener mutex initialization failed");
    listener->mutex_ready=1;
    pthread_condattr_t attributes;
    if(pthread_condattr_init(&attributes)) {
        listener_partial_destroy(listener);
        return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"PgVN listener condition initialization failed");
    }
    int clock_status=pthread_condattr_setclock(&attributes,CLOCK_MONOTONIC);
    int cond_status=clock_status ? clock_status : pthread_cond_init(&listener->changed,&attributes);
    pthread_condattr_destroy(&attributes);
    if(cond_status) {
        listener_partial_destroy(listener);
        return svn_error_create(cond_status,NULL,"PgVN monotonic listener condition initialization failed");
    }
    listener->cond_ready=1;
    svn_stringbuf_t *contents;
    svn_error_t *read_error=svn_stringbuf_from_file2(&contents,configuration,pool);
    if(read_error) {
        svn_error_clear(read_error);
        listener_partial_destroy(listener);
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN configuration file is unavailable");
    }
    if(memchr(contents->data,0,contents->len) || !svn_utf__is_valid(contents->data,contents->len)) {
        listener_partial_destroy(listener);
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN configuration file must contain valid UTF-8");
    }
    svn_config_t *config;
    svn_error_t *error=svn_config_read3(&config,configuration,TRUE,TRUE,TRUE,pool);
    if(error) {
        svn_error_clear(error);
        listener_partial_destroy(listener);
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid PgVN service configuration");
    }
    const char *dsn,*key_path,*previous_key_path,*host,*domain,*port_text;
    svn_config_get(config,&dsn,"pgvn","connection",NULL);
    svn_config_get(config,&key_path,"pgvn","credential-key-file",NULL);
    svn_config_get(config,&previous_key_path,"pgvn","credential-previous-key-file",NULL);
    svn_config_get(config,&host,"pgvn","listen-host","127.0.0.1");
    svn_config_get(config,&port_text,"pgvn","listen-port","3690");
    svn_config_get(config,&domain,"pgvn","identity-domain","pgvn");
    char *end=NULL;
    long port=port_text ? strtol(port_text,&end,10) : 0;
    int numeric_port=port_text && *port_text;
    for(const unsigned char *p=(const unsigned char *)port_text;numeric_port && *p;++p)
        numeric_port=*p>='0' && *p<='9';
    if(!dsn || !*dsn || !pgvn_utf8_valid(dsn) || !key_path || !*key_path
        || !host || !*host || !pgvn_utf8_valid(host)
        || !port_text || !numeric_port || !end || *end || port<1024 || port>65535) {
        listener_partial_destroy(listener);
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"PgVN listener configuration is invalid");
    }
    error=pgvn_credential_identity(domain,"startup-check");
    if(error) { listener_partial_destroy(listener); return error; }
    error=connection_limit(&listener->connection_limit,config);
    if(error) { listener_partial_destroy(listener); return error; }
    error=byte_limit(&listener->parameters.max_request_size,config,"max-request-bytes");
    if(!error) error=byte_limit(&listener->parameters.max_response_size,config,"max-response-bytes");
    if(error) { listener_partial_destroy(listener); return error; }
    pgvn_context_options options={dsn,3,5000};
    error=pgvn_context_create(&listener->context,&options,pool);
    if(error) { listener_partial_destroy(listener); return error; }
    PGresult *result;
    error=pgvn_control_query(&result,listener->context,
        apr_psprintf(pool,"SELECT max(version)=%d FROM pgvn.schema_versions",PGVN_CATALOG_VERSION),0,NULL);
    if(error) { listener_partial_destroy(listener); return error; }
    svn_boolean_t current=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t");
    PQclear(result);
    if(!current) {
        listener_partial_destroy(listener);
        return svn_error_create(PGVN_ERR_SCHEMA,NULL,"PgVN SVN service requires the current catalog schema");
    }
    listener->context->minimum_catalog_version=PGVN_CATALOG_VERSION;
    svn_config_get(config,&listener->context->hook_root,"pgvn","hook-root",NULL);
    error=pgvn_hooks_configure(listener->context,config,pool);
    if(!error) error=svn_dirent_get_absolute(&listener->context->configuration_path,configuration,pool);
    if(!error) listener->context->cancellation_event=stop_event;
    if(!error) error=pgvn_keyring_load(&listener->parameters.pgvn_key,key_path,previous_key_path,pool);
    if(!error) error=svn_config_get_bool(config,&listener->parameters.read_only,"pgvn","read-only",FALSE);
    if(error) { listener_partial_destroy(listener); return error; }
    listener->parameters.pgvn_context=listener->context;
    listener->parameters.pgvn_domain=apr_pstrdup(pool,domain);
    listener->parameters.root="/";
    listener->parameters.username_case=CASE_ASIS;
    listener->parameters.compression_level=SVN_DELTA_COMPRESSION_LEVEL_DEFAULT;
    listener->parameters.error_check_interval=4096;
    apr_sockaddr_t *address;
    apr_status_t status=apr_sockaddr_info_get(&address,host,APR_UNSPEC,(apr_port_t)port,0,pool);
    if(!status) status=apr_socket_create(&listener->socket,address->family,SOCK_STREAM,APR_PROTO_TCP,pool);
    if(!status) status=apr_socket_opt_set(listener->socket,APR_SO_REUSEADDR,1);
    if(!status) status=apr_socket_timeout_set(listener->socket,0);
    if(!status) status=apr_socket_bind(listener->socket,address);
    if(!status) status=apr_socket_listen(listener->socket,PGVN_SVN_MAX_CONNECTIONS);
    if(status) {
        listener_partial_destroy(listener);
        return listener_error(status);
    }
    error=NULL;
    listener->io_cancel.signalled=0;
    pgvn_svn_set_io_cancel(&listener->io_cancel);
    int thread_status=pthread_create(&listener->accept_thread,NULL,accept_main,listener);
    if(thread_status) {
        pgvn_svn_set_io_cancel(NULL);
        apr_socket_close(listener->socket);
        listener_partial_destroy(listener);
        return svn_error_create(thread_status,NULL,"PgVN listener thread creation failed");
    }
    listener->accept_started=1;
    pgvn_telemetry_session_limit(listener->connection_limit);
    *output=listener;
    return error;
}

static int monotonic_now(struct timespec *value)
{
    return clock_gettime(CLOCK_MONOTONIC,value);
}

static void cancel_active(pgvn_listener *listener)
{
    pgvn_cancel_signal(&listener->io_cancel);
    pthread_mutex_lock(&listener->mutex);
    for(unsigned i=0;i<listener->connection_limit;++i)
        if(listener->clients[i].socket) (void)apr_socket_shutdown(listener->clients[i].socket,APR_SHUTDOWN_READWRITE);
    pthread_cond_broadcast(&listener->changed);
    pthread_mutex_unlock(&listener->mutex);
}

void pgvn_listener_stop(pgvn_listener *listener)
{
    if(!listener) return;
    pthread_mutex_lock(&listener->mutex);
    listener->stopping=1;
    pgvn_cancel_signal(listener->stop_event);
    pthread_cond_broadcast(&listener->changed);
    pthread_mutex_unlock(&listener->mutex);
    if(listener->accept_started) pthread_join(listener->accept_thread,NULL);
    if(listener->socket) {
        apr_socket_close(listener->socket);
        listener->socket=NULL;
    }
    struct timespec now;
    int failed_clock=monotonic_now(&now)!=0;
    uint64_t deadline=failed_clock ? 0 : (uint64_t)now.tv_sec*1000+(uint64_t)now.tv_nsec/1000000+PGVN_SHUTDOWN_GRACE_MS;
    int active=1;
    while(active && !failed_clock) {
        pthread_mutex_lock(&listener->mutex);
        active=0;
        for(unsigned i=0;i<listener->connection_limit;++i)
            if(listener->clients[i].in_use && !listener->clients[i].done) active=1;
        if(!active) { pthread_mutex_unlock(&listener->mutex); break; }
        if(monotonic_now(&now)) { pthread_mutex_unlock(&listener->mutex); failed_clock=1; break; }
        uint64_t current=(uint64_t)now.tv_sec*1000+(uint64_t)now.tv_nsec/1000000;
        if(current>=deadline) { pthread_mutex_unlock(&listener->mutex); break; }
        uint64_t wake=current+250;
        if(wake>deadline) wake=deadline;
        struct timespec absolute={(time_t)(wake/1000),(long)(wake%1000)*1000000};
        int wait_status=pthread_cond_timedwait(&listener->changed,&listener->mutex,&absolute);
        pthread_mutex_unlock(&listener->mutex);
        if(wait_status && wait_status!=ETIMEDOUT) { failed_clock=1; break; }
    }
    if(active) cancel_active(listener);
    for(unsigned i=0;i<listener->connection_limit;++i) {
        pthread_t thread;
        int join=0;
        pthread_mutex_lock(&listener->mutex);
        pgvn_client_slot *slot=&listener->clients[i];
        if(slot->in_use) { thread=slot->thread; join=1; }
        pthread_mutex_unlock(&listener->mutex);
        if(join) pthread_join(thread,NULL);
    }
    pgvn_telemetry_session_limit(0);
    pgvn_svn_set_io_cancel(NULL);
    listener_partial_destroy(listener);
}

int pgvn_listener_failed(pgvn_listener *listener)
{
    if(!listener) return 0;
    pthread_mutex_lock(&listener->mutex);
    int failed=listener->accept_failed;
    pthread_mutex_unlock(&listener->mutex);
    return failed;
}
#endif
