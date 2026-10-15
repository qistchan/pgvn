#include <string.h>
#include "internal.h"
#include <apr_strings.h>
#include <apr_uuid.h>
#include "svn_time.h"
#include "private/svn_utf_private.h"
#include "pgfs_native.h"

svn_error_t *pgvn_validate_path(const char *path)
{
    if(!path || path[0]!='/' || strlen(path)>8192 || !svn_utf__is_valid(path,strlen(path)))
        return svn_error_create(SVN_ERR_FS_PATH_SYNTAX,NULL,"PgVN requires a canonical UTF-8 absolute route");
    if(path[1] && path[strlen(path)-1]=='/')
        return svn_error_create(SVN_ERR_FS_PATH_SYNTAX,NULL,"PgVN route must not end with a separator");
    const char *segment=path+1;
    for(const unsigned char *p=(const unsigned char *)path;*p;++p) {
        if(*p<32 || *p==127 || *p=='\\')
            return svn_error_create(SVN_ERR_FS_PATH_SYNTAX,NULL,"Invalid PgVN route character");
        if(*p=='/' && p!=(const unsigned char *)path) {
            apr_size_t size=(const char *)p-segment;
            if(!size || (size==1 && segment[0]=='.') || (size==2 && !strncmp(segment,"..",2)))
                return svn_error_create(SVN_ERR_FS_PATH_SYNTAX,NULL,"Invalid PgVN route segment");
            segment=(const char *)p+1;
        }
    }
    if(!strcmp(segment,".") || !strcmp(segment,".."))
        return svn_error_create(SVN_ERR_FS_PATH_SYNTAX,NULL,"Invalid PgVN route segment");
    return SVN_NO_ERROR;
}

static pgvn_binding *binding_from_result(PGresult *result,apr_pool_t *pool)
{
    pgvn_binding *binding=apr_pcalloc(pool,sizeof(*binding));
    binding->repository_id=apr_pstrdup(pool,PQgetvalue(result,0,0));
    binding->svn_uuid=apr_pstrdup(pool,PQgetvalue(result,0,1));
    binding->canonical_name=apr_pstrdup(pool,PQgetvalue(result,0,2));
    binding->configuration_version=apr_atoi64(PQgetvalue(result,0,3));
    return binding;
}

svn_error_t *pgvn_catalog_resolve(pgvn_binding **output,pgvn_context *context,
    const char *path,apr_pool_t *pool)
{
    *output=NULL;
    SVN_ERR(pgvn_validate_path(path));
    const char *values[]={path};
    PGresult *result;
    SVN_ERR(pgvn_control_query(&result,context,
        "SELECT r.repository_id::text,s.svn_uuid::text,r.canonical_name,r.generation,p.route_prefix,r.enabled "
        "FROM pgvn.routes p JOIN pgvn.repositories r USING(repository_id) "
        "JOIN pgfs.repositories s USING(repository_id) "
        "WHERE p.route_prefix='/' OR $1=p.route_prefix OR left($1,length(p.route_prefix)+1)=p.route_prefix||'/' "
        "ORDER BY length(p.route_prefix) DESC LIMIT 1",1,values));
    if(PQntuples(result)!=1 || strcmp(PQgetvalue(result,0,5),"t")) {
        PQclear(result);
        return svn_error_create(SVN_ERR_FS_NOT_FOUND,NULL,"PgVN repository route is unavailable");
    }
    pgvn_binding *binding=binding_from_result(result,pool);
    const char *prefix=PQgetvalue(result,0,4);
    const char *inside=!strcmp(prefix,"/") ? path : path+strlen(prefix);
    binding->in_repository_path=apr_pstrdup(pool,*inside ? inside : "/");
    PQclear(result);
    *output=binding;
    return SVN_NO_ERROR;
}

static const char *new_uuid(apr_pool_t *pool)
{
    apr_uuid_t uuid; char buffer[APR_UUID_FORMATTED_LENGTH+1];
    apr_uuid_get(&uuid); apr_uuid_format(buffer,&uuid);
    return apr_pstrdup(pool,buffer);
}

static svn_error_t *create_body(pgvn_context *context,const char *id,const char *uuid,
    const char *name,const char *route,apr_pool_t *pool)
{
    SVN_ERR(pgfs_native_create_records(context->control,id,uuid,new_uuid(pool),svn_time_to_cstring(apr_time_now(),pool)));
    const char *values[]={id,name,route};
    PGresult *result;
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "INSERT INTO pgvn.repositories(repository_id,canonical_name,generation) VALUES($1::uuid,$2,1)",2,values));
    PQclear(result);
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "INSERT INTO pgvn.configurations(repository_id,generation,authz_text,published_by) "
        "VALUES($1::uuid,1,E'[/]\\n* =\\n','create')",1,values));
    PQclear(result);
    SVN_ERR(pgvn_exec(&result,context,context->control,
        "INSERT INTO pgvn.routes(route_prefix,repository_id) VALUES($3,$1::uuid)",3,values));
    PQclear(result);
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_validate_name(const char *name)
{
    if(!name || !*name || strlen(name)>256 || !svn_utf__is_valid(name,strlen(name))
       || strchr(name,'/') || strchr(name,'\\') || !strcmp(name,".") || !strcmp(name,".."))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid PgVN repository name");
    for(const unsigned char *p=(const unsigned char *)name;*p;++p)
        if(*p<32 || *p==127) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid PgVN repository name");
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_repo_create(pgvn_binding **output,pgvn_context *context,
    const char *name,const char *route,const char *public_uuid,apr_pool_t *pool)
{
    *output=NULL;
    SVN_ERR(pgvn_validate_path(route));
    SVN_ERR(pgvn_validate_name(name));
    apr_uuid_t parsed;
    if(public_uuid && apr_uuid_parse(&parsed,public_uuid)!=APR_SUCCESS)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"SVN UUID is invalid");
    const char *id=new_uuid(pool),*uuid=public_uuid ? public_uuid : new_uuid(pool);
    SVN_ERR(pgvn_control_begin(context));
    SVN_ERR(pgvn_control_end(context,create_body(context,id,uuid,name,route,pool)));
    return pgvn_catalog_resolve(output,context,route,pool);
}
