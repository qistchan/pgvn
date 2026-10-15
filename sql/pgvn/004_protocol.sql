-- Versioned protocol settings and external hook definitions.
BEGIN;
ALTER TABLE pgvn.credentials ALTER COLUMN identity_domain TYPE text COLLATE "C";
ALTER TABLE pgvn.credentials ALTER COLUMN username TYPE text COLLATE "C";

CREATE FUNCTION pgvn.valid_hooks(definitions jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE item record; argument jsonb;
BEGIN
    IF definitions IS NULL OR jsonb_typeof(definitions)<>'object' OR octet_length(definitions::text)>16384 THEN RETURN false; END IF;
    FOR item IN SELECT key,value FROM jsonb_each(definitions) LOOP
        IF item.key<>ALL(ARRAY['start-commit','pre-commit','post-commit','pre-revprop-change',
            'post-revprop-change','pre-lock','post-lock','pre-unlock','post-unlock']) THEN RETURN false; END IF;
        IF jsonb_typeof(item.value)='string' THEN
            IF item.value#>>'{}' NOT IN ('allow','reject') THEN RETURN false; END IF;
        ELSIF jsonb_typeof(item.value)='object' THEN
            IF EXISTS(SELECT 1 FROM jsonb_object_keys(item.value) k WHERE k NOT IN ('executable','arguments','timeout_seconds')) THEN RETURN false; END IF;
            IF jsonb_typeof(item.value->'executable') IS DISTINCT FROM 'string'
                OR (item.value->>'executable') !~* '^[-A-Za-z0-9_.]{1,128}[.]exe$' THEN RETURN false; END IF;
            IF item.value?'timeout_seconds' AND (jsonb_typeof(item.value->'timeout_seconds') IS DISTINCT FROM 'number'
                OR (item.value->>'timeout_seconds') !~ '^([1-9]|[1-5][0-9]|60)$') THEN RETURN false; END IF;
            IF item.value?'arguments' THEN
                IF jsonb_typeof(item.value->'arguments') IS DISTINCT FROM 'array' THEN RETURN false; END IF;
                IF jsonb_array_length(item.value->'arguments')>16 THEN RETURN false; END IF;
                FOR argument IN SELECT value FROM jsonb_array_elements(item.value->'arguments') LOOP
                    IF jsonb_typeof(argument)<>'string' OR octet_length(argument#>>'{}')>2048 THEN RETURN false; END IF;
                END LOOP;
            END IF;
        ELSE RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END;
$$;

CREATE FUNCTION pgvn.valid_settings(settings jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE item record;
BEGIN
    IF settings IS NULL OR jsonb_typeof(settings)<>'object' OR octet_length(settings::text)>4096 THEN RETURN false; END IF;
    FOR item IN SELECT key,value FROM jsonb_each(settings) LOOP
        IF jsonb_typeof(item.value)<>'string' THEN RETURN false; END IF;
        IF item.key IN ('anonymous_access','authenticated_access') THEN
            IF item.value#>>'{}' NOT IN ('none','read','write') THEN RETURN false; END IF;
        ELSIF item.key='identity_domain' THEN
            IF octet_length(item.value#>>'{}') NOT BETWEEN 1 AND 128 OR (item.value#>>'{}') COLLATE "C" ~ '[[:cntrl:]]' THEN RETURN false; END IF;
        ELSE RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END;
$$;

INSERT INTO pgvn.schema_versions(version,migration_sha256)
VALUES(4,current_setting('pgvn.migration_sha256'));
COMMIT;
