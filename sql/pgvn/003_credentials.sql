-- SVN credentials are independent of PostgreSQL role passwords.
-- AES-256-GCM deployment keys live outside this database.
BEGIN;

CREATE TABLE pgvn.credentials (
    identity_domain text NOT NULL CHECK (octet_length(identity_domain) BETWEEN 1 AND 128),
    username text NOT NULL CHECK (octet_length(username) BETWEEN 1 AND 256),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    enabled boolean NOT NULL DEFAULT true,
    key_id text NOT NULL CHECK (key_id ~ '^[A-Za-z0-9_.-]{1,64}$'),
    nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
    ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) BETWEEN 1 AND 4096),
    authentication_tag bytea NOT NULL CHECK (octet_length(authentication_tag) = 16),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (identity_domain, username)
);
REVOKE ALL ON pgvn.credentials FROM PUBLIC;

-- Hold a row lock in the same SQL transaction as revision publication.
-- Revocation/rotation either precedes the publication or waits for it.
CREATE FUNCTION pgvn.guard_credential(domain_name text, user_name text, expected bigint)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE active boolean; actual bigint;
BEGIN
    SELECT c.enabled,c.generation INTO active,actual FROM pgvn.credentials c
      WHERE c.identity_domain=domain_name AND c.username=user_name FOR SHARE;
    RETURN COALESCE(active AND actual=expected,false);
END;
$$;
REVOKE ALL ON FUNCTION pgvn.guard_credential(text,text,bigint) FROM PUBLIC;

INSERT INTO pgvn.schema_versions(version,migration_sha256)
VALUES (3,current_setting('pgvn.migration_sha256'));
COMMIT;
