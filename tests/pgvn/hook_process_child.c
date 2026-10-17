/* Deliberately adversarial, test-owned executable for the native hook runner. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <dirent.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
static void consume(FILE *target)
{
    char buffer[32768]; size_t size;
    while((size=fread(buffer,1,sizeof(buffer),stdin))) if(target && fwrite(buffer,1,size,target)!=size) exit(30);
}
static void stamp(const char *path)
{
    char temporary[8192];
    int size=snprintf(temporary,sizeof(temporary),"%s.tmp.%ld",path,(long)getpid());
    if(size<0 || (size_t)size>=sizeof(temporary)) exit(31);
    FILE *file=fopen(temporary,"wx"); if(!file) exit(31);
    fprintf(file,"%ld\n",(long)getpid());
    if(fflush(file) || fsync(fileno(file)) || fclose(file) || rename(temporary,path)) exit(31);
}
static void sleeper(void) { for(;;) pause(); }
int main(int argc,char **argv)
{
    if(argc<2) return 32;
    if(!strcmp(argv[1],"record")) {
        if(argc<3) return 32;
        char path[8192]; snprintf(path,sizeof(path),"%s.event.json",argv[2]);
        FILE *file=fopen(path,"w"); if(!file) return 33; consume(file); fclose(file);
        snprintf(path,sizeof(path),"%s.args",argv[2]); file=fopen(path,"wb"); if(!file) return 33;
        for(int i=3;i<argc;++i) { uint32_t size=(uint32_t)strlen(argv[i]); fwrite(&size,sizeof(size),1,file); fwrite(argv[i],1,size,file); } fclose(file);
        snprintf(path,sizeof(path),"%s.env",argv[2]); file=fopen(path,"w"); if(!file) return 33;
        const char *names[]={"PGVN_EVENT","PGVN_REPOSITORY_ID","PGVN_USER","PGVN_TRANSACTION","PGVN_REVISION","PGVN_CONFIG","PGVN_DSN","PGPASSWORD","HOME","PATH","LANG","PGSERVICEFILE","PGPASSFILE","PGSYSCONFDIR"};
        for(unsigned i=0;i<sizeof(names)/sizeof(*names);++i) { const char *value=getenv(names[i]); fprintf(file,"%s=%s\n",names[i],value ? value : "<absent>"); } fclose(file);
        DIR *directory=opendir("/proc/self/fd"); if(!directory) return 34;
        unsigned count=0; struct dirent *entry;
        while((entry=readdir(directory))) { int fd=atoi(entry->d_name); if(fd>2 && fd!=dirfd(directory)) ++count; }
        closedir(directory); snprintf(path,sizeof(path),"%s.fds",argv[2]); file=fopen(path,"w"); if(!file) return 33; fprintf(file,"%u\n",count); fclose(file);
    } else if(!strcmp(argv[1],"exit")) {
        consume(NULL); if(argc>3) fputs(argv[3],stderr); return argc>2 ? atoi(argv[2]) : 1;
    } else if(!strcmp(argv[1],"bad-stderr")) {
        consume(NULL); const char bytes[]={0,(char)0xff}; fwrite(bytes,1,sizeof(bytes),stderr); return 3;
    } else if(!strcmp(argv[1],"token")) {
        consume(NULL); if(argc>2) fputs(argv[2],stdout);
    } else if(!strcmp(argv[1],"long-token")) {
        consume(NULL); for(int i=0;i<9000;++i) fputc('t',stdout);
    } else if(!strcmp(argv[1],"binary-token")) {
        consume(NULL); const char bytes[]={'x',0,'y'}; fwrite(bytes,1,sizeof(bytes),stdout);
    } else if(!strcmp(argv[1],"flood")) {
        char buffer[4096]; memset(buffer,'x',sizeof(buffer));
        for(int i=0;i<64;++i) { fwrite(buffer,1,sizeof(buffer),stdout); fwrite(buffer,1,sizeof(buffer),stderr); }
        fflush(stdout); fflush(stderr); consume(NULL);
    } else if(!strcmp(argv[1],"close-input")) {
        close(STDIN_FILENO);
    } else if(!strcmp(argv[1],"grandchild") || !strcmp(argv[1],"tree-sleep")) {
        if(argc<3) return 32;
        pid_t first=fork(); if(first<0) return 35;
        if(!first) {
            if(setsid()<0) _exit(36);
            pid_t detached=fork(); if(detached<0) _exit(37);
            if(detached) _exit(0);
            stamp(argv[2]); sleeper();
        }
        int status; waitpid(first,&status,0);
        for(int i=0;i<500 && access(argv[2],F_OK);++i) { struct timespec delay={0,1000000}; nanosleep(&delay,NULL); }
        if(access(argv[2],F_OK)) return 38;
        if(!strcmp(argv[1],"tree-sleep")) sleeper();
    } else return 39;
    return 0;
}
