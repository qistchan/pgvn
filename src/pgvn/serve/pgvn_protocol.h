#ifndef PGVN_PROTOCOL_H
#define PGVN_PROTOCOL_H
#include "server.h"
#include "credentials.h"
#include "telemetry.h"

svn_error_t *pgvn_svn_find(repository_t *repository,const char *url,svn_boolean_t read_only,
    apr_pool_t *result_pool,apr_pool_t *scratch_pool);
svn_error_t *pgvn_svn_password(const char **password,repository_t *repository,const char *user,apr_pool_t *pool);
svn_error_t *pgvn_svn_authenticated(repository_t *repository,const char *user,apr_pool_t *pool);
svn_error_t *pgvn_svn_command(repository_t *repository,const char *user,apr_pool_t *pool);
apr_status_t pgvn_svn_wire_error(apr_status_t code);
void pgvn_svn_set_io_cancel(pgvn_cancel_event event);
svn_error_t *pgvn_svn_socket_read(apr_socket_t *socket,apr_pool_t *pool,char *buffer,apr_size_t *length);
svn_error_t *pgvn_svn_socket_write(apr_socket_t *socket,apr_pool_t *pool,const char *buffer,apr_size_t *length,svn_boolean_t wait);
svn_error_t *pgvn_cram_server(svn_ra_svn_conn_t *conn,apr_pool_t *pool,repository_t *repository,
    const char **user,svn_boolean_t *success);
#endif
