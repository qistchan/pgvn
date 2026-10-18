#include <apr_general.h>
#include <apr_pools.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "build_info.h"
#include "../../src/pgvn/monitor/data.h"

static void usage(void)
{
    fputs("usage: linux_monitor_probe CONFIG\n",stderr);
    exit(2);
}

int main(int argc,char **argv)
{
    if(argc==2 && !strcmp(argv[1],"--version")) {
        char json[PGVN_BUILD_JSON_SIZE];
        pgvn_build_json(json,sizeof(json));
        puts(json);
        return 0;
    }
    if(argc!=2) usage();
    if(apr_initialize()!=APR_SUCCESS) return 3;
    apr_pool_t *pool=NULL;
    if(apr_pool_create(&pool,NULL)!=APR_SUCCESS) { apr_terminate(); return 3; }
    pgvn_monitor_data *data=NULL;
    pgvn_monitor_status status=pgvn_monitor_data_start(&data,argv[1]);
    if(status) {
        printf("start %d\n",(int)status);
        apr_pool_destroy(pool); apr_terminate(); return 4;
    }
    char line[8192];
    while(fgets(line,sizeof(line),stdin)) {
        line[strcspn(line,"\r\n")]=0;
        if(!strcmp(line,"business 0")) pgvn_monitor_business(data,0);
        else if(!strcmp(line,"business 1")) pgvn_monitor_business(data,1);
        else if(!strcmp(line,"tick")) pgvn_monitor_tick(data);
        else if(!strcmp(line,"live")) printf("live %d\n",pgvn_monitor_live(data));
        else if(!strcmp(line,"ready")) printf("ready %d\n",pgvn_monitor_ready(data));
        else if(!strcmp(line,"metrics")) {
            apr_pool_clear(pool);
            const char *metrics=pgvn_monitor_metrics(data,pool);
            fputs(metrics ? metrics : "",stdout); fputc('\n',stdout);
        } else if(!strncmp(line,"api ",4)) {
            char *path=line+4,*query=strchr(path,'?');
            if(query) { *query++=0; } else query="";
            int code=0;
            apr_pool_clear(pool);
            const char *body=pgvn_monitor_api(data,path,query,&code,pool);
            printf("api %d %s\n",code,body ? body : "");
        } else if(!strncmp(line,"authorize ",10)) {
            const char *header=line+10;
            printf("authorize %d\n",pgvn_monitor_authorize(data,header,strlen(header)));
        } else if(!strcmp(line,"stop")) {
            pgvn_monitor_data_stop(data); data=NULL; puts("stopped"); fflush(stdout); break;
        } else if(*line) {
            fputs("unknown\n",stdout);
        }
        fflush(stdout);
    }
    if(data) pgvn_monitor_data_stop(data);
    apr_pool_destroy(pool); apr_terminate();
    return 0;
}
