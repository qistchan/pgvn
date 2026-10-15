/* Native FS API test driver. Uses the public API, not PgFS internals. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <apr_general.h>
#include <apr_strings.h>
#include "svn_error.h"
#include "svn_fs.h"
#include "svn_io.h"
#include "svn_pools.h"
#include "svn_props.h"

#include "snapshot_probe.inc"

static svn_error_t *run(int argc,char **argv,apr_pool_t *pool)
{
    if(argc<3) return svn_error_create(SVN_ERR_CL_ARG_PARSING_ERROR,NULL,"fs_probe OP REPO [ARGS]");
    svn_fs_t *fs;
    const char *db=apr_pstrcat(pool,argv[2],"/db",NULL);
    SVN_ERR(svn_fs_open2(&fs,db,NULL,pool,pool));
    if(!strcmp(argv[1],"snapshot")) return probe_snapshot(fs,atol(argv[3]),pool);
    if(!strcmp(argv[1],"delete-fs")) return svn_fs_delete_fs(db,pool);
    if(!strcmp(argv[1],"revision-mkdir")) {
        svn_fs_root_t *root;
        SVN_ERR(svn_fs_revision_root(&root,fs,atol(argv[3]),pool));
        return svn_fs_make_dir(root,argv[4],pool);
    }
    if(!strcmp(argv[1],"relation") || !strcmp(argv[1],"relation-other-fs")) {
        svn_fs_t *other=fs;
        if(!strcmp(argv[1],"relation-other-fs")) SVN_ERR(svn_fs_open2(&other,db,NULL,pool,pool));
        svn_fs_root_t *a,*b;
        SVN_ERR(svn_fs_revision_root(&a,fs,atol(argv[3]),pool));
        SVN_ERR(svn_fs_revision_root(&b,other,atol(argv[5]),pool));
        svn_fs_node_relation_t relation;
        SVN_ERR(svn_fs_node_relation(&relation,a,argv[4],b,argv[6],pool));
        printf("%d\n",(int)relation);
        return SVN_NO_ERROR;
    }
    if(!strcmp(argv[1],"begin")) {
        svn_fs_txn_t *txn;
        svn_revnum_t base=argc>3 ? atol(argv[3]) : 0;
        SVN_ERR(svn_fs_begin_txn2(&txn,fs,base,argc>4 ? (apr_uint32_t)atoi(argv[4]) : 0,pool));
        const char *name;
        SVN_ERR(svn_fs_txn_name(&name,txn,pool));
        printf("%s\n",name);
        return SVN_NO_ERROR;
    }
    if(!strcmp(argv[1],"binary-revprop")) {
        const char bytes[]={'a',0,'b',(char)0xff};
        svn_string_t *value=svn_string_ncreate(bytes,sizeof(bytes),pool);
        SVN_ERR(svn_fs_change_rev_prop2(fs,atol(argv[3]),"test:raw",NULL,value,pool));
        svn_string_t *actual;
        SVN_ERR(svn_fs_revision_prop2(&actual,fs,atol(argv[3]),"test:raw",TRUE,pool,pool));
        if(!actual || !svn_string_compare(value,actual)) return svn_error_create(SVN_ERR_TEST_FAILED,NULL,"Binary revprop roundtrip failed");
        svn_string_t *wrong=svn_string_create("wrong",pool);
        svn_error_t *error=svn_fs_change_rev_prop2(fs,atol(argv[3]),"test:raw",(const svn_string_t *const *)&wrong,NULL,pool);
        if(!error || error->apr_err!=SVN_ERR_FS_PROP_BASEVALUE_MISMATCH) return svn_error_create(SVN_ERR_TEST_FAILED,error,"Revprop CAS mismatch not rejected");
        svn_error_clear(error);
        SVN_ERR(svn_fs_change_rev_prop2(fs,atol(argv[3]),"test:raw",(const svn_string_t *const *)&actual,NULL,pool));
        printf("binary property and CAS verified\n");
        return SVN_NO_ERROR;
    }
    if(argc<4) return svn_error_create(SVN_ERR_CL_ARG_PARSING_ERROR,NULL,"Transaction ID required");
    svn_fs_txn_t *txn;
    SVN_ERR(svn_fs_open_txn(&txn,fs,argv[3],pool));
    if(!strcmp(argv[1],"commit")) {
        const char *conflict=NULL; svn_revnum_t rev=SVN_INVALID_REVNUM;
        svn_error_t *error=svn_fs_commit_txn(&conflict,&rev,txn,pool);
        if(error) { fprintf(stderr,"conflict=%s new_rev=%ld\n",conflict ? conflict : "",rev); return error; }
        printf("%ld\n",rev);
        return SVN_NO_ERROR;
    }
    if(!strcmp(argv[1],"abort")) return svn_fs_abort_txn(txn,pool);
    svn_fs_root_t *root;
    SVN_ERR(svn_fs_txn_root(&root,txn,pool));
    if(!strcmp(argv[1],"verify-txn")) return svn_fs_verify_root(root,pool);
    if(argc<5) return svn_error_create(SVN_ERR_CL_ARG_PARSING_ERROR,NULL,"Path required");
    const char *path=argv[4];
    if(!strcmp(argv[1],"relation-txn")) {
        svn_fs_root_t *base;
        SVN_ERR(svn_fs_revision_root(&base,fs,svn_fs_txn_base_revision(txn),pool));
        svn_fs_node_relation_t relation;
        SVN_ERR(svn_fs_node_relation(&relation,base,path,root,path,pool));
        printf("%d\n",(int)relation); return SVN_NO_ERROR;
    }
    if(!strcmp(argv[1],"stable-id")) {
        const svn_fs_id_t *before,*after;
        SVN_ERR(svn_fs_node_id(&before,root,path,pool));
        svn_string_t *a=svn_fs_unparse_id(before,pool);
        SVN_ERR(svn_fs_change_node_prop(root,path,"test:identity",svn_string_create("value",pool),pool));
        SVN_ERR(svn_fs_node_id(&after,root,path,pool));
        svn_string_t *b=svn_fs_unparse_id(after,pool);
        if(!svn_string_compare(a,b)) return svn_error_create(SVN_ERR_TEST_FAILED,NULL,"Mutable node ID changed within one transaction");
        printf("stable mutable node ID\n"); return SVN_NO_ERROR;
    }

    if(!strcmp(argv[1],"mkdir")) return svn_fs_make_dir(root,path,pool);
    if(!strcmp(argv[1],"mkfile")) return svn_fs_make_file(root,path,pool);
    if(!strcmp(argv[1],"delete")) return svn_fs_delete(root,path,pool);
    if(!strcmp(argv[1],"binary-nodeprop")) {
        const char bytes[]={0,'x',(char)0xff,0};
        svn_string_t *value=svn_string_ncreate(bytes,sizeof(bytes),pool);
        SVN_ERR(svn_fs_change_node_prop(root,path,"test:raw",value,pool));
        svn_string_t *actual;
        SVN_ERR(svn_fs_node_prop(&actual,root,path,"test:raw",pool));
        if(!actual || !svn_string_compare(value,actual)) return svn_error_create(SVN_ERR_TEST_FAILED,NULL,"Binary nodeprop roundtrip failed");
        return SVN_NO_ERROR;
    }
    if(!strcmp(argv[1],"hold-writer")) {
        if(argc<7) return svn_error_create(SVN_ERR_CL_ARG_PARSING_ERROR,NULL,"Marker and release paths required");
        svn_stream_t *stream;
        SVN_ERR(svn_fs_apply_text(&stream,root,path,NULL,pool));
        apr_size_t length=7;
        SVN_ERR(svn_stream_write(stream,"partial",&length));
        SVN_ERR(svn_io_file_create(argv[5],"writer ready",pool));
        for(int i=0;i<300;++i) {
            FILE *release=fopen(argv[6],"rb");
            if(release) { fclose(release); return svn_stream_close(stream); }
            apr_sleep(100000);
        }
        return svn_error_create(SVN_ERR_TEST_FAILED,NULL,"Held writer timeout");
    }
    if(!strcmp(argv[1],"abandon")) {
        svn_stream_t *stream;
        return svn_fs_apply_text(&stream,root,path,NULL,pool);
    }
    if(!strcmp(argv[1],"write")) {
        if(argc<6) return svn_error_create(SVN_ERR_CL_ARG_PARSING_ERROR,NULL,"Input file required");
        svn_stream_t *input,*output;
        SVN_ERR(svn_stream_open_readonly(&input,argv[5],pool,pool));
        SVN_ERR(svn_fs_apply_text(&output,root,path,argc>6 ? argv[6] : NULL,pool));
        return svn_stream_copy3(input,output,NULL,NULL,pool);
    }
    return svn_error_create(SVN_ERR_CL_ARG_PARSING_ERROR,NULL,"Unknown probe operation");
}

int main(int argc,char **argv)
{
    if(apr_initialize()!=APR_SUCCESS) return 2;
    apr_pool_t *pool=svn_pool_create(NULL);
    svn_error_t *error=svn_fs_initialize(pool);
    if(!error) error=run(argc,argv,pool);
    int code=0;
    if(error) {
        svn_handle_error2(error,stderr,FALSE,"fs_probe: ");
        svn_error_clear(error); code=1;
    }
    svn_pool_destroy(pool); apr_terminate();
    return code;
}
