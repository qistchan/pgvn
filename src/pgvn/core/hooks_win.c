#include <stdio.h>
#include <string.h>
#include <wchar.h>
#include <stdlib.h>
#include "internal.h"
#include "hook_name.h"
#include <apr_strings.h>
#include <apr_base64.h>
#include "private/svn_utf_private.h"

#define HOOK_JSON_LIMIT (32u*1024u*1024u)
#define HOOK_OUTPUT_LIMIT 8192

static svn_error_t *event_limit(void)
{
    return svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN hook event exceeds its resource limit");
}

static svn_error_t *json_string(svn_stringbuf_t *buffer,const char *value)
{
    if(!value) { svn_stringbuf_appendcstr(buffer,"null"); return SVN_NO_ERROR; }
    size_t size=strlen(value);
    if(size>HOOK_JSON_LIMIT/6 || buffer->len>HOOK_JSON_LIMIT-6*size-2) return event_limit();
    svn_stringbuf_appendbyte(buffer,'"');
    for(const unsigned char *p=(const unsigned char *)value;*p;++p) {
        if(*p=='"' || *p=='\\') { svn_stringbuf_appendbyte(buffer,'\\'); svn_stringbuf_appendbyte(buffer,*p); }
        else if(*p<32) { char escape[7]; snprintf(escape,sizeof(escape),"\\u%04x",*p); svn_stringbuf_appendcstr(buffer,escape); }
        else svn_stringbuf_appendbyte(buffer,*p);
    }
    svn_stringbuf_appendbyte(buffer,'"');
    return SVN_NO_ERROR;
}

static svn_error_t *json_field(svn_stringbuf_t *buffer,const char *name,const char *value)
{
    svn_stringbuf_appendcstr(buffer,",\""); svn_stringbuf_appendcstr(buffer,name); svn_stringbuf_appendcstr(buffer,"\":");
    return json_string(buffer,value);
}

static svn_error_t *json_array(svn_stringbuf_t *buffer,const char *name,const apr_array_header_t *values)
{
    svn_stringbuf_appendcstr(buffer,",\""); svn_stringbuf_appendcstr(buffer,name); svn_stringbuf_appendcstr(buffer,"\":[");
    for(int i=0;values && i<values->nelts;++i) {
        if(i) svn_stringbuf_appendbyte(buffer,',');
        SVN_ERR(json_string(buffer,APR_ARRAY_IDX(values,i,const char *)));
    }
    svn_stringbuf_appendbyte(buffer,']'); return SVN_NO_ERROR;
}

static svn_error_t *event_json(svn_stringbuf_t **output,const pgvn_hook_context *event,apr_pool_t *pool)
{
    svn_stringbuf_t *buffer=svn_stringbuf_create("{\"version\":1",pool);
    SVN_ERR(json_field(buffer,"event",pgvn_hook_name(event->event)));
    SVN_ERR(json_field(buffer,"repository_id",event->repository_id));
    SVN_ERR(json_field(buffer,"username",event->username));
    SVN_ERR(json_field(buffer,"transaction_id",event->transaction_id));
    svn_stringbuf_appendcstr(buffer,apr_psprintf(pool,",\"revision\":%ld,\"force\":%s",event->revision,event->force ? "true" : "false"));
    SVN_ERR(json_field(buffer,"path",event->path));
    SVN_ERR(json_field(buffer,"property_name",event->property_name));
    char action[2]={event->action,0};
    SVN_ERR(json_field(buffer,"action",event->action ? action : NULL));
    SVN_ERR(json_field(buffer,"lock_token",event->lock_token));
    SVN_ERR(json_field(buffer,"lock_comment",event->lock_comment));
    SVN_ERR(json_array(buffer,"paths",event->paths));
    SVN_ERR(json_array(buffer,"capabilities",event->capabilities));
    if(event->property_value) {
        if(event->property_value->len>16u*1024u*1024u) return event_limit();
        int size=apr_base64_encode_len((int)event->property_value->len);
        if(buffer->len+(apr_size_t)size+32>HOOK_JSON_LIMIT) return event_limit();
        char *encoded=apr_palloc(pool,(apr_size_t)size);
        apr_base64_encode_binary(encoded,(const unsigned char *)event->property_value->data,(int)event->property_value->len);
        /* Base64 is ASCII and needs no JSON escape expansion. */
        svn_stringbuf_appendcstr(buffer,",\"property_value_base64\":\"");
        svn_stringbuf_appendcstr(buffer,encoded); svn_stringbuf_appendbyte(buffer,'"');
    } else SVN_ERR(json_field(buffer,"property_value_base64",NULL));
    svn_stringbuf_appendcstr(buffer,"}\n");
    if(buffer->len>HOOK_JSON_LIMIT) return event_limit();
    *output=buffer; return SVN_NO_ERROR;
}

static wchar_t *wide(const char *value,apr_pool_t *pool)
{
    if(!value) value="";
    int size=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,value,-1,NULL,0);
    if(!size) return NULL;
    wchar_t *output=apr_palloc(pool,(apr_size_t)size*sizeof(*output));
    return MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,value,-1,output,size) ? output : NULL;
}

static wchar_t *command_line(const char *program,const apr_array_header_t *arguments,apr_pool_t *pool)
{
    int count=1+(arguments ? arguments->nelts : 0);
    wchar_t **values=apr_palloc(pool,(apr_size_t)count*sizeof(*values));
    apr_size_t required=1;
    for(int i=0;i<count;++i) {
        values[i]=wide(i ? APR_ARRAY_IDX(arguments,i-1,const char *) : program,pool);
        if(!values[i]) return NULL;
        required+=2*wcslen(values[i])+3;
    }
    if(required>32767) return NULL;
    wchar_t *output=apr_palloc(pool,required*sizeof(*output)),*p=output;
    for(int i=0;i<count;++i) {
        if(i) *p++=L' '; *p++=L'"';
        const wchar_t *value=values[i];
        for(;;) {
            apr_size_t slashes=0;
            while(*value==L'\\') { ++slashes; ++value; }
            apr_size_t n=(!*value || *value==L'"') ? slashes*2 : slashes;
            while(n--) *p++=L'\\';
            if(!*value) break;
            if(*value==L'"') *p++=L'\\';
            *p++=*value++;
        }
        *p++=L'"';
    }
    *p=0; return output;
}

static int compare_environment(const void *a,const void *b)
{
    return _wcsicmp(*(const wchar_t *const *)a,*(const wchar_t *const *)b);
}

static wchar_t *environment_block(const pgvn_policy *policy,const pgvn_hook_context *event,apr_pool_t *pool)
{
    wchar_t *values[32]; int count=0;
    const wchar_t *names[]={L"SystemRoot",L"WINDIR",L"TEMP",L"TMP",L"PATH",L"PGSERVICEFILE",L"PGPASSFILE",L"PGSYSCONFDIR"};
    for(size_t i=0;i<sizeof(names)/sizeof(names[0]);++i) {
        DWORD size=GetEnvironmentVariableW(names[i],NULL,0);
        if(!size) continue;
        if(size>32767) return NULL;
        size_t name_size=wcslen(names[i]);
        wchar_t *value=apr_palloc(pool,(name_size+size+1)*sizeof(*value));
        memcpy(value,names[i],name_size*sizeof(*value)); value[name_size]=L'=';
        if(!GetEnvironmentVariableW(names[i],value+name_size+1,size)) return NULL;
        values[count++]=value;
    }
    const char *extra[]={"PGVN_EVENT","PGVN_REPOSITORY_ID","PGVN_USER","PGVN_TRANSACTION","PGVN_REVISION","PGVN_CONFIG"};
    const char *data[]={pgvn_hook_name(event->event),event->repository_id,event->username,event->transaction_id,
        apr_psprintf(pool,"%ld",event->revision),policy->context->configuration_path};
    for(size_t i=0;i<sizeof(extra)/sizeof(extra[0]);++i) {
        values[count]=wide(apr_pstrcat(pool,extra[i],"=",data[i] ? data[i] : "",NULL),pool);
        if(!values[count++]) return NULL;
    }
    qsort(values,(size_t)count,sizeof(*values),compare_environment);
    size_t size=1;
    for(int i=0;i<count;++i) size+=wcslen(values[i])+1;
    if(size>32767) return NULL;
    wchar_t *block=apr_pcalloc(pool,size*sizeof(*block)),*p=block;
    for(int i=0;i<count;++i) { size_t length=wcslen(values[i])+1; memcpy(p,values[i],length*sizeof(*p)); p+=length; }
    return block;
}

typedef struct hook_io {
    HANDLE pipe;
    const char *input;
    size_t input_size;
    char output[HOOK_OUTPUT_LIMIT+1];
    size_t output_size;
    svn_boolean_t truncated;
} hook_io;

static DWORD WINAPI write_event(void *baton)
{
    hook_io *io=baton; size_t offset=0;
    while(offset<io->input_size) {
        DWORD size=(DWORD)((io->input_size-offset)>32768 ? 32768 : io->input_size-offset),written=0;
        if(!WriteFile(io->pipe,io->input+offset,size,&written,NULL) || !written) break;
        offset+=written;
    }
    CloseHandle(io->pipe); io->pipe=NULL; return 0;
}

static DWORD WINAPI read_output(void *baton)
{
    hook_io *io=baton; char buffer[4096]; DWORD size;
    while(ReadFile(io->pipe,buffer,sizeof(buffer),&size,NULL) && size) {
        size_t retain=size;
        if(retain>HOOK_OUTPUT_LIMIT-io->output_size) { retain=HOOK_OUTPUT_LIMIT-io->output_size; io->truncated=TRUE; }
        memcpy(io->output+io->output_size,buffer,retain); io->output_size+=retain;
    }
    io->output[io->output_size]=0;
    return 0;
}

static int executable_name(const char *name)
{
    return pgvn_hook_executable_valid(name, 1);
}

svn_error_t *pgvn_external_hook(const pgvn_policy *policy,const pgvn_hook_context *event,apr_pool_t *pool)
{
    const pgvn_hook_handler *handler=policy->external_hooks[event->event];
    if(!handler || !executable_name(handler->executable) || handler->timeout_seconds<1 || handler->timeout_seconds>60
        || (handler->arguments && handler->arguments->nelts>16) || !policy->context->hook_root)
        return svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN external hook is not configured for this node");
    svn_stringbuf_t *input;
    SVN_ERR(event_json(&input,event,pool));
    const char *program=apr_pstrcat(pool,policy->context->hook_root,"/",handler->executable,NULL);
    wchar_t *root=wide(policy->context->hook_root,pool),*application=wide(program,pool);
    wchar_t *line=command_line(program,handler->arguments,pool),*environment=environment_block(policy,event,pool);
    if(!root || !application || !line || !environment ||
        !((root[0] && root[1]==L':' && (root[2]==L'/' || root[2]==L'\\')) || (root[0]==L'\\' && root[1]==L'\\')))
        return svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN hook paths, arguments or environment are invalid");

    HANDLE program_file=INVALID_HANDLE_VALUE,root_directory=INVALID_HANDLE_VALUE,job=NULL;
    HANDLE child[3]={NULL,NULL,NULL},threads[3]={NULL,NULL,NULL};
    hook_io *io=apr_pcalloc(pool,3*sizeof(*io));
    PROCESS_INFORMATION process={0}; STARTUPINFOEXW startup={0}; BOOL attributes=FALSE;
    DWORD failure=0,exit_code=0; svn_error_t *error=NULL;
    root_directory=CreateFileW(root,FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,NULL,OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS,NULL);
    program_file=CreateFileW(application,GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,FILE_FLAG_OPEN_REPARSE_POINT,NULL);
    if(root_directory==INVALID_HANDLE_VALUE || program_file==INVALID_HANDLE_VALUE) { failure=GetLastError(); goto cleanup; }
    wchar_t *final_root=apr_palloc(pool,32768*sizeof(wchar_t)),*final_program=apr_palloc(pool,32768*sizeof(wchar_t));
    DWORD root_length=GetFinalPathNameByHandleW(root_directory,final_root,32768,0);
    DWORD program_length=GetFinalPathNameByHandleW(program_file,final_program,32768,0);
    BY_HANDLE_FILE_INFORMATION info;
    if(!root_length || root_length>=32768 || !program_length || program_length>=32768 ||
        !GetFileInformationByHandle(program_file,&info) || GetFileType(program_file)!=FILE_TYPE_DISK ||
        (info.dwFileAttributes&(FILE_ATTRIBUTE_DIRECTORY|FILE_ATTRIBUTE_REPARSE_POINT))) { failure=ERROR_INVALID_NAME; goto cleanup; }
    while(root_length && final_root[root_length-1]==L'\\') final_root[--root_length]=0;
    if(_wcsnicmp(final_program,final_root,root_length) || final_program[root_length]!=L'\\' ||
        wcschr(final_program+root_length+1,L'\\')) { failure=ERROR_ACCESS_DENIED; goto cleanup; }
    SECURITY_ATTRIBUTES security={sizeof(security),NULL,TRUE};
    for(int i=0;i<3;++i) {
        BOOL made=i ? CreatePipe(&io[i].pipe,&child[i],&security,8192) : CreatePipe(&child[i],&io[i].pipe,&security,8192);
        if(!made || !SetHandleInformation(io[i].pipe,HANDLE_FLAG_INHERIT,0)) { failure=GetLastError(); goto cleanup; }
    }
    startup.StartupInfo.cb=sizeof(startup); startup.StartupInfo.dwFlags=STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput=child[0]; startup.StartupInfo.hStdOutput=child[1]; startup.StartupInfo.hStdError=child[2];
    SIZE_T size=0; InitializeProcThreadAttributeList(NULL,1,0,&size);
    startup.lpAttributeList=apr_palloc(pool,size);
    if(!InitializeProcThreadAttributeList(startup.lpAttributeList,1,0,&size)) { failure=GetLastError(); goto cleanup; }
    attributes=TRUE;
    if(!UpdateProcThreadAttribute(startup.lpAttributeList,0,PROC_THREAD_ATTRIBUTE_HANDLE_LIST,child,sizeof(child),NULL,NULL)) { failure=GetLastError(); goto cleanup; }
    job=CreateJobObjectW(NULL,NULL);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits={0}; limits.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if(!job || !SetInformationJobObject(job,JobObjectExtendedLimitInformation,&limits,sizeof(limits))) { failure=GetLastError(); goto cleanup; }
    if(!CreateProcessW(application,line,NULL,NULL,TRUE,CREATE_SUSPENDED|CREATE_NO_WINDOW|CREATE_UNICODE_ENVIRONMENT|EXTENDED_STARTUPINFO_PRESENT,
        environment,root,&startup.StartupInfo,&process)) { failure=GetLastError(); goto cleanup; }
    if(!AssignProcessToJobObject(job,process.hProcess)) { failure=GetLastError(); TerminateProcess(process.hProcess,1); goto cleanup; }
    for(int i=0;i<3;++i) { CloseHandle(child[i]); child[i]=NULL; }
    io[0].input=input->data; io[0].input_size=input->len;
    threads[0]=CreateThread(NULL,0,write_event,&io[0],0,NULL);
    threads[1]=CreateThread(NULL,0,read_output,&io[1],0,NULL);
    threads[2]=CreateThread(NULL,0,read_output,&io[2],0,NULL);
    if(!threads[0] || !threads[1] || !threads[2] || ResumeThread(process.hThread)==(DWORD)-1) { failure=GetLastError(); goto cleanup; }
    ULONGLONG deadline=GetTickCount64()+(ULONGLONG)handler->timeout_seconds*1000;
    for(;;) {
        DWORD state=WaitForSingleObject(process.hProcess,50);
        if(state==WAIT_OBJECT_0) break;
        if(state==WAIT_FAILED) { failure=GetLastError(); goto cleanup; }
        if(policy->context->cancellation_event && WaitForSingleObject(policy->context->cancellation_event,0)==WAIT_OBJECT_0) {
            error=svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN hook cancelled during service shutdown"); goto cleanup;
        }
        if(GetTickCount64()>=deadline) { error=svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN hook exceeded its timeout"); goto cleanup; }
    }
    if(!GetExitCodeProcess(process.hProcess,&exit_code)) failure=GetLastError();
cleanup:
    if(job) CloseHandle(job);
    for(int i=0;i<3;++i) if(child[i]) CloseHandle(child[i]);
    for(int i=0;i<3;++i) if(threads[i]) {
        if(WaitForSingleObject(threads[i],5000)==WAIT_TIMEOUT) CancelSynchronousIo(threads[i]);
        WaitForSingleObject(threads[i],INFINITE); CloseHandle(threads[i]);
    }
    for(int i=0;i<3;++i) if(io[i].pipe) CloseHandle(io[i].pipe);
    if(process.hProcess) { WaitForSingleObject(process.hProcess,5000); CloseHandle(process.hProcess); }
    if(process.hThread) CloseHandle(process.hThread);
    if(attributes) DeleteProcThreadAttributeList(startup.lpAttributeList);
    if(program_file!=INVALID_HANDLE_VALUE) CloseHandle(program_file);
    if(root_directory!=INVALID_HANDLE_VALUE) CloseHandle(root_directory);
    if(failure) return svn_error_createf(SVN_ERR_REPOS_HOOK_FAILURE,error,"PgVN %s hook could not run (Win32 %lu)",pgvn_hook_name(event->event),(unsigned long)failure);
    if(error) return error;
    if(exit_code) {
        const char *message=io[2].output;
        if(strlen(message)!=io[2].output_size || !svn_utf__is_valid(message,io[2].output_size)) message="(non-text output omitted)";
        const char *name=pgvn_hook_name(event->event),*action=NULL;
        if(event->event==PGVN_HOOK_START_COMMIT || event->event==PGVN_HOOK_PRE_COMMIT) action="Commit";
        else if(event->event==PGVN_HOOK_PRE_REVPROP) action="Revprop change";
        else if(event->event==PGVN_HOOK_PRE_LOCK) action="Lock";
        else if(event->event==PGVN_HOOK_PRE_UNLOCK) action="Unlock";
        const char *description=action ?
            apr_psprintf(pool,"%s blocked by %s hook (exit code %d)",action,name,(int)exit_code) :
            apr_psprintf(pool,"%s hook failed (exit code %d)",name,(int)exit_code);
        return svn_error_createf(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"%s%s%s",description,
            *message ? " with output:\n" : " with no output.",message);
    }
    if(event->lock_token_result) {
        size_t start=0,end=io[1].output_size;
        while(start<end && strchr(" \t\r\n",io[1].output[start])) ++start;
        while(end>start && strchr(" \t\r\n",io[1].output[end-1])) --end;
        if(io[1].truncated || end-start>4096 || memchr(io[1].output,0,io[1].output_size) ||
            memchr(io[1].output+start,'\n',end-start) || memchr(io[1].output+start,'\r',end-start) || !svn_utf__is_valid(io[1].output+start,end-start))
            return svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN pre-lock hook returned an invalid token");
        *event->lock_token_result=apr_pstrmemdup(pool,io[1].output+start,end-start);
    }
    return SVN_NO_ERROR;
}
