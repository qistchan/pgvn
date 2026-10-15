#include <stdio.h>
#include <string.h>
#include <wchar.h>
#include "http_win.h"
#include "listener.h"
#include <apr_general.h>
#include "pgvn.h"
#include "svn_pools.h"

static HANDLE stop_event;
static const wchar_t *monitor_url;
static const char *configuration;
static const wchar_t *service_name = L"PgVN";
static SERVICE_STATUS_HANDLE service_handle;
static SERVICE_STATUS service_status;
static DWORD service_result;
static SRWLOCK service_status_lock=SRWLOCK_INIT;

static void report_status(DWORD state, DWORD error)
{
    AcquireSRWLockExclusive(&service_status_lock);
    service_status.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
    service_status.dwCurrentState = state;
    service_status.dwControlsAccepted = state == SERVICE_RUNNING ? SERVICE_ACCEPT_STOP|SERVICE_ACCEPT_SHUTDOWN : 0;
    service_status.dwWin32ExitCode = error ? ERROR_SERVICE_SPECIFIC_ERROR : NO_ERROR;
    service_status.dwServiceSpecificExitCode=error;
    service_status.dwWaitHint = state == SERVICE_START_PENDING || state == SERVICE_STOP_PENDING ? 15000 : 0;
    service_status.dwCheckPoint = state == SERVICE_START_PENDING || state == SERVICE_STOP_PENDING ? service_status.dwCheckPoint+1 : 0;
    if (service_handle) SetServiceStatus(service_handle, &service_status);
    ReleaseSRWLockExclusive(&service_status_lock);
}

static void shutdown_progress(void)
{
    report_status(SERVICE_STOP_PENDING,NO_ERROR);
}

static DWORD WINAPI service_control(DWORD control, DWORD event, void *data, void *context)
{
    (void)event; (void)data; (void)context;
    if (control == SERVICE_CONTROL_STOP || control == SERVICE_CONTROL_SHUTDOWN) {
        report_status(SERVICE_STOP_PENDING, NO_ERROR);
        SetEvent(stop_event);
    }
    return NO_ERROR;
}

static BOOL WINAPI console_control(DWORD control)
{
    if (control == CTRL_C_EVENT || control == CTRL_BREAK_EVENT || control == CTRL_CLOSE_EVENT) {
        SetEvent(stop_event);
        return TRUE;
    }
    return FALSE;
}

static DWORD run_monitor(void)
{
    apr_pool_t *pool=svn_pool_create(NULL);
    pgvn_listener *listener=NULL;
    if(configuration) {
        svn_error_t *error=pgvn_listener_start(&listener,configuration,stop_event,pool);
        if(error) {
            DWORD code=(DWORD)pgvn_error_exit_code(error);
            fprintf(stderr,"PgVN SVN startup failed (code %ld)\n",(long)error->apr_err);
            svn_error_clear(error); svn_pool_destroy(pool); return code;
        }
    }
    pgvn_monitor monitor;
    DWORD result = pgvn_monitor_start(&monitor, monitor_url);
    if (result) {
        fprintf(stderr, "PgVN monitor startup failed: Win32 error %lu\n", (unsigned long)result);
        pgvn_listener_stop(listener,shutdown_progress); svn_pool_destroy(pool);
        return result==ERROR_ACCESS_DENIED ? PGVN_EXIT_DENIED : result==ERROR_ALREADY_EXISTS ? PGVN_EXIT_CONFLICT : PGVN_EXIT_FAILURE;
    }
    report_status(SERVICE_RUNNING, NO_ERROR);
    (void)WaitForSingleObject(stop_event, INFINITE);
    report_status(SERVICE_STOP_PENDING,NO_ERROR);
    pgvn_listener_stop(listener,shutdown_progress);
    pgvn_monitor_stop(&monitor);
    svn_pool_destroy(pool);
    return NO_ERROR;
}

static void WINAPI service_main(DWORD argc, wchar_t **argv)
{
    (void)argc; (void)argv;
    service_handle = RegisterServiceCtrlHandlerExW(service_name, service_control, NULL);
    if (!service_handle) return;
    report_status(SERVICE_START_PENDING, NO_ERROR);
    service_result=run_monitor();
    report_status(SERVICE_STOPPED,service_result);
}

int wmain(int argc, wchar_t **argv)
{
    int service = 0;
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
    if (apr_initialize() != APR_SUCCESS) return 1;
    apr_pool_t *arguments=svn_pool_create(NULL);
    for (int i = 1; i < argc; ++i) {
        if (!wcscmp(argv[i], L"--version") || !wcscmp(argv[i], L"--diagnose")) {
            char json[1024];
            pgvn_build_json(json, sizeof(json));
            fputs(json, stdout);
            svn_pool_destroy(arguments);
            apr_terminate();
            return 0;
        } else if (!wcscmp(argv[i], L"--monitor-url") && i + 1 < argc) {
            monitor_url = argv[++i];
        } else if (!wcscmp(argv[i], L"--config") && i + 1 < argc) {
            const wchar_t *value=argv[++i];
            int length=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,value,-1,NULL,0,NULL,NULL);
            if(!length || configuration) { svn_pool_destroy(arguments); apr_terminate(); return 2; }
            char *text=apr_palloc(arguments,length);
            WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,value,-1,text,length,NULL,NULL);
            configuration=text;
        } else if (!wcscmp(argv[i], L"--service")) {
            service = 1;
        } else if (!wcscmp(argv[i], L"--service-name") && i + 1 < argc) {
            service_name = argv[++i];
        } else {
            fputs("Usage: pgvnserve --version | [--config FILE] --monitor-url http://127.0.0.1:PORT/ [--service --service-name NAME]\n"
                  "Without --config only the bootstrap monitor starts.\n", stderr);
            svn_pool_destroy(arguments);
            apr_terminate();
            return 2;
        }
    }
    if (!monitor_url || !pgvn_monitor_url_valid(monitor_url)) {
        fputs("An explicit loopback monitor URL with a port from 1024 to 65535 is required.\n", stderr);
        svn_pool_destroy(arguments);
        apr_terminate();
        return 2;
    }
    stop_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!stop_event) { svn_pool_destroy(arguments); apr_terminate(); return 1; }
    DWORD result;
    if (service) {
        SERVICE_TABLE_ENTRYW entries[] = {{(wchar_t *)service_name, service_main}, {NULL, NULL}};
        result = StartServiceCtrlDispatcherW(entries) ? service_result : PGVN_EXIT_FAILURE;
    } else {
        SetConsoleCtrlHandler(console_control, TRUE);
        result = run_monitor();
        SetConsoleCtrlHandler(console_control, FALSE);
    }
    CloseHandle(stop_event);
    svn_pool_destroy(arguments);
    apr_terminate();
    return (int)result;
}
