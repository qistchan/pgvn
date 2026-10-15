/* Exercise production liveness logic with an intentionally stalled main tick. */
#include <stdio.h>
#include <apr_general.h>
#include "data.h"

int main(void)
{
    if(apr_initialize()!=APR_SUCCESS) return 1;
    pgvn_monitor_data *data=NULL;
    if(pgvn_monitor_data_start(&data,NULL)) { apr_terminate(); return 1; }
    int initial=pgvn_monitor_live(data),ready=pgvn_monitor_ready(data);
    Sleep(15100);
    int stalled=pgvn_monitor_live(data);
    pgvn_monitor_tick(data); int recovered=pgvn_monitor_live(data);
    pgvn_monitor_data_stop(data); apr_terminate();
    printf("{\"initial_live\":%d,\"unconfigured_ready\":%d,\"stalled_live\":%d,\"recovered_live\":%d}\n",initial,ready,stalled,recovered);
    return initial && !ready && !stalled && recovered ? 0 : 1;
}
