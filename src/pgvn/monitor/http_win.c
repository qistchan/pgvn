#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
#include "http_win.h"

static int path_is(const HTTP_REQUEST *request, const wchar_t *path)
{
    return request->CookedUrl.AbsPathLength == wcslen(path) * sizeof(wchar_t)
        && !wmemcmp(request->CookedUrl.pAbsPath, path, wcslen(path));
}

static void respond(pgvn_monitor *monitor, HTTP_REQUEST_ID id,
                    USHORT status, const char *reason,
                    const char *type, const char *body)
{
    HTTP_RESPONSE response;
    HTTP_DATA_CHUNK chunk;
    HTTP_UNKNOWN_HEADER headers[2];
    char content_length[32];
    memset(&response, 0, sizeof(response));
    memset(&chunk, 0, sizeof(chunk));
    memset(headers, 0, sizeof(headers));
    response.StatusCode = status;
    response.pReason = reason;
    response.ReasonLength = (USHORT)strlen(reason);
    response.Headers.KnownHeaders[HttpHeaderContentType].pRawValue = type;
    response.Headers.KnownHeaders[HttpHeaderContentType].RawValueLength = (USHORT)strlen(type);
    response.Headers.KnownHeaders[HttpHeaderCacheControl].pRawValue = "no-store";
    response.Headers.KnownHeaders[HttpHeaderCacheControl].RawValueLength = 8;
    snprintf(content_length, sizeof(content_length), "%zu", strlen(body));
    response.Headers.KnownHeaders[HttpHeaderContentLength].pRawValue = content_length;
    response.Headers.KnownHeaders[HttpHeaderContentLength].RawValueLength = (USHORT)strlen(content_length);
    headers[0].pName = "X-Content-Type-Options";
    headers[0].NameLength = 22;
    headers[0].pRawValue = "nosniff";
    headers[0].RawValueLength = 7;
    response.Headers.UnknownHeaderCount = 1;
    response.Headers.pUnknownHeaders = headers;
    if (status == 405) {
        headers[1].pName = "Allow";
        headers[1].NameLength = 5;
        headers[1].pRawValue = "GET";
        headers[1].RawValueLength = 3;
        response.Headers.UnknownHeaderCount = 2;
    }
    chunk.DataChunkType = HttpDataChunkFromMemory;
    chunk.FromMemory.pBuffer = (void *)body;
    chunk.FromMemory.BufferLength = (ULONG)strlen(body);
    response.EntityChunkCount = 1;
    response.pEntityChunks = &chunk;
    (void)HttpSendHttpResponse(monitor->queue, id,
        status >= 400 ? HTTP_SEND_RESPONSE_FLAG_DISCONNECT : 0,
        &response, NULL, NULL, NULL, 0, NULL, NULL);
}

static DWORD WINAPI worker(void *baton)
{
    pgvn_monitor_worker *state = baton;
    pgvn_monitor *monitor = state->owner;
    unsigned char *buffer = state->buffer;
    for (;;) {
        ULONG received = 0;
        HTTP_REQUEST *request = (HTTP_REQUEST *)buffer;
        memset(buffer, 0, PGVN_MONITOR_HEADER_LIMIT);
        ULONG result = HttpReceiveHttpRequest(monitor->queue, HTTP_NULL_ID, 0,
            request, PGVN_MONITOR_HEADER_LIMIT, &received, NULL);
        if (result == ERROR_OPERATION_ABORTED || result == ERROR_INVALID_HANDLE) break;
        if (result == ERROR_MORE_DATA) {
            respond(monitor, request->RequestId, 431, "Request Header Fields Too Large",
                    "application/json", "{\"error\":\"headers_too_large\"}\n");
            (void)HttpCancelHttpRequest(monitor->queue, request->RequestId, NULL);
            continue;
        }
        if (result != NO_ERROR) break;
        if (request->Verb != HttpVerbGET) {
            respond(monitor, request->RequestId, 405, "Method Not Allowed",
                    "application/json", "{\"error\":\"read_only\"}\n");
        } else if (request->Flags & HTTP_REQUEST_FLAG_MORE_ENTITY_BODY_EXISTS) {
            respond(monitor, request->RequestId, 413, "Payload Too Large",
                    "application/json", "{\"error\":\"request_body_not_supported\"}\n");
        } else if (path_is(request, L"/health/live")) {
            respond(monitor, request->RequestId, 200, "OK", "application/json",
                    "{\"live\":true,\"milestone\":\"M0\"}\n");
        } else if (path_is(request, L"/health/ready")) {
            respond(monitor, request->RequestId, 503, "Service Unavailable",
                    "application/json", "{\"ready\":false,\"reason\":\"core_not_initialized\"}\n");
        } else if (path_is(request, L"/monitor/") || path_is(request, L"/")) {
            respond(monitor, request->RequestId, 200, "OK", "text/html; charset=utf-8",
                "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
                "<title>PgVN bootstrap</title><h1>PgVN bootstrap monitor</h1>"
                "<p>Milestone M0. Repository and protocol services are not ready.</p>"
                "<p><a href=\"/health/live\">Liveness</a> "
                "<a href=\"/health/ready\">Readiness</a></p></html>");
        } else {
            respond(monitor, request->RequestId, 404, "Not Found", "application/json",
                    "{\"error\":\"not_found\"}\n");
        }
    }
    return NO_ERROR;
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
    if (monitor->queue) {
        (void)HttpShutdownRequestQueue(monitor->queue);
        for (int i = 0; i < PGVN_MONITOR_WORKERS; ++i) {
            if (monitor->workers[i].thread) {
                (void)WaitForSingleObject(monitor->workers[i].thread, INFINITE);
                CloseHandle(monitor->workers[i].thread);
            }
            free(monitor->workers[i].buffer);
        }
        HttpCloseRequestQueue(monitor->queue);
    }
    if (monitor->group) HttpCloseUrlGroup(monitor->group);
    if (monitor->session) HttpCloseServerSession(monitor->session);
    if (monitor->initialized) HttpTerminate(HTTP_INITIALIZE_SERVER, NULL);
    memset(monitor, 0, sizeof(*monitor));
}

ULONG pgvn_monitor_start(pgvn_monitor *monitor, const wchar_t *url)
{
    HTTPAPI_VERSION version = HTTPAPI_VERSION_2;
    HTTP_BINDING_INFO binding;
    HTTP_TIMEOUT_LIMIT_INFO timeouts;
    ULONG limit = PGVN_MONITOR_QUEUE_LIMIT;
    ULONG result;
    memset(monitor, 0, sizeof(*monitor));
    if (!pgvn_monitor_url_valid(url)) return ERROR_INVALID_PARAMETER;
    result = HttpInitialize(version, HTTP_INITIALIZE_SERVER, NULL);
    if (result) return result;
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
