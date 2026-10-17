#ifndef PGVN_RUNTIME_H
#define PGVN_RUNTIME_H
#include "platform.h"
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#ifdef _WIN32
#include <psapi.h>
typedef SRWLOCK pgvn_rwlock;
typedef HANDLE pgvn_thread;
#define PGVN_RWLOCK_INIT SRWLOCK_INIT
#define PGVN_THREAD_PROC DWORD WINAPI
#define PGVN_OS_NOMEM ERROR_NOT_ENOUGH_MEMORY
#define PGVN_OS_INVALID ERROR_INVALID_PARAMETER
#define PGVN_OS_ERROR() GetLastError()
static inline void pgvn_rwlock_init(pgvn_rwlock *lock) { InitializeSRWLock(lock); }
static inline void pgvn_rwlock_destroy(pgvn_rwlock *lock) { (void)lock; }
static inline void pgvn_read_lock(pgvn_rwlock *lock) { AcquireSRWLockShared(lock); }
static inline void pgvn_read_unlock(pgvn_rwlock *lock) { ReleaseSRWLockShared(lock); }
static inline void pgvn_write_lock(pgvn_rwlock *lock) { AcquireSRWLockExclusive(lock); }
static inline void pgvn_write_unlock(pgvn_rwlock *lock) { ReleaseSRWLockExclusive(lock); }
static inline pgvn_cancel_event pgvn_event_create(void) { return CreateEventW(NULL,TRUE,FALSE,NULL); }
static inline void pgvn_event_close(pgvn_cancel_event event) { if(event) CloseHandle(event); }
static inline int pgvn_wait_stop(pgvn_cancel_event event,uint32_t ms) { return WaitForSingleObject(event,ms)==WAIT_OBJECT_0; }
static inline pgvn_thread pgvn_thread_create(DWORD (WINAPI *fn)(void *),void *baton) { return CreateThread(NULL,0,fn,baton,0,NULL); }
static inline int pgvn_thread_wait(pgvn_thread thread,uint32_t ms) { return WaitForSingleObject(thread,ms)==WAIT_OBJECT_0; }
static inline void pgvn_thread_close(pgvn_thread thread) { if(thread) CloseHandle(thread); }
#else
#include <pthread.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
typedef pthread_rwlock_t pgvn_rwlock;
#define PGVN_RWLOCK_INIT PTHREAD_RWLOCK_INITIALIZER
#define PGVN_THREAD_PROC void *
#define PGVN_OS_NOMEM ENOMEM
#define PGVN_OS_INVALID EINVAL
#define PGVN_OS_ERROR() errno
static inline void pgvn_rwlock_init(pgvn_rwlock *lock) { if(pthread_rwlock_init(lock,NULL)) abort(); }
static inline void pgvn_rwlock_destroy(pgvn_rwlock *lock) { pthread_rwlock_destroy(lock); }
static inline void pgvn_read_lock(pgvn_rwlock *lock) { if(pthread_rwlock_rdlock(lock)) abort(); }
static inline void pgvn_read_unlock(pgvn_rwlock *lock) { if(pthread_rwlock_unlock(lock)) abort(); }
static inline void pgvn_write_lock(pgvn_rwlock *lock) { if(pthread_rwlock_wrlock(lock)) abort(); }
static inline void pgvn_write_unlock(pgvn_rwlock *lock) { if(pthread_rwlock_unlock(lock)) abort(); }
static inline pgvn_cancel_event pgvn_event_create(void) { return calloc(1,sizeof(pgvn_cancel_state)); }
static inline void pgvn_event_close(pgvn_cancel_event event) { free(event); }
static inline int pgvn_wait_stop(pgvn_cancel_event event,uint32_t ms)
{
    uint64_t start; if(pgvn_monotonic_milliseconds(&start)) abort();
    for(;;) {
        if(pgvn_cancel_is_set(event)) return 1;
        uint64_t now; if(pgvn_monotonic_milliseconds(&now)) abort();
        if(ms!=UINT32_MAX && now-start>=ms) return 0;
        uint64_t delay=ms==UINT32_MAX || ms-(now-start)>25 ? 25 : ms-(now-start);
        struct timespec pause={0,(long)delay*1000000};
        while(nanosleep(&pause,&pause) && errno==EINTR) { if(pgvn_cancel_is_set(event)) return 1; }
    }
}
typedef struct pgvn_thread_state {
    pthread_t native;
    void *(*fn)(void *);
    void *baton;
    volatile pgvn_atomic32 finished;
    int joined;
} *pgvn_thread;
static inline void *pgvn_thread_entry(void *baton)
{
    pgvn_thread state=baton; void *result=state->fn(state->baton);
    __atomic_store_n(&state->finished,1,__ATOMIC_RELEASE); return result;
}
static inline pgvn_thread pgvn_thread_create(void *(*fn)(void *),void *baton)
{
    pgvn_thread state=calloc(1,sizeof(*state)); if(!state) return NULL;
    state->fn=fn; state->baton=baton;
    int error=pthread_create(&state->native,NULL,pgvn_thread_entry,state);
    if(error) { free(state); errno=error; return NULL; }
    return state;
}
static inline int pgvn_thread_wait(pgvn_thread thread,uint32_t ms)
{
    if(!thread || thread->joined) return 1;
    uint64_t start; if(pgvn_monotonic_milliseconds(&start)) abort();
    while(!__atomic_load_n(&thread->finished,__ATOMIC_ACQUIRE)) {
        uint64_t now; if(pgvn_monotonic_milliseconds(&now)) abort();
        if(ms!=UINT32_MAX && now-start>=ms) return 0;
        struct timespec pause={0,10000000}; nanosleep(&pause,NULL);
    }
    if(pthread_join(thread->native,NULL)) abort();
    thread->joined=1; return 1;
}
static inline void pgvn_thread_close(pgvn_thread thread) { if(thread) { pgvn_thread_wait(thread,UINT32_MAX); free(thread); } }
#endif
static inline uint64_t pgvn_tick(void) { uint64_t value; if(pgvn_monotonic_milliseconds(&value)) abort(); return value; }
static inline pgvn_atomic32 pgvn_exchange32(volatile pgvn_atomic32 *value,pgvn_atomic32 next)
{
#ifdef _WIN32
    return InterlockedExchange(value,next);
#else
    return __atomic_exchange_n(value,next,__ATOMIC_SEQ_CST);
#endif
}
static inline pgvn_atomic64 pgvn_exchange64(volatile pgvn_atomic64 *value,pgvn_atomic64 next)
{
#ifdef _WIN32
    return InterlockedExchange64(value,next);
#else
    return __atomic_exchange_n(value,next,__ATOMIC_SEQ_CST);
#endif
}
static inline pgvn_atomic32 pgvn_compare32(volatile pgvn_atomic32 *value,pgvn_atomic32 next,pgvn_atomic32 expected)
{
#ifdef _WIN32
    return InterlockedCompareExchange(value,next,expected);
#else
    __atomic_compare_exchange_n(value,&expected,next,0,__ATOMIC_SEQ_CST,__ATOMIC_SEQ_CST); return expected;
#endif
}
static inline pgvn_atomic64 pgvn_compare64(volatile pgvn_atomic64 *value,pgvn_atomic64 next,pgvn_atomic64 expected)
{
#ifdef _WIN32
    return InterlockedCompareExchange64(value,next,expected);
#else
    __atomic_compare_exchange_n(value,&expected,next,0,__ATOMIC_SEQ_CST,__ATOMIC_SEQ_CST); return expected;
#endif
}
static inline unsigned long pgvn_process_id(void)
{
#ifdef _WIN32
    return GetCurrentProcessId();
#else
    return (unsigned long)getpid();
#endif
}
int pgvn_socket_poll(int fd,int write,int milliseconds);
static inline char *pgvn_strdup(const char *value)
{
#ifdef _WIN32
    return _strdup(value);
#else
    return strdup(value);
#endif
}
void pgvn_timestamp(char output[32]);
int pgvn_hostname(char *output,size_t capacity);
int pgvn_process_resources(uint64_t *working_set,uint64_t *private_bytes,uint64_t *cpu_ticks,unsigned *processors);
#endif
