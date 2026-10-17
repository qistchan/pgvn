-- Versioned logical hook declarations. Keep the legacy validator unchanged so
-- already-connected old nodes reject declarations they cannot interpret.
BEGIN;

CREATE FUNCTION pgvn.valid_hooks(definitions jsonb, supports_declarations boolean)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE item record; normalized jsonb := '{}'::jsonb; handler jsonb;
BEGIN
    IF definitions IS NULL OR jsonb_typeof(definitions)<>'object'
        OR octet_length(definitions::text)>16384 THEN RETURN false; END IF;
    FOR item IN SELECT key,value FROM jsonb_each(definitions) LOOP
        handler := item.value;
        IF jsonb_typeof(handler)='object' AND (handler?'version' OR handler?'name') THEN
            IF supports_declarations IS DISTINCT FROM true
                OR jsonb_typeof(handler->'version') IS DISTINCT FROM 'number'
                OR handler->>'version'<>'1'
                OR jsonb_typeof(handler->'name') IS DISTINCT FROM 'string'
                OR (handler->>'name') COLLATE "C" !~ '^[a-z][a-z0-9_-]{0,63}$'
                OR EXISTS(SELECT 1 FROM jsonb_object_keys(handler) k
                    WHERE k NOT IN ('version','name','arguments','timeout_seconds')) THEN RETURN false; END IF;
            -- Reuse the existing event/argument/timeout limits exactly.
            handler := (handler-'version'-'name') || '{"executable":"logical.exe"}'::jsonb;
        END IF;
        normalized := normalized || jsonb_build_object(item.key,handler);
    END LOOP;
    RETURN pgvn.valid_hooks(normalized);
END;
$$;

CREATE OR REPLACE VIEW pgvn_monitor.configuration_health AS
SELECT NOT EXISTS(SELECT 1 FROM pgvn.repositories r LEFT JOIN pgvn.configurations c
    ON c.repository_id=r.repository_id AND c.generation=r.generation
    WHERE r.enabled AND (c.repository_id IS NULL OR NOT pgvn.valid_hooks(c.hooks,true)
        OR NOT pgvn.valid_settings(c.settings,true)
        OR NOT EXISTS(SELECT 1 FROM pgvn.routes x WHERE x.repository_id=r.repository_id))) AS healthy,
    (SELECT max(generation) FROM pgvn.repositories) AS max_configuration_generation;

INSERT INTO pgvn.schema_versions(version,migration_sha256)
VALUES(8,current_setting('pgvn.migration_sha256'));
COMMIT;
