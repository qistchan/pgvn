#include "runtime.h"
#include <stdio.h>
#include <string.h>
#include <apr_time.h>
#ifndef _WIN32
#include <poll.h>
#endif
void pgvn_timestamp(char output[32])
{
    apr_time_exp_t now;
    if(apr_time_exp_gmt(&now,apr_time_now())) { *output=0; return; }
    apr_size_t length=0;
    if(apr_strftime(output,&length,32,"%Y-%m-%dT%H:%M:%SZ",&now)) *output=0;
}
int pgvn_hostname(char *output,size_t capacity)
{
#ifdef _WIN32
    DWORD size=(DWORD)capacity; return GetComputerNameA(output,&size)!=0;
#else
    if(!capacity || gethostname(output,capacity)) return 0;
    output[capacity-1]=0; return 1;
#endif
}
int pgvn_process_resources(uint64_t *working_set,uint64_t *private_bytes,uint64_t *cpu_ticks,unsigned *processors)
{
#ifdef _WIN32
    PROCESS_MEMORY_COUNTERS_EX memory={0}; memory.cb=sizeof(memory);
    FILETIME created,exited,kernel,user; ULARGE_INTEGER k,u;
    if(!GetProcessMemoryInfo(GetCurrentProcess(),(PROCESS_MEMORY_COUNTERS *)&memory,sizeof(memory)) ||
       !GetProcessTimes(GetCurrentProcess(),&created,&exited,&kernel,&user)) return 0;
    k.LowPart=kernel.dwLowDateTime; k.HighPart=kernel.dwHighDateTime;
    u.LowPart=user.dwLowDateTime; u.HighPart=user.dwHighDateTime;
    *working_set=memory.WorkingSetSize; *private_bytes=memory.PrivateUsage; *cpu_ticks=k.QuadPart+u.QuadPart;
    *processors=GetActiveProcessorCount(ALL_PROCESSOR_GROUPS); return *processors>0;
#else
    struct timespec cpu; if(clock_gettime(CLOCK_PROCESS_CPUTIME_ID,&cpu)) return 0;
    FILE *file=fopen("/proc/self/smaps_rollup","r"); if(!file) return 0;
    char line[256]; unsigned long long value; *working_set=*private_bytes=0;
    while(fgets(line,sizeof(line),file)) {
        if(sscanf(line,"Rss: %llu kB",&value)==1) *working_set=value*1024;
        else if(sscanf(line,"Private_Clean: %llu kB",&value)==1 || sscanf(line,"Private_Dirty: %llu kB",&value)==1 ||
                sscanf(line,"Swap: %llu kB",&value)==1) *private_bytes+=value*1024;
    }
    fclose(file); long count=sysconf(_SC_NPROCESSORS_ONLN); if(count<1) return 0;
    *processors=(unsigned)count; *cpu_ticks=(uint64_t)cpu.tv_sec*10000000+(uint64_t)cpu.tv_nsec/100;
    return *working_set>0;
#endif
}

int pgvn_socket_poll(int fd,int write,int milliseconds)
{
#ifdef _WIN32
    WSAPOLLFD descriptor={0}; descriptor.fd=(SOCKET)fd;
    descriptor.events=POLLRDNORM|(write ? POLLWRNORM : 0);
    int result=WSAPoll(&descriptor,1,milliseconds);
#else
    struct pollfd descriptor={fd,POLLIN|(write ? POLLOUT : 0),0};
    int result=poll(&descriptor,1,milliseconds);
    if(result<0 && errno==EINTR) return 0;
#endif
    if(result<0 || (result>0 && (descriptor.revents&(POLLERR|POLLHUP|POLLNVAL)))) return -1;
    return result;
}
