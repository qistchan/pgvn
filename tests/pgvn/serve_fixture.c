#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <apr_general.h>
#include <apr_strings.h>
#include "credentials.h"
#include "internal.h"
#include "svn_pools.h"
#include "svn_utf.h"

static void fatal(svn_error_t *error)
{
    if(!error) return;
    fprintf(stderr,"serve_fixture failed (code %ld)\n",(long)error->apr_err);
    svn_error_clear(error);
    exit(1);
}

int main(int argc,char **argv)
{
    if(argc!=5 || apr_initialize()!=APR_SUCCESS) return 2;
    apr_pool_t *pool=svn_pool_create(NULL);
    svn_utf_initialize2(TRUE,pool);
    const char *dsn=getenv("PGVN_SERVE_DSN");
    if(!dsn || !*dsn) return 2;
    pgvn_context_options options={dsn,3,5000};
    pgvn_context *context=NULL;
    fatal(pgvn_context_create(&context,&options,pool));
    fatal(pgvn_key_create(argv[1],"serve-test",pool));
    pgvn_credential_key *key=NULL;
    fatal(pgvn_key_load(&key,argv[1],pool));
    pgvn_binding *binding=NULL;
    fatal(pgvn_repo_create(&binding,context,argv[2],argv[3],NULL,pool));
    fatal(pgvn_credential_put(context,key,"pgvn",argv[4],"serve-correct-password",pool));
    const char *authz=apr_psprintf(pool,"[%s:/]\n%s = rw\n",argv[2],argv[4]);
    fatal(pgvn_policy_publish_settings(context,binding->repository_id,1,authz,NULL,"{}",
        "{\"identity_domain\":\"pgvn\",\"svn_authz_mode\":\"enforce\"}","serve_fixture",pool));
    fatal(pgvn_repo_set_enabled(context,binding->repository_id,TRUE,pool));
    printf("{\"repository_id\":\"%s\",\"uuid\":\"%s\",\"name\":\"%s\",\"route\":\"%s\"}\n",
        binding->repository_id,binding->svn_uuid,argv[2],argv[3]);
    svn_pool_destroy(pool);
    apr_terminate();
    return ferror(stdout) ? 1 : 0;
}
