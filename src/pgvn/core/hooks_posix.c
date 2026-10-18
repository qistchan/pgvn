#define _GNU_SOURCE
#ifndef _WIN32
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/vfs.h>
#include <time.h>
#include <unistd.h>
#include <linux/magic.h>
#include <linux/close_range.h>
#include <sys/random.h>
#include "internal.h"
#include "hooks_common.h"
#include "private/svn_utf_private.h"

#define HOOK_OUTPUT_LIMIT 8192
#define HOOK_OUTPUT_DRAIN_BYTES (64u*1024u)
#define HOOK_OUTPUT_DRAIN_READS 16u
#define HOOK_ARGUMENT_LIMIT 16
#define HOOK_ARGUMENT_BYTES 2048
#define HOOK_EXEC_LIMIT (128u*1024u)
#define HOOK_CLEANUP_MS 5000

typedef struct hook_output {
    char data[HOOK_OUTPUT_LIMIT+1];
    size_t size;
    svn_boolean_t truncated;
    int fd;
} hook_output;

typedef struct hook_cgroup {
    int parent_fd;
    int group_fd;
    int processes_fd;
    int kill_fd;
    int events_fd;
    char name[64];
} hook_cgroup;

static svn_error_t *hook_failure(apr_pool_t *pool,const char *message)
{
    return svn_error_createf(SVN_ERR_REPOS_HOOK_FAILURE,NULL,"PgVN external hook %s: %s",
        message,strerror(errno));
}

static svn_error_t *hook_message(const char *message)
{
    return svn_error_create(SVN_ERR_REPOS_HOOK_FAILURE,NULL,message);
}

static int descriptor_above_stdio(int descriptor)
{
    if(descriptor<0) return -1;
    if(descriptor>=3) return descriptor;
    int replacement=fcntl(descriptor,F_DUPFD_CLOEXEC,3);
    int saved=errno;
    close(descriptor);
    errno=saved;
    return replacement;
}

static svn_boolean_t valid_utf8(const char *value)
{
    return value && svn_utf__is_valid(value,strlen(value));
}

static int open_absolute_directory(const char *path)
{
    if(!path || path[0]!='/' || strlen(path)>4096 || !valid_utf8(path)) { errno=EINVAL; return -1; }
    char normalized[4097];
    size_t length=strlen(path);
    while(length>1 && path[length-1]=='/') --length;
    memcpy(normalized,path,length);
    normalized[length]=0;
    int directory=open("/",O_RDONLY|O_DIRECTORY|O_CLOEXEC);
    if(directory<0) return -1;
    const char *part=normalized+1;
    while(*part) {
        const char *end=strchr(part,'/');
        size_t size=end ? (size_t)(end-part) : strlen(part);
        if(!size || (size==1 && part[0]=='.') || (size==2 && part[0]=='.' && part[1]=='.') || size>NAME_MAX) {
            close(directory); errno=EINVAL; return -1;
        }
        char name[NAME_MAX+1];
        memcpy(name,part,size); name[size]=0;
        int next=openat(directory,name,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        int saved=errno;
        close(directory);
        if(next<0) { errno=saved; return -1; }
        directory=next;
        if(!end) break;
        part=end+1;
    }
    return descriptor_above_stdio(directory);
}

static int write_all(int fd,const char *data,size_t size)
{
    size_t offset=0;
    while(offset<size) {
        ssize_t count=write(fd,data+offset,size-offset);
        if(count>0) offset+=(size_t)count;
        else if(count<0 && errno==EINTR) continue;
        else return -1;
    }
    return 0;
}

static int cgroup_populated(int fd)
{
    char buffer[256];
    if(lseek(fd,0,SEEK_SET)<0) return -1;
    ssize_t size=read(fd,buffer,sizeof(buffer)-1);
    if(size<0) return -1;
    buffer[size]=0;
    const char *value=strstr(buffer,"populated ");
    if(!value || (value[10]!='0' && value[10]!='1')) { errno=EPROTO; return -1; }
    return value[10]-'0';
}

static int cgroup_open(hook_cgroup *group,const char *path)
{
    memset(group,0,sizeof(*group));
    group->parent_fd=group->group_fd=group->processes_fd=group->kill_fd=group->events_fd=-1;
    group->parent_fd=open_absolute_directory(path);
    if(group->parent_fd<0) return -1;
    struct statfs filesystem;
    if(fstatfs(group->parent_fd,&filesystem)) return -1;
    if((unsigned long)filesystem.f_type!=(unsigned long)CGROUP2_SUPER_MAGIC) {
        errno=ENODEV;
        return -1;
    }
    int check=openat(group->parent_fd,"cgroup.kill",O_WRONLY|O_CLOEXEC|O_NOFOLLOW);
    if(check<0) return -1;
    close(check);

    for(unsigned attempt=0;attempt<32;++attempt) {
        uint64_t random=0;
        ssize_t got=getrandom(&random,sizeof(random),0);
        if(got!=(ssize_t)sizeof(random)) { if(got>=0) errno=EIO; return -1; }
        snprintf(group->name,sizeof(group->name),"pgvn-hook-%ld-%016llx",(long)getpid(),
            (unsigned long long)random);
        if(!mkdirat(group->parent_fd,group->name,0700)) break;
        if(errno!=EEXIST) { group->name[0]=0; return -1; }
        group->name[0]=0;
    }
    if(!group->name[0]) { errno=EEXIST; return -1; }
    group->group_fd=descriptor_above_stdio(openat(group->parent_fd,group->name,
        O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC));
    if(group->group_fd<0) return -1;
    group->processes_fd=descriptor_above_stdio(openat(group->group_fd,"cgroup.procs",
        O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
    group->kill_fd=descriptor_above_stdio(openat(group->group_fd,"cgroup.kill",
        O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
    group->events_fd=descriptor_above_stdio(openat(group->group_fd,"cgroup.events",
        O_RDONLY|O_CLOEXEC|O_NOFOLLOW));
    if(group->processes_fd<0 || group->kill_fd<0 || group->events_fd<0 ||
        write_all(group->kill_fd,"1",1)) return -1;
    return 0;
}

static int cgroup_cleanup(hook_cgroup *group,pid_t child)
{
    int failed=0;
    int saved_error=0;
    struct timespec now;
    if(group->kill_fd>=0 && write_all(group->kill_fd,"1",1)) {
        failed=1;
        saved_error=errno;
    }
    if(group->name[0] && group->events_fd>=0) {
        uint64_t deadline=0;
        int timed=clock_gettime(CLOCK_MONOTONIC,&now)==0;
        unsigned attempts=0;
        if(timed)
            deadline=(uint64_t)now.tv_sec*1000+(uint64_t)now.tv_nsec/1000000+HOOK_CLEANUP_MS;
        else if(!failed) {
            failed=1;
            saved_error=errno;
        }
        int empty=0,reaped=child<=0;
        int status;
        if(failed && child>0) (void)kill(child,SIGKILL);
        while(!empty || !reaped) {
            pid_t waited;
            if(!reaped) {
                waited=waitpid(child,&status,WNOHANG);
                if(waited==child || (waited<0 && errno==ECHILD)) reaped=1;
                else if(waited<0 && errno!=EINTR) {
                    if(!failed) saved_error=errno;
                    failed=1;
                }
            }
            int populated=cgroup_populated(group->events_fd);
            if(populated<0) {
                if(!failed) saved_error=errno;
                failed=1;
            }
            else empty=!populated;
            if(empty && reaped) break;
            if(timed) {
                if(clock_gettime(CLOCK_MONOTONIC,&now)) {
                    timed=0;
                    if(!failed) saved_error=errno;
                    failed=1;
                } else {
                    uint64_t current=(uint64_t)now.tv_sec*1000+(uint64_t)now.tv_nsec/1000000;
                    if(current>=deadline) {
                        if(!failed) saved_error=ETIMEDOUT;
                        failed=1;
                        break;
                    }
                }
            } else if(++attempts>=HOOK_CLEANUP_MS/50) break;
            struct timespec pause={0,50*1000*1000};
            nanosleep(&pause,NULL);
        }
        if(empty && reaped && unlinkat(group->parent_fd,group->name,AT_REMOVEDIR)) {
            if(!failed) saved_error=errno;
            failed=1;
        } else if(!empty || !reaped) {
            if(!failed) saved_error=ETIMEDOUT;
            failed=1;
        }
    } else if(group->name[0] && group->parent_fd>=0) {
        if(group->group_fd>=0) { close(group->group_fd); group->group_fd=-1; }
        if(unlinkat(group->parent_fd,group->name,AT_REMOVEDIR)) {
            if(!failed) saved_error=errno;
            failed=1;
        }
    } else if(child>0) {
        int status,reaped=0,timed=clock_gettime(CLOCK_MONOTONIC,&now)==0;
        uint64_t deadline=timed ? (uint64_t)now.tv_sec*1000+(uint64_t)now.tv_nsec/1000000+HOOK_CLEANUP_MS : 0;
        unsigned attempts=0;
        (void)kill(child,SIGKILL);
        while(!reaped) {
            pid_t waited=waitpid(child,&status,WNOHANG);
            if(waited==child || (waited<0 && errno==ECHILD)) reaped=1;
            else if(waited<0 && errno!=EINTR) {
                if(!failed) saved_error=errno;
                failed=1;
                break;
            }
            if(reaped) break;
            if(timed && clock_gettime(CLOCK_MONOTONIC,&now)) timed=0;
            if((timed && (uint64_t)now.tv_sec*1000+(uint64_t)now.tv_nsec/1000000>=deadline) ||
                (!timed && ++attempts>=HOOK_CLEANUP_MS/50)) {
                if(!failed) saved_error=ETIMEDOUT;
                failed=1;
                break;
            }
            struct timespec pause={0,50*1000*1000};
            nanosleep(&pause,NULL);
        }
    }
    if(group->processes_fd>=0) close(group->processes_fd);
    if(group->events_fd>=0) close(group->events_fd);
    if(group->kill_fd>=0) close(group->kill_fd);
    if(group->group_fd>=0) close(group->group_fd);
    if(group->parent_fd>=0) close(group->parent_fd);
    group->events_fd=group->kill_fd=group->processes_fd=group->group_fd=group->parent_fd=-1;
    if(failed) errno=saved_error ? saved_error : EIO;
    return failed ? -1 : 0;
}

static int cgroup_kill(hook_cgroup *group)
{
    return group->kill_fd>=0 ? write_all(group->kill_fd,"1",1) : (errno=EBADF,-1);
}

static int set_nonblocking(int fd,svn_boolean_t enabled)
{
    int flags=fcntl(fd,F_GETFL);
    return flags<0 ? -1 : fcntl(fd,F_SETFL,enabled ? flags|O_NONBLOCK : flags&~O_NONBLOCK);
}

static int make_pipe(int descriptors[2])
{
    if(pipe2(descriptors,O_CLOEXEC|O_NONBLOCK)) return -1;
    for(int i=0;i<2;++i) {
        descriptors[i]=descriptor_above_stdio(descriptors[i]);
        if(descriptors[i]<0) {
            int saved=errno;
            for(int j=0;j<2;++j) if(descriptors[j]>=0) close(descriptors[j]);
            descriptors[0]=descriptors[1]=-1;
            errno=saved;
            return -1;
        }
    }
    return 0;
}

static int make_environment(char ***output,const pgvn_policy *policy,
    const pgvn_hook_context *event,apr_pool_t *pool)
{
    const char *names[]={"PATH","LANG","LC_ALL","TMPDIR","PGSERVICEFILE","PGPASSFILE","PGSYSCONFDIR"};
    const char *values[16]; size_t count=0,total=0;
    for(size_t i=0;i<sizeof(names)/sizeof(names[0]);++i) {
        const char *value=getenv(names[i]);
        if(!value && !strcmp(names[i],"PATH")) value="/usr/bin:/bin";
        if(!value) continue;
        size_t size=strlen(value);
        if(size>4096 || !svn_utf__is_valid(value,size)) return 0;
        values[count++]=apr_psprintf(pool,"%s=%s",names[i],value);
    }
    const char *extra[]={"PGVN_EVENT","PGVN_REPOSITORY_ID","PGVN_USER","PGVN_TRANSACTION",
        "PGVN_REVISION","PGVN_CONFIG"};
    const char *data[]={pgvn_hook_name(event->event),event->repository_id,event->username,
        event->transaction_id,apr_psprintf(pool,"%ld",event->revision),policy->context->configuration_path};
    for(size_t i=0;i<sizeof(extra)/sizeof(extra[0]);++i) {
        if(data[i] && !valid_utf8(data[i])) return 0;
        values[count++]=apr_psprintf(pool,"%s=%s",extra[i],data[i] ? data[i] : "");
    }
    char **environment=apr_pcalloc(pool,(count+1)*sizeof(*environment));
    for(size_t i=0;i<count;++i) {
        size_t size=strlen(values[i])+1;
        if(size>HOOK_EXEC_LIMIT-total) return 0;
        total+=size;
        environment[i]=apr_pstrdup(pool,values[i]);
    }
    *output=environment;
    return 1;
}

static int child_close_unrelated(int first,int second,int third,int maximum)
{
    int keep[]={first,second,third};
    for(int i=0;i<3;++i) for(int j=i+1;j<3;++j) if(keep[j]<keep[i]) {
        int swap=keep[i]; keep[i]=keep[j]; keep[j]=swap;
    }
    unsigned int start=3;
    for(int i=0;i<3;++i) {
        if(keep[i]>(int)start && syscall(SYS_close_range,start,(unsigned int)keep[i]-1,0)<0 && errno!=ENOSYS)
            return -1;
        start=(unsigned int)keep[i]+1;
    }
    if(syscall(SYS_close_range,start,~0U,0)<0) {
        if(errno!=ENOSYS) return -1;
        for(int fd=3;fd<maximum;++fd)
            if(fd!=first && fd!=second && fd!=third) close(fd);
    }
    return 0;
}

static void child_fail(int error_fd,int error)
{
    ssize_t written=write(error_fd,&error,sizeof(error));
    (void)written;
    _exit(127);
}

static void launch_child(const int input[2],const int output[2],const int errors[2],
    const int gate[2],const int exec_status[2],int executable,int root,
    char *const argv[],char *const environment[],int maximum)
{
    close(input[1]); close(output[0]); close(errors[0]); close(gate[1]); close(exec_status[0]);
    if(dup2(input[0],STDIN_FILENO)<0 || dup2(output[1],STDOUT_FILENO)<0 ||
        dup2(errors[1],STDERR_FILENO)<0 || fchdir(root)<0) child_fail(exec_status[1],errno);
    if(sigaction(SIGPIPE,&(struct sigaction){.sa_handler=SIG_DFL},NULL)) child_fail(exec_status[1],errno);
    sigset_t empty;
    sigemptyset(&empty);
    if(sigprocmask(SIG_SETMASK,&empty,NULL)) child_fail(exec_status[1],errno);
    if(fcntl(executable,F_SETFD,0)<0) child_fail(exec_status[1],errno);
    if(child_close_unrelated(gate[0],executable,exec_status[1],maximum)) child_fail(exec_status[1],errno);
    char start;
    ssize_t count;
    do count=read(gate[0],&start,1); while(count<0 && errno==EINTR);
    if(count!=1) child_fail(exec_status[1],ECANCELED);
    close(gate[0]);
    fexecve(executable,argv,environment);
    child_fail(exec_status[1],errno);
}

static int capture_output(hook_output *output)
{
    char buffer[4096];
    size_t remaining=HOOK_OUTPUT_DRAIN_BYTES;
    unsigned reads=0;
    while(remaining && reads<HOOK_OUTPUT_DRAIN_READS) {
        size_t requested=remaining<sizeof(buffer) ? remaining : sizeof(buffer);
        ++reads;
        ssize_t size=read(output->fd,buffer,requested);
        if(size>0) {
            size_t retain=(size_t)size;
            if(retain>HOOK_OUTPUT_LIMIT-output->size) {
                retain=HOOK_OUTPUT_LIMIT-output->size;
                output->truncated=TRUE;
            }
            memcpy(output->data+output->size,buffer,retain);
            output->size+=retain;
            remaining-=(size_t)size;
        } else if(!size) {
            close(output->fd); output->fd=-1; return 0;
        } else if(errno==EINTR) return 0;
        else if(errno==EAGAIN || errno==EWOULDBLOCK) return 0;
        else { close(output->fd); output->fd=-1; return -1; }
    }
    return 0;
}

static int read_exec_status(int *fd,int *child_error)
{
    ssize_t size=read(*fd,child_error,sizeof(*child_error));
    if(size==(ssize_t)sizeof(*child_error)) { close(*fd); *fd=-1; return 1; }
    if(size==0) { close(*fd); *fd=-1; return 2; }
    if(size<0 && (errno==EAGAIN || errno==EWOULDBLOCK)) return 0;
    if(size<0 && errno==EINTR) return 0;
    if(size<0) return -1;
    errno=EPROTO;
    return -1;
}

static void close_pipe(int descriptors[2])
{
    if(descriptors[0]>=0) close(descriptors[0]);
    if(descriptors[1]>=0) close(descriptors[1]);
    descriptors[0]=descriptors[1]=-1;
}

static void consume_generated_sigpipe(svn_boolean_t pipe_was_pending,
    svn_boolean_t pipe_write_error,const sigset_t *blocked)
{
    sigset_t pending;
    if(!pipe_was_pending && pipe_write_error && !sigpending(&pending) &&
        sigismember(&pending,SIGPIPE)) {
        struct timespec immediate={0,0};
        (void)sigtimedwait(blocked,NULL,&immediate);
    }
}

svn_error_t *pgvn_external_hook(const pgvn_policy *policy,const pgvn_hook_context *event,apr_pool_t *pool)
{
    const pgvn_hook_handler *handler=policy && event && event->event>=0 && event->event<=PGVN_HOOK_POST_UNLOCK
        ? policy->external_hooks[event->event] : NULL;
    if(!handler || !handler->executable || !pgvn_hook_filename_valid(handler->executable,FALSE) ||
        handler->timeout_seconds<1 || handler->timeout_seconds>60 ||
        (handler->arguments && (handler->arguments->nelts<0 || handler->arguments->nelts>HOOK_ARGUMENT_LIMIT)) ||
        !policy->context->hook_root || !policy->context->hook_cgroup_root)
        return hook_message("is not fully configured for POSIX dispatch");

    svn_stringbuf_t *input;
    SVN_ERR(pgvn_hook_event_json(&input,event,pool));
    char **environment=NULL;
    if(!make_environment(&environment,policy,event,pool))
        return hook_message("arguments or environment exceed UTF-8 resource limits");
    size_t argument_count=handler->arguments ? (size_t)handler->arguments->nelts : 0;
    char **argv=apr_pcalloc(pool,(argument_count+2)*sizeof(*argv));
    size_t exec_size=0;
    argv[0]=apr_pstrdup(pool,handler->executable);
    exec_size=strlen(argv[0])+1;
    for(size_t i=0;i<argument_count;++i) {
        const char *argument=APR_ARRAY_IDX(handler->arguments,(int)i,const char *);
        size_t size=argument ? strlen(argument) : 0;
        if(!argument || size>HOOK_ARGUMENT_BYTES || !svn_utf__is_valid(argument,size) ||
            size+1>HOOK_EXEC_LIMIT-exec_size)
            return hook_message("arguments exceed the UTF-8 resource limits");
        exec_size+=size+1;
        argv[i+1]=apr_pstrdup(pool,argument);
    }
    for(char **entry=environment;*entry;++entry) {
        size_t size=strlen(*entry)+1;
        if(size>HOOK_EXEC_LIMIT-exec_size) return hook_message("environment exceeds the execution size limit");
        exec_size+=size;
    }
    argv[argument_count+1]=NULL;

    int root=open_absolute_directory(policy->context->hook_root);
    if(root<0) return hook_failure(pool,"could not securely open hook-root");
    int executable=descriptor_above_stdio(openat(root,handler->executable,
        O_RDONLY|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC));
    struct stat executable_info;
    int executable_error=0;
    if(executable<0) executable_error=errno;
    else if(fstat(executable,&executable_info)) executable_error=errno;
    else if(!S_ISREG(executable_info.st_mode) || !(executable_info.st_mode&0111)) executable_error=EACCES;
    if(executable_error) {
        if(executable>=0) close(executable);
        close(root);
        errno=executable_error;
        return hook_failure(pool,"is missing, non-executable, or not a regular file");
    }

    hook_cgroup group;
    if(cgroup_open(&group,policy->context->hook_cgroup_root)) {
        int saved=errno;
        if(group.parent_fd>=0 || group.group_fd>=0) (void)cgroup_cleanup(&group,0);
        close(executable); close(root); errno=saved;
        return hook_failure(pool,"requires a writable cgroup v2 hook-cgroup-root with cgroup.kill");
    }

    int input_pipe[2]={-1,-1},output_pipe[2]={-1,-1},error_pipe[2]={-1,-1};
    int gate[2]={-1,-1},exec_status[2]={-1,-1};
    hook_output stdout_output={.fd=-1},stderr_output={.fd=-1};
    pid_t child=-1;
    svn_error_t *error=NULL;
    const char *failure=NULL;
    int status=0,child_error=0,exec_complete=0,exec_failed=0;
    size_t input_offset=0;
    svn_boolean_t child_done=FALSE,killed=FALSE,pipe_write_error=FALSE;
    char placement_failure[128];
    sigset_t blocked,old_mask,pending;
    svn_boolean_t mask_blocked=FALSE,pipe_was_pending=FALSE;
    struct timespec start_time,now;
    int max_fd=(int)sysconf(_SC_OPEN_MAX);
    if(max_fd<0 || max_fd>1048576) max_fd=65536;
    if(make_pipe(input_pipe) || make_pipe(output_pipe) || make_pipe(error_pipe) ||
        make_pipe(gate) || make_pipe(exec_status)) {
        failure="could not create process pipes";
        goto done;
    }
    if(pgvn_cancel_is_set(policy->context->cancellation_event)) {
        error=hook_message("was cancelled during service shutdown");
        goto done;
    }
    if(set_nonblocking(input_pipe[0],FALSE) || set_nonblocking(output_pipe[1],FALSE) ||
        set_nonblocking(error_pipe[1],FALSE) || set_nonblocking(gate[0],FALSE)) {
        failure="could not prepare process pipes";
        goto done;
    }
    sigemptyset(&blocked); sigaddset(&blocked,SIGPIPE);
    if(pthread_sigmask(SIG_BLOCK,&blocked,&old_mask)) {
        failure="could not safely prepare hook input delivery";
        goto done;
    }
    mask_blocked=TRUE;
    if(sigpending(&pending)) {
        failure="could not inspect pending hook input signals";
        goto done;
    }
    pipe_was_pending=sigismember(&pending,SIGPIPE);
    stdout_output.fd=output_pipe[0]; output_pipe[0]=-1;
    stderr_output.fd=error_pipe[0]; error_pipe[0]=-1;
    child=fork();
    if(child<0) { failure="could not fork hook process"; goto done; }
    if(!child) launch_child(input_pipe,output_pipe,error_pipe,gate,exec_status,
        executable,root,argv,environment,max_fd);

    close(input_pipe[0]); input_pipe[0]=-1;
    close(output_pipe[1]); output_pipe[1]=-1;
    close(error_pipe[1]); error_pipe[1]=-1;
    close(gate[0]); gate[0]=-1;
    close(exec_status[1]); exec_status[1]=-1;
    char pid_text[32];
    int pid_size=snprintf(pid_text,sizeof(pid_text),"%ld",(long)child);
    if(pid_size>0 && (size_t)pid_size<sizeof(pid_text)) pid_text[pid_size++]='\n';
    if(group.processes_fd<0 || write_all(group.processes_fd,pid_text,(size_t)pid_size)) {
        snprintf(placement_failure,sizeof(placement_failure),
            "could not place hook process inside its cgroup: %s",strerror(errno));
        failure=placement_failure;
        goto done;
    }
    if(pgvn_cancel_is_set(policy->context->cancellation_event)) {
        error=hook_message("was cancelled during service shutdown");
        goto done;
    }
    if(write_all(gate[1],"1",1)) {
        if(errno==EPIPE) pipe_write_error=TRUE;
        failure="could not release hook process";
        goto done;
    }
    close(gate[1]); gate[1]=-1;
    if(clock_gettime(CLOCK_MONOTONIC,&start_time)) { failure="could not start hook deadline"; goto done; }
    uint64_t deadline=(uint64_t)start_time.tv_sec*1000+
        (uint64_t)start_time.tv_nsec/1000000+(uint64_t)handler->timeout_seconds*1000;
    for(;;) {
        struct pollfd descriptors[4];
        nfds_t count=0;
        if(input_pipe[1]>=0) descriptors[count++]=(struct pollfd){input_pipe[1],POLLOUT,0};
        if(stdout_output.fd>=0) descriptors[count++]=(struct pollfd){stdout_output.fd,POLLIN|POLLHUP,0};
        if(stderr_output.fd>=0) descriptors[count++]=(struct pollfd){stderr_output.fd,POLLIN|POLLHUP,0};
        if(exec_status[0]>=0) descriptors[count++]=(struct pollfd){exec_status[0],POLLIN|POLLHUP,0};
        int polled=poll(descriptors,count,50);
        if(polled<0 && errno!=EINTR) { failure="failed while polling hook process"; break; }
        for(nfds_t i=0;i<count;++i) {
            if(!descriptors[i].revents) continue;
            if(descriptors[i].fd==input_pipe[1] && (descriptors[i].revents&(POLLOUT|POLLERR|POLLHUP))) {
                if(input_offset<input->len) {
                    ssize_t size=write(input_pipe[1],input->data+input_offset,input->len-input_offset);
                    if(size>0) input_offset+=(size_t)size;
                    else if(size<0 && errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) {
                        if(errno==EPIPE) pipe_write_error=TRUE;
                        else failure="failed while sending hook event";
                        close(input_pipe[1]); input_pipe[1]=-1;
                    }
                }
                if(input_pipe[1]>=0 && input_offset==input->len) { close(input_pipe[1]); input_pipe[1]=-1; }
            } else if(descriptors[i].fd==stdout_output.fd) {
                if(capture_output(&stdout_output)) failure="could not drain hook stdout";
            } else if(descriptors[i].fd==stderr_output.fd) {
                if(capture_output(&stderr_output)) failure="could not drain hook stderr";
            }
            else if(descriptors[i].fd==exec_status[0]) {
                int read_status=read_exec_status(&exec_status[0],&child_error);
                if(read_status<0) failure="could not read hook startup status";
                else if(read_status>0) {
                    exec_complete=1;
                    if(read_status==1) exec_failed=1;
                }
            }
        }
        if(failure) break;
        if(!child_done && child>0) {
            pid_t waited=waitpid(child,&status,WNOHANG);
            if(waited==child) {
                child_done=TRUE;
                child=-1;
                if(!killed) {
                    if(cgroup_kill(&group)) { failure="could not terminate hook descendants after process exit"; break; }
                    killed=TRUE;
                }
                if(input_pipe[1]>=0) {
                    if(input_offset<input->len) pipe_write_error=TRUE;
                    close(input_pipe[1]); input_pipe[1]=-1;
                }
            } else if(waited<0 && errno!=EINTR) {
                failure="could not wait for hook process";
                break;
            }
        }
        if(child_done && stdout_output.fd<0 && stderr_output.fd<0 && exec_complete) break;
        if(clock_gettime(CLOCK_MONOTONIC,&now)) { failure="could not check hook deadline"; break; }
        uint64_t current=(uint64_t)now.tv_sec*1000+(uint64_t)now.tv_nsec/1000000;
        if(pgvn_cancel_is_set(policy->context->cancellation_event)) {
            error=hook_message("was cancelled during service shutdown");
            break;
        }
        if(current>=deadline) { error=hook_message("exceeded its timeout"); break; }
    }

    if(mask_blocked) {
        consume_generated_sigpipe(pipe_was_pending,pipe_write_error,&blocked);
        (void)pthread_sigmask(SIG_SETMASK,&old_mask,NULL);
        mask_blocked=FALSE;
    }
    if(exec_failed && !error) {
        errno=child_error;
        error=hook_failure(pool,"could not execute the pinned hook file");
    }
    if(pipe_write_error && input_offset<input->len && !error)
        error=hook_message("did not accept the complete event");
    if(!child_done && !failure && !error) failure="hook process exited without a wait status";

done:
    if(mask_blocked) {
        consume_generated_sigpipe(pipe_was_pending,pipe_write_error,&blocked);
        (void)pthread_sigmask(SIG_SETMASK,&old_mask,NULL);
    }
    close_pipe(input_pipe); close_pipe(output_pipe); close_pipe(error_pipe);
    close_pipe(gate); close_pipe(exec_status);
    if(stdout_output.fd>=0) close(stdout_output.fd);
    if(stderr_output.fd>=0) close(stderr_output.fd);
    if(cgroup_cleanup(&group,child)) {
        svn_error_t *cleanup=hook_failure(pool,"could not kill and drain its cgroup descendants");
        error=svn_error_compose_create(error,cleanup);
    }
    close(executable); close(root);
    if(failure) {
        return svn_error_compose_create(error,hook_message(failure));
    }
    if(error) return error;
    if(exec_failed) return hook_failure(pool,"could not execute the pinned hook file");
    if(!child_done) return hook_message("did not complete normally");
    if(!WIFEXITED(status)) return hook_message("terminated by a signal");
    stdout_output.data[stdout_output.size]=0;
    stderr_output.data[stderr_output.size]=0;
    return pgvn_hook_result(event,stdout_output.data,stdout_output.size,stdout_output.truncated,
        stderr_output.data,stderr_output.size,(unsigned long)WEXITSTATUS(status),pool);
}
#endif
