#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
#include <apr_strings.h>
#include "svn_pools.h"
#include "http_win.h"
#include "pgvn_web.h"
#include "telemetry.h"
static _Thread_local int response_status,response_sent;

static void respond(pgvn_monitor *monitor,HTTP_REQUEST_ID id,USHORT status,
                    const char *type,const char *body,const char *request_id)
{
    response_status=status; response_sent=0;
    HTTP_RESPONSE response={0}; HTTP_DATA_CHUNK chunk={0}; HTTP_UNKNOWN_HEADER headers[7]={0};
    const char *reason=status==200 ? "OK" : status==400 ? "Bad Request" : status==401 ? "Unauthorized" : status==403 ? "Forbidden" :
        status==404 ? "Not Found" : status==405 ? "Method Not Allowed" : status==409 ? "Conflict" : status==413 ? "Payload Too Large" :
        status==431 ? "Request Header Fields Too Large" : "Service Unavailable";
    char length[32]; snprintf(length,sizeof(length),"%zu",strlen(body));
    response.StatusCode=status; response.pReason=reason; response.ReasonLength=(USHORT)strlen(reason);
    response.Headers.KnownHeaders[HttpHeaderContentType].pRawValue=type;
    response.Headers.KnownHeaders[HttpHeaderContentType].RawValueLength=(USHORT)strlen(type);
    response.Headers.KnownHeaders[HttpHeaderCacheControl].pRawValue="no-store";
    response.Headers.KnownHeaders[HttpHeaderCacheControl].RawValueLength=8;
    response.Headers.KnownHeaders[HttpHeaderContentLength].pRawValue=length;
    response.Headers.KnownHeaders[HttpHeaderContentLength].RawValueLength=(USHORT)strlen(length);
    const char *names[]={"X-Content-Type-Options","Content-Security-Policy","Referrer-Policy","X-Frame-Options","X-Request-ID","Allow","WWW-Authenticate"};
    const char *values[]={"nosniff","default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        "no-referrer","DENY",request_id,"GET","Bearer realm=\"PgVN monitor\""};
    for(int i=0;i<7;++i) {
        if((i==5 && status!=405) || (i==6 && status!=401)) continue;
        HTTP_UNKNOWN_HEADER *header=&headers[response.Headers.UnknownHeaderCount++];
        header->pName=names[i]; header->NameLength=(USHORT)strlen(names[i]); header->pRawValue=values[i]; header->RawValueLength=(USHORT)strlen(values[i]);
    }
    response.Headers.pUnknownHeaders=headers;
    chunk.DataChunkType=HttpDataChunkFromMemory; chunk.FromMemory.pBuffer=(void *)body; chunk.FromMemory.BufferLength=(ULONG)strlen(body);
    response.EntityChunkCount=1; response.pEntityChunks=&chunk;
    OVERLAPPED operation={0}; operation.hEvent=CreateEventW(NULL,TRUE,FALSE,NULL);
    if(!operation.hEvent) { (void)HttpCancelHttpRequest(monitor->queue,id,NULL); return; }
    ULONG sent=HttpSendHttpResponse(monitor->queue,id,status>=400 ? HTTP_SEND_RESPONSE_FLAG_DISCONNECT : 0,
        &response,NULL,NULL,NULL,0,&operation,NULL);
    if(sent==ERROR_IO_PENDING) {
        HANDLE events[]={operation.hEvent,monitor->stop};
        DWORD result=WaitForMultipleObjects(2,events,FALSE,5000);
        if(result!=WAIT_OBJECT_0) (void)CancelIoEx(monitor->queue,&operation);
        /* Headers, body and OVERLAPPED remain alive until cancellation or
         * completion is acknowledged. Never abandon or terminate the worker. */
        DWORD transferred=0; response_sent=GetOverlappedResult(monitor->queue,&operation,&transferred,TRUE)!=0;
    }
    if(sent==NO_ERROR) response_sent=1;
    CloseHandle(operation.hEvent);
}
static void error_response(pgvn_monitor *monitor,HTTP_REQUEST_ID id,int status,const char *code,const char *request_id,apr_pool_t *pool)
{
    respond(monitor,id,(USHORT)status,"application/json; charset=utf-8",
        apr_psprintf(pool,"{\"error\":{\"code\":\"%s\",\"request_id\":\"%s\"}}\n",code,request_id),request_id);
}
static int ascii(char *out,size_t capacity,const wchar_t *text,USHORT bytes)
{
    size_t length=bytes/sizeof(wchar_t);
    if(bytes%sizeof(wchar_t) || length>=capacity || (length && !text)) return 0;
    for(size_t i=0;i<length;++i) { if(text[i]<32 || text[i]>126) return 0; out[i]=(char)text[i]; }
    out[length]=0; return 1;
}
static int origin_allowed(const HTTP_REQUEST *request,const char *origin)
{
    int found=0;
    for(USHORT i=0;i<request->Headers.UnknownHeaderCount;++i) {
        HTTP_UNKNOWN_HEADER *header=&request->Headers.pUnknownHeaders[i];
        if(header->NameLength==6 && !_strnicmp(header->pName,"Origin",6)) {
            if(found++ || header->RawValueLength!=strlen(origin) || memcmp(header->pRawValue,origin,header->RawValueLength)) return 0;
        }
    }
    return 1;
}
static DWORD WINAPI worker(void *baton)
{
    pgvn_monitor_worker *state=baton; pgvn_monitor *monitor=state->owner;
    apr_pool_t *pool=svn_pool_create(NULL);
    while(WaitForSingleObject(monitor->stop,0)==WAIT_TIMEOUT) {
        ULONG received=0; HTTP_REQUEST *request=(HTTP_REQUEST *)state->buffer;
        memset(state->buffer,0,PGVN_MONITOR_HEADER_LIMIT);
        ULONG result=HttpReceiveHttpRequest(monitor->queue,HTTP_NULL_ID,0,request,PGVN_MONITOR_HEADER_LIMIT,&received,NULL);
        if(result==ERROR_OPERATION_ABORTED || result==ERROR_INVALID_HANDLE) break;
        if(result!=NO_ERROR && result!=ERROR_MORE_DATA) break;
        InterlockedExchange64(&state->request_id,(LONG64)request->RequestId);
        if(WaitForSingleObject(monitor->stop,0)!=WAIT_TIMEOUT) { HttpCancelHttpRequest(monitor->queue,request->RequestId,NULL); break; }
        apr_time_t request_started=pgvn_telemetry_http_start(); response_status=503; response_sent=0;
        char request_id[32]; snprintf(request_id,sizeof(request_id),"monitor-%016llx",(unsigned long long)InterlockedIncrement64(&monitor->request_sequence));
        if(result==ERROR_MORE_DATA) {
            error_response(monitor,request->RequestId,431,"headers_too_large",request_id,pool);
            (void)HttpCancelHttpRequest(monitor->queue,request->RequestId,NULL);
        } else if(request->Verb!=HttpVerbGET) error_response(monitor,request->RequestId,405,"read_only",request_id,pool);
        else if(request->Flags&HTTP_REQUEST_FLAG_MORE_ENTITY_BODY_EXISTS) error_response(monitor,request->RequestId,413,"request_body_not_supported",request_id,pool);
        else {
            char path[256],query[1024];
            if(!ascii(path,sizeof(path),request->CookedUrl.pAbsPath,request->CookedUrl.AbsPathLength) ||
               !ascii(query,sizeof(query),request->CookedUrl.pQueryString,request->CookedUrl.QueryStringLength))
                error_response(monitor,request->RequestId,400,"invalid_url",request_id,pool);
            else if(!strcmp(path,"/health/live")) {
                int live=pgvn_monitor_live(monitor->data);
                respond(monitor,request->RequestId,live ? 200 : 503,"application/json",live ? "{\"live\":true}\n" : "{\"live\":false}\n",request_id);
            }
            else if(!strcmp(path,"/health/ready")) {
                int ready=pgvn_monitor_ready(monitor->data);
                respond(monitor,request->RequestId,ready ? 200 : 503,"application/json",ready ? "{\"ready\":true}\n" : "{\"ready\":false}\n",request_id);
            } else if(!strcmp(path,"/") || !strcmp(path,"/monitor/"))
                respond(monitor,request->RequestId,200,"text/html; charset=utf-8",pgvn_web_index,request_id);
            else if(!strcmp(path,"/monitor/monitor.css")) respond(monitor,request->RequestId,200,"text/css; charset=utf-8",pgvn_web_style,request_id);
            else if(!strcmp(path,"/monitor/monitor.js")) respond(monitor,request->RequestId,200,"text/javascript; charset=utf-8",pgvn_web_script,request_id);
            else if(!strncmp(path,"/api/monitor/v1/",16) || !strcmp(path,"/metrics")) {
                HTTP_KNOWN_HEADER *authorization=&request->Headers.KnownHeaders[HttpHeaderAuthorization];
                if(!origin_allowed(request,monitor->origin)) error_response(monitor,request->RequestId,403,"origin_denied",request_id,pool);
                else if(!authorization->pRawValue || !pgvn_monitor_authorize(monitor->data,authorization->pRawValue,authorization->RawValueLength))
                    error_response(monitor,request->RequestId,401,"authentication_required",request_id,pool);
                else if(!strcmp(path,"/metrics")) {
                    if(*query) error_response(monitor,request->RequestId,400,"invalid_query",request_id,pool);
                    else respond(monitor,request->RequestId,200,"text/plain; version=0.0.4; charset=utf-8",pgvn_monitor_metrics(monitor->data,pool),request_id);
                } else {
                    int status; const char *body=pgvn_monitor_api(monitor->data,path,*query=='?' ? query+1 : query,&status,pool);
                    if(status==200) respond(monitor,request->RequestId,200,"application/json; charset=utf-8",body,request_id);
                    else error_response(monitor,request->RequestId,status,body,request_id,pool);
                }
            } else error_response(monitor,request->RequestId,404,"not_found",request_id,pool);
        }
        pgvn_telemetry_http_finish(request_started,response_status,response_sent,request_id);
        InterlockedExchange64(&state->request_id,0); SecureZeroMemory(state->buffer,PGVN_MONITOR_HEADER_LIMIT); svn_pool_clear(pool);
    }
    InterlockedExchange64(&state->request_id,0); svn_pool_destroy(pool); return NO_ERROR;
}

int pgvn_monitor_url_valid(const wchar_t *url)
{
    const wchar_t *prefix = L"http://127.0.0.1:";
    if (wcsncmp(url, prefix, wcslen(prefix))) return 0;
    const wchar_t *port = url + wcslen(prefix);
    if (*port < L'0' || *port > L'9') return 0;
    const wchar_t *end = port;
    while (*end >= L'0' && *end <= L'9') ++end;
    if (*end != L'/' || end[1]) return 0;
    if (end - port > 5) return 0;
    unsigned long number = wcstoul(port, NULL, 10);
    return number >= 1024 && number <= 65535;
}

void pgvn_monitor_stop(pgvn_monitor *monitor)
{
    if(monitor->stop) SetEvent(monitor->stop);
    if (monitor->queue) {
        (void)HttpShutdownRequestQueue(monitor->queue);
        (void)CancelIoEx(monitor->queue,NULL);
        for (int i = 0; i < PGVN_MONITOR_WORKERS; ++i) {
            if (monitor->workers[i].thread) {
                (void)WaitForSingleObject(monitor->workers[i].thread, INFINITE);
                CloseHandle(monitor->workers[i].thread);
            }
            if(monitor->workers[i].buffer) SecureZeroMemory(monitor->workers[i].buffer,PGVN_MONITOR_HEADER_LIMIT);
            free(monitor->workers[i].buffer);
        }
        HttpCloseRequestQueue(monitor->queue);
    }
    if (monitor->group) HttpCloseUrlGroup(monitor->group);
    if (monitor->session) HttpCloseServerSession(monitor->session);
    if (monitor->initialized) HttpTerminate(HTTP_INITIALIZE_SERVER, NULL);
    pgvn_monitor_data_stop(monitor->data);
    if(monitor->stop) CloseHandle(monitor->stop);
    memset(monitor, 0, sizeof(*monitor));
}

ULONG pgvn_monitor_start(pgvn_monitor *monitor, const wchar_t *url,const char *configuration)
{
    HTTPAPI_VERSION version = HTTPAPI_VERSION_2;
    HTTP_BINDING_INFO binding;
    HTTP_TIMEOUT_LIMIT_INFO timeouts;
    ULONG limit = PGVN_MONITOR_QUEUE_LIMIT;
    ULONG result;
    memset(monitor, 0, sizeof(*monitor));
    if (!pgvn_monitor_url_valid(url)) return ERROR_INVALID_PARAMETER;
    monitor->stop=CreateEventW(NULL,TRUE,FALSE,NULL);
    if(!monitor->stop) return GetLastError();
    result=pgvn_monitor_data_start(&monitor->data,configuration);
    if(result) goto failed;
    snprintf(monitor->origin,sizeof(monitor->origin),"http://127.0.0.1:%lu",wcstoul(url+wcslen(L"http://127.0.0.1:"),NULL,10));
    result = HttpInitialize(version, HTTP_INITIALIZE_SERVER, NULL);
    if (result) goto failed;
    monitor->initialized = 1;
    result = HttpCreateServerSession(version, &monitor->session, 0);
    if (result) goto failed;
    result = HttpCreateUrlGroup(monitor->session, &monitor->group, 0);
    if (result) goto failed;
    result = HttpCreateRequestQueue(version, NULL, NULL, 0, &monitor->queue);
    if (result) goto failed;
    result = HttpSetRequestQueueProperty(monitor->queue, HttpServerQueueLengthProperty,
                                         &limit, sizeof(limit), 0, NULL);
    if (result) goto failed;
    memset(&binding, 0, sizeof(binding));
    binding.Flags.Present = 1;
    binding.RequestQueueHandle = monitor->queue;
    result = HttpSetUrlGroupProperty(monitor->group, HttpServerBindingProperty,
                                    &binding, sizeof(binding));
    if (result) goto failed;
    memset(&timeouts, 0, sizeof(timeouts));
    timeouts.Flags.Present = 1;
    timeouts.EntityBody = 5;
    timeouts.DrainEntityBody = 2;
    timeouts.RequestQueue = 5;
    timeouts.IdleConnection = 5;
    timeouts.HeaderWait = 5;
    timeouts.MinSendRate = 1024;
    result = HttpSetUrlGroupProperty(monitor->group, HttpServerTimeoutsProperty,
                                    &timeouts, sizeof(timeouts));
    if (result) goto failed;
    HTTP_TIMEOUT_LIMIT_INFO actual;
    ULONG actual_size = 0;
    memset(&actual, 0, sizeof(actual));
    result = HttpQueryUrlGroupProperty(monitor->group, HttpServerTimeoutsProperty,
                                      &actual, sizeof(actual), &actual_size);
    if (result) goto failed;
    if (actual.HeaderWait != timeouts.HeaderWait || actual.RequestQueue != timeouts.RequestQueue
        || actual.EntityBody != timeouts.EntityBody || actual.IdleConnection != timeouts.IdleConnection
        || actual.DrainEntityBody != timeouts.DrainEntityBody || actual.MinSendRate != timeouts.MinSendRate) {
        result = ERROR_INVALID_DATA;
        goto failed;
    }
    result = HttpAddUrlToUrlGroup(monitor->group, url, 0, 0);
    if (result) goto failed;
    for (int i = 0; i < PGVN_MONITOR_WORKERS; ++i) {
        monitor->workers[i].owner = monitor;
        monitor->workers[i].buffer = malloc(PGVN_MONITOR_HEADER_LIMIT);
        if (!monitor->workers[i].buffer) { result = ERROR_NOT_ENOUGH_MEMORY; goto failed; }
        monitor->workers[i].thread = CreateThread(NULL, 0, worker, &monitor->workers[i], 0, NULL);
        if (!monitor->workers[i].thread) { result = GetLastError(); goto failed; }
    }
    return NO_ERROR;
failed:
    pgvn_monitor_stop(monitor);
    return result;
}
