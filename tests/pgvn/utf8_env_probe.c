#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <apr_general.h>
#include <libpq-fe.h>

typedef char *(__cdecl *getenv_proc)(const char *);
static int opens(const char *path)
{
    if(!path) return 0;
    FILE *stream=fopen(path,"rb");
    if(!stream) return 0;
    fclose(stream);return 1;
}
static void inspect(const char *phase)
{
    wchar_t wide[2048];char utf8[8192],ansi[8192];
    DWORD count=GetEnvironmentVariableW(L"PGSERVICEFILE",wide,2048);
    if(!count || count>=2048) { puts("{\"error\":\"missing_environment\"}");return; }
    WideCharToMultiByte(CP_UTF8,0,wide,-1,utf8,sizeof(utf8),NULL,NULL);
    WideCharToMultiByte(CP_ACP,0,wide,-1,ansi,sizeof(ansi),NULL,NULL);
    const char *narrow=getenv("PGSERVICEFILE");
    HMODULE ucrt=GetModuleHandleW(L"ucrtbase.dll");
    getenv_proc crt=ucrt ? (getenv_proc)GetProcAddress(ucrt,"getenv") : NULL;
    const char *ucrt_value=crt ? crt("PGSERVICEFILE") : NULL;
    HANDLE file=CreateFileW(wide,GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,NULL,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,NULL);
    int wide_read=file!=INVALID_HANDLE_VALUE;
    if(wide_read) CloseHandle(file);
    PGconn *connection=PQconnectdb("service=pgvn_service connect_timeout=3");
    int ready=connection && PQstatus(connection)==CONNECTION_OK;
    const char *error=connection ? PQerrorMessage(connection) : "";
    printf("{\"phase\":\"%s\",\"acp\":%u,\"wide_read\":%d,\"getenv_utf8\":%d,\"getenv_acp\":%d,"
      "\"ucrt_getenv_utf8\":%d,\"ucrt_getenv_acp\":%d,\"fopen_getenv\":%d,\"fopen_utf8\":%d,\"fopen_acp\":%d,"
      "\"pq_ok\":%d,\"pq_service_file_error\":%d,\"pq_service_missing\":%d,\"pq_password_error\":%d}\n",
      phase,GetACP(),wide_read,narrow && !strcmp(narrow,utf8),narrow && !strcmp(narrow,ansi),
      ucrt_value && !strcmp(ucrt_value,utf8),ucrt_value && !strcmp(ucrt_value,ansi),opens(narrow),opens(utf8),opens(ansi),ready,
      strstr(error,"service file")!=NULL,strstr(error,"definition of service")!=NULL,strstr(error,"password")!=NULL);
    if(connection) PQfinish(connection);
}
int main(int argc,char **argv)
{
    inspect("before_apr");
    int app=argc>1 && !strcmp(argv[1],"app");
    apr_status_t status=app ? apr_app_initialize(&argc,(const char *const **)&argv,NULL) : apr_initialize();
    if(status!=APR_SUCCESS) return 2;
    inspect(app ? "apr_app_initialize" : "apr_initialize");
    apr_terminate();return 0;
}
