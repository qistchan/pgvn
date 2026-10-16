/* Test-only native binding for unchanged svnadmin/svnlook command bodies.
 * Repository arguments are aliases, never materialized repository directories.
 * This is not a production CLI or a replacement for protocol authorization. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
#include <apr_strings.h>
#include <apr_uuid.h>
#include "internal.h"
#include "pgfs_native.h"
#include "svn_checksum.h"
#include "svn_dirent_uri.h"
#include "svn_hash.h"
#include "svn_io.h"
#include "svn_pools.h"

static svn_error_t *unsupported(const char *message)
{ return svn_error_create(SVN_ERR_UNSUPPORTED_FEATURE,NULL,message); }

static svn_error_t *context(pgvn_context **output,apr_pool_t *pool)
{
    const char *database=getenv("PGVN_UPSTREAM_DATABASE"),*dsn=getenv("PGVN_DSN");
    const char prefix[]="pgvn_m6_net_";
    if(GetACP()!=CP_UTF8 || !database || !dsn || strlen(database)!=sizeof(prefix)-1+12 ||
       strncmp(database,prefix,sizeof(prefix)-1))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Guarded UTF-8 PgVN upstream fixture is required");
    for(const char *p=database+sizeof(prefix)-1;*p;++p)
        if(!strchr("0123456789abcdef",*p))
            return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid upstream fixture database guard");
    char *parse_error=NULL;PQconninfoOption *parsed=PQconninfoParse(dsn,&parse_error);
    if(!parsed) {
        if(parse_error) PQfreemem(parse_error);
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid guarded fixture connection string");
    }
    unsigned matched=0;svn_boolean_t unsafe=FALSE;
    for(PQconninfoOption *option=parsed;option->keyword;++option) {
        if(!strcmp(option->keyword,"host")) {if(option->val && !strcmp(option->val,"127.0.0.1")) matched|=1;}
        else if(!strcmp(option->keyword,"port")) {if(option->val && !strcmp(option->val,"16432")) matched|=2;}
        else if(!strcmp(option->keyword,"dbname")) {if(option->val && !strcmp(option->val,database)) matched|=4;}
        else if(!strcmp(option->keyword,"user")) {if(option->val && !strcmp(option->val,"pgvn_admin")) matched|=8;}
        else if(!strcmp(option->keyword,"hostaddr") && option->val && *option->val && strcmp(option->val,"127.0.0.1")) unsafe=TRUE;
        else if(!strcmp(option->keyword,"service") && option->val && *option->val) unsafe=TRUE;
    }
    PQconninfoFree(parsed);
    if(matched!=15 || unsafe)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Fixture connection options do not match the required loopback database guard");
    pgvn_context_options options={dsn,16,5000};
    SVN_ERR(pgvn_context_create(output,&options,pool));
    if(strcmp(PQdb((*output)->control),database) || strcmp(PQhost((*output)->control),"127.0.0.1") ||
       strcmp(PQport((*output)->control),"16432") || strcmp(PQuser((*output)->control),"pgvn_admin"))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Upstream fixture connection does not match its guard");
    return SVN_NO_ERROR;
}

static svn_error_t *inside(const char **absolute,const char *path,apr_pool_t *pool)
{
    const char *base=getenv("PGVN_UPSTREAM_ROOT"),*root;
    if(!base || !*base || !path || !*path)
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Upstream fixture root and alias are required");
    SVN_ERR(svn_dirent_get_absolute(&root,base,pool));
    SVN_ERR(svn_dirent_get_absolute(absolute,path,pool));
    if(!strcmp(root,*absolute) || !svn_dirent_is_ancestor(root,*absolute))
        return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Upstream alias escapes its fixture root");
    return SVN_NO_ERROR;
}

static svn_error_t *alias(const char **name,const char **route,const char *path,apr_pool_t *pool)
{
    const char *absolute,*root;
    SVN_ERR(inside(&absolute,path,pool));
    SVN_ERR(svn_dirent_get_absolute(&root,getenv("PGVN_UPSTREAM_ROOT"),pool));
    const char *relative=svn_dirent_skip_ancestor(root,absolute);
    if(!relative || !*relative) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Empty upstream repository alias");
    svn_node_kind_t kind;SVN_ERR(svn_io_check_path(absolute,&kind,pool));
    if(kind!=svn_node_none)
        return svn_error_create(SVN_ERR_REPOS_BAD_ARGS,NULL,"Native repository alias must not be a local directory or file");
    svn_checksum_t *hash;SVN_ERR(svn_checksum(&hash,svn_checksum_sha1,relative,strlen(relative),pool));
    *name=apr_pstrcat(pool,"network-",svn_checksum_to_cstring(hash,pool),NULL);
    *route=apr_pstrcat(pool,"/",relative,NULL);
    return pgvn_validate_path(*route);
}

static svn_error_t *lookup(pgvn_binding **binding,pgvn_context *ctx,const char *path,apr_pool_t *pool)
{
    if(!strncmp(path,"pgvn:",5)) {
        apr_uuid_t parsed;
        if(apr_uuid_parse(&parsed,path+5)!=APR_SUCCESS)
            return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid fixture repository ID");
        return pgvn_repository_lookup(binding,ctx,apr_pstrcat(pool,"id:",path+5,NULL),pool);
    }
    const char *name,*route;SVN_ERR(alias(&name,&route,path,pool));
    SVN_ERR(pgvn_repository_lookup(binding,ctx,apr_pstrcat(pool,"name:",name,NULL),pool));
    /* Administrative name lookup deliberately has no selected route. Check
     * the exact alias route without requiring the repository to be enabled
     * (incremental hotcopy needs to open a disabled destination). */
    const char *values[]={route};PGresult *result;
    SVN_ERR(pgvn_exec(&result,ctx,ctx->control,
        "SELECT repository_id::text FROM pgvn.routes WHERE route_prefix=$1",1,values));
    svn_boolean_t matches=PQntuples(result)==1 &&
        !strcmp(PQgetvalue(result,0,0),(*binding)->repository_id);
    PQclear(result);
    if(!matches)
        return svn_error_create(SVN_ERR_FS_CONFLICT,NULL,"Fixture alias hash resolves to a different route");
    (*binding)->route_prefix=route;
    return SVN_NO_ERROR;
}

static svn_error_t *ledger(const char *operation,const char *path,const char *id,apr_pool_t *pool)
{
    const char *target=getenv("PGVN_UPSTREAM_LEDGER"),*absolute;
    if(!target) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Upstream fixture ledger is required");
    SVN_ERR(inside(&absolute,target,pool));
    if(strncmp(path,"pgvn:",5)) SVN_ERR(svn_dirent_get_absolute(&path,path,pool));
    apr_size_t size=strlen(path);
    if(size>16000) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Fixture alias is too long for its ledger");
    char *encoded=apr_palloc(pool,2*size+1);
    const char digits[]="0123456789abcdef";
    for(apr_size_t i=0;i<size;++i) {unsigned char c=(unsigned char)path[i];encoded[2*i]=digits[c>>4];encoded[2*i+1]=digits[c&15];}
    encoded[2*size]=0;
    const char *line=apr_psprintf(pool,"{\"operation\":\"%s\",\"repository_id\":\"%s\",\"alias_hex\":\"%s\",\"process_code_page\":%u}\n",operation,id,encoded,GetACP());
    int count=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,absolute,-1,NULL,0);
    if(!count) return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"Invalid fixture ledger path");
    wchar_t *wide=apr_palloc(pool,count*sizeof(*wide));
    if(!MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,absolute,-1,wide,count))
        return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"Cannot encode fixture ledger path");
    HANDLE file=CreateFileW(wide,GENERIC_READ|GENERIC_WRITE,FILE_SHARE_READ|FILE_SHARE_WRITE,NULL,OPEN_ALWAYS,FILE_ATTRIBUTE_NORMAL,NULL);
    if(file==INVALID_HANDLE_VALUE) return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"Cannot open fixture ledger");
    OVERLAPPED lock;memset(&lock,0,sizeof(lock));
    BOOL held=LockFileEx(file,LOCKFILE_EXCLUSIVE_LOCK,0,MAXDWORD,MAXDWORD,&lock);
    LARGE_INTEGER end;end.QuadPart=0;DWORD written=0;DWORD bytes=(DWORD)strlen(line);
    BOOL success=held && SetFilePointerEx(file,end,NULL,FILE_END) && WriteFile(file,line,bytes,&written,NULL) && written==bytes;
    if(held) UnlockFileEx(file,0,MAXDWORD,MAXDWORD,&lock);
    CloseHandle(file);
    return success ? SVN_NO_ERROR : svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"Cannot append fixture ledger");
}

svn_error_t *pgvn_network_repos_open3(svn_repos_t **repos,const char *path,apr_hash_t *config,
    apr_pool_t *result_pool,apr_pool_t *scratch_pool)
{
    pgvn_context *ctx;pgvn_binding *binding;pgvn_repository *repository;
    SVN_ERR(context(&ctx,result_pool));SVN_ERR(lookup(&binding,ctx,path,result_pool));
    SVN_ERR(pgvn_repos_open_admin(&repository,ctx,binding->repository_id,result_pool));
    SVN_ERR(ledger("open",path,binding->repository_id,result_pool));
    *repos=pgvn_repo_svn(repository);return SVN_NO_ERROR;
}

svn_error_t *pgvn_network_repos_create(svn_repos_t **repos,const char *path,const char *unused1,
    const char *unused2,apr_hash_t *config,apr_hash_t *fs_config,apr_pool_t *pool)
{
    if(config && apr_hash_count(config)) return unsupported("Per-directory repository configuration is not a PgVN fixture");
    for(apr_hash_index_t *i=fs_config ? apr_hash_first(pool,fs_config) : NULL;i;i=apr_hash_next(i)) {
        const char *key=apr_hash_this_key(i),*value=apr_hash_this_val(i);
        if(!strcmp(key,SVN_FS_CONFIG_FS_TYPE)) {
            if(strcmp(value,"pg") && strcmp(value,"pgvn")) return unsupported("Native fixture creation requires the PgVN PG backend");
        } else if(!strcmp(key,SVN_FS_CONFIG_COMPATIBLE_VERSION)) {
            if(strncmp(value,"1.14.",5)) return unsupported("Historical on-disk repository formats are not a PgVN fixture");
        } else if(!strcmp(key,SVN_FS_CONFIG_BDB_TXN_NOSYNC)) {
            if(strcmp(value,"0")) return unsupported("Berkeley DB transaction options are not a PgVN fixture");
        } else if(!strcmp(key,SVN_FS_CONFIG_BDB_LOG_AUTOREMOVE)) {
            if(strcmp(value,"1")) return unsupported("Berkeley DB log options are not a PgVN fixture");
        } else return unsupported("Unsupported per-filesystem create option in native PgVN fixture");
    }
    pgvn_context *ctx;pgvn_binding *binding;pgvn_repository *repository;const char *name,*route;
    SVN_ERR(context(&ctx,pool));SVN_ERR(alias(&name,&route,path,pool));
    SVN_ERR(pgvn_repo_create(&binding,ctx,name,route,NULL,pool));
    SVN_ERR(pgvn_repos_open_admin(&repository,ctx,binding->repository_id,pool));
    SVN_ERR(ledger("create",path,binding->repository_id,pool));
    *repos=pgvn_repo_svn(repository);return SVN_NO_ERROR;
}

svn_error_t *pgvn_network_repos_hotcopy3(const char *source,const char *destination,
    svn_boolean_t clean_logs,svn_boolean_t incremental,svn_repos_notify_func_t notify,void *notify_baton,
    svn_cancel_func_t cancel,void *cancel_baton,apr_pool_t *pool)
{
    if(clean_logs) return unsupported("Berkeley DB log cleanup is not a PgVN fixture");
    if(cancel) SVN_ERR(cancel(cancel_baton));
    pgvn_context *ctx;pgvn_binding *src,*dst=NULL;const char *name,*route;svn_revnum_t first=0,last;
    SVN_ERR(context(&ctx,pool));SVN_ERR(lookup(&src,ctx,source,pool));
    SVN_ERR(alias(&name,&route,destination,pool));
    if(incremental) {
        svn_error_t *error=lookup(&dst,ctx,destination,pool);
        if(error) {if(pgvn_error_exit_code(error)!=PGVN_EXIT_NOT_FOUND) return error;svn_error_clear(error);dst=NULL;}
        if(dst) {
            apr_pool_t *scratch=svn_pool_create(pool);pgvn_repository *old;
            SVN_ERR(pgvn_repos_open_admin(&old,ctx,dst->repository_id,scratch));
            SVN_ERR(svn_fs_youngest_rev(&first,svn_repos_fs(pgvn_repo_svn(old)),scratch));++first;
            svn_pool_destroy(scratch);
        }
    }
    SVN_ERR(pgvn_repo_hotcopy(&dst,ctx,src->repository_id,name,route,dst ? dst->repository_id : NULL,pool));
    pgvn_repository *copied;SVN_ERR(pgvn_repos_open_admin(&copied,ctx,dst->repository_id,pool));
    SVN_ERR(svn_fs_youngest_rev(&last,svn_repos_fs(pgvn_repo_svn(copied)),pool));
    SVN_ERR(ledger(incremental ? "hotcopy-incremental" : "hotcopy",destination,dst->repository_id,pool));
    if(notify && first<=last) {svn_repos_notify_t *event=svn_repos_notify_create(svn_repos_notify_hotcopy_rev_range,pool);event->start_revision=first;event->end_revision=last;notify(notify_baton,event,pool);}
    return SVN_NO_ERROR;
}

typedef struct freeze_item {const char *id;const char *alias;svn_fs_t *fs;} freeze_item;
static int compare_ids(const void *a,const void *b)
{return strcmp(((const freeze_item *)a)->id,((const freeze_item *)b)->id);}
static svn_error_t *freeze_locked(const apr_array_header_t *paths,svn_repos_freeze_func_t callback,void *baton,apr_pool_t *pool)
{
    pgvn_context *ctx;SVN_ERR(context(&ctx,pool));
    apr_array_header_t *items=apr_array_make(pool,paths->nelts,sizeof(freeze_item));
    for(int i=0;i<paths->nelts;++i) {
        pgvn_binding *binding;const char *path=APR_ARRAY_IDX(paths,i,const char *);
        SVN_ERR(lookup(&binding,ctx,path,pool));
        freeze_item *item=apr_array_push(items);item->id=binding->repository_id;item->alias=path;
    }
    qsort(items->elts,items->nelts,sizeof(freeze_item),compare_ids);
    for(int i=0;i<items->nelts;++i) {
        freeze_item *item=&APR_ARRAY_IDX(items,i,freeze_item);
        if(i && !strcmp(item->id,APR_ARRAY_IDX(items,i-1,freeze_item).id)) continue;
        pgvn_repository *repository;SVN_ERR(pgvn_repos_open_admin(&repository,ctx,item->id,pool));
        item->fs=svn_repos_fs(pgvn_repo_svn(repository));SVN_ERR(pgfs_native_exclusive(item->fs,pool));
        SVN_ERR(ledger("freeze",item->alias,item->id,pool));
    }
    return callback(baton,pool);
}
svn_error_t *pgvn_network_repos_freeze(const apr_array_header_t *paths,svn_repos_freeze_func_t callback,void *baton,apr_pool_t *pool)
{
    if(!paths || !paths->nelts || !callback) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Freeze requires repositories and a callback");
    apr_pool_t *held=svn_pool_create(pool);svn_error_t *error=freeze_locked(paths,callback,baton,held);svn_pool_destroy(held);return error;
}

svn_error_t *pgvn_network_repos_pack(svn_repos_t *repos,svn_repos_notify_func_t notify,void *notify_baton,
    svn_cancel_func_t cancel,void *cancel_baton,apr_pool_t *pool)
{
    if(cancel) SVN_ERR(cancel(cancel_baton));
    const char *path=svn_repos_path(repos,pool);
    if(!path || strncmp(path,"pgvn:",5)) return unsupported("Pack fixture requires an ID-backed PgVN repository");
    SVN_ERR(pgfs_native_pack(svn_repos_fs(repos),pool));
    return ledger("pack",path,path+5,pool);
}

svn_error_t *pgvn_network_repos_recover4(const char *path,svn_boolean_t nonblocking,
    svn_repos_notify_func_t notify,void *notify_baton,svn_cancel_func_t cancel,void *cancel_baton,apr_pool_t *pool)
{
    /* No file recovery is invoked. The existing PgVN maintenance operation
     * validates its SQL state; lock-wait behavior must be tested separately. */
    if(cancel) SVN_ERR(cancel(cancel_baton));
    apr_pool_t *scratch=svn_pool_create(pool);svn_repos_t *repos;
    svn_error_t *error=pgvn_network_repos_open3(&repos,path,NULL,scratch,scratch);
    if(!error && notify) {svn_repos_notify_t *event=svn_repos_notify_create(svn_repos_notify_recover_start,scratch);notify(notify_baton,event,scratch);}
    if(!error) error=pgfs_native_recover(svn_repos_fs(repos),scratch);
    if(!error) error=ledger("recover",path,svn_repos_path(repos,scratch)+5,scratch);
    svn_pool_destroy(scratch);return error;
}

svn_error_t *pgvn_network_repos_upgrade2(const char *path,svn_boolean_t nonblocking,
    svn_repos_notify_func_t notify,void *notify_baton,apr_pool_t *pool)
{return unsupported("Per-repository on-disk upgrade is unavailable; PgVN schema migration uses pgvnadmin db migrate");}
