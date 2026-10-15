#include <stdio.h>
#include <string.h>
#include <wchar.h>
#include "http_win.h"
#include <apr_general.h>
#include "pgvn.h"

static HANDLE stop_event;
static const wchar_t *monitor_url;
static const wchar_t *service_name = L"PgVN";
static SERVICE_STATUS_HANDLE service_handle;
static SERVICE_STATUS service_status;

static void report_status(DWORD state, DWORD error)
{
    service_status.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
    service_status.dwCurrentState = state;
    service_status.dwControlsAccepted = state == SERVICE_RUNNING ? SERVICE_ACCEPT_STOP : 0;
    service_status.dwWin32ExitCode = error;
    service_status.dwWaitHint = state == SERVICE_START_PENDING ? 10000 : 0;
    if (service_handle) SetServiceStatus(service_handle, &service_status);
}

static DWORD WINAPI service_control(DWORD control, DWORD event, void *data, void *context)
{
    (void)event; (void)data; (void)context;
    if (control == SERVICE_CONTROL_STOP) {
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
    pgvn_monitor monitor;
    DWORD result = pgvn_monitor_start(&monitor, monitor_url);
    if (result) {
        fprintf(stderr, "PgVN monitor startup failed: Win32 error %lu\n", (unsigned long)result);
        return result;
    }
    report_status(SERVICE_RUNNING, NO_ERROR);
    (void)WaitForSingleObject(stop_event, INFINITE);
    pgvn_monitor_stop(&monitor);
    return NO_ERROR;
}

static void WINAPI service_main(DWORD argc, wchar_t **argv)
{
    (void)argc; (void)argv;
    service_handle = RegisterServiceCtrlHandlerExW(service_name, service_control, NULL);
    if (!service_handle) return;
    report_status(SERVICE_START_PENDING, NO_ERROR);
    DWORD result = run_monitor();
    report_status(SERVICE_STOPPED, result);
}

int wmain(int argc, wchar_t **argv)
{
    int service = 0;
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
    if (apr_initialize() != APR_SUCCESS) return 1;
    for (int i = 1; i < argc; ++i) {
        if (!wcscmp(argv[i], L"--version") || !wcscmp(argv[i], L"--diagnose")) {
            char json[1024];
            pgvn_build_json(json, sizeof(json));
            fputs(json, stdout);
            apr_terminate();
            return 0;
        } else if (!wcscmp(argv[i], L"--monitor-url") && i + 1 < argc) {
            monitor_url = argv[++i];
        } else if (!wcscmp(argv[i], L"--service")) {
            service = 1;
        } else if (!wcscmp(argv[i], L"--service-name") && i + 1 < argc) {
            service_name = argv[++i];
        } else {
            fputs("Usage: pgvnserve --version | --monitor-url http://127.0.0.1:PORT/ [--service --service-name NAME]\n"
                  "Bootstrap: SVN repository protocol follows in M3.\n", stderr);
            apr_terminate();
            return 2;
        }
    }
    if (!monitor_url || !pgvn_monitor_url_valid(monitor_url)) {
        fputs("An explicit loopback monitor URL with a port from 1024 to 65535 is required.\n", stderr);
        apr_terminate();
        return 2;
    }
    stop_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!stop_event) { apr_terminate(); return 1; }
    DWORD result;
    if (service) {
        SERVICE_TABLE_ENTRYW entries[] = {{(wchar_t *)service_name, service_main}, {NULL, NULL}};
        result = StartServiceCtrlDispatcherW(entries) ? NO_ERROR : GetLastError();
    } else {
        SetConsoleCtrlHandler(console_control, TRUE);
        result = run_monitor();
        SetConsoleCtrlHandler(console_control, FALSE);
    }
    CloseHandle(stop_event);
    apr_terminate();
    return result ? 1 : 0;
}
