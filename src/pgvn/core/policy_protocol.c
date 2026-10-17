#include <string.h>
#include <stdlib.h>
#include "internal.h"
#include <apr_strings.h>

svn_error_t *pgvn_policy_load_protocol(pgvn_policy *policy,apr_pool_t *pool)
{
    policy->anonymous_access="none";
    policy->authenticated_access="write";
    policy->svn_authz_bypass=FALSE;
    if(policy->context->catalog_version<4) return SVN_NO_ERROR;
    const char *values[]={policy->repository_id,apr_psprintf(pool,"%" APR_INT64_T_FMT,policy->generation)};
    PGresult *result;
    const char *validator=policy->context->catalog_version>=7 ? "pgvn.valid_settings(settings,true)" : "pgvn.valid_settings(settings)";
    SVN_ERR(pgvn_control_query(&result,policy->context,apr_pstrcat(pool,
        "SELECT ",policy->context->catalog_version>=8 ? "pgvn.valid_hooks(hooks,true)" : "pgvn.valid_hooks(hooks)"," AND ",validator,",settings->>'identity_domain',"
        "COALESCE(settings->>'anonymous_access','none'),COALESCE(settings->>'authenticated_access','write'),"
        "COALESCE(settings->>'svn_authz_mode','enforce') "
        "FROM pgvn.configurations WHERE repository_id=$1::uuid AND generation=$2::bigint",NULL),2,values));
    if(PQntuples(result)!=1 || strcmp(PQgetvalue(result,0,0),"t")) {
        PQclear(result); return svn_error_create(SVN_ERR_AUTHZ_INVALID_CONFIG,NULL,"PgVN protocol configuration is invalid or unavailable");
    }
    if(!PQgetisnull(result,0,1)) policy->identity_domain=apr_pstrdup(pool,PQgetvalue(result,0,1));
    policy->anonymous_access=apr_pstrdup(pool,PQgetvalue(result,0,2));
    policy->authenticated_access=apr_pstrdup(pool,PQgetvalue(result,0,3));
    const char *mode=PQgetvalue(result,0,4);
    if(strcmp(mode,"enforce") && strcmp(mode,"bypass")) {
        PQclear(result); return svn_error_create(SVN_ERR_AUTHZ_INVALID_CONFIG,NULL,"Unknown PgVN SVN authorization mode");
    }
    policy->svn_authz_bypass=!strcmp(mode,"bypass");
    if(policy->svn_authz_bypass && strcmp(policy->anonymous_access,"none")) {
        PQclear(result); return svn_error_create(SVN_ERR_AUTHZ_INVALID_CONFIG,NULL,"PgVN SVN bypass requires authenticated access");
    }
    PQclear(result);
    SVN_ERR(pgvn_control_query(&result,policy->context,
        "SELECT h.key,h.value->>'executable',COALESCE(h.value->>'timeout_seconds','30'),a.value#>>'{}',h.value->>'name' "
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
            svn_error_t *error=pgvn_hook_resolve(handler,policy->context,
                PQgetisnull(result,row,4) ? NULL : PQgetvalue(result,row,4),pool);
            if(error) { PQclear(result); return error; }
            handler->timeout_seconds=(unsigned)atoi(PQgetvalue(result,row,2));
            handler->arguments=apr_array_make(pool,4,sizeof(const char *));
            policy->external_hooks[index]=handler;
        }
        if(!PQgetisnull(result,row,3)) APR_ARRAY_PUSH(handler->arguments,const char *)=apr_pstrdup(pool,PQgetvalue(result,row,3));
    }
    PQclear(result);
    return SVN_NO_ERROR;
}
