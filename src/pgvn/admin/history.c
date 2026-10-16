#include <stdlib.h>
#include <string.h>
#include "admin.h"
#include <apr_base64.h>
#include <apr_strings.h>
#include "svn_io.h"
#include "svn_pools.h"
#include "svn_props.h"
#include "pgfs_native.h"

static int compare_strings(const void *a,const void *b)
{
    return strcmp(*(const char *const *)a,*(const char *const *)b);
}

static apr_array_header_t *keys(apr_hash_t *hash,apr_pool_t *pool)
{
    apr_array_header_t *names=apr_array_make(pool,apr_hash_count(hash),sizeof(const char *));
    for(apr_hash_index_t *i=apr_hash_first(pool,hash);i;i=apr_hash_next(i))
        APR_ARRAY_PUSH(names,const char *)=apr_hash_this_key(i);
    qsort(names->elts,names->nelts,sizeof(const char *),compare_strings);
    return names;
}

static void base64(const char *value,apr_size_t length)
{
    while(length) {
        char encoded[43700];
        int amount=length>32766 ? 32766 : (int)length;
        apr_base64_encode_binary(encoded,(const unsigned char *)value,amount);
        fputs(encoded,stdout); value+=amount; length-=amount;
    }
}

static svn_error_t *path_argument(const char **path,const char *value,apr_pool_t *pool)
{
    if(!value || !*value) value="/";
    *path=value[0]=='/' ? value : apr_pstrcat(pool,"/",value,NULL);
    return pgvn_validate_fspath(*path);
}

static svn_error_t *get_root(svn_fs_root_t **root,svn_fs_txn_t **txn,svn_revnum_t *revision,
    admin_command *command,svn_fs_t *fs)
{
    *txn=NULL; *revision=SVN_INVALID_REVNUM;
    if(command->values[OPT_TXN]) {
        SVN_ERR(svn_fs_open_txn(txn,fs,command->values[OPT_TXN],command->pool));
        return svn_fs_txn_root(root,*txn,command->pool);
    }
    svn_revnum_t end;
    SVN_ERR(admin_revision_range(revision,&end,command,fs,FALSE));
    if(*revision!=end) return admin_usage("look accepts one revision");
    if(!SVN_IS_VALID_REVNUM(*revision)) SVN_ERR(svn_fs_youngest_rev(revision,fs,command->pool));
    return svn_fs_revision_root(root,fs,*revision,command->pool);
}

static svn_error_t *write_stream(svn_stream_t *stream,svn_boolean_t json)
{
    if(json) fputs("{\"encoding\":\"base64\",\"data\":\"",stdout);
    for(;;) {
        char buffer[32766]; apr_size_t amount=sizeof(buffer);
        SVN_ERR(admin_cancel(NULL)); SVN_ERR(svn_stream_read_full(stream,buffer,&amount));
        if(json) base64(buffer,amount);
        else if(amount && fwrite(buffer,1,amount,stdout)!=amount)
            return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"Unable to write command output");
        if(amount<sizeof(buffer)) break;
    }
    SVN_ERR(svn_stream_close(stream));
    if(json) puts("\"}");
    return SVN_NO_ERROR;
}

typedef struct tree_item { const char *path; int depth; } tree_item;
static svn_error_t *tree(svn_fs_root_t *root,const char *initial,admin_command *command)
{
    apr_array_header_t *pending=apr_array_make(command->pool,16,sizeof(tree_item));
    tree_item *first=apr_array_push(pending); first->path=initial; first->depth=0;
    svn_boolean_t json=(command->flags & OPTION(OPT_JSON))!=0;
    int emitted=0;
    if(json) fputs("{\"nodes\":[",stdout);
    while(pending->nelts) {
        SVN_ERR(admin_cancel(NULL));
        tree_item item=APR_ARRAY_IDX(pending,pending->nelts-1,tree_item); --pending->nelts;
        apr_pool_t *scratch=svn_pool_create(command->pool);
        svn_node_kind_t kind; SVN_ERR(svn_fs_check_path(&kind,root,item.path,scratch));
        if(kind==svn_node_none) return svn_error_create(SVN_ERR_FS_NOT_FOUND,NULL,"Tree path does not exist");
        if(json) {
            if(emitted++) putchar(','); fputs("{\"path\":",stdout); admin_json_cstr(stdout,item.path);
            fputs(",\"kind\":",stdout); admin_json_cstr(stdout,kind==svn_node_dir ? "dir" : "file"); putchar('}');
        } else {
            if(!(command->flags & OPTION(OPT_FULL_PATHS))) for(int i=0;i<item.depth;++i) putchar(' ');
            const char *display=item.path;
            if(!(command->flags & OPTION(OPT_FULL_PATHS)) && item.depth) display=strrchr(item.path,'/')+1;
            else if(display[0]=='/' && display[1]) ++display;
            fputs(display,stdout);
            if(kind==svn_node_dir && strcmp(item.path,"/")) putchar('/');
            putchar('\n');
        }
        if(kind==svn_node_dir) {
            apr_hash_t *entries; SVN_ERR(svn_fs_dir_entries(&entries,root,item.path,scratch));
            apr_array_header_t *names=keys(entries,scratch);
            for(int i=names->nelts-1;i>=0;--i) {
                tree_item *child=apr_array_push(pending);
                child->path=apr_pstrcat(command->pool,!strcmp(item.path,"/") ? "" : item.path,"/",APR_ARRAY_IDX(names,i,const char *),NULL);
                child->depth=item.depth+1;
            }
        }
        svn_pool_destroy(scratch);
    }
    if(json) puts("]}");
    return SVN_NO_ERROR;
}

static int compare_changes(const void *a,const void *b)
{
    return strcmp((*(svn_fs_path_change3_t *const *)a)->path.data,(*(svn_fs_path_change3_t *const *)b)->path.data);
}

static svn_error_t *changed(svn_fs_root_t *root,admin_command *command)
{
    svn_fs_path_change_iterator_t *iterator;
    SVN_ERR(svn_fs_paths_changed3(&iterator,root,command->pool,command->pool));
    apr_array_header_t *changes=apr_array_make(command->pool,16,sizeof(svn_fs_path_change3_t *));
    for(;;) {
        svn_fs_path_change3_t *change; SVN_ERR(svn_fs_path_change_get(&change,iterator));
        if(!change) break;
        svn_fs_path_change3_t *copy=apr_pmemdup(command->pool,change,sizeof(*copy));
        copy->path.data=apr_pstrmemdup(command->pool,change->path.data,change->path.len);
        copy->copyfrom_path=change->copyfrom_path ? apr_pstrdup(command->pool,change->copyfrom_path) : NULL;
        APR_ARRAY_PUSH(changes,svn_fs_path_change3_t *)=copy;
    }
    qsort(changes->elts,changes->nelts,sizeof(svn_fs_path_change3_t *),compare_changes);
    svn_boolean_t json=(command->flags & OPTION(OPT_JSON))!=0;
    if(json) fputs("{\"changes\":[",stdout);
    for(int i=0;i<changes->nelts;++i) {
        svn_fs_path_change3_t *change=APR_ARRAY_IDX(changes,i,svn_fs_path_change3_t *);
        const char *action=change->change_kind==svn_fs_path_change_add ? "add" : change->change_kind==svn_fs_path_change_delete ? "delete" :
            change->change_kind==svn_fs_path_change_replace ? "replace" : "modify";
        if(json) {
            if(i) putchar(','); fputs("{\"path\":",stdout); admin_json_string(stdout,change->path.data,change->path.len);
            fputs(",\"action\":",stdout); admin_json_cstr(stdout,action);
            fputs(",\"kind\":",stdout); admin_json_cstr(stdout,change->node_kind==svn_node_dir ? "dir" : change->node_kind==svn_node_file ? "file" : "unknown");
            printf(",\"text_mod\":%s,\"prop_mod\":%s,\"copyfrom_rev\":%ld,\"copyfrom_path\":",
                change->text_mod ? "true" : "false",change->prop_mod ? "true" : "false",change->copyfrom_rev);
            admin_json_cstr(stdout,change->copyfrom_path); putchar('}');
        } else {
            char flag=change->change_kind==svn_fs_path_change_add ? 'A' : change->change_kind==svn_fs_path_change_delete ? 'D' :
                change->change_kind==svn_fs_path_change_replace ? 'R' : change->text_mod ? 'U' : '_';
            printf("%c%c  %s%s\n",flag,change->prop_mod ? 'U' : ' ',change->path.data[0]=='/' ? change->path.data+1 : change->path.data,
                change->node_kind==svn_node_dir ? "/" : "");
        }
    }
    if(json) puts("]}");
    return SVN_NO_ERROR;
}

static svn_error_t *properties(admin_command *command,svn_fs_t *fs,svn_fs_root_t *root,
    svn_fs_txn_t *txn,svn_revnum_t revision)
{
    svn_boolean_t get=!strcmp(command->argv[1],"propget");
    const char *name=get ? command->argv[3] : NULL;
    int path_index=get ? 4 : 3;
    const char *path=NULL;
    apr_hash_t *properties;
    if(command->flags & OPTION(OPT_REVPROP)) {
        if(command->argc>path_index) return admin_usage("Revision/transaction properties do not take a node path");
        if(txn) SVN_ERR(svn_fs_txn_proplist(&properties,txn,command->pool));
        else SVN_ERR(svn_fs_revision_proplist2(&properties,fs,revision,TRUE,command->pool,command->pool));
    } else {
        SVN_ERR(path_argument(&path,command->argc>path_index ? command->argv[path_index] : "/",command->pool));
        SVN_ERR(svn_fs_node_proplist(&properties,root,path,command->pool));
    }
    svn_boolean_t json=(command->flags & OPTION(OPT_JSON))!=0;
    if(get) {
        svn_string_t *value=apr_hash_get(properties,name,APR_HASH_KEY_STRING);
        if(!value) return svn_error_create(SVN_ERR_FS_NOT_FOUND,NULL,"Property does not exist");
        if(json) { fputs("{\"name\":",stdout); admin_json_cstr(stdout,name); fputs(",\"encoding\":\"base64\",\"value\":\"",stdout); base64(value->data,value->len); puts("\"}"); }
        else if(value->len && fwrite(value->data,1,value->len,stdout)!=value->len) return svn_error_create(SVN_ERR_IO_WRITE_ERROR,NULL,"Unable to write property output");
        return SVN_NO_ERROR;
    }
    apr_array_header_t *names=keys(properties,command->pool);
    if(json) fputs("{\"properties\":[",stdout);
    for(int i=0;i<names->nelts;++i) {
        name=APR_ARRAY_IDX(names,i,const char *); svn_string_t *value=apr_hash_get(properties,name,APR_HASH_KEY_STRING);
        if(json) {
            if(i) putchar(','); fputs("{\"name\":",stdout); admin_json_cstr(stdout,name);
            if(command->flags & OPTION(OPT_VERBOSE)) { fputs(",\"encoding\":\"base64\",\"value\":\"",stdout); base64(value->data,value->len); putchar('"'); }
            putchar('}');
        } else {
            puts(name);
            if(command->flags & OPTION(OPT_VERBOSE)) { fwrite(value->data,1,value->len,stdout); putchar('\n'); }
        }
    }
    if(json) puts("]}");
    return SVN_NO_ERROR;
}

static svn_error_t *dump_load_verify(admin_command *command,pgvn_repository *repository)
{
    svn_repos_t *repos=pgvn_repo_svn(repository); svn_fs_t *fs=svn_repos_fs(repos);
    const char *op=command->argv[0]; svn_revnum_t start,end;
    svn_boolean_t loading=!strcmp(op,"load");
    if(loading) {
        const char *values[]={repository->repository_id}; PGresult *result;
        SVN_ERR(pgvn_control_query(&result,command->context,"SELECT enabled FROM pgvn.repositories WHERE repository_id=$1::uuid",1,values));
        svn_boolean_t disabled=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"f"); PQclear(result);
        if(!disabled) return admin_usage("Disable the destination repository before loading a dump");
        SVN_ERR(pgfs_native_exclusive(fs,command->pool));
    }
    SVN_ERR(admin_revision_range(&start,&end,command,fs,loading));
    if(!strcmp(op,"verify")) {
        SVN_ERR(svn_repos_verify_fs3(repos,start,end,FALSE,FALSE,NULL,NULL,NULL,NULL,admin_cancel,NULL,command->pool));
        if(!SVN_IS_VALID_REVNUM(start)) start=0;
        if(!SVN_IS_VALID_REVNUM(end)) SVN_ERR(svn_fs_youngest_rev(&end,fs,command->pool));
        printf("{\"ok\":true,\"start\":%ld,\"end\":%ld,\"verified_revisions\":%ld}\n",start,end,end-start+1);
        return SVN_NO_ERROR;
    }
    svn_stream_t *stream;
    if(command->values[OPT_FILE]) {
        if(loading) SVN_ERR(svn_stream_open_readonly(&stream,command->values[OPT_FILE],command->pool,command->pool));
        else {
            apr_file_t *file;
            SVN_ERR(svn_io_file_open(&file,command->values[OPT_FILE],APR_WRITE|APR_CREATE|APR_EXCL|APR_BINARY,APR_OS_DEFAULT,command->pool));
            stream=svn_stream_from_aprfile2(file,FALSE,command->pool);
        }
    } else if(loading) SVN_ERR(svn_stream_for_stdin2(&stream,TRUE,command->pool));
    else SVN_ERR(svn_stream_for_stdout(&stream,command->pool));
    svn_error_t *error;
    if(loading) {
        enum svn_repos_load_uuid uuid_action=command->flags & OPTION(OPT_FORCE_UUID) ? svn_repos_load_uuid_force :
            command->flags & OPTION(OPT_IGNORE_UUID) ? svn_repos_load_uuid_ignore : svn_repos_load_uuid_default;
        svn_boolean_t hooks=(command->flags & OPTION(OPT_USE_HOOKS))!=0;
        error=svn_repos_load_fs6(repos,stream,start,end,uuid_action,NULL,hooks,hooks,TRUE,FALSE,FALSE,NULL,NULL,admin_cancel,NULL,command->pool);
    } else error=svn_repos_dump_fs4(repos,stream,start,end,(command->flags & OPTION(OPT_INCREMENTAL))!=0,
        (command->flags & OPTION(OPT_DELTAS))!=0,TRUE,TRUE,NULL,NULL,NULL,NULL,admin_cancel,NULL,command->pool);
    error=svn_error_compose_create(error,svn_stream_close(stream));
    if(error) return error;
    if(loading) {
        svn_revnum_t head; SVN_ERR(svn_fs_youngest_rev(&head,fs,command->pool));
        printf("{\"ok\":true,\"head\":%ld}\n",head);
    }
    return SVN_NO_ERROR;
}

svn_error_t *admin_history(admin_command *command)
{
    svn_boolean_t look=!strcmp(command->argv[0],"look");
    pgvn_repository *repository;
    SVN_ERR(admin_open(&repository,command,command->argv[look ? 2 : 1]));
    if(!look) return dump_load_verify(command,repository);
    svn_fs_t *fs=svn_repos_fs(pgvn_repo_svn(repository));
    const char *op=command->argv[1];
    svn_boolean_t json=(command->flags & OPTION(OPT_JSON))!=0;
    if(!strcmp(op,"youngest")) {
        svn_revnum_t head; SVN_ERR(svn_fs_youngest_rev(&head,fs,command->pool));
        printf(json ? "{\"youngest\":%ld}\n" : "%ld\n",head); return SVN_NO_ERROR;
    }
    if(!strcmp(op,"uuid")) {
        const char *uuid; SVN_ERR(svn_fs_get_uuid(fs,&uuid,command->pool));
        if(json) { fputs("{\"uuid\":",stdout); admin_json_cstr(stdout,uuid); puts("}"); }
        else puts(uuid);
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"transactions")) {
        apr_array_header_t *names; SVN_ERR(svn_fs_list_transactions(&names,fs,command->pool));
        qsort(names->elts,names->nelts,sizeof(const char *),compare_strings);
        if(json) fputs("{\"transactions\":[",stdout);
        for(int i=0;i<names->nelts;++i) {
            if(json) { if(i) putchar(','); admin_json_cstr(stdout,APR_ARRAY_IDX(names,i,const char *)); }
            else puts(APR_ARRAY_IDX(names,i,const char *));
        }
        if(json) puts("]}");
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"txn-info") && !command->values[OPT_TXN]) return admin_usage("txn-info requires --txn");
    svn_fs_root_t *root; svn_fs_txn_t *txn; svn_revnum_t revision;
    SVN_ERR(get_root(&root,&txn,&revision,command,fs));
    if(!strcmp(op,"info") || !strcmp(op,"txn-info")) {
        apr_hash_t *props;
        if(txn) SVN_ERR(svn_fs_txn_proplist(&props,txn,command->pool));
        else SVN_ERR(svn_fs_revision_proplist2(&props,fs,revision,TRUE,command->pool,command->pool));
        svn_string_t *author=apr_hash_get(props,SVN_PROP_REVISION_AUTHOR,APR_HASH_KEY_STRING);
        svn_string_t *date=apr_hash_get(props,SVN_PROP_REVISION_DATE,APR_HASH_KEY_STRING);
        svn_string_t *log=apr_hash_get(props,SVN_PROP_REVISION_LOG,APR_HASH_KEY_STRING);
        printf("{\"revision\":%ld,\"transaction\":",revision); admin_json_cstr(stdout,command->values[OPT_TXN]);
        fputs(",\"encoding\":\"base64\",\"author\":\"",stdout); if(author) base64(author->data,author->len);
        fputs("\",\"date\":\"",stdout); if(date) base64(date->data,date->len);
        fputs("\",\"log\":\"",stdout); if(log) base64(log->data,log->len); puts("\"}");
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"changed")) return changed(root,command);
    if(!strcmp(op,"proplist") || !strcmp(op,"propget")) return properties(command,fs,root,txn,revision);
    const char *path; SVN_ERR(path_argument(&path,command->argc>3 ? command->argv[3] : "/",command->pool));
    if(!strcmp(op,"tree")) return tree(root,path,command);
    svn_stream_t *stream; SVN_ERR(svn_fs_file_contents(&stream,root,path,command->pool));
    return write_stream(stream,json);
}
