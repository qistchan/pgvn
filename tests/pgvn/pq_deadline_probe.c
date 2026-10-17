#include <stdio.h>
#include <string.h>
#include "pgfs_pq.h"
static int64_t cancel_at;
static int cancelled(void *baton) { (void)baton; return pgfs_pq_clock()>=cancel_at; }
int main(int argc,char **argv)
{
    if(argc!=3) return 2;
    PGconn *connection=PQconnectdb(argv[1]);
    if(!connection || PQstatus(connection)!=CONNECTION_OK) return 3;
    PGresult *result=pgfs_pq_exchange(connection,"SELECT 42",0,NULL,NULL,NULL,NULL,0,1000,NULL,NULL,0);
    if(!result || PQresultStatus(result)!=PGRES_TUPLES_OK || strcmp(PQgetvalue(result,0,0),"42")) return 4;
    PQclear(result);
    puts("READY"); fflush(stdout);
    if(getchar()!='\n') return 5;
    int64_t start=pgfs_pq_clock(); cancel_at=start+75000;
    result=pgfs_pq_exchange(connection,"SELECT pg_sleep(30)",0,NULL,NULL,NULL,NULL,0,250,
        !strcmp(argv[2],"cancel") ? cancelled : NULL,NULL,0);
    int64_t elapsed=pgfs_pq_clock()-start;
    int ok=!result && PQstatus(connection)==CONNECTION_BAD && elapsed>=50000 && elapsed<1500000;
    if(result) PQclear(result);
    PQfinish(connection);
    printf("{\"status\":\"%s\",\"elapsed_ms\":%.3f}\n",ok ? "PASS" : "FAIL",elapsed/1000.0);
    return ok ? 0 : 1;
}
