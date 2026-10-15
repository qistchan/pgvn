#include <string.h>
#include "admin.h"
#include <apr_strings.h>

static svn_error_t *show(admin_command *command,const char *id)
{
    const char *values[]={id};
    return admin_emit_query(command,
        "SELECT json_build_object('id',r.repository_id,'uuid',s.svn_uuid,'name',r.canonical_name,"
        "'enabled',r.enabled,'generation',r.generation,'head',s.head_rev,"
        "'routes',(SELECT json_agg(route_prefix ORDER BY route_prefix) FROM pgvn.routes WHERE repository_id=r.repository_id)) "
        "FROM pgvn.repositories r JOIN pgfs.repositories s USING(repository_id) WHERE r.repository_id=$1::uuid",1,values);
}

svn_error_t *admin_repo(admin_command *command)
{
    const char *op=command->argv[1]; pgvn_binding *binding;
    if(!strcmp(op,"list")) {
        apr_int64_t limit=100,offset=0;
        if(command->values[OPT_LIMIT]) SVN_ERR(admin_number(&limit,command->values[OPT_LIMIT],1,1000));
        if(command->values[OPT_OFFSET]) SVN_ERR(admin_number(&offset,command->values[OPT_OFFSET],0,2147483647));
        const char *values[]={apr_psprintf(command->pool,"%" APR_INT64_T_FMT,limit),apr_psprintf(command->pool,"%" APR_INT64_T_FMT,offset)};
        return admin_emit_query(command,
            "SELECT json_build_object('items',COALESCE(json_agg(q),'[]'::json),'limit',$1::integer,'offset',$2::integer) "
            "FROM (SELECT r.repository_id AS id,r.canonical_name AS name,s.svn_uuid AS uuid,r.enabled,r.generation,s.head_rev AS head "
            "FROM pgvn.repositories r JOIN pgfs.repositories s USING(repository_id) ORDER BY r.canonical_name COLLATE \"C\" LIMIT $1::integer OFFSET $2::integer) q",2,values);
    }
    if(!strcmp(op,"create") || !strcmp(op,"register-existing")) {
        const char *name=command->argv[2];
        const char *route=command->values[OPT_ROUTE] ? command->values[OPT_ROUTE] : apr_pstrcat(command->pool,"/",name,NULL);
        if(!strcmp(op,"create")) SVN_ERR(pgvn_repo_create(&binding,command->context,name,route,command->values[OPT_UUID],command->pool));
        else SVN_ERR(pgvn_repo_register(&binding,command->context,command->argv[3],name,route,command->pool));
    } else {
        SVN_ERR(pgvn_repository_lookup(&binding,command->context,command->argv[2],command->pool));
        if(!strcmp(op,"enable") || !strcmp(op,"disable")) SVN_ERR(pgvn_repo_set_enabled(command->context,binding->repository_id,!strcmp(op,"enable"),command->pool));
    }
    return show(command,binding->repository_id);
}

svn_error_t *admin_diagnose(admin_command *command)
{
    SVN_ERR(admin_db_check(command->context,TRUE,command->pool));
    return admin_emit_query(command,
        "SELECT json_build_object('product','PgVN','version','0.1.0-dev','svn_base','1.14.5',"
        "'server_version_num',current_setting('server_version_num')::integer,'database',current_database(),"
        "'schema',json_build_object('pgfs',4,'pgvn',2,'checksums','match'),"
        "'privileges',json_build_object('admin',has_table_privilege(current_user,'pgvn.configurations','INSERT'),"
        "'storage_read',has_table_privilege(current_user,'pgfs.revisions','SELECT'),"
        "'storage_write',has_table_privilege(current_user,'pgfs.revisions','INSERT')),"
        "'capabilities',json_build_object('repository_by_id',true,'svn_protocol',false,'dav_protocol',false,'monitor','bootstrap'))",0,NULL);
}
