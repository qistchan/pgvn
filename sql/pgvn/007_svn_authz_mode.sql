-- Native SVN authorization mode. Old validators reject the new field.
BEGIN;

CREATE FUNCTION pgvn.valid_settings(settings jsonb, supports_svn_authz_mode boolean)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE item record;
BEGIN
    IF settings IS NULL OR jsonb_typeof(settings)<>'object' OR octet_length(settings::text)>4096 THEN RETURN false; END IF;
    FOR item IN SELECT key,value FROM jsonb_each(settings) LOOP
        IF jsonb_typeof(item.value)<>'string' THEN RETURN false; END IF;
        IF item.key IN ('anonymous_access','authenticated_access') THEN
            IF item.value#>>'{}' NOT IN ('none','read','write') THEN RETURN false; END IF;
        ELSIF item.key='identity_domain' THEN
            IF octet_length(item.value#>>'{}') NOT BETWEEN 1 AND 128 OR (item.value#>>'{}') COLLATE "C" ~ '[[:cntrl:]]' THEN RETURN false; END IF;
        ELSIF item.key='svn_authz_mode' THEN
            IF supports_svn_authz_mode IS DISTINCT FROM true OR item.value#>>'{}' NOT IN ('enforce','bypass') THEN RETURN false; END IF;
        ELSE RETURN false;
        END IF;
    END LOOP;
    IF settings->>'svn_authz_mode'='bypass' AND COALESCE(settings->>'anonymous_access','none')<>'none' THEN RETURN false; END IF;
    RETURN true;
END;
$$;

-- Existing binaries call the one-argument validator even on a previously
-- validated connection. They must not silently ignore the new mode.
CREATE OR REPLACE FUNCTION pgvn.valid_settings(settings jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
    SELECT pgvn.valid_settings(settings,false)
$$;

CREATE OR REPLACE VIEW pgvn_monitor.configuration_health AS
SELECT NOT EXISTS(SELECT 1 FROM pgvn.repositories r LEFT JOIN pgvn.configurations c
    ON c.repository_id=r.repository_id AND c.generation=r.generation
    WHERE r.enabled AND (c.repository_id IS NULL OR NOT pgvn.valid_hooks(c.hooks) OR NOT pgvn.valid_settings(c.settings,true)
        OR NOT EXISTS(SELECT 1 FROM pgvn.routes x WHERE x.repository_id=r.repository_id))) AS healthy,
    (SELECT max(generation) FROM pgvn.repositories) AS max_configuration_generation;

-- Append a non-sensitive mode field without changing existing view columns.
CREATE OR REPLACE VIEW pgvn_monitor.repositories AS
SELECT r.repository_id,r.canonical_name,r.enabled,r.generation,s.head_rev,
       s.svn_uuid,r.created_at,pgvn_monitor.safe_revision_date(p.value) AS last_commit_at,
       (SELECT count(*) FROM pgfs.transactions t WHERE t.repository_id=r.repository_id AND t.state='active') AS active_transactions,
       (SELECT count(*) FROM pgfs.locks l WHERE l.repository_id=r.repository_id AND
        (l.expiration_date=0 OR l.expiration_date>(extract(epoch FROM clock_timestamp())*1000000)::bigint)) AS lock_count,
       COALESCE(c.settings->>'svn_authz_mode','enforce') AS svn_authz_mode
FROM pgvn.repositories r JOIN pgfs.repositories s USING(repository_id)
JOIN pgvn.configurations c ON c.repository_id=r.repository_id AND c.generation=r.generation
LEFT JOIN pgfs.revision_properties p ON p.repository_id=r.repository_id AND p.rev=s.head_rev AND p.name='svn:date';

INSERT INTO pgvn.schema_versions(version,migration_sha256)
VALUES(7,current_setting('pgvn.migration_sha256'));
COMMIT;
