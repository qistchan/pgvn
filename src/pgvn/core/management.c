#include <string.h>
#include "internal.h"
#include <apr_strings.h>
#include <apr_uuid.h>
#include "pgfs_native.h"

svn_error_t *pgvn_repository_lookup(pgvn_binding **output,pgvn_context *context,
    const char *selector,apr_pool_t *pool)
{
    *output=NULL;
    if(!selector || !*selector) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Repository selector is required");
    const char *name=selector,*id=selector;
    if(!strncmp(selector,"name:",5)) { name=selector+5; id=""; }
    else if(!strncmp(selector,"id:",3)) {
        apr_uuid_t parsed; char buffer[APR_UUID_FORMATTED_LENGTH+1];
        if(apr_uuid_parse(&parsed,selector+3)!=APR_SUCCESS)
            return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid repository ID");
        apr_uuid_format(buffer,&parsed); id=apr_pstrdup(pool,buffer); name="";
    }
    const char *values[]={name,id};
    PGresult *result;
    SVN_ERR(pgvn_control_query(&result,context,
        "SELECT r.repository_id::text,s.svn_uuid::text,r.canonical_name,r.generation "
        "FROM pgvn.repositories r JOIN pgfs.repositories s USING(repository_id) "
        "WHERE r.canonical_name=$1 OR r.repository_id::text=$2",2,values));
    if(PQntuples(result)!=1) {
        svn_boolean_t ambiguous=PQntuples(result)>1; PQclear(result);
        return svn_error_create(ambiguous ? SVN_ERR_INCORRECT_PARAMS : SVN_ERR_FS_NOT_FOUND,NULL,
            ambiguous ? "Ambiguous selector; use name:NAME or id:UUID" : "PgVN repository does not exist");
    }
    pgvn_binding *binding=apr_pcalloc(pool,sizeof(*binding));
    binding->repository_id=apr_pstrdup(pool,PQgetvalue(result,0,0));
    binding->svn_uuid=apr_pstrdup(pool,PQgetvalue(result,0,1));
    binding->canonical_name=apr_pstrdup(pool,PQgetvalue(result,0,2));
    binding->configuration_version=apr_atoi64(PQgetvalue(result,0,3));
    binding->in_repository_path="/";
    PQclear(result); *output=binding;
    return SVN_NO_ERROR;
}

static svn_error_t *register_body(pgvn_context *context,const char *id,const char *name,const char *route)
{
    const char *values[]={id,name,route}; PGresult *result;
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "SELECT repository_id FROM pgfs.repositories WHERE repository_id=$1::uuid FOR UPDATE",1,values));
    svn_boolean_t exists=PQntuples(result)==1; PQclear(result);
    if(!exists) return svn_error_create(SVN_ERR_FS_NOT_FOUND,NULL,"Existing PgFS storage ID was not found");
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "INSERT INTO pgvn.repositories(repository_id,canonical_name,enabled,generation) VALUES($1::uuid,$2,false,1)",2,values));
    PQclear(result);
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "INSERT INTO pgvn.configurations(repository_id,generation,authz_text,published_by) "
        "VALUES($1::uuid,1,E'[/]\\n* =\\n',current_user)",1,values));
    PQclear(result);
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "INSERT INTO pgvn.routes(route_prefix,repository_id) VALUES($3,$1::uuid)",3,values));
    PQclear(result);
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_repo_register(pgvn_binding **output,pgvn_context *context,
    const char *id,const char *name,const char *route,apr_pool_t *pool)
{
    *output=NULL; SVN_ERR(pgvn_require_admin(context));
    SVN_ERR(pgvn_validate_path(route)); SVN_ERR(pgvn_validate_name(name));
    apr_uuid_t parsed;
    if(!id || apr_uuid_parse(&parsed,id)!=APR_SUCCESS)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Existing storage ID must be a UUID");
    SVN_ERR(pgvn_control_begin(context));
    SVN_ERR(pgvn_control_end(context,register_body(context,id,name,route)));
    return pgvn_repository_lookup(output,context,apr_pstrcat(pool,"id:",id,NULL),pool);
}

static svn_error_t *set_enabled_body(pgvn_context *context,const char *id,svn_boolean_t enabled)
{
    const char *values[]={id,enabled ? "true" : "false"}; PGresult *result;
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "SELECT enabled FROM pgvn.repositories WHERE repository_id=$1::uuid FOR UPDATE",1,values));
    if(PQntuples(result)!=1) { PQclear(result); return svn_error_create(SVN_ERR_FS_NOT_FOUND,NULL,"Repository does not exist"); }
    svn_boolean_t unchanged=(!strcmp(PQgetvalue(result,0,0),"t"))==enabled;
    PQclear(result);
    if(unchanged) return SVN_NO_ERROR;
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "INSERT INTO pgvn.configurations(repository_id,generation,authz_text,groups_text,settings,hooks,published_by) "
        "SELECT c.repository_id,c.generation+1,c.authz_text,c.groups_text,c.settings,c.hooks,current_user "
        "FROM pgvn.configurations c JOIN pgvn.repositories r ON r.repository_id=c.repository_id AND r.generation=c.generation "
        "WHERE r.repository_id=$1::uuid",1,values));
    PQclear(result);
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "UPDATE pgvn.repositories SET enabled=$2::boolean,generation=generation+1 WHERE repository_id=$1::uuid",2,values));
    PQclear(result);
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_repo_set_enabled(pgvn_context *context,const char *id,svn_boolean_t enabled,apr_pool_t *pool)
{
    SVN_ERR(pgvn_require_admin(context)); SVN_ERR(pgvn_control_begin(context));
    return pgvn_control_end(context,set_enabled_body(context,id,enabled));
}

static svn_error_t *purge_body(PGconn *connection,void *baton,apr_pool_t *pool)
{
    pgvn_repository *repository=baton; pgvn_context *context=repository->context;
    const char *values[]={repository->repository_id}; PGresult *result;
    SVN_ERR(pgvn_exec(&result,context,connection,
        "SELECT repository_id FROM pgfs.repositories WHERE repository_id=$1::uuid FOR UPDATE",1,values));
    PQclear(result);
    SVN_ERR(pgvn_exec(&result,context,connection,
        "SELECT enabled FROM pgvn.repositories WHERE repository_id=$1::uuid FOR UPDATE",1,values));
    svn_boolean_t disabled=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"f"); PQclear(result);
    if(!disabled) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Disable the repository before purging it");
    SVN_ERR(pgvn_exec(&result,context,connection,
        "SELECT pg_try_advisory_xact_lock(hashtextextended('pgvn-repo:'||$1,0))",1,values));
    svn_boolean_t exclusive=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
    if(!exclusive) return svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"Repository still has open handles; retry after draining");
    SVN_ERR(pgvn_exec(&result,context,connection,
        "SELECT EXISTS(SELECT 1 FROM pgfs.transactions WHERE repository_id=$1::uuid AND state='active')",1,values));
    svn_boolean_t active=!strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
    if(active) return svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"Repository has active transactions; abort them before purging");
    const char *tables[]={"routes","configurations","repositories"};
    for(int i=0;i<3;++i) {
        SVN_ERR(pgvn_exec(&result,context,connection,
            apr_psprintf(pool,"DELETE FROM pgvn.%s WHERE repository_id=$1::uuid",tables[i]),1,values));
        PQclear(result);
    }
    return pgfs_native_delete_rows(repository->fs,pool);
}

svn_error_t *pgvn_repo_purge(pgvn_context *context,const char *id,apr_pool_t *pool)
{
    pgvn_repository *repository;
    SVN_ERR(pgvn_repos_open_admin(&repository,context,id,pool));
    return pgfs_native_atomic(repository->fs,purge_body,repository,pool);
}

typedef struct hotcopy_baton {
    pgvn_repository *source;
    const char *destination;
    const char *name;
    const char *route;
    svn_boolean_t fresh;
} hotcopy_baton;

static svn_error_t *hotcopy_body(PGconn *connection,void *baton,apr_pool_t *pool)
{
    hotcopy_baton *copy=baton; pgvn_context *context=copy->source->context;
    const char *values[]={copy->source->repository_id,copy->destination,copy->name,copy->route};
    PGresult *result;
    /* Match purge/publication ordering: storage rows, then control rows. */
    SVN_ERR(pgvn_exec(&result,context,connection,
        "SELECT repository_id FROM pgfs.repositories WHERE repository_id IN($1::uuid,$2::uuid) "
        "ORDER BY repository_id FOR UPDATE",2,values));
    PQclear(result);
    if(!copy->fresh) {
        SVN_ERR(pgvn_exec(&result,context,connection,
            "SELECT pg_try_advisory_xact_lock(hashtextextended('pgvn-repo:'||$2,0))",2,values));
        svn_boolean_t exclusive=!strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
        if(!exclusive) return svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"Hotcopy destination has open handles");
        SVN_ERR(pgvn_exec(&result,context,connection,
            "SELECT enabled FROM pgvn.repositories WHERE repository_id=$2::uuid FOR UPDATE",2,values));
        svn_boolean_t disabled=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"f"); PQclear(result);
        if(!disabled) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Disable the incremental hotcopy destination first");
        SVN_ERR(pgvn_exec(&result,context,connection,
            "SELECT EXISTS(SELECT 1 FROM pgfs.transactions WHERE repository_id=$2::uuid AND state='active')",2,values));
        svn_boolean_t active=!strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
        if(active) return svn_error_create(SVN_ERR_FS_TXN_OUT_OF_DATE,NULL,"Hotcopy destination has active transactions");
    }
    SVN_ERR(pgfs_native_hotcopy_rows(copy->source->fs,copy->destination,copy->fresh,pool));
    if(copy->fresh) {
        SVN_ERR(pgvn_exec(&result,context,connection,
            "INSERT INTO pgvn.repositories(repository_id,canonical_name,enabled,generation) VALUES($2::uuid,$3,false,1)",3,values));
        PQclear(result);
        SVN_ERR(pgvn_exec(&result,context,connection,
            "INSERT INTO pgvn.configurations(repository_id,generation,authz_text,groups_text,settings,hooks,published_by) "
            "SELECT $2::uuid,1,c.authz_text,c.groups_text,c.settings,c.hooks,current_user "
            "FROM pgvn.configurations c JOIN pgvn.repositories r ON r.repository_id=c.repository_id AND r.generation=c.generation "
            "WHERE r.repository_id=$1::uuid",2,values));
        PQclear(result);
        SVN_ERR(pgvn_exec(&result,context,connection,
            "INSERT INTO pgvn.routes(route_prefix,repository_id) VALUES($4,$2::uuid)",4,values));
        PQclear(result);
    }
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_repo_hotcopy(pgvn_binding **output,pgvn_context *context,
    const char *source,const char *name,const char *route,const char *incremental,apr_pool_t *pool)
{
    *output=NULL;
    hotcopy_baton copy;
    memset(&copy,0,sizeof(copy));
    copy.fresh=incremental==NULL;
    if(copy.fresh) { SVN_ERR(pgvn_validate_name(name)); SVN_ERR(pgvn_validate_path(route)); }
    copy.destination=copy.fresh ? pgvn_new_uuid(pool) : incremental;
    copy.name=name; copy.route=route;
    if(!strcmp(source,copy.destination))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Hotcopy source and destination must differ");
    SVN_ERR(pgvn_repos_open_admin(&copy.source,context,source,pool));
    SVN_ERR(pgfs_native_atomic(copy.source->fs,hotcopy_body,&copy,pool));
    return pgvn_repository_lookup(output,context,apr_pstrcat(pool,"id:",copy.destination,NULL),pool);
}
