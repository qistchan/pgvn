#ifndef PGVN_EXECUTABLE_POSIX_H
#define PGVN_EXECUTABLE_POSIX_H
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
/* Trusted executable paths: no symlink component, no writable-by-others code.
 * Root-owned sticky directories are permitted only as intermediate parents. */
static int pgvn_program_open(const char *path,int *parent)
{
    if(parent) *parent=-1;
    if(!path || *path!='/' || strlen(path)>8192 || strstr(path,"//")) { errno=EINVAL; return -1; }
    char *copy=strdup(path+1); if(!copy) return -1;
    int directory=open("/",O_RDONLY|O_DIRECTORY|O_CLOEXEC),file=-1;
    char *component=copy;
    while(directory>=0) {
        char *next=strchr(component,'/'); if(next) *next++=0;
        if(!*component || !strcmp(component,".") || !strcmp(component,"..")) { errno=EINVAL; break; }
        int fd=openat(directory,component,O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK|(next ? O_DIRECTORY : 0));
        struct stat info;
        if(fd<0) break;
        int valid=!fstat(fd,&info) && (info.st_uid==0 || info.st_uid==geteuid());
        if(next) valid=valid && S_ISDIR(info.st_mode) && (!(info.st_mode&0022) ||
            (info.st_uid==0 && (info.st_mode&S_ISVTX) && strchr(next,'/')));
        else valid=valid && S_ISREG(info.st_mode) && info.st_nlink==1 && !(info.st_mode&0022) &&
            faccessat(fd,"",X_OK,AT_EMPTY_PATH|AT_EACCESS)==0;
        if(!valid) { close(fd); errno=EACCES; break; }
        if(!next) { file=fd; break; }
        close(directory); directory=fd; component=next;
    }
    if(file>=0 && parent) *parent=directory;
    else if(directory>=0) close(directory);
    free(copy); return file;
}
#endif
