#include <string.h>
#include <stdlib.h>
#include "admin.h"
#include "telemetry.h"
#include <apr_strings.h>

typedef struct admin_migration {
    const char *namespace_name;
    int version;
    const char *sha256;
    const char *sql;
    const char *source;
} admin_migration;
#include "pgvn_migrations.h"

static svn_error_t *exists(svn_boolean_t *present,pgvn_context *context,const char *relation)
{
    const char *values[]={relation}; PGresult *result;
    SVN_ERR(pgvn_control_query(&result,context,"SELECT to_regclass($1) IS NOT NULL",1,values));
    *present=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
    return SVN_NO_ERROR;
}

static svn_error_t *migration_hash(const char **hash,pgvn_context *context,const admin_migration *migration,apr_pool_t *pool)
{
    *hash=NULL;
    const char *table=!strcmp(migration->namespace_name,"pgfs") ? "pgvn.storage_migrations" : "pgvn.schema_versions";
    svn_boolean_t present;
    SVN_ERR(exists(&present,context,table));
    if(!present) return SVN_NO_ERROR;
    const char *values[]={apr_psprintf(pool,"%d",migration->version)}; PGresult *result;
    SVN_ERR(pgvn_control_query(&result,context,
        apr_psprintf(pool,"SELECT migration_sha256 FROM %s WHERE version=$1::integer",table),1,values));
    if(PQntuples(result)==1) *hash=apr_pstrdup(pool,PQgetvalue(result,0,0));
    PQclear(result);
    return SVN_NO_ERROR;
}

svn_error_t *admin_db_check(pgvn_context *context,svn_boolean_t latest,apr_pool_t *pool)
{
    SVN_ERR(pgvn_database_validate(context));
    PGresult *result;
    SVN_ERR(pgvn_control_query(&result,context,"SELECT max(version) FROM pgvn.schema_versions",0,NULL));
    int catalog=atoi(PQgetvalue(result,0,0)); PQclear(result);
    if(latest && catalog!=PGVN_CATALOG_VERSION) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"Run pgvnadmin db migrate to install the latest catalog schema");
    SVN_ERR(pgvn_control_query(&result,context,"SELECT string_agg(version::text,',' ORDER BY version) FROM pgvn.storage_migrations",0,NULL));
    svn_boolean_t ledger=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"1,2,3,4"); PQclear(result);
    if(!ledger) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"Storage migration ledger is incomplete or contains an unsupported version");
    SVN_ERR(pgvn_control_query(&result,context,
        "SELECT count(*) FROM unnest(ARRAY['pgfs.repositories','pgfs.node_revisions','pgfs.revisions',"
        "'pgfs.revision_properties','pgfs.transactions','pgfs.transaction_properties','pgfs.transaction_changes',"
        "'pgfs.revision_changes','pgfs.contents','pgfs.content_chunks','pgfs.directories','pgfs.directory_entries',"
        "'pgfs.property_sets','pgfs.properties','pgfs.locks','pgfs.hotcopy_objects',"
        "'pgvn.repositories','pgvn.routes','pgvn.configurations']) AS q(name) WHERE to_regclass(name) IS NULL",0,NULL));
    svn_boolean_t tables=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"0"); PQclear(result);
    if(!tables) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"A required PgVN table is missing");
    if(catalog>=2) {
        svn_boolean_t present;
        SVN_ERR(exists(&present,context,"pgvn.monitor_tokens"));
        if(!present) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"Catalog schema 2 is missing its monitor token table");
    }
    if(catalog>=3) {
        svn_boolean_t present;
        SVN_ERR(exists(&present,context,"pgvn.credentials"));
        if(!present) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"Catalog schema 3 is missing its credential table");
    }
    if(catalog>=4) {
        SVN_ERR(pgvn_control_query(&result,context,"SELECT to_regprocedure('pgvn.valid_hooks(jsonb)') IS NOT NULL "
            "AND to_regprocedure('pgvn.valid_settings(jsonb)') IS NOT NULL",0,NULL));
        svn_boolean_t present=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
        if(!present) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"Catalog schema 4 is missing protocol validators");
    }
    if(catalog>=5) {
        SVN_ERR(pgvn_control_query(&result,context,"SELECT to_regclass('pgvn_monitor.nodes') IS NOT NULL "
            "AND to_regclass('pgvn_monitor.samples') IS NOT NULL "
            "AND to_regprocedure('pgvn_monitor.token_snapshot()') IS NOT NULL "
            "AND to_regprocedure('pgvn_monitor.publish(uuid,text,text,text,integer,timestamptz,jsonb)') IS NOT NULL",0,NULL));
        svn_boolean_t present=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
        if(!present) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"Catalog schema 5 is missing monitor surfaces");
    }
    if(catalog>=6) {
        SVN_ERR(pgvn_control_query(&result,context,"SELECT to_regclass('pgvn.dav_transactions') IS NOT NULL "
            "AND to_regclass('pgvn.dav_bindings') IS NOT NULL",0,NULL));
        svn_boolean_t present=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
        if(!present) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"Catalog schema 6 is missing DAV state tables");
    }
    if(catalog>=7) {
        SVN_ERR(pgvn_control_query(&result,context,
            "SELECT pgvn.valid_hooks('{\"pre-commit\":{\"executable\":\"pre-commit\"}}'::jsonb)"
            " AND pgvn.valid_hooks('{\"pre-commit\":{\"executable\":\"pre-commit.exe\"}}'::jsonb)"
            " AND NOT pgvn.valid_hooks('{\"pre-commit\":{\"executable\":\"pre-commit.sh\"}}'::jsonb)"
            " AND NOT pgvn.valid_hooks('{\"pre-commit\":{\"executable\":\"../hook\"}}'::jsonb)",0,NULL));
        svn_boolean_t present=PQntuples(result)==1 && !strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
        if(!present) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"Catalog schema 7 is missing logical hook validation");
    }
    for(apr_size_t i=0;i<sizeof(pgvn_migrations)/sizeof(pgvn_migrations[0]);++i) {
        const admin_migration *migration=&pgvn_migrations[i];
        if(!latest && !strcmp(migration->namespace_name,"pgvn") && migration->version>catalog) continue;
        const char *hash;
        SVN_ERR(migration_hash(&hash,context,migration,pool));
        if(!hash || strcmp(hash,migration->sha256))
            return svn_error_createf(PGVN_ERR_SCHEMA,NULL,"Installed migration checksum mismatch: %s",migration->source);
    }
    return SVN_NO_ERROR;
}

static svn_error_t *script(pgvn_context *context,const char *sql)
{
    apr_time_t start=apr_time_now(); pgvn_telemetry_query_start(); PGresult *result=PQexec(context->control,sql);
    const char *state=result ? PQresultErrorField(result,PG_DIAG_SQLSTATE) : NULL;
    svn_boolean_t success=result && (PQresultStatus(result)==PGRES_COMMAND_OK || PQresultStatus(result)==PGRES_TUPLES_OK);
    pgvn_metric_query(context,success ? "00000" : state ? state : "08006",apr_time_now()-start);
    svn_error_t *error=success ? NULL : svn_error_createf(state && !strcmp(state,"42501") ? SVN_ERR_AUTHZ_UNWRITABLE : PGVN_ERR_SCHEMA,
        NULL,"PgVN migration operation failed (SQLSTATE %s)",state ? state : "08006");
    if(result) PQclear(result);
    return error;
}

static svn_error_t *migrate_body(pgvn_context *context,apr_pool_t *pool)
{
    PGresult *result;
    SVN_ERR(pgvn_exec(&result,context,context->control,"SELECT pg_advisory_xact_lock(1347849806,2)",0,NULL));
    PQclear(result);
    svn_boolean_t catalog_exists;
    SVN_ERR(exists(&catalog_exists,context,"pgvn.schema_versions"));
    if(catalog_exists) {
        SVN_ERR(pgvn_exec(&result,context,context->control,"SELECT string_agg(version::text,',' ORDER BY version) FROM pgvn.schema_versions",0,NULL));
        svn_boolean_t supported=pgvn_catalog_version(PQgetvalue(result,0,0))>0; PQclear(result);
        if(!supported) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"Unsupported or incomplete PgVN catalog schema versions");
        SVN_ERR(pgvn_exec(&result,context,context->control,"SELECT string_agg(version::text,',' ORDER BY version) FROM pgvn.storage_migrations",0,NULL));
        supported=!strcmp(PQgetvalue(result,0,0),"1,2,3,4"); PQclear(result);
        if(!supported) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"Unsupported or incomplete storage migration ledger");
    }
    svn_boolean_t storage_exists;
    SVN_ERR(exists(&storage_exists,context,"pgfs.schema_versions"));
    if(storage_exists) {
        SVN_ERR(pgvn_exec(&result,context,context->control,"SELECT string_agg(version::text,',' ORDER BY version) FROM pgfs.schema_versions",0,NULL));
        const char *versions=PQgetvalue(result,0,0);
        svn_boolean_t supported=!strcmp(versions,"1") || !strcmp(versions,"1,2") || !strcmp(versions,"1,2,3") || !strcmp(versions,"1,2,3,4");
        PQclear(result);
        if(!supported) return svn_error_create(PGVN_ERR_SCHEMA,NULL,"Unsupported or incomplete PgFS schema versions; no migration was applied");
    }
    for(apr_size_t i=0;i<sizeof(pgvn_migrations)/sizeof(pgvn_migrations[0]);++i) {
        const admin_migration *migration=&pgvn_migrations[i]; const char *hash;
        SVN_ERR(migration_hash(&hash,context,migration,pool));
        if(hash) {
            if(strcmp(hash,migration->sha256))
                return svn_error_createf(PGVN_ERR_SCHEMA,NULL,"Installed migration checksum mismatch: %s",migration->source);
            continue;
        }
        if(catalog_exists && !strcmp(migration->namespace_name,"pgfs"))
            return svn_error_create(PGVN_ERR_SCHEMA,NULL,"Storage migration checksum is missing; inspect the existing installation");
        const char *values[]={migration->sha256};
        SVN_ERR(pgvn_exec(&result,context,context->control,
            "SELECT set_config('pgvn.migration_sha256',$1,true)",1,values)); PQclear(result);
        SVN_ERR(script(context,migration->sql));
    }
    /* Catalog migration 1 creates the checksum ledger for the storage layer. */
    for(int i=0;i<4;++i) {
        const admin_migration *migration=&pgvn_migrations[i];
        const char *values[]={apr_psprintf(pool,"%d",migration->version),migration->sha256};
        SVN_ERR(pgvn_exec(&result,context,context->control,
            "INSERT INTO pgvn.storage_migrations(version,migration_sha256) VALUES($1::integer,$2) ON CONFLICT DO NOTHING",2,values));
        PQclear(result);
    }
    return SVN_NO_ERROR;
}

svn_error_t *admin_db_migrate(pgvn_context *context,apr_pool_t *pool)
{
    SVN_ERR(pgvn_control_begin(context));
    SVN_ERR(pgvn_control_end(context,migrate_body(context,pool)));
    return admin_db_check(context,TRUE,pool);
}

static svn_error_t *grant_role(admin_command *command,const char *role,int kind)
{
    if(!role || !*role) return admin_usage("Role name is required");
    const char *values[]={role}; PGresult *result;
    SVN_ERR(pgvn_control_query(&result,command->context,"SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)",1,values));
    svn_boolean_t found=!strcmp(PQgetvalue(result,0,0),"t"); PQclear(result);
    if(!found) return svn_error_create(SVN_ERR_FS_NOT_FOUND,NULL,"PostgreSQL role does not exist; create it before granting PgVN privileges");
    char *escaped=PQescapeIdentifier(command->context->control,role,strlen(role));
    if(!escaped) return admin_usage("Invalid PostgreSQL role name");
    const char *identifier=apr_pstrdup(command->pool,escaped); PQfreemem(escaped);
    const char *sql;
    if(kind==0) sql=apr_psprintf(command->pool,
        "GRANT USAGE ON SCHEMA pgfs,pgvn TO %s;"
        "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA pgfs,pgvn TO %s;"
        "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA pgfs TO %s;"
        "GRANT EXECUTE ON FUNCTION pgvn.guard_repository(uuid,bigint),pgvn.guard_credential(text,text,bigint) TO %s;",identifier,identifier,identifier,identifier);
    else if(kind==1) sql=apr_psprintf(command->pool,
        "GRANT USAGE ON SCHEMA pgfs,pgvn TO %s;"
        "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA pgfs TO %s;"
        "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA pgfs TO %s;"
        "GRANT SELECT ON pgvn.repositories,pgvn.routes,pgvn.configurations,pgvn.schema_versions,pgvn.storage_migrations,pgvn.credentials TO %s;"
        "GRANT EXECUTE ON FUNCTION pgvn.guard_repository(uuid,bigint),pgvn.guard_credential(text,text,bigint) TO %s;",identifier,identifier,identifier,identifier,identifier);
    else sql=apr_psprintf(command->pool,
        "GRANT USAGE ON SCHEMA pgfs,pgvn TO %s;"
        "GRANT SELECT ON pgfs.repositories,pgvn.repositories,pgvn.routes,pgvn.schema_versions,pgvn.storage_migrations,pgfs.schema_versions TO %s;",identifier,identifier);
    SVN_ERR(script(command->context,sql));
    if(kind==1 && command->context->catalog_version>=6) {
        sql=apr_psprintf(command->pool,"GRANT SELECT,INSERT,UPDATE,DELETE ON pgvn.dav_transactions,pgvn.dav_bindings TO %s;",identifier);
        SVN_ERR(script(command->context,sql));
    }
    if(kind==0) sql=apr_psprintf(command->pool,
        "GRANT USAGE ON SCHEMA pgvn_monitor TO %s;"
        "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA pgvn_monitor TO %s;"
        "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA pgvn_monitor TO %s;",identifier,identifier,identifier);
    else {
        sql=apr_psprintf(command->pool,
            "GRANT USAGE ON SCHEMA pgvn_monitor TO %s;"
            "GRANT SELECT ON pgvn_monitor.repositories,pgvn_monitor.transactions,pgvn_monitor.locks,pgvn_monitor.nodes,pgvn_monitor.samples,pgvn_monitor.lock_items,pgvn_monitor.recent_revisions,pgvn_monitor.configuration_health TO %s;"
            "GRANT EXECUTE ON FUNCTION pgvn_monitor.token_snapshot(),pgvn_monitor.safe_revision_date(bytea) TO %s;",identifier,identifier,identifier);
        SVN_ERR(script(command->context,sql));
        if(kind==1) sql=apr_psprintf(command->pool,
            "GRANT EXECUTE ON FUNCTION pgvn_monitor.publish(uuid,text,text,text,integer,timestamptz,jsonb) TO %s;",identifier);
        else return SVN_NO_ERROR;
    }
    return script(command->context,sql);
}

svn_error_t *admin_db(admin_command *command)
{
    const char *op=command->argv[1];
    if(!strcmp(op,"init") || !strcmp(op,"migrate")) {
        SVN_ERR(admin_db_migrate(command->context,command->pool));
        admin_ok("db.migrate"); return SVN_NO_ERROR;
    }
    SVN_ERR(admin_db_check(command->context,TRUE,command->pool));
    if(!strcmp(op,"grant")) {
        SVN_ERR(pgvn_control_begin(command->context));
        svn_error_t *error=NULL;
        if(command->values[OPT_ADMIN_ROLE]) error=grant_role(command,command->values[OPT_ADMIN_ROLE],0);
        if(!error && command->values[OPT_SERVICE_ROLE]) error=grant_role(command,command->values[OPT_SERVICE_ROLE],1);
        if(!error && command->values[OPT_MONITOR_ROLE]) error=grant_role(command,command->values[OPT_MONITOR_ROLE],2);
        SVN_ERR(pgvn_control_end(command->context,error));
        admin_ok("db.grant"); return SVN_NO_ERROR;
    }
    return admin_emit_query(command,
        "SELECT json_build_object('ok',true,'database',current_database(),'server_version_num',current_setting('server_version_num')::integer,"
        "'schema',json_build_object('pgfs',4,'pgvn',(SELECT max(version) FROM pgvn.schema_versions),'checksums','match'))",0,NULL);
}
