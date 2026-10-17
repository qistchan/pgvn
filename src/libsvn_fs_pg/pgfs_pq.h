/* Bounded libpq exchange. A failed exchange poisons its connection; callers
 * must never replay SQL whose commit outcome may be unknown. */
#ifndef PGFS_PQ_H
#define PGFS_PQ_H
#include <stdint.h>
#include <stdlib.h>
#include <errno.h>
#include <libpq-fe.h>
#ifdef _WIN32
#include <winsock2.h>
#include <windows.h>
#else
#include <poll.h>
#include <sys/socket.h>
#include <time.h>
#endif

static inline int64_t pgfs_pq_clock(void)
{
#ifdef _WIN32
    LARGE_INTEGER now, frequency;
    if(!QueryPerformanceCounter(&now) || !QueryPerformanceFrequency(&frequency)) abort();
    return (now.QuadPart/frequency.QuadPart)*1000000 +
        (now.QuadPart%frequency.QuadPart)*1000000/frequency.QuadPart;
#else
    struct timespec now;
    if(clock_gettime(CLOCK_MONOTONIC,&now)) abort();
    return (int64_t)now.tv_sec*1000000 + now.tv_nsec/1000;
#endif
}

static inline int pgfs_pq_poll(int socket,int write,int milliseconds)
{
#ifdef _WIN32
    WSAPOLLFD descriptor={(SOCKET)socket,write ? POLLWRNORM : POLLRDNORM,0};
    return WSAPoll(&descriptor,1,milliseconds);
#else
    struct pollfd descriptor={socket,write ? POLLOUT : POLLIN,0};
    int result=poll(&descriptor,1,milliseconds);
    return result<0 && errno==EINTR ? 0 : result;
#endif
}

static inline void pgfs_pq_abandon(PGconn *connection)
{
#ifdef LIBPQ_HAS_ASYNC_CANCEL
    PGcancelConn *cancel=PQcancelCreate(connection);
    int64_t deadline=pgfs_pq_clock()+250000;
    if(cancel && PQcancelStart(cancel)) {
        for(;;) {
            PostgresPollingStatusType state=PQcancelPoll(cancel);
            if(state!=PGRES_POLLING_READING && state!=PGRES_POLLING_WRITING) break;
            if(pgfs_pq_clock()>=deadline) break;
            if(pgfs_pq_poll(PQcancelSocket(cancel),state==PGRES_POLLING_WRITING,10)<0) break;
        }
    }
    if(cancel) PQcancelFinish(cancel);
#endif
    int socket=PQsocket(connection);
    if(socket>=0) {
#ifdef _WIN32
        shutdown((SOCKET)socket,SD_BOTH);
#else
        shutdown(socket,SHUT_RDWR);
#endif
    }
    /* Consume local EOF so the owner sees CONNECTION_BAD and closes once. */
    (void)PQconsumeInput(connection);
}

static inline PGresult *pgfs_pq_exchange(PGconn *connection,const char *sql,
    int count,const Oid *types,const char *const *values,const int *lengths,
    const int *formats,int result_format,int timeout_ms,
    int (*cancelled)(void *),void *baton,int raw)
{
    if(PQstatus(connection)!=CONNECTION_OK || (cancelled && cancelled(baton))) return NULL;
    if(PQsetnonblocking(connection,1)) return NULL;
    int64_t deadline=pgfs_pq_clock()+(int64_t)timeout_ms*1000;
    PGresult *last=NULL;
    if(!(raw ? PQsendQuery(connection,sql) :
         PQsendQueryParams(connection,sql,count,types,values,lengths,formats,result_format))) goto failed;
    for(;;) {
        if((cancelled && cancelled(baton)) || pgfs_pq_clock()>=deadline) goto failed;
        int flush=PQflush(connection);
        if(flush<0 || !PQconsumeInput(connection)) goto failed;
        if(!flush && !PQisBusy(connection)) {
            PGresult *next=PQgetResult(connection);
            if(!next) return last;
            if(last) PQclear(last);
            last=next;
            /* COPY needs a separate streaming protocol; these APIs never use it. */
            ExecStatusType status=PQresultStatus(last);
            if(status==PGRES_COPY_IN || status==PGRES_COPY_OUT || status==PGRES_COPY_BOTH) goto failed;
            continue;
        }
        if(pgfs_pq_poll(PQsocket(connection),flush!=0,25)<0) goto failed;
    }
failed:
    if(last) PQclear(last);
    pgfs_pq_abandon(connection);
    return NULL;
}
#endif
