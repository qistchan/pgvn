#ifndef PGVN_PLATFORM_H
#define PGVN_PLATFORM_H

#include <stdint.h>

#ifdef _WIN32
#include <windows.h>
typedef LONG pgvn_atomic32;
typedef LONG64 pgvn_atomic64;
typedef HANDLE pgvn_cancel_event;
typedef SRWLOCK pgvn_rwlock;
#define PGVN_RWLOCK_INITIALIZER SRWLOCK_INIT
#else
#include <errno.h>
#include <pthread.h>
#include <time.h>
typedef int32_t pgvn_atomic32;
typedef int64_t pgvn_atomic64;
/* Borrowed by the core. The owner initializes to zero before starting workers
 * and keeps the state alive until all workers have joined. */
typedef struct pgvn_cancel_state { volatile pgvn_atomic32 signalled; } pgvn_cancel_state;
typedef pgvn_cancel_state *pgvn_cancel_event;
typedef pthread_rwlock_t pgvn_rwlock;
#define PGVN_RWLOCK_INITIALIZER PTHREAD_RWLOCK_INITIALIZER
#endif

_Static_assert(sizeof(pgvn_atomic32)==4 && sizeof(pgvn_atomic64)==8,"PgVN counter widths must be stable");

static inline pgvn_atomic32 pgvn_atomic_increment32(volatile pgvn_atomic32 *value)
{
#ifdef _WIN32
    return InterlockedIncrement(value);
#else
    return __atomic_add_fetch(value,1,__ATOMIC_SEQ_CST);
#endif
}

static inline pgvn_atomic32 pgvn_atomic_decrement32(volatile pgvn_atomic32 *value)
{
#ifdef _WIN32
    return InterlockedDecrement(value);
#else
    return __atomic_sub_fetch(value,1,__ATOMIC_SEQ_CST);
#endif
}

static inline pgvn_atomic64 pgvn_atomic_increment64(volatile pgvn_atomic64 *value)
{
#ifdef _WIN32
    return InterlockedIncrement64(value);
#else
    return __atomic_add_fetch(value,1,__ATOMIC_SEQ_CST);
#endif
}

static inline pgvn_atomic64 pgvn_atomic_add64(volatile pgvn_atomic64 *value,pgvn_atomic64 amount)
{
#ifdef _WIN32
    return InterlockedAdd64(value,amount);
#else
    return __atomic_add_fetch(value,amount,__ATOMIC_SEQ_CST);
#endif
}

static inline pgvn_atomic32 pgvn_atomic_exchange32(volatile pgvn_atomic32 *value,pgvn_atomic32 replacement)
{
#ifdef _WIN32
    return InterlockedExchange(value,replacement);
#else
    return __atomic_exchange_n(value,replacement,__ATOMIC_SEQ_CST);
#endif
}

static inline pgvn_atomic32 pgvn_atomic_read32(volatile pgvn_atomic32 *value)
{
#ifdef _WIN32
    return InterlockedCompareExchange(value,0,0);
#else
    return __atomic_load_n(value,__ATOMIC_SEQ_CST);
#endif
}

static inline pgvn_atomic64 pgvn_atomic_read64(volatile pgvn_atomic64 *value)
{
#ifdef _WIN32
    return InterlockedCompareExchange64(value,0,0);
#else
    return __atomic_load_n(value,__ATOMIC_SEQ_CST);
#endif
}

static inline void pgvn_rwlock_write_lock(pgvn_rwlock *lock)
{
#ifdef _WIN32
    AcquireSRWLockExclusive(lock);
#else
    (void)pthread_rwlock_wrlock(lock);
#endif
}

static inline void pgvn_rwlock_read_lock(pgvn_rwlock *lock)
{
#ifdef _WIN32
    AcquireSRWLockShared(lock);
#else
    (void)pthread_rwlock_rdlock(lock);
#endif
}

static inline void pgvn_rwlock_write_unlock(pgvn_rwlock *lock)
{
#ifdef _WIN32
    ReleaseSRWLockExclusive(lock);
#else
    (void)pthread_rwlock_unlock(lock);
#endif
}

static inline void pgvn_rwlock_read_unlock(pgvn_rwlock *lock)
{
#ifdef _WIN32
    ReleaseSRWLockShared(lock);
#else
    (void)pthread_rwlock_unlock(lock);
#endif
}

static inline int pgvn_cancel_is_set(pgvn_cancel_event event)
{
    if(!event) return 0;
#ifdef _WIN32
    return WaitForSingleObject(event,0)==WAIT_OBJECT_0;
#else
    return pgvn_atomic_read32(&event->signalled)!=0;
#endif
}

static inline void pgvn_cancel_signal(pgvn_cancel_event event)
{
    if(!event) return;
#ifdef _WIN32
    SetEvent(event);
#else
    __atomic_store_n(&event->signalled,1,__ATOMIC_SEQ_CST);
#endif
}

/* Return an errno-style status, never silently substitute the wall clock.
 * POSIX builds enable clock_gettime with _POSIX_C_SOURCE=200809L. */
static inline int pgvn_monotonic_milliseconds(uint64_t *milliseconds)
{
#ifdef _WIN32
    *milliseconds=GetTickCount64();
#else
    struct timespec now;
    if(clock_gettime(CLOCK_MONOTONIC,&now)) return errno;
    *milliseconds=(uint64_t)now.tv_sec*1000+(uint64_t)now.tv_nsec/1000000;
#endif
    return 0;
}

#endif
