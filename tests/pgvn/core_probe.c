/* Native public FS/repos API probe for the directory-free core, not a product CLI. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <io.h>
#include "internal.h"
#include "credentials.h"
#include "svn_checksum.h"
#include <apr_general.h>
#include <apr_strings.h>
#include "svn_io.h"
#include "svn_pools.h"
#include "svn_props.h"
#include "pgfs_native.h"
#include "../snapshot_probe.inc"

static void string_json(const char *value)
{
    if(!value) { fputs("null",stdout); return; }
    putchar('"');
    for(const unsigned char *p=(const unsigned char *)value;*p;++p) {
        if(*p=='"' || *p=='\\') { putchar('\\'); putchar(*p); }
        else if(*p<32) printf("\\u%04x",*p);
        else putchar(*p);
    }
    putchar('"');
}

static svn_error_t *read_text(const char **text,const char *path,apr_pool_t *pool)
{
    svn_stringbuf_t *buffer;
    SVN_ERR(svn_stringbuf_from_file2(&buffer,path,pool));
    *text=buffer->data;
    return SVN_NO_ERROR;
}

static svn_error_t *run(int argc,const char **argv,apr_pool_t *pool)
{
    if(argc<2) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"core_probe operation required");
    const char *dsn=getenv("PGVN_DSN");
    pgvn_context_options options={dsn,3,5000};
    pgvn_context *context;
    SVN_ERR(pgvn_context_create(&context,&options,pool));
    const char *op=argv[1];
    if(!strcmp(op,"credential-digest") && (argc==5 || argc==6)) {
        pgvn_credential_key *key; const char *password; apr_int64_t generation;
        SVN_ERR(pgvn_keyring_load(&key,argv[2],argc==6 ? argv[5] : NULL,pool));
        SVN_ERR(pgvn_credential_get(&password,&generation,context,key,argv[3],argv[4],pool));
        if(!password) return svn_error_create(SVN_ERR_AUTHN_FAILED,NULL,"Credential is unavailable");
        svn_checksum_t *checksum;
        SVN_ERR(svn_checksum(&checksum,svn_checksum_sha1,password,strlen(password),pool));
        printf("{\"generation\":%" APR_INT64_T_FMT ",\"sha1\":\"%s\"}\n",generation,svn_checksum_to_cstring(checksum,pool));
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"credential-guard") && argc==6) {
        apr_int64_t pause=apr_atoi64(argv[5]);
        if(pause<0 || pause>30000) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Invalid probe hold interval");
        SVN_ERR(pgvn_control_begin(context));
        svn_error_t *error=pgvn_credential_guard(context,context->control,argv[2],argv[3],apr_atoi64(argv[4]),pool);
        if(!error) { puts("locked"); fflush(stdout); Sleep((DWORD)pause); }
        return pgvn_control_end(context,error);
    }
    if(!strcmp(op,"create") && argc>=4) {
        pgvn_binding *binding;
        SVN_ERR(pgvn_repo_create(&binding,context,argv[2],argv[3],argc>4 ? argv[4] : NULL,pool));
        fputs("{\"id\":",stdout); string_json(binding->repository_id);
        fputs(",\"uuid\":",stdout); string_json(binding->svn_uuid);
        fputs(",\"name\":",stdout); string_json(binding->canonical_name);
        printf(",\"generation\":%lld}\n",binding->configuration_version);
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"resolve") && argc==3) {
        pgvn_binding *binding;
        SVN_ERR(pgvn_catalog_resolve(&binding,context,argv[2],pool));
        fputs("{\"id\":",stdout); string_json(binding->repository_id);
        fputs(",\"path\":",stdout); string_json(binding->in_repository_path);
        fputs(",\"name\":",stdout); string_json(binding->canonical_name);
        printf(",\"generation\":%lld}\n",binding->configuration_version);
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"publish") && argc>=6) {
        const char *authz,*groups=NULL;
        SVN_ERR(read_text(&authz,argv[4],pool));
        if(argc>6) SVN_ERR(read_text(&groups,argv[6],pool));
        SVN_ERR(pgvn_policy_publish(context,argv[2],apr_atoi64(argv[3]),authz,groups,argv[5],"test-admin",pool));
        puts("published");
        return SVN_NO_ERROR;
    }
    if(argc<3) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Repository ID required");
    pgvn_repository *repository;
    apr_pool_t *repo_pool=svn_pool_create(pool);
    SVN_ERR(pgvn_repos_open(&repository,context,argv[2],repo_pool));
    svn_repos_t *repos=pgvn_repo_svn(repository);
    svn_fs_t *fs=svn_repos_fs(repos);
    if(!strcmp(op,"info")) {
        const char *uuid; svn_revnum_t head; apr_array_header_t *files;
        const svn_fs_info_placeholder_t *info;
        SVN_ERR(svn_fs_get_uuid(fs,&uuid,pool));
        SVN_ERR(svn_fs_youngest_rev(&head,fs,pool));
        SVN_ERR(svn_fs_info_config_files(&files,fs,pool,pool));
        SVN_ERR(svn_fs_info(&info,fs,pool,pool));
        fputs("{\"uuid\":",stdout); string_json(uuid);
        fputs(",\"identity\":",stdout); string_json(svn_repos_path(repos,pool));
        fputs(",\"fs_type\":",stdout); string_json(info->fs_type);
        printf(",\"head\":%ld,\"config_files\":%d}\n",head,files->nelts);
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"hold-open")) {
        puts("READY"); fflush(stdout);
        if(getchar()==EOF) return svn_error_create(SVN_ERR_CANCELLED,NULL,"Probe input closed");
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"metrics")) {
        pgvn_metrics before,after,closed,snapshot1,snapshot2;
        pgvn_metrics_snapshot(&before,context);
        for(int i=0;i<20;++i) { svn_revnum_t head; SVN_ERR(svn_fs_youngest_rev(&head,fs,pool)); }
        pgvn_metrics_snapshot(&after,context);
        pgvn_status *status;
        pgvn_metrics_snapshot(&snapshot1,context);
        SVN_ERR(pgvn_status_snapshot(&status,context,pool));
        pgvn_metrics_snapshot(&snapshot2,context);
        svn_pool_destroy(repo_pool);
        pgvn_metrics_snapshot(&closed,context);
        printf("{\"sql_delta\":%lld,\"errors\":%lld,\"open\":%d,\"closed\":%d,\"snapshot_sql\":%lld,\"ready\":%s}\n",
            (long long)(after.sql_calls-before.sql_calls),(long long)after.sql_errors,
            before.connections,closed.connections,(long long)(snapshot2.sql_calls-snapshot1.sql_calls),status->ready ? "true" : "false");
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"capacity")) {
        int opened=1;
        svn_error_t *error=NULL;
        for(int i=0;i<32;++i) {
            pgvn_repository *other;
            apr_pool_t *child=svn_pool_create(repo_pool);
            error=pgvn_repos_open(&other,context,argv[2],child);
            if(error) break;
            ++opened;
        }
        if(!error) return svn_error_create(SVN_ERR_TEST_FAILED,NULL,"Connection limit was not enforced");
        if(!error->message || !strstr(error->message,"connection limit")) return error;
        svn_error_clear(error);
        pgvn_metrics at_limit,closed;
        pgvn_metrics_snapshot(&at_limit,context);
        svn_pool_destroy(repo_pool);
        pgvn_metrics_snapshot(&closed,context);
        printf("{\"opened_fs\":%d,\"connections_at_limit\":%d,\"connections_after_close\":%d}\n",
            opened,at_limit.connections,closed.connections);
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"verify")) {
        svn_revnum_t head; SVN_ERR(svn_fs_youngest_rev(&head,fs,pool));
        for(svn_revnum_t rev=0;rev<=head;++rev) {
            apr_pool_t *iteration=svn_pool_create(pool);
            svn_fs_root_t *root;
            SVN_ERR(svn_fs_revision_root(&root,fs,rev,iteration));
            SVN_ERR(svn_fs_verify_root(root,iteration));
            svn_pool_destroy(iteration);
        }
        printf("%ld\n",head+1);
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"snapshot") && argc==4) return probe_snapshot(fs,atol(argv[3]),pool);
    if(!strcmp(op,"cat") && argc==5) {
        svn_fs_root_t *root; svn_stream_t *stream;
        SVN_ERR(svn_fs_revision_root(&root,fs,atol(argv[3]),pool));
        SVN_ERR(svn_fs_file_contents(&stream,root,argv[4],pool));
        for(;;) {
            char bytes[65536]; apr_size_t size=sizeof(bytes);
            SVN_ERR(svn_stream_read_full(stream,bytes,&size));
            if(size) fwrite(bytes,1,size,stdout);
            if(!size) break;
        }
        return svn_stream_close(stream);
    }
    if(!strcmp(op,"policy") && argc==6) {
        svn_boolean_t allowed;
        SVN_ERR(pgvn_policy_check(&allowed,repository->policy,!strcmp(argv[3],"-") ? NULL : argv[3],argv[4],
            !strcmp(argv[5],"write") ? svn_authz_write : svn_authz_read,pool));
        puts(allowed ? "allowed" : "denied");
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"pack")) return pgfs_native_pack(fs,pool);
    if(!strcmp(op,"recover")) return pgfs_native_recover(fs,pool);
    if(argc<4) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Username required");
    svn_fs_access_t *access;
    SVN_ERR(svn_fs_create_access(&access,argv[3],pool));
    SVN_ERR(svn_fs_set_access(fs,access));
    if(!strcmp(op,"receipt") && argc==5) {
        svn_revnum_t revision;
        SVN_ERR(pgvn_transaction_receipt(&revision,repository,argv[4],argv[3],pool));
        printf("revision=%ld\n",revision);
        return SVN_NO_ERROR;
    }
    if(!strcmp(op,"begin")) {
        svn_revnum_t head; svn_fs_txn_t *txn;
        SVN_ERR(svn_fs_youngest_rev(&head,fs,pool));
        apr_hash_t *properties=apr_hash_make(pool);
        apr_hash_set(properties,SVN_PROP_REVISION_AUTHOR,APR_HASH_KEY_STRING,svn_string_create(argv[3],pool));
        apr_hash_set(properties,SVN_PROP_REVISION_LOG,APR_HASH_KEY_STRING,svn_string_create("PgVN M1 test",pool));
        SVN_ERR(svn_repos_fs_begin_txn_for_commit2(&txn,repos,head,properties,pool));
        const char *id; SVN_ERR(svn_fs_txn_name(&id,txn,pool)); puts(id);
        return SVN_NO_ERROR;
    }
    if(argc<5) return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Transaction required");
    svn_fs_txn_t *txn; SVN_ERR(svn_fs_open_txn(&txn,fs,argv[4],pool));
    if(!strcmp(op,"commit") || !strcmp(op,"commit-paused")) {
        svn_revnum_t revision; const char *conflict=NULL;
        svn_error_t *error;
        if(!strcmp(op,"commit-paused")) {
            puts("READY"); fflush(stdout);
            if(getchar()==EOF) return svn_error_create(SVN_ERR_CANCELLED,NULL,"Probe input closed");
            /* Intentionally bypass repos pre-hooks to exercise the storage guard. */
            error=svn_fs_commit_txn(&conflict,&revision,txn,pool);
        } else error=svn_repos_fs_commit_txn(&conflict,repos,&revision,txn,pool);
        printf("revision=%ld\n",revision); fflush(stdout);
        return error;
    }
    if(!strcmp(op,"abort")) return svn_fs_abort_txn(txn,pool);
    svn_fs_root_t *root; SVN_ERR(svn_fs_txn_root(&root,txn,pool));
    if(!strcmp(op,"mkdir") && argc==6) return svn_fs_make_dir(root,argv[5],pool);
    if(!strcmp(op,"delete") && argc==6) return svn_fs_delete(root,argv[5],pool);
    if(!strcmp(op,"copy") && argc==8) {
        svn_fs_root_t *source; SVN_ERR(svn_fs_revision_root(&source,fs,atol(argv[5]),pool));
        return svn_fs_copy(source,argv[6],root,argv[7],pool);
    }
    if((!strcmp(op,"put") || !strcmp(op,"hold-writer")) && argc==7) {
        svn_node_kind_t kind; SVN_ERR(svn_fs_check_path(&kind,root,argv[5],pool));
        if(kind==svn_node_none) SVN_ERR(svn_fs_make_file(root,argv[5],pool));
        svn_stream_t *destination,*source;
        SVN_ERR(svn_fs_apply_text(&destination,root,argv[5],NULL,pool));
        SVN_ERR(svn_stream_open_readonly(&source,argv[6],pool,pool));
        for(;;) {
            char bytes[65536]; apr_size_t size=sizeof(bytes);
            SVN_ERR(svn_stream_read_full(source,bytes,&size));
            if(!size) break;
            SVN_ERR(svn_stream_write(destination,bytes,&size));
        }
        SVN_ERR(svn_stream_close(source));
        if(!strcmp(op,"hold-writer")) {
            puts("READY"); fflush(stdout);
            if(getchar()==EOF) return svn_error_create(SVN_ERR_CANCELLED,NULL,"Probe input closed");
        }
        return svn_stream_close(destination);
    }
    return svn_error_create(SVN_ERR_INCORRECT_PARAMS,NULL,"Unsupported core probe command");
}

int wmain(int argc,wchar_t **wide)
{
    _setmode(_fileno(stdout),_O_BINARY);
    if(apr_initialize()!=APR_SUCCESS) return 1;
    apr_pool_t *pool=svn_pool_create(NULL);
    const char **argv=apr_palloc(pool,(argc+1)*sizeof(*argv));
    for(int i=0;i<argc;++i) {
        int length=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,wide[i],-1,NULL,0,NULL,NULL);
        if(!length) return 2;
        char *utf8=apr_palloc(pool,length);
        WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,wide[i],-1,utf8,length,NULL,NULL);
        argv[i]=utf8;
    }
    argv[argc]=NULL;
    svn_error_t *error=run(argc,argv,pool);
    int code=error ? 1 : 0;
    if(error) { svn_handle_error2(error,stderr,FALSE,"PgVN: "); svn_error_clear(error); }
    svn_pool_destroy(pool); apr_terminate();
    return code;
}
