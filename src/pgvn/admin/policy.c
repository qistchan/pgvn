#include <string.h>
#include "admin.h"
#include <apr_strings.h>
#include "svn_io.h"

svn_error_t *admin_policy(admin_command *command)
{
    const char *group=command->argv[0],*op=command->argv[1];
    if(!strcmp(group,"authz") && !strcmp(op,"validate")) {
        const char *rules,*groups=NULL;
        SVN_ERR(admin_read_text(&rules,command->values[OPT_AUTHZ],command->pool));
        if(command->values[OPT_GROUPS]) SVN_ERR(admin_read_text(&groups,command->values[OPT_GROUPS],command->pool));
        svn_authz_t *authz;
        svn_error_t *error=svn_repos_authz_parse2(&authz,svn_stream_from_string(svn_string_create(rules,command->pool),command->pool),
            groups ? svn_stream_from_string(svn_string_create(groups,command->pool),command->pool) : NULL,NULL,NULL,command->pool,command->pool);
        if(error) return svn_error_create(SVN_ERR_AUTHZ_INVALID_CONFIG,error,"Invalid authorization rules");
        admin_ok("authz.validate"); return SVN_NO_ERROR;
    }
    pgvn_binding *binding;
    SVN_ERR(pgvn_repository_lookup(&binding,command->context,command->argv[2],command->pool));
    if(!strcmp(group,"authz")) {
        const char *mode=command->argv[5];
        if(strcmp(mode,"read") && strcmp(mode,"write")) return admin_usage("Authz check mode must be read or write");
        pgvn_policy *policy; svn_boolean_t allowed;
        SVN_ERR(pgvn_policy_acquire_internal(&policy,command->context,binding->repository_id,0,TRUE,command->pool));
        SVN_ERR(pgvn_policy_check(&allowed,policy,!strcmp(command->argv[3],"-") ? NULL : command->argv[3],command->argv[4],
            !strcmp(mode,"write") ? svn_authz_write : svn_authz_read,command->pool));
        printf("{\"allowed\":%s,\"generation\":%lld}\n",allowed ? "true" : "false",(long long)policy->generation);
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"publish")) {
        const char *rules,*groups=NULL,*hooks="{}",*settings=NULL;
        apr_int64_t generation;
        SVN_ERR(admin_number(&generation,command->values[OPT_GENERATION],1,APR_INT64_MAX-1));
        const char *snapshot_values[]={binding->repository_id,apr_psprintf(command->pool,"%" APR_INT64_T_FMT,generation)};
        PGresult *snapshot;
        SVN_ERR(pgvn_control_query(&snapshot,command->context,
            "SELECT c.groups_text,c.hooks::text FROM pgvn.configurations c JOIN pgvn.repositories r "
            "ON r.repository_id=c.repository_id AND r.generation=c.generation "
            "WHERE r.repository_id=$1::uuid AND r.generation=$2::bigint",2,snapshot_values));
        if(PQntuples(snapshot)!=1) { PQclear(snapshot); return svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"PgVN configuration generation conflict"); }
        groups=PQgetisnull(snapshot,0,0) ? NULL : apr_pstrdup(command->pool,PQgetvalue(snapshot,0,0));
        hooks=apr_pstrdup(command->pool,PQgetvalue(snapshot,0,1));
        PQclear(snapshot);
        SVN_ERR(admin_read_text(&rules,command->values[OPT_AUTHZ],command->pool));
        if(command->values[OPT_GROUPS]) SVN_ERR(admin_read_text(&groups,command->values[OPT_GROUPS],command->pool));
        if(command->values[OPT_HOOKS]) SVN_ERR(admin_read_text(&hooks,command->values[OPT_HOOKS],command->pool));
        if(command->values[OPT_SETTINGS]) SVN_ERR(admin_read_text(&settings,command->values[OPT_SETTINGS],command->pool));
        SVN_ERR(pgvn_policy_publish_settings(command->context,binding->repository_id,generation,rules,groups,hooks,settings,"pgvnadmin",command->pool));
    }
    const char *values[]={binding->repository_id};
    return admin_emit_query(command,
        "SELECT json_build_object('id',r.repository_id,'name',r.canonical_name,'enabled',r.enabled,'generation',r.generation,"
        "'authz',c.authz_text,'groups',c.groups_text,'settings',c.settings,'hooks',c.hooks,"
        "'svn_authz_mode',COALESCE(c.settings->>'svn_authz_mode','enforce'),'published_at',c.published_at,'published_by',c.published_by) "
        "FROM pgvn.repositories r JOIN pgvn.configurations c ON c.repository_id=r.repository_id AND c.generation=r.generation "
        "WHERE r.repository_id=$1::uuid",1,values);
}
