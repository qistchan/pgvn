-- Administrative tokens; plaintext tokens and deployment keys are not stored.
BEGIN;
CREATE TABLE pgvn.monitor_tokens (
    token_id uuid PRIMARY KEY,
    token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash)=32),
    scope text NOT NULL DEFAULT 'monitor:read' CHECK (scope='monitor:read'),
    label text NOT NULL CHECK (length(label) BETWEEN 1 AND 100),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    CHECK (expires_at>=created_at)
);
INSERT INTO pgvn.schema_versions(version,migration_sha256)
VALUES (2,current_setting('pgvn.migration_sha256'));
COMMIT;
