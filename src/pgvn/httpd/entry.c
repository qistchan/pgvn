#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>
#include "ap_release.h"
#include "pgvn_build_info.h"

/* The pinned Apache main.c is compiled with main=pgvn_httpd_main and linked
 * to the unchanged SDK libhttpd/APR. The process owns its UTF-8 manifest. */
int pgvn_httpd_main(int argc,const char *const argv[]);

int wmain(int argc,wchar_t **wide)
{
    SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX);
    if(GetACP()!=CP_UTF8) {
        fputs("PgVN HTTP host requires a UTF-8 process code page (Windows 10 1903 or later).\n",stderr);
        return 2;
    }
    if(argc==2 && !wcscmp(wide[1],L"--pgvn-runtime")) {
        printf("{\"product\":\"PgVNHTTPD\",\"apache_base\":\"%s\",\"process_code_page\":%u,"
               "\"build_identity\":" PGVN_BUILD_JSON_PREFIX "}}\n",AP_SERVER_BASEREVISION,GetACP());
        return 0;
    }
    char **arguments=calloc((size_t)argc+1,sizeof(*arguments));
    if(!arguments) return 1;
    int result=2;
    for(int i=0;i<argc;++i) {
        int size=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,wide[i],-1,NULL,0,NULL,NULL);
        if(!size) goto done;
        arguments[i]=malloc((size_t)size);
        if(!arguments[i]) { result=1;goto done; }
        if(!WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,wide[i],-1,arguments[i],size,NULL,NULL)) goto done;
    }
    result=pgvn_httpd_main(argc,(const char *const *)arguments);
done:
    for(int i=0;i<argc;++i) free(arguments[i]);
    free(arguments);return result;
}
