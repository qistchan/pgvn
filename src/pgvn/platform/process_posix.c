#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "process.h"
#include "executable_posix.h"
#include <apr_strings.h>
#include <stdio.h>
#include <signal.h>
#include <spawn.h>
#include <poll.h>
#include <sys/wait.h>
extern char **environ;
const char *pgvn_process_helper(apr_pool_t *pool)
{
    char path[8193]; ssize_t size=readlink("/proc/self/exe",path,sizeof(path)-1);
    if(size<1 || size>=(ssize_t)sizeof(path)-1) return NULL;
    path[size]=0; char *slash=strrchr(path,'/'); if(!slash) return NULL;
    slash[1]=0; return apr_pstrcat(pool,path,"pgvn-hook-runner",NULL);
}
static void close_fd(int *fd) { if(*fd>=0) { close(*fd); *fd=-1; } }
static void drain(int *fd,struct pgvn_process_output *output)
{
    char buffer[4096];
    for(int i=0;i<16 && *fd>=0;++i) {
        ssize_t size=read(*fd,buffer,sizeof(buffer));
        if(size>0) {
            size_t retain=(size_t)size;
            if(retain>PGVN_PROCESS_OUTPUT_LIMIT-output->size) { retain=PGVN_PROCESS_OUTPUT_LIMIT-output->size; output->truncated=1; }
            memcpy(output->bytes+output->size,buffer,retain); output->size+=retain; output->bytes[output->size]=0;
        } else if(!size || (errno!=EINTR && errno!=EAGAIN && errno!=EWOULDBLOCK)) close_fd(fd);
        else if(errno!=EINTR) break;
    }
}
svn_error_t *pgvn_process_run(pgvn_process_result *result,const char *helper,
    const char *const *arguments,char *const *environment,const char *input,size_t input_size,
    pgvn_cancel_event cancel,apr_pool_t *pool)
{
    (void)pool; memset(result,0,sizeof(*result));
    int pipes[4][2]={{-1,-1},{-1,-1},{-1,-1},{-1,-1}},program=-1,status=0,failure=0;
    pid_t pid=-1; posix_spawn_file_actions_t actions; posix_spawnattr_t attributes;
    int actions_ready=0,attributes_ready=0,mask_ready=0,had_pipe=0;
    sigset_t blocked,previous,pending; sigemptyset(&blocked); sigaddset(&blocked,SIGPIPE);
    if(pthread_sigmask(SIG_BLOCK,&blocked,&previous)) { failure=EINVAL; goto cleanup; }
    mask_ready=1; sigpending(&pending); had_pipe=sigismember(&pending,SIGPIPE);
    if(!helper) helper=pgvn_process_helper(pool);
    program=pgvn_program_open(helper,NULL);
    if(program<0) { failure=errno; goto cleanup; }
    unsigned char magic[4];
    if(pread(program,magic,sizeof(magic),0)!=4 || memcmp(magic,"\177ELF",4)) { failure=ENOEXEC; goto cleanup; }
    int safe_program=fcntl(program,F_DUPFD_CLOEXEC,10); close_fd(&program); program=safe_program;
    if(program<0) { failure=errno; goto cleanup; }
    for(int i=input ? 0 : 3;i<4;++i) if(pipe2(pipes[i],O_CLOEXEC)) { failure=errno; goto cleanup; }
    if((failure=posix_spawn_file_actions_init(&actions))) goto cleanup;
    actions_ready=1;
    if((failure=posix_spawnattr_init(&attributes))) goto cleanup;
    attributes_ready=1;
    if(input) for(int i=0;i<3;++i)
        if((failure=posix_spawn_file_actions_adddup2(&actions,pipes[i][i ? 1 : 0],i))) goto cleanup;
    if((failure=posix_spawn_file_actions_adddup2(&actions,pipes[3][0],3))) goto cleanup;
    sigset_t defaults,empty; sigemptyset(&empty); sigemptyset(&defaults);
    sigaddset(&defaults,SIGPIPE); sigaddset(&defaults,SIGCHLD); sigaddset(&defaults,SIGTERM); sigaddset(&defaults,SIGINT);
    if((failure=posix_spawnattr_setsigmask(&attributes,&empty)) ||
       (failure=posix_spawnattr_setsigdefault(&attributes,&defaults)) ||
       (failure=posix_spawnattr_setflags(&attributes,POSIX_SPAWN_SETSIGMASK|POSIX_SPAWN_SETSIGDEF))) goto cleanup;
    char executable[64]; snprintf(executable,sizeof(executable),"/proc/self/fd/%d",program);
    failure=posix_spawn(&pid,executable,&actions,&attributes,(char *const *)arguments,environment ? environment : environ);
    if(failure) { pid=-1; goto cleanup; }
    close_fd(&program); close_fd(&pipes[3][0]);
    for(int i=0;i<3 && input;++i) {
        close_fd(&pipes[i][i ? 1 : 0]); int fd=pipes[i][i ? 0 : 1];
        if(fcntl(fd,F_SETFL,fcntl(fd,F_GETFL)|O_NONBLOCK)) { failure=errno; break; }
    }
    size_t offset=0; int done=0,notified=0;
    while(!done || (input && (pipes[1][0]>=0 || pipes[2][0]>=0))) {
        if(!notified && (failure || pgvn_cancel_is_set(cancel))) {
            result->cancelled=pgvn_cancel_is_set(cancel); notified=1; close_fd(&pipes[3][1]);
        }
        if(input) {
            if(pipes[0][1]>=0 && offset<input_size) {
                size_t chunk=input_size-offset; if(chunk>32768) chunk=32768;
                ssize_t written=write(pipes[0][1],input+offset,chunk);
                if(written>0) offset+=(size_t)written;
                else if(written<0 && errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) close_fd(&pipes[0][1]);
            }
            if(offset==input_size || done) close_fd(&pipes[0][1]);
            if(pipes[1][0]>=0) drain(&pipes[1][0],&result->output);
            if(pipes[2][0]>=0) drain(&pipes[2][0],&result->error);
        }
        if(!done) {
            pid_t reaped=waitpid(pid,&status,WNOHANG);
            if(reaped==pid) done=1;
            else if(reaped<0 && errno!=EINTR) { failure=errno; done=1; }
        }
        struct pollfd descriptors[3]; nfds_t count=0;
        if(input) for(int i=0;i<3;++i) {
            int fd=pipes[i][i ? 0 : 1];
            if(fd>=0) descriptors[count++]=(struct pollfd){fd,i ? POLLIN : POLLOUT,0};
        }
        if(!done || count) poll(descriptors,count,25);
    }
    result->exit_code=WIFEXITED(status) ? WEXITSTATUS(status) : WIFSIGNALED(status) ? 128+WTERMSIG(status) : 125;
cleanup:
    close_fd(&program);
    for(int i=0;i<4;++i) for(int j=0;j<2;++j) close_fd(&pipes[i][j]);
    if(attributes_ready) posix_spawnattr_destroy(&attributes);
    if(actions_ready) posix_spawn_file_actions_destroy(&actions);
    if(mask_ready) {
        if(!sigismember(&previous,SIGPIPE) && !had_pipe) {
            struct timespec immediate={0,0}; while(sigtimedwait(&blocked,NULL,&immediate)>=0) { }
        }
        pthread_sigmask(SIG_SETMASK,&previous,NULL);
    }
    return failure ? svn_error_wrap_apr(failure,"PgVN supervised process failed") : SVN_NO_ERROR;
}
