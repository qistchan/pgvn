#include <string.h>
#include "internal.h"
#include <apr_file_info.h>
#include <apr_strings.h>
#include "svn_dirent_uri.h"
#ifndef _WIN32
#include <sys/stat.h>
#include <unistd.h>
#endif

static svn_error_t *invalid(const char *message)
{
    return svn_error_create(SVN_ERR_AUTHZ_INVALID_CONFIG,NULL,message);
}

static svn_boolean_t logical_name(const char *name)
{
    size_t size=name ? strlen(name) : 0;
    if(!size || size>64 || *name<'a' || *name>'z') return FALSE;
    for(const char *p=name;*p;++p)
        if(!((*p>='a' && *p<='z') || (*p>='0' && *p<='9') || *p=='_' || *p=='-')) return FALSE;
    return TRUE;
}

/* Values are single filenames, never paths or command lines. Linux mappings
 * deliberately use extensionless names; legacy .exe declarations are Windows
 * only and are never translated implicitly. */
svn_boolean_t pgvn_hook_filename_valid(const char *name,svn_boolean_t windows)
{
    size_t size=name ? strlen(name) : 0;
    if(!size || size>(windows ? 132u : 128u)) return FALSE;
    if(windows && (size<5 || svn_cstring_casecmp(name+size-4,".exe"))) return FALSE;
    for(const char *p=name;*p;++p)
        if(!((*p>='A' && *p<='Z') || (*p>='a' && *p<='z') || (*p>='0' && *p<='9') ||
             *p=='_' || *p=='-' || (windows && *p=='.'))) return FALSE;
    return TRUE;
}

typedef struct mapping_baton {
    apr_hash_t *mappings;
    apr_pool_t *pool;
    svn_boolean_t valid;
    unsigned count;
} mapping_baton;

#ifdef _WIN32
#define NATIVE_WINDOWS TRUE
#else
#define NATIVE_WINDOWS FALSE
#endif

static svn_boolean_t mapping(const char *name,const char *value,void *baton,apr_pool_t *scratch)
{
    mapping_baton *state=baton;
    if(++state->count>128 || !logical_name(name) || !pgvn_hook_filename_valid(value,NATIVE_WINDOWS)) {
        state->valid=FALSE; return FALSE;
    }
    apr_hash_set(state->mappings,apr_pstrdup(state->pool,name),APR_HASH_KEY_STRING,apr_pstrdup(state->pool,value));
    return TRUE;
}

svn_error_t *pgvn_hooks_configure(pgvn_context *context,svn_config_t *config,apr_pool_t *pool)
{
    mapping_baton state={apr_hash_make(pool),pool,TRUE,0};
    if(config) svn_config_enumerate2(config,"pgvn-hooks",mapping,&state,pool);
    if(!state.valid) return invalid("Invalid PgVN node hook mapping");
    /* Publish only a complete, validated snapshot. */
    context->hook_implementations=state.mappings;
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_hook_resolve(pgvn_hook_handler *handler,pgvn_context *context,
    const char *name,apr_pool_t *pool)
{
    if(!name) {
        if(!NATIVE_WINDOWS) return invalid("Legacy .exe hooks require an explicit version 1 declaration and Linux node mapping");
        return SVN_NO_ERROR;
    }
    if(!logical_name(name)) return invalid("Invalid PgVN logical hook name");
    const char *filename=context->hook_implementations
        ? apr_hash_get(context->hook_implementations,name,APR_HASH_KEY_STRING) : NULL;
    if(!filename || !pgvn_hook_filename_valid(filename,NATIVE_WINDOWS))
        return invalid("Required PgVN hook implementation is not mapped on this node");
    if(!context->hook_root || !svn_dirent_is_absolute(context->hook_root))
        return invalid("PgVN logical hooks require an absolute node hook-root");
    const char *path=apr_pstrcat(pool,context->hook_root,"/",filename,NULL);
    apr_finfo_t info;
    if(apr_stat(&info,path,APR_FINFO_TYPE|APR_FINFO_LINK,pool) || info.filetype!=APR_REG)
        return invalid("Required PgVN hook implementation is not a regular file on this node");
#ifndef _WIN32
    if(access(path,X_OK)) return invalid("Required PgVN hook implementation is not executable on this node");
#endif
    /* Availability check only. The platform launcher must securely reopen and
     * validate the executable at dispatch time to handle replacement races. */
    handler->executable=apr_pstrdup(pool,filename);
    return SVN_NO_ERROR;
}
