/* One supervisor per invocation. It becomes a Linux child subreaper before
 * forking the hook. Orphaned descendants, including double-fork/setsid daemons,
 * are adopted here and killed/reaped before the supervisor reports completion.
 * EOF on a private control pipe also cleans up after the service disappears. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "runtime.h"
#include "executable_posix.h"
#include <stdio.h>
#include <signal.h>
#include <poll.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <dirent.h>
extern char **environ;
static volatile sig_atomic_t interrupted;
static void stop_signal(int signal_number) { (void)signal_number; interrupted=1; }
static void close_others(int keep)
{
#ifdef SYS_close_range
    if(keep<4) { if(!syscall(SYS_close_range,4u,~0u,0u)) return; }
    else if((keep==4 || !syscall(SYS_close_range,4u,(unsigned)keep-1,0u)) &&
            !syscall(SYS_close_range,(unsigned)keep+1,~0u,0u)) return;
#endif
    long limit=sysconf(_SC_OPEN_MAX); if(limit<0) limit=1048576;
    for(int fd=4;fd<limit;++fd) if(fd!=keep) close(fd);
}
static int kill_children(void)
{
    /* PPid in /proc/PID/stat is available even on kernels without the optional
     * /proc/PID/task/TID/children interface. Only this supervisor reaps its
     * children, so a matched child PID cannot be reused before this scan ends. */
    DIR *directory=opendir("/proc"); if(!directory) return -1;
    struct dirent *entry; int self_seen=0; pid_t self=getpid();
    while((entry=readdir(directory))) {
        if(!*entry->d_name || strspn(entry->d_name,"0123456789")!=strlen(entry->d_name)) continue;
        long pid=strtol(entry->d_name,NULL,10); if(pid<1 || pid>INT_MAX) continue;
        char path[96]; snprintf(path,sizeof(path),"/proc/%ld/stat",pid);
        int fd=open(path,O_RDONLY|O_CLOEXEC); if(fd<0) continue;
        char stat[4096]; ssize_t size=read(fd,stat,sizeof(stat)-1); close(fd);
        if(size<1) continue;
        stat[size]=0; char *name_end=strrchr(stat,')');
        if(!name_end || strlen(name_end)<4) continue;
        char *end; long parent=strtol(name_end+4,&end,10);
        if(end==name_end+4 || *end!=' ') continue;
        if(pid==self) self_seen=1;
        if(parent==self) kill((pid_t)pid,SIGKILL);
    }
    closedir(directory); return self_seen ? 0 : -1;
}
static int cleanup(void)
{
    for(;;) {
        if(kill_children()) return -1;
        int status; pid_t child;
        while((child=waitpid(-1,&status,WNOHANG))>0) { }
        if(child<0 && errno==ECHILD) return 0;
        if(child<0 && errno!=EINTR) return -1;
        struct timespec pause={0,1000000}; nanosleep(&pause,NULL);
    }
}
int main(int argc,char **argv)
{
    /* The caller may disappear with both output readers closed. Diagnostics
     * must never kill the supervisor before its descendant cleanup. */
    signal(SIGPIPE,SIG_IGN);
    if(argc<4 || (strcmp(argv[1],"--hook") && strcmp(argv[1],"--command"))) return 125;
    unsigned seconds=0;
    for(const char *p=argv[2];*p;++p) {
        if(*p<'0' || *p>'9' || seconds>8640) return 125;
        seconds=seconds*10+(unsigned)(*p-'0');
    }
    if(!seconds || seconds>86400) return 125;
    int hook=!strcmp(argv[1],"--hook"),program=-1,parent=-1,script=0;
    if(hook && argc<5) return 125;
    close_others(-1);
    struct stat control;
    if(fstat(3,&control) || !S_ISFIFO(control.st_mode) || prctl(PR_SET_CHILD_SUBREAPER,1)) {
        fputs("PgVN process containment is unavailable\n",stderr); return 125;
    }
    if(kill_children()) { fputs("PgVN descendant tracking is unavailable\n",stderr); return 125; }
    if(hook) {
        const char *name=argv[4];
        if(!*name || strlen(name)>128 || strspn(name,"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")!=strlen(name)) return 125;
        size_t size=strlen(argv[3])+strlen(name)+2; char *path=malloc(size);
        if(!path) return 125;
        snprintf(path,size,"%s/%s",argv[3],name); program=pgvn_program_open(path,&parent); free(path);
        unsigned char magic[2];
        if(program>=0 && pread(program,magic,2,0)==2 && magic[0]=='#' && magic[1]=='!') script=1;
        if(program<0) { fputs("PgVN hook executable is unavailable or insufficiently protected\n",stderr); return 125; }
    }
    if(hook && prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0)) return 125;
    struct pollfd guard_check={3,POLLIN,0};
    if(poll(&guard_check,1,0)>0 && guard_check.revents) return 143;
    struct sigaction action={0}; action.sa_handler=stop_signal; sigemptyset(&action.sa_mask);
    sigaction(SIGTERM,&action,NULL); sigaction(SIGINT,&action,NULL); sigaction(SIGHUP,&action,NULL);
    uint64_t deadline=pgvn_tick()+(uint64_t)seconds*1000;
    pid_t child=fork();
    if(child<0) return 125;
    if(!child) {
        close(3);
        struct sigaction defaults={0}; defaults.sa_handler=SIG_DFL; sigemptyset(&defaults.sa_mask);
        sigaction(SIGTERM,&defaults,NULL); sigaction(SIGINT,&defaults,NULL); sigaction(SIGHUP,&defaults,NULL); sigaction(SIGPIPE,&defaults,NULL);
        sigset_t empty; sigemptyset(&empty); sigprocmask(SIG_SETMASK,&empty,NULL);
        if(hook) {
            if(fchdir(parent) || fcntl(program,F_SETFD,script ? 0 : FD_CLOEXEC)) _exit(127);
            close_others(program);
            /* Only shebang interpreters retain the executable descriptor; all service/control FDs are closed. */
            fexecve(program,argv+4,environ);
        } else {
            close_others(-1); execvp(argv[3],argv+3);
        }
        fputs("PgVN child could not execute\n",stderr); _exit(127);
    }
    if(program>=0) close(program);
    if(parent>=0) close(parent);
    close(STDIN_FILENO);
    int status=0,result=125;
    for(;;) {
        pid_t reaped=waitpid(child,&status,WNOHANG);
        if(reaped==child) { result=WIFEXITED(status) ? WEXITSTATUS(status) : 128+WTERMSIG(status); break; }
        if(reaped<0 && errno!=EINTR) break;
        if(interrupted) { fputs("PgVN child cancelled\n",stderr); result=143; break; }
        if(pgvn_tick()>=deadline) { fputs("PgVN child exceeded its timeout\n",stderr); result=124; break; }
        struct pollfd guard={3,POLLIN,0}; int ready=poll(&guard,1,25);
        if(ready<0 && errno!=EINTR) break;
        if(ready>0 && guard.revents) { fputs("PgVN child cancelled after caller stopped\n",stderr); result=143; break; }
    }
    /* Never use process-group membership as the descendant boundary. */
    if(cleanup()) { fputs("PgVN descendant cleanup failed\n",stderr); return 125; }
    return result;
}
