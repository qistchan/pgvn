/* Native external-hook fixture. All output goes to the explicit test directory. */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <fcntl.h>
#include <io.h>

static char *utf8(const wchar_t *value)
{
    int size=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,value,-1,NULL,0,NULL,NULL);
    if(!size) return NULL;
    char *text=malloc((size_t)size);
    if(!text || !WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,value,-1,text,size,NULL,NULL)) { free(text); return NULL; }
    return text;
}

static void quoted(FILE *file,const char *value)
{
    if(!value) { fputs("null",file); return; }
    fputc('"',file);
    for(const unsigned char *p=(const unsigned char *)value;*p;++p) {
        if(*p=='"' || *p=='\\') { fputc('\\',file); fputc(*p,file); }
        else if(*p<32) fprintf(file,"\\u%04x",*p);
        else fputc(*p,file);
    }
    fputc('"',file);
}

static int pid_file(const wchar_t *path)
{
    FILE *file=_wfopen(path,L"wb");
    if(!file) return 0;
    fprintf(file,"%lu",(unsigned long)GetCurrentProcessId());
    return fclose(file)==0;
}

int wmain(int argc,wchar_t **argv)
{
    if(argc>=3 && !wcscmp(argv[1],L"child")) {
        if(!pid_file(argv[2])) return 40;
        Sleep(INFINITE); return 0;
    }
    if(argc<3) return 41;
    _setmode(_fileno(stdin),_O_BINARY);
    size_t capacity=32u*1024u*1024u+1;
    char *input=malloc(capacity);
    if(!input) return 42;
    size_t size=fread(input,1,capacity,stdin);
    if(ferror(stdin) || size==capacity) { free(input); return 43; }
    wchar_t path[32768];
    int length=swprintf(path,32768,L"%ls/hook-%llu-%lu.json",argv[2],(unsigned long long)GetTickCount64(),(unsigned long)GetCurrentProcessId());
    if(length<0 || length>=32768) { free(input); return 44; }
    FILE *file=_wfopen(path,L"wb");
    if(!file) { free(input); return 45; }
    fputs("{\"pid\":",file); fprintf(file,"%lu,\"argv\":[",(unsigned long)GetCurrentProcessId());
    for(int i=1;i<argc;++i) {
        char *value=utf8(argv[i]); if(i>1) fputc(',',file); quoted(file,value); free(value);
    }
    fputs("],\"env\":{",file);
    const wchar_t *names[]={L"PGVN_EVENT",L"PGVN_REPOSITORY_ID",L"PGVN_USER",L"PGVN_TRANSACTION",L"PGVN_REVISION",L"PGVN_CONFIG",L"PGPASSWORD",L"PGVN_DSN"};
    for(size_t i=0;i<sizeof(names)/sizeof(names[0]);++i) {
        if(i) fputc(',',file);
        char *name=utf8(names[i]); quoted(file,name); free(name); fputc(':',file);
        DWORD required=GetEnvironmentVariableW(names[i],NULL,0);
        wchar_t *wide=required ? malloc((size_t)required*sizeof(wchar_t)) : NULL;
        char *value=NULL;
        if(wide && GetEnvironmentVariableW(names[i],wide,required)) value=utf8(wide);
        quoted(file,value); free(value); free(wide);
    }
    fputs("},\"event\":",file); fwrite(input,1,size,file); fputs("}\n",file);
    free(input); if(fclose(file)) return 46;
    if(!wcscmp(argv[1],L"reject")) { fputs("hook rejected by test\n",stderr); return 19; }
    if(!wcscmp(argv[1],L"token")) { puts("opaquelocktoken:65792085-27ae-4605-acf6-b569ae68b4cc"); return 0; }
    if(!wcscmp(argv[1],L"flood")) {
        for(int i=0;i<20000;++i) { fputc('e',stderr); fputc('o',stdout); }
        return 19;
    }
    if(!wcscmp(argv[1],L"gate")) {
        if(argc<5 || !pid_file(argv[3])) return 47;
        while(GetFileAttributesW(argv[4])==INVALID_FILE_ATTRIBUTES) Sleep(25);
    }
    if(!wcscmp(argv[1],L"child-wait")) {
        if(argc<4) return 48;
        wchar_t executable[32768],line[32768];
        if(!GetModuleFileNameW(NULL,executable,32768)) return 49;
        if(swprintf(line,32768,L"\"%ls\" child \"%ls\"",executable,argv[3])<0) return 50;
        STARTUPINFOW startup={0}; PROCESS_INFORMATION process={0}; startup.cb=sizeof(startup);
        if(!CreateProcessW(executable,line,NULL,NULL,FALSE,CREATE_NO_WINDOW,NULL,NULL,&startup,&process)) return 51;
        CloseHandle(process.hThread); CloseHandle(process.hProcess);
        Sleep(INFINITE);
    }
    return 0;
}
