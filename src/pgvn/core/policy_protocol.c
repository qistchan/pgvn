#include <string.h>
#include <stdlib.h>
#include "internal.h"
#include <apr_strings.h>

svn_error_t *pgvn_policy_load_protocol(pgvn_policy *policy,apr_pool_t *pool)
{
    policy->anonymous_access="none";
    policy->authenticated_access="write";
    if(policy->context->catalog_version<4) return SVN_NO_ERROR;
    const char *values[]={policy->repository_id,apr_psprintf(pool,"%" APR_INT64_T_FMT,policy->generation)};
    PGresult *result;
    SVN_ERR(pgvn_control_query(&result,policy->context,
        "SELECT pgvn.valid_hooks(hooks) AND pgvn.valid_settings(settings),settings->>'identity_domain',"
        "COALESCE(settings->>'anonymous_access','none'),COALESCE(settings->>'authenticated_access','write') "
        "FROM pgvn.configurations WHERE repository_id=$1::uuid AND generation=$2::bigint",2,values));
    if(PQntuples(result)!=1 || strcmp(PQgetvalue(result,0,0),"t")) {
        PQclear(result); return svn_error_create(SVN_ERR_AUTHZ_INVALID_CONFIG,NULL,"PgVN protocol configuration is invalid or unavailable");
    }
    if(!PQgetisnull(result,0,1)) policy->identity_domain=apr_pstrdup(pool,PQgetvalue(result,0,1));
    policy->anonymous_access=apr_pstrdup(pool,PQgetvalue(result,0,2));
    policy->authenticated_access=apr_pstrdup(pool,PQgetvalue(result,0,3));
    PQclear(result);
    SVN_ERR(pgvn_control_query(&result,policy->context,
        "SELECT h.key,h.value->>'executable',COALESCE(h.value->>'timeout_seconds','30'),a.value#>>'{}' "
        "FROM pgvn.configurations c CROSS JOIN LATERAL jsonb_each(c.hooks) h "
        "LEFT JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(h.value)='object' "
        "THEN COALESCE(h.value->'arguments','[]'::jsonb) ELSE '[]'::jsonb END) WITH ORDINALITY a ON true "
        "WHERE c.repository_id=$1::uuid AND c.generation=$2::bigint AND jsonb_typeof(h.value)='object' "
        "ORDER BY h.key,a.ordinality",2,values));
    for(int row=0;row<PQntuples(result);++row) {
        int index=-1;
        for(int i=0;i<9;++i) if(!strcmp(PQgetvalue(result,row,0),pgvn_hook_name((pgvn_hook_event)i))) index=i;
        if(index<0) { PQclear(result); return svn_error_create(SVN_ERR_AUTHZ_INVALID_CONFIG,NULL,"Unknown PgVN hook event"); }
        pgvn_hook_handler *handler=policy->external_hooks[index];
        if(!handler) {
            handler=apr_pcalloc(pool,sizeof(*handler));
            handler->executable=apr_pstrdup(pool,PQgetvalue(result,row,1));
            handler->timeout_seconds=(unsigned)atoi(PQgetvalue(result,row,2));
            handler->arguments=apr_array_make(pool,4,sizeof(const char *));
            policy->external_hooks[index]=handler;
        }
        if(!PQgetisnull(result,row,3)) APR_ARRAY_PUSH(handler->arguments,const char *)=apr_pstrdup(pool,PQgetvalue(result,row,3));
    }
    PQclear(result);
    return SVN_NO_ERROR;
}
