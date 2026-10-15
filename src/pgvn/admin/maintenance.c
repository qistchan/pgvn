#include <string.h>
#include "admin.h"
#include <apr_strings.h>
#include "pgfs_native.h"

static wchar_t *wide(const char *text,apr_pool_t *pool)
{
    int size=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text,-1,NULL,0);
    if(!size) return NULL;
    wchar_t *result=apr_palloc(pool,size*sizeof(*result));
    if(!MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text,-1,result,size)) return NULL;
    return result;
}

static svn_error_t *child_command_line(wchar_t **output,admin_command *command)
{
    apr_size_t required=1;
    wchar_t **args=apr_palloc(command->pool,command->child_argc*sizeof(*args));
    for(int i=0;i<command->child_argc;++i) {
        args[i]=wide(command->child_argv[i],command->pool);
        if(!args[i]) return admin_usage("Child command arguments must be UTF-8");
        required+=2*wcslen(args[i])+3;
    }
    if(required>32767) return admin_usage("Child command line exceeds the Windows length limit");
    wchar_t *buffer=apr_palloc(command->pool,required*sizeof(*buffer)),*p=buffer;
    for(int i=0;i<command->child_argc;++i) {
        if(i) *p++=L' ';
        *p++=L'"';
        const wchar_t *a=args[i];
        for(;;) {
            apr_size_t slashes=0;
            while(*a==L'\\') { ++slashes; ++a; }
            apr_size_t count=(*a==L'"' || !*a) ? slashes*2 : slashes;
            while(count--) *p++=L'\\';
            if(!*a) break;
            if(*a==L'"') *p++=L'\\';
            *p++=*a++;
        }
        *p++=L'"';
    }
    *p=0; *output=buffer;
    return SVN_NO_ERROR;
}

static svn_error_t *freeze_child(void *baton,apr_pool_t *pool)
{
    admin_command *command=baton; wchar_t *line;
    apr_int64_t seconds=300;
    if(command->values[OPT_TIMEOUT]) SVN_ERR(admin_number(&seconds,command->values[OPT_TIMEOUT],1,86400));
    SVN_ERR(child_command_line(&line,command));
    STARTUPINFOEXW startup; PROCESS_INFORMATION process;
    memset(&startup,0,sizeof(startup)); memset(&process,0,sizeof(process));
    startup.StartupInfo.cb=sizeof(startup);
    startup.StartupInfo.dwFlags=STARTF_USESTDHANDLES;
    HANDLE inherited[3]={NULL,NULL,NULL}; HANDLE job=NULL;
    DWORD win_error=NO_ERROR,child_exit=0; svn_error_t *error=NULL;
    BOOL attributes_initialized=FALSE;
    const DWORD ids[]={STD_INPUT_HANDLE,STD_OUTPUT_HANDLE,STD_ERROR_HANDLE};
    for(int i=0;i<3;++i) {
        HANDLE original=GetStdHandle(ids[i]);
        if(!original || original==INVALID_HANDLE_VALUE) {
            SECURITY_ATTRIBUTES security={sizeof(security),NULL,TRUE};
            inherited[i]=CreateFileW(L"NUL",i ? GENERIC_WRITE : GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE,&security,OPEN_EXISTING,0,NULL);
            if(inherited[i]==INVALID_HANDLE_VALUE) { inherited[i]=NULL; win_error=GetLastError(); goto cleanup; }
        } else if(!DuplicateHandle(GetCurrentProcess(),original,GetCurrentProcess(),&inherited[i],0,TRUE,DUPLICATE_SAME_ACCESS)) {
            win_error=GetLastError(); goto cleanup;
        }
    }
    startup.StartupInfo.hStdInput=inherited[0]; startup.StartupInfo.hStdOutput=inherited[1]; startup.StartupInfo.hStdError=inherited[2];
    SIZE_T size=0;
    InitializeProcThreadAttributeList(NULL,1,0,&size);
    startup.lpAttributeList=apr_palloc(pool,size);
    if(!InitializeProcThreadAttributeList(startup.lpAttributeList,1,0,&size)) { win_error=GetLastError(); goto cleanup; }
    attributes_initialized=TRUE;
    if(!UpdateProcThreadAttribute(startup.lpAttributeList,0,PROC_THREAD_ATTRIBUTE_HANDLE_LIST,inherited,sizeof(inherited),NULL,NULL)) {
        win_error=GetLastError(); goto cleanup;
    }
    job=CreateJobObjectW(NULL,NULL);
    if(!job) { win_error=GetLastError(); goto cleanup; }
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
    memset(&limits,0,sizeof(limits)); limits.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if(!SetInformationJobObject(job,JobObjectExtendedLimitInformation,&limits,sizeof(limits))) { win_error=GetLastError(); goto cleanup; }
    if(!CreateProcessW(NULL,line,NULL,NULL,TRUE,CREATE_SUSPENDED|CREATE_NO_WINDOW|EXTENDED_STARTUPINFO_PRESENT,
                       NULL,NULL,&startup.StartupInfo,&process)) { win_error=GetLastError(); goto cleanup; }
    if(!AssignProcessToJobObject(job,process.hProcess)) { win_error=GetLastError(); TerminateProcess(process.hProcess,1); goto cleanup; }
    if(ResumeThread(process.hThread)==(DWORD)-1) { win_error=GetLastError(); goto cleanup; }
    ULONGLONG deadline=GetTickCount64()+(ULONGLONG)seconds*1000;
    for(;;) {
        DWORD state=WaitForSingleObject(process.hProcess,50);
        if(state==WAIT_OBJECT_0) break;
        if(state==WAIT_FAILED) { win_error=GetLastError(); goto cleanup; }
        error=admin_cancel(NULL);
        if(error) goto cleanup;
        if(GetTickCount64()>=deadline) { error=svn_error_create(SVN_ERR_CANCELLED,NULL,"Freeze child exceeded its timeout"); goto cleanup; }
    }
    if(!GetExitCodeProcess(process.hProcess,&child_exit)) { win_error=GetLastError(); goto cleanup; }
    if(child_exit) error=svn_error_createf(SVN_ERR_EXTERNAL_PROGRAM,NULL,"Freeze child exited with code %lu",(unsigned long)child_exit);
cleanup:
    if(job) CloseHandle(job);
    if(process.hProcess) { WaitForSingleObject(process.hProcess,5000); CloseHandle(process.hProcess); }
    if(process.hThread) CloseHandle(process.hThread);
    if(attributes_initialized) DeleteProcThreadAttributeList(startup.lpAttributeList);
    for(int i=0;i<3;++i) if(inherited[i]) CloseHandle(inherited[i]);
    if(win_error) error=svn_error_compose_create(error,svn_error_wrap_apr(win_error,"Unable to execute freeze child"));
    return error;
}

svn_error_t *admin_maintenance(admin_command *command)
{
    const char *op=command->argv[1]; pgvn_binding *binding;
    SVN_ERR(pgvn_repository_lookup(&binding,command->context,command->argv[2],command->pool));
    if(!strcmp(op,"purge")) {
        if(strcmp(binding->repository_id,command->values[OPT_CONFIRM_ID])) return admin_usage("Confirmation ID does not match the selected repository");
        SVN_ERR(pgvn_repo_purge(command->context,binding->repository_id,command->pool));
        admin_ok("maintenance.purge"); return SVN_NO_ERROR;
    }
    if(!strcmp(op,"hotcopy")) {
        pgvn_binding *destination;
        const char *incremental=NULL;
        if(command->flags & OPTION(OPT_INCREMENTAL)) {
            if(command->values[OPT_ROUTE]) return admin_usage("An incremental hotcopy uses the registered destination route");
            SVN_ERR(pgvn_repository_lookup(&destination,command->context,command->argv[3],command->pool));
            incremental=destination->repository_id;
        }
        const char *route=command->values[OPT_ROUTE] ? command->values[OPT_ROUTE] : apr_pstrcat(command->pool,"/",command->argv[3],NULL);
        SVN_ERR(pgvn_repo_hotcopy(&destination,command->context,binding->repository_id,command->argv[3],route,incremental,command->pool));
        fputs("{\"ok\":true,\"id\":",stdout); admin_json_cstr(stdout,destination->repository_id);
        fputs(",\"uuid\":",stdout); admin_json_cstr(stdout,destination->svn_uuid); puts("}");
        return SVN_NO_ERROR;
    }
    pgvn_repository *repository;
    SVN_ERR(pgvn_repos_open_admin(&repository,command->context,binding->repository_id,command->pool));
    svn_fs_t *fs=svn_repos_fs(pgvn_repo_svn(repository));
    if(!strcmp(op,"pack")) SVN_ERR(pgfs_native_pack(fs,command->pool));
    else if(!strcmp(op,"recover")) SVN_ERR(pgfs_native_recover(fs,command->pool));
    else if(!strcmp(op,"freeze")) return svn_fs_freeze(fs,freeze_child,command,command->pool);
    else {
        svn_fs_txn_t *txn;
        SVN_ERR(svn_fs_open_txn(&txn,fs,command->argv[3],command->pool));
        SVN_ERR(svn_fs_abort_txn(txn,command->pool));
    }
    admin_ok(op);
    return SVN_NO_ERROR;
}
