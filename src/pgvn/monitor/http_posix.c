#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/queue.h>
#include <event2/event.h>
#include <event2/bufferevent.h>
#include <event2/buffer.h>
#include <event2/http.h>
#include <event2/listener.h>
#include <event2/keyvalq_struct.h>
#include <apr_strings.h>
#include "svn_pools.h"
#include "http_posix.h"
#include "pgvn_web.h"
#include "telemetry.h"
struct pgvn_monitor_connection {
    pgvn_monitor *owner;
    struct bufferevent *buffer;
    struct event *deadline;
    pgvn_monitor_connection *next;
    apr_time_t started;
    uint64_t sequence;
    struct evhttp_request *pending;
    int status,sent,counted,closed;
    char request_id[32];
};
static void connection_free(void *baton)
{
    pgvn_monitor_connection *state=baton;
    pgvn_monitor *monitor=state->owner;
    if(state->started) pgvn_telemetry_http_finish(state->started,state->status,state->sent,state->request_id);
    if(state->counted) --monitor->responses;
    pgvn_monitor_connection **link=&monitor->connections;
    while(*link && *link!=state) link=&(*link)->next;
    if(*link) *link=state->next;
    --monitor->active;
    if(monitor->bound && !pgvn_cancel_is_set(monitor->stop) && monitor->active<PGVN_MONITOR_QUEUE_LIMIT)
        evconnlistener_enable(evhttp_bound_socket_get_listener(monitor->bound));
    bufferevent_decref(state->buffer);
    if(state->deadline) event_free(state->deadline);
    free(state);
}
static void connection_timeout(evutil_socket_t fd,short events,void *baton)
{
    (void)fd; (void)events;
    pgvn_monitor_connection *state=baton;
    if(state->closed) { connection_free(state); return; }
    int socket=bufferevent_getfd(state->buffer);
    if(socket>=0) shutdown(socket,SHUT_RDWR);
    /* Before parsing a request no HTTP close callback is installed. Our own
     * reference keeps this raw socket bufferevent and its FD valid until here. */
    if(!state->started) connection_free(state);
}
static struct bufferevent *connection_create(struct event_base *base,void *baton)
{
    pgvn_monitor *monitor=baton;
    pgvn_monitor_connection *state=calloc(1,sizeof(*state)); if(!state) return NULL;
    struct bufferevent *socket=bufferevent_socket_new(base,-1,BEV_OPT_CLOSE_ON_FREE);
    if(!socket) { free(state); return NULL; }
    state->owner=monitor; state->buffer=socket;
    /* HTTP requires a socket bufferevent. Retain one public reference so a
     * parser failure cannot leave the header-deadline timer with a stale FD. */
    bufferevent_incref(socket);
    state->next=monitor->connections; monitor->connections=state; ++monitor->active;
    if(monitor->bound && monitor->active>=PGVN_MONITOR_QUEUE_LIMIT)
        evconnlistener_disable(evhttp_bound_socket_get_listener(monitor->bound));
    state->deadline=evtimer_new(base,connection_timeout,state);
    if(!state->deadline) { connection_free(state); bufferevent_free(socket); return NULL; }
    const struct timeval timeout={5,0}; evtimer_add(state->deadline,&timeout);
    return socket;
}
static pgvn_monitor_connection *connection_state(pgvn_monitor *monitor,struct evhttp_request *request)
{
    struct bufferevent *buffer=evhttp_connection_get_bufferevent(evhttp_request_get_connection(request));
    for(pgvn_monitor_connection *state=monitor->connections;state;state=state->next)
        if(state->buffer==buffer) return state;
    return NULL;
}
static void connection_closed(struct evhttp_connection *connection,void *baton)
{
    (void)connection;
    pgvn_monitor_connection *state=baton;
    state->pending=NULL; state->closed=1;
    /* Keep our socket reference through the rest of evhttp_connection_free,
     * including its final shutdown(fd), so another service thread cannot reuse
     * that descriptor before libevent finishes. Release on the next dispatch. */
    evtimer_del(state->deadline); event_active(state->deadline,EV_TIMEOUT,0);
}
static void completed(struct evhttp_request *request,void *baton)
{
    (void)request; ((pgvn_monitor_connection *)baton)->sent=1;
}
static void respond(struct evhttp_request *request,pgvn_monitor_connection *state,int status,const char *type,const char *body)
{
    state->status=status;
    struct evkeyvalq *headers=evhttp_request_get_output_headers(request);
    evhttp_add_header(headers,"Content-Type",type);
    evhttp_add_header(headers,"Cache-Control","no-store");
    evhttp_add_header(headers,"Connection","close");
    evhttp_add_header(headers,"X-Content-Type-Options","nosniff");
    evhttp_add_header(headers,"Content-Security-Policy","default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'");
    evhttp_add_header(headers,"Referrer-Policy","no-referrer");
    evhttp_add_header(headers,"X-Frame-Options","DENY");
    evhttp_add_header(headers,"X-Request-ID",state->request_id);
    if(status==405) evhttp_add_header(headers,"Allow","GET");
    if(status==401) evhttp_add_header(headers,"WWW-Authenticate","Bearer realm=\"PgVN monitor\"");
    struct evbuffer *buffer=evbuffer_new();
    if(!buffer) { evhttp_send_error(request,503,"Unavailable"); return; }
    evbuffer_add(buffer,body,strlen(body));
    evhttp_request_set_on_complete_cb(request,completed,state);
    const struct timeval timeout={5,0}; evtimer_add(state->deadline,&timeout);
    evhttp_send_reply(request,status,NULL,buffer); evbuffer_free(buffer);
}
static void error_response(struct evhttp_request *request,pgvn_monitor_connection *state,int status,const char *code,apr_pool_t *pool)
{
    respond(request,state,status,"application/json; charset=utf-8",apr_psprintf(pool,
        "{\"error\":{\"code\":\"%s\",\"request_id\":\"%s\"}}\n",code,state->request_id));
}
static int header_count(const struct evkeyvalq *headers,const char *name)
{
    int count=0; const struct evkeyval *entry;
    TAILQ_FOREACH(entry,headers,next) if(!strcasecmp(entry->key,name)) ++count;
    return count;
}
static void dispatch_request(struct evhttp_request *request,pgvn_monitor *monitor,pgvn_monitor_connection *state)
{
    apr_pool_t *pool=svn_pool_create(NULL);
    ++monitor->responses; state->counted=1;
    const struct evkeyvalq *headers=evhttp_request_get_input_headers(request);
    const char *host=evhttp_find_header(headers,"Host"),*origin=evhttp_find_header(headers,"Origin");
    const char *authorization=evhttp_find_header(headers,"Authorization");
    if(header_count(headers,"Host")!=1 || !host || strcmp(host,monitor->authority) ||
       header_count(headers,"Authorization")>1 || header_count(headers,"Content-Length")>1) {
        error_response(request,state,400,"invalid_headers",pool); goto done;
    }
    if(evhttp_request_get_command(request)!=EVHTTP_REQ_GET) { error_response(request,state,405,"read_only",pool); goto done; }
    const char *length=evhttp_find_header(headers,"Content-Length");
    if(evbuffer_get_length(evhttp_request_get_input_buffer(request)) || evhttp_find_header(headers,"Transfer-Encoding") ||
       (length && strcmp(length,"0"))) { error_response(request,state,413,"request_body_not_supported",pool); goto done; }
    const char *raw=evhttp_request_get_uri(request);
    int ascii=raw && strlen(raw)<=1280;
    if(ascii) for(const unsigned char *p=(const unsigned char *)raw;*p;++p) if(*p<32 || *p>126) ascii=0;
    if(!ascii) {
        error_response(request,state,400,"invalid_url",pool); goto done;
    }
    struct evhttp_uri *uri=evhttp_uri_parse(raw);
    if(!uri) { error_response(request,state,400,"invalid_url",pool); goto done; }
    const char *path=evhttp_uri_get_path(uri),*query=evhttp_uri_get_query(uri);
    if(!query) query="";
    if(!path || *path!='/' || strlen(path)>=256 || strlen(query)>=1024) error_response(request,state,400,"invalid_url",pool);
    else if(!strcmp(path,"/health/live")) {
        int live=pgvn_monitor_live(monitor->data);
        respond(request,state,live ? 200 : 503,"application/json",live ? "{\"live\":true}\n" : "{\"live\":false}\n");
    } else if(!strcmp(path,"/health/ready")) {
        int ready=pgvn_monitor_ready(monitor->data);
        respond(request,state,ready ? 200 : 503,"application/json",ready ? "{\"ready\":true}\n" : "{\"ready\":false}\n");
    } else if(!strcmp(path,"/") || !strcmp(path,"/monitor/")) respond(request,state,200,"text/html; charset=utf-8",pgvn_web_index);
    else if(!strcmp(path,"/monitor/monitor.css")) respond(request,state,200,"text/css; charset=utf-8",pgvn_web_style);
    else if(!strcmp(path,"/monitor/monitor.js")) respond(request,state,200,"text/javascript; charset=utf-8",pgvn_web_script);
    else if(!strncmp(path,"/api/monitor/v1/",16) || !strcmp(path,"/metrics")) {
        if(header_count(headers,"Origin")>1 || (origin && strcmp(origin,monitor->origin))) error_response(request,state,403,"origin_denied",pool);
        else if(!authorization || !pgvn_monitor_authorize(monitor->data,authorization,strlen(authorization))) error_response(request,state,401,"authentication_required",pool);
        else if(!strcmp(path,"/metrics")) {
            if(*query) error_response(request,state,400,"invalid_query",pool);
            else respond(request,state,200,"text/plain; version=0.0.4; charset=utf-8",pgvn_monitor_metrics(monitor->data,pool));
        } else {
            int status; const char *body=pgvn_monitor_api(monitor->data,path,query,&status,pool);
            if(status==200) respond(request,state,200,"application/json; charset=utf-8",body);
            else error_response(request,state,status,body,pool);
        }
    } else error_response(request,state,404,"not_found",pool);
    evhttp_uri_free(uri);
done:
    svn_pool_destroy(pool);
}
static void dispatch_queued(pgvn_monitor *monitor)
{
    while(monitor->responses<PGVN_MONITOR_WORKERS) {
        pgvn_monitor_connection *oldest=NULL;
        for(pgvn_monitor_connection *state=monitor->connections;state;state=state->next)
            if(state->pending && (!oldest || state->sequence<oldest->sequence)) oldest=state;
        if(!oldest) break;
        struct evhttp_request *request=oldest->pending; oldest->pending=NULL;
        dispatch_request(request,monitor,oldest);
    }
}
static void request_handler(struct evhttp_request *request,void *baton)
{
    pgvn_monitor *monitor=baton;
    pgvn_monitor_connection *state=connection_state(monitor,request);
    if(!state) { evhttp_send_error(request,503,"Unavailable"); return; }
    if(state->started) { int fd=bufferevent_getfd(state->buffer); if(fd>=0) shutdown(fd,SHUT_RDWR); return; }
    evhttp_connection_set_closecb(evhttp_request_get_connection(request),connection_closed,state);
    state->started=pgvn_telemetry_http_start(); state->status=503;
    state->sequence=++monitor->sequence;
    snprintf(state->request_id,sizeof(state->request_id),"monitor-%016llx",(unsigned long long)state->sequence);
    /* Libevent owns the live request until reply/connection close. Closecb
     * removes queued state before any later event-loop dispatch can use it. */
    state->pending=request;
    dispatch_queued(monitor);
}
static void stop_tick(evutil_socket_t fd,short events,void *baton)
{
    (void)fd; (void)events; pgvn_monitor *monitor=baton;
    if(pgvn_cancel_is_set(monitor->stop)) { event_base_loopbreak(monitor->base); return; }
    /* bufferevent_free clears callbacks even while our reference keeps the
     * object alive. Reclaim parser-rejected/aborted connections promptly. */
    pgvn_monitor_connection *state=monitor->connections;
    while(state) {
        pgvn_monitor_connection *next=state->next;
        bufferevent_data_cb read_cb=NULL,write_cb=NULL; bufferevent_event_cb event_cb=NULL;
        bufferevent_getcb(state->buffer,&read_cb,&write_cb,&event_cb,NULL);
        if(!read_cb && !write_cb && !event_cb) connection_free(state);
        state=next;
    }
    dispatch_queued(monitor);
}
static PGVN_THREAD_PROC http_main(void *baton)
{
    pgvn_monitor *monitor=baton; event_base_dispatch(monitor->base); return 0;
}
int pgvn_monitor_url_valid(const char *url)
{
    const char *prefix="http://127.0.0.1:";
    if(!url || strlen(url)>64 || strncmp(url,prefix,strlen(prefix))) return 0;
    const char *port=url+strlen(prefix); unsigned value=0;
    if(!*port) return 0;
    for(;*port>='0' && *port<='9';++port) { value=value*10+(unsigned)(*port-'0'); if(value>65535) return 0; }
    return value>=1024 && !strcmp(port,"/");
}
void pgvn_monitor_stop(pgvn_monitor *monitor)
{
    if(!monitor) return;
    pgvn_cancel_signal(monitor->stop);
    if(monitor->thread) { pgvn_thread_close(monitor->thread); monitor->thread=NULL; }
    if(monitor->http) { evhttp_free(monitor->http); monitor->http=NULL; monitor->bound=NULL; }
    while(monitor->connections) connection_free(monitor->connections);
    if(monitor->timer) { event_free(monitor->timer); monitor->timer=NULL; }
    if(monitor->base) { event_base_free(monitor->base); monitor->base=NULL; }
    if(monitor->data) { pgvn_monitor_data_stop(monitor->data); monitor->data=NULL; }
    pgvn_event_close(monitor->stop); monitor->stop=NULL;
}
int pgvn_monitor_start(pgvn_monitor *monitor,const char *url,const char *configuration)
{
    if(!pgvn_monitor_url_valid(url)) return EINVAL;
    memset(monitor,0,sizeof(*monitor));
    size_t length=strlen(url)-1; memcpy(monitor->origin,url,length); monitor->origin[length]=0;
    snprintf(monitor->authority,sizeof(monitor->authority),"%s",monitor->origin+7);
    int status=(int)pgvn_monitor_data_start(&monitor->data,configuration); if(status) goto failed;
    monitor->stop=pgvn_event_create(); monitor->base=event_base_new();
    if(!monitor->stop || !monitor->base) { status=ENOMEM; goto failed; }
    monitor->http=evhttp_new(monitor->base); if(!monitor->http) { status=ENOMEM; goto failed; }
    evhttp_set_bevcb(monitor->http,connection_create,monitor);
    evhttp_set_gencb(monitor->http,request_handler,monitor);
    evhttp_set_max_headers_size(monitor->http,PGVN_MONITOR_HEADER_LIMIT);
    evhttp_set_max_body_size(monitor->http,1);
    evhttp_set_timeout(monitor->http,5);
    evhttp_set_allowed_methods(monitor->http,EVHTTP_REQ_GET|EVHTTP_REQ_POST|EVHTTP_REQ_HEAD|EVHTTP_REQ_PUT|EVHTTP_REQ_DELETE|EVHTTP_REQ_OPTIONS|EVHTTP_REQ_TRACE|EVHTTP_REQ_CONNECT|EVHTTP_REQ_PATCH);
    int port=atoi(strrchr(monitor->authority,':')+1);
    monitor->bound=evhttp_bind_socket_with_handle(monitor->http,"127.0.0.1",(ev_uint16_t)port);
    if(!monitor->bound) { status=errno ? errno : EADDRINUSE; goto failed; }
    if(listen(evhttp_bound_socket_get_fd(monitor->bound),PGVN_MONITOR_QUEUE_LIMIT)) { status=errno; goto failed; }
    monitor->timer=event_new(monitor->base,-1,EV_PERSIST,stop_tick,monitor);
    if(!monitor->timer) { status=ENOMEM; goto failed; }
    const struct timeval delay={0,25000}; event_add(monitor->timer,&delay);
    monitor->thread=pgvn_thread_create(http_main,monitor);
    if(!monitor->thread) { status=errno; goto failed; }
    return 0;
failed:
    pgvn_monitor_stop(monitor); return status;
}
