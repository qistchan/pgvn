#include <string.h>
#include "pgvn_protocol.h"
#include <apr_strings.h>
#include <apr_poll.h>
#include "svn_path.h"
#include "svn_dirent_uri.h"

/* This process has one SVN listener. Set before its worker threads start and
 * clear only after all of them have joined; the event itself is thread safe. */
static pgvn_cancel_event io_cancel;
void pgvn_svn_set_io_cancel(pgvn_cancel_event event) { io_cancel=event; }

static svn_error_t *wait_socket(apr_socket_t *socket,apr_pool_t *pool,apr_int16_t events,uint64_t deadline)
{
    apr_pollfd_t descriptor={0}; descriptor.p=pool;
    descriptor.desc_type=APR_POLL_SOCKET; descriptor.desc.s=socket; descriptor.reqevents=events;
    for(;;) {
        if(pgvn_cancel_is_set(io_cancel))
            return svn_error_create(SVN_ERR_CANCELLED,NULL,"PgVN SVN connection cancelled after the drain deadline");
        uint64_t now=0;
        int clock_status=pgvn_monotonic_milliseconds(&now);
        if(clock_status) return svn_error_wrap_apr(clock_status,"PgVN monotonic clock failed");
        if(now>=deadline) return svn_error_wrap_apr(APR_TIMEUP,"PgVN SVN connection exceeded its idle timeout");
        apr_int32_t ready=0;
        apr_status_t status=apr_poll(&descriptor,1,&ready,APR_USEC_PER_SEC);
        if(APR_STATUS_IS_TIMEUP(status) || APR_STATUS_IS_EINTR(status)) continue;
        if(status) return svn_error_wrap_apr(status,"PgVN SVN socket poll failed");
        if(ready) return SVN_NO_ERROR;
    }
}

static svn_error_t *socket_io(apr_socket_t *socket,apr_pool_t *pool,char *buffer,
    apr_size_t *length,svn_boolean_t writing,svn_boolean_t wait)
{
    if(!*length) return SVN_NO_ERROR;
    apr_interval_time_t interval;
    apr_status_t status=apr_socket_timeout_get(socket,&interval);
    if(!status && interval!=0) status=apr_socket_timeout_set(socket,0);
    if(status) { *length=0; return svn_error_wrap_apr(status,"PgVN SVN nonblocking socket configuration failed"); }
    apr_size_t requested=*length;
    uint64_t started=0;
    int clock_status=pgvn_monotonic_milliseconds(&started);
    if(clock_status) { *length=0; return svn_error_wrap_apr(clock_status,"PgVN monotonic clock failed"); }
    uint64_t deadline=started+30000;
    for(;;) {
        if(pgvn_cancel_is_set(io_cancel)) {
            *length=0; return svn_error_create(SVN_ERR_CANCELLED,NULL,"PgVN SVN connection is draining");
        }
        *length=requested;
        status=writing ? apr_socket_send(socket,buffer,length) : apr_socket_recv(socket,buffer,length);
        if((APR_STATUS_IS_EAGAIN(status) || APR_STATUS_IS_TIMEUP(status)) && wait) {
            svn_error_t *error=wait_socket(socket,pool,writing ? APR_POLLOUT : APR_POLLIN,deadline);
            if(error) { *length=0; return error; }
            continue;
        }
        if(!status || (APR_STATUS_IS_EOF(status) && !writing)) pgvn_telemetry_io(writing ? 0 : *length,writing ? *length : 0);
        if(status && !(APR_STATUS_IS_EOF(status) && !writing))
            return svn_error_wrap_apr(status,writing ? "Can't write to connection" : "Can't read from connection");
        return SVN_NO_ERROR;
    }
}

svn_error_t *pgvn_svn_socket_read(apr_socket_t *socket,apr_pool_t *pool,char *buffer,apr_size_t *length)
{
    return socket_io(socket,pool,buffer,length,FALSE,TRUE);
}

svn_error_t *pgvn_svn_socket_write(apr_socket_t *socket,apr_pool_t *pool,const char *buffer,apr_size_t *length,svn_boolean_t wait)
{
    return socket_io(socket,pool,(char *)buffer,length,TRUE,wait);
}

apr_status_t pgvn_svn_wire_error(apr_status_t code)
{
    if(code==PGVN_ERR_DATABASE) return SVN_ERR_FS_GENERAL;
    if(code==PGVN_ERR_SCHEMA) return SVN_ERR_FS_UNSUPPORTED_FORMAT;
    return code;
}

static enum access_type access_level(const char *setting,svn_boolean_t read_only)
{
    enum access_type value=!strcmp(setting,"write") ? WRITE_ACCESS : !strcmp(setting,"read") ? READ_ACCESS : NO_ACCESS;
    return read_only && value==WRITE_ACCESS ? READ_ACCESS : value;
}

svn_error_t *pgvn_svn_find(repository_t *repository,const char *url,svn_boolean_t read_only,
    apr_pool_t *pool,apr_pool_t *scratch_pool)
{
    if(!url || strlen(url)>16384 || strncmp(url,"svn://",6))
        return svn_error_create(SVN_ERR_BAD_URL,NULL,"PgVN requires a standard svn URL");
    const char *path=strchr(url+6,'/');
    if(path && strstr(path,"%00")) return svn_error_create(SVN_ERR_BAD_URL,NULL,"PgVN URL paths cannot contain an encoded NUL");
    const char *decoded=path ? svn_path_uri_decode(path,scratch_pool) : "/";
    pgvn_binding *binding;
    svn_error_t *error=pgvn_catalog_resolve(&binding,repository->pgvn_context,decoded,scratch_pool);
    if(error && error->apr_err==SVN_ERR_FS_NOT_FOUND) {
        svn_error_clear(error);
        return svn_error_create(SVN_ERR_RA_SVN_REPOS_NOT_FOUND,NULL,"No repository found at the requested URL");
    }
    SVN_ERR(error);
    SVN_ERR(pgvn_repos_open_svn(&repository->pgvn,repository->pgvn_context,binding->repository_id,pool));
    repository->repos=pgvn_repo_svn(repository->pgvn);
    repository->fs=svn_repos_fs(repository->repos);
    repository->repos_root=apr_pstrcat(pool,"pgvn:",binding->repository_id,NULL);
    repository->fs_path=svn_stringbuf_create(binding->in_repository_path,pool);
    const char *authority=path ? apr_pstrmemdup(pool,url,path-url) : apr_pstrdup(pool,url);
    repository->repos_url=apr_pstrcat(pool,authority,svn_path_uri_encode(binding->route_prefix,pool),NULL);
    repository->authz_repos_name=repository->pgvn->policy->canonical_name;
    repository->repos_name=svn_path_uri_encode(repository->authz_repos_name,pool);
    /* A NULL authzdb also disables upstream history and directory filtering.
     * Blanket anonymous access stays NONE, so authentication is still required. */
    repository->authzdb=pgvn_repository_svn_bypass(repository->pgvn) ? NULL : repository->pgvn->policy->authz;
    if(repository->pgvn->policy->identity_domain) repository->pgvn_domain=repository->pgvn->policy->identity_domain;
    repository->realm=repository->pgvn_domain;
    repository->uuid=apr_pstrdup(pool,binding->svn_uuid);
    repository->use_sasl=FALSE;
    repository->anon_access=access_level(repository->pgvn->policy->anonymous_access,read_only);
    repository->auth_access=access_level(repository->pgvn->policy->authenticated_access,read_only);
    if(pgvn_repository_svn_bypass(repository->pgvn)) {
        repository->anon_access=NO_ACCESS;
        repository->auth_access=read_only ? READ_ACCESS : WRITE_ACCESS;
    }
    repository->hooks_env=NULL;
    /* Presence advertises CRAM-MD5; passwords are fetched per identity only. */
    SVN_ERR(svn_config_create2(&repository->pwdb,TRUE,TRUE,pool));
    SVN_ERR(svn_repos_remember_client_capabilities(repository->repos,repository->capabilities));
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_svn_password(const char **password,repository_t *repository,const char *user,apr_pool_t *pool)
{
    svn_error_t *error=pgvn_credential_get(password,&repository->pgvn_auth_generation,repository->pgvn_context,
        repository->pgvn_key,repository->pgvn_domain,user,pool);
    if(error && error->apr_err==SVN_ERR_AUTHN_FAILED) {
        svn_error_t *mapped=svn_error_create(SVN_ERR_RA_NOT_AUTHORIZED,NULL,"PgVN credential authentication failed");
        svn_error_clear(error); return mapped;
    }
    return error;
}

svn_error_t *pgvn_svn_authenticated(repository_t *repository,const char *user,apr_pool_t *pool)
{
    SVN_ERR(pgvn_credential_check(repository->pgvn_context,repository->pgvn_domain,user,repository->pgvn_auth_generation,pool));
    repository->pgvn->credential_domain=apr_pstrdup(pool,repository->pgvn_domain);
    repository->pgvn->credential_user=apr_pstrdup(pool,user);
    repository->pgvn->credential_generation=repository->pgvn_auth_generation;
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_svn_command(repository_t *repository,const char *user,apr_pool_t *pool)
{
    pgvn_command_repository(repository->pgvn->repository_id);
    SVN_ERR(pgvn_policy_revalidate(repository->pgvn->policy,repository->pgvn_context,pool));
    if(pgvn_repository_svn_bypass(repository->pgvn) && (!user || !*user ||
       !repository->pgvn->credential_user || strcmp(user,repository->pgvn->credential_user)))
        return svn_error_create(SVN_ERR_AUTHN_FAILED,NULL,"PgVN SVN bypass requires authentication");
    if(user) return pgvn_credential_check(repository->pgvn_context,repository->pgvn_domain,
        user,repository->pgvn->credential_generation,pool);
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_svn_command_begin(repository_t *repository)
{
    return pgvn_control_request_begin(repository->pgvn_context);
}

svn_error_t *pgvn_svn_command_end(repository_t *repository)
{
    return pgvn_control_request_end(repository->pgvn_context);
}
