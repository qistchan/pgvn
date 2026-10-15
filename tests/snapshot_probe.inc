/* Stable, binary-safe logical snapshot for comparing actual repository data. */
static void probe_hex(const char *data,apr_size_t length)
{
    static const char hex[]="0123456789abcdef";
    for(apr_size_t i=0;i<length;++i) {
        unsigned char byte=(unsigned char)data[i];
        putchar(hex[byte>>4]); putchar(hex[byte&15]);
    }
}

static int probe_compare_names(const void *left,const void *right)
{
    return strcmp(*(const char *const *)left,*(const char *const *)right);
}

static svn_error_t *probe_snapshot_node(svn_fs_root_t *root,const char *path,apr_pool_t *pool)
{
    svn_node_kind_t kind;
    SVN_ERR(svn_fs_check_path(&kind,root,path,pool));
    svn_revnum_t created,origin,copyrev;
    const char *created_path,*copypath;
    SVN_ERR(svn_fs_node_created_rev(&created,root,path,pool));
    SVN_ERR(svn_fs_node_origin_rev(&origin,root,path,pool));
    SVN_ERR(svn_fs_node_created_path(&created_path,root,path,pool));
    SVN_ERR(svn_fs_copied_from(&copyrev,&copypath,root,path,pool));
    printf("N|"); probe_hex(path,strlen(path));
    printf("|%d|%ld|%ld|",(int)kind,created,origin);
    probe_hex(created_path,strlen(created_path)); printf("|%ld|",copyrev);
    if(copypath) probe_hex(copypath,strlen(copypath));
    if(kind==svn_node_file) {
        svn_stream_t *stream;
        SVN_ERR(svn_fs_file_contents(&stream,root,path,pool));
        svn_checksum_ctx_t *ctx=svn_checksum_ctx_create(svn_checksum_sha1,pool);
        svn_filesize_t length=0; char *buffer=apr_palloc(pool,65536);
        for(;;) {
            apr_size_t amount=65536;
            SVN_ERR(svn_stream_read_full(stream,buffer,&amount));
            SVN_ERR(svn_checksum_update(ctx,buffer,amount));
            length+=(svn_filesize_t)amount;
            if(!amount) break;
        }
        SVN_ERR(svn_stream_close(stream));
        svn_checksum_t *checksum; SVN_ERR(svn_checksum_final(&checksum,ctx,pool));
        svn_filesize_t reported;
        SVN_ERR(svn_fs_file_length(&reported,root,path,pool));
        if(length!=reported) return svn_error_create(SVN_ERR_TEST_FAILED,NULL,"Snapshot file length mismatch");
        printf("|%" APR_INT64_T_FMT "|%s",length,svn_checksum_to_cstring(checksum,pool));
    }
    putchar('\n');
    apr_hash_t *props; SVN_ERR(svn_fs_node_proplist(&props,root,path,pool));
    apr_array_header_t *names=apr_array_make(pool,(int)apr_hash_count(props),sizeof(const char *));
    for(apr_hash_index_t *hi=apr_hash_first(pool,props);hi;hi=apr_hash_next(hi)) {
        const void *key; apr_hash_this(hi,&key,NULL,NULL); APR_ARRAY_PUSH(names,const char *)=key;
    }
    qsort(names->elts,(size_t)names->nelts,sizeof(const char *),probe_compare_names);
    for(int i=0;i<names->nelts;++i) {
        const char *name=APR_ARRAY_IDX(names,i,const char *);
        svn_string_t *value=apr_hash_get(props,name,APR_HASH_KEY_STRING);
        printf("P|"); probe_hex(path,strlen(path)); putchar('|'); probe_hex(name,strlen(name));
        putchar('|'); probe_hex(value->data,value->len); putchar('\n');
    }
    if(kind==svn_node_dir) {
        apr_hash_t *entries; SVN_ERR(svn_fs_dir_entries(&entries,root,path,pool));
        apr_array_header_t *children=apr_array_make(pool,(int)apr_hash_count(entries),sizeof(const char *));
        for(apr_hash_index_t *hi=apr_hash_first(pool,entries);hi;hi=apr_hash_next(hi)) {
            const void *key; apr_hash_this(hi,&key,NULL,NULL); APR_ARRAY_PUSH(children,const char *)=key;
        }
        qsort(children->elts,(size_t)children->nelts,sizeof(const char *),probe_compare_names);
        apr_pool_t *iter=svn_pool_create(pool);
        for(int i=0;i<children->nelts;++i) {
            svn_pool_clear(iter);
            const char *child=apr_pstrcat(iter,strcmp(path,"/") ? path : "","/",APR_ARRAY_IDX(children,i,const char *),NULL);
            SVN_ERR(probe_snapshot_node(root,child,iter));
        }
        svn_pool_destroy(iter);
    }
    return SVN_NO_ERROR;
}

static svn_error_t *probe_snapshot(svn_fs_t *fs,svn_revnum_t rev,apr_pool_t *pool)
{
    svn_fs_root_t *root;
    SVN_ERR(svn_fs_revision_root(&root,fs,rev,pool));
    return probe_snapshot_node(root,"/",pool);
}
