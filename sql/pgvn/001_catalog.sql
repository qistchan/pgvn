-- Runner supplies a SHA-256 via set_config('pgvn.migration_sha256', ..., false).
BEGIN;
CREATE SCHEMA IF NOT EXISTS pgvn;
CREATE TABLE IF NOT EXISTS pgvn.schema_versions (
    version integer PRIMARY KEY,
    migration_sha256 text NOT NULL CHECK (migration_sha256 ~ '^[0-9a-f]{64}$'),
    installed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS pgvn.storage_migrations (
    version integer PRIMARY KEY,
    migration_sha256 text NOT NULL CHECK (migration_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE TABLE IF NOT EXISTS pgvn.repositories (
    repository_id uuid PRIMARY KEY REFERENCES pgfs.repositories(repository_id),
    canonical_name text COLLATE "C" NOT NULL UNIQUE CHECK (canonical_name <> ''),
    enabled boolean NOT NULL DEFAULT true,
    generation bigint NOT NULL CHECK (generation > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS pgvn.configurations (
    repository_id uuid NOT NULL REFERENCES pgvn.repositories(repository_id),
    generation bigint NOT NULL CHECK (generation > 0),
    authz_text text NOT NULL,
    groups_text text,
    settings jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(settings) = 'object'),
    hooks jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(hooks) = 'object'),
    published_by text NOT NULL,
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (repository_id, generation)
);
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='pgvn.repositories'::regclass AND conname='published_configuration') THEN
        ALTER TABLE pgvn.repositories ADD CONSTRAINT published_configuration
            FOREIGN KEY (repository_id,generation) REFERENCES pgvn.configurations(repository_id,generation)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS pgvn.routes (
    route_prefix text COLLATE "C" PRIMARY KEY,
    repository_id uuid NOT NULL REFERENCES pgvn.repositories(repository_id),
    CHECK (left(route_prefix,1)='/' AND (route_prefix='/' OR right(route_prefix,1)<>'/')),
    CHECK (position('//' IN route_prefix)=0 AND position(chr(92) IN route_prefix)=0),
    CHECK (route_prefix !~ '(^|/)\.{1,2}(/|$)' AND route_prefix !~ '[[:cntrl:]]')
);
CREATE INDEX IF NOT EXISTS routes_repository ON pgvn.routes(repository_id);
-- A service may lock the published policy without gaining UPDATE permission
-- on the control catalog. The lock lives in its revision publication txn.
CREATE OR REPLACE FUNCTION pgvn.guard_repository(id uuid, expected bigint)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE active boolean; actual bigint;
BEGIN
    SELECT r.enabled,r.generation INTO active,actual
    FROM pgvn.repositories r WHERE r.repository_id=id FOR SHARE;
    RETURN COALESCE(active AND actual=expected,false);
END $$;
REVOKE ALL ON FUNCTION pgvn.guard_repository(uuid,bigint) FROM PUBLIC;
INSERT INTO pgvn.schema_versions(version,migration_sha256)
VALUES (1,current_setting('pgvn.migration_sha256')) ON CONFLICT DO NOTHING;
COMMIT;
