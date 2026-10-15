-- Durable HTTP protocol state. Transactions and bindings never denote files.
BEGIN;
CREATE TABLE pgvn.dav_transactions (
    repository_id uuid NOT NULL REFERENCES pgvn.repositories ON DELETE CASCADE,
    txn_id text COLLATE "C" NOT NULL CHECK (octet_length(txn_id) BETWEEN 1 AND 256),
    owner_name text COLLATE "C" CHECK (octet_length(owner_name) BETWEEN 1 AND 1024),
    creation_phase text NOT NULL DEFAULT 'creating' CHECK (creation_phase IN ('creating','active','failed')),
    creation_generation bigint NOT NULL CHECK (creation_generation>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    deleted_at timestamptz,
    PRIMARY KEY(repository_id,txn_id),
    FOREIGN KEY(repository_id,txn_id) REFERENCES pgfs.transactions ON DELETE CASCADE
);
CREATE INDEX dav_creating ON pgvn.dav_transactions(repository_id,created_at)
    INCLUDE(txn_id) WHERE creation_phase='creating';
CREATE TABLE pgvn.dav_bindings (
    repository_id uuid NOT NULL,
    binding_kind text NOT NULL CHECK (binding_kind IN ('activity','virtual')),
    binding_name text COLLATE "C" NOT NULL CHECK (octet_length(binding_name) BETWEEN 1 AND 1024),
    txn_id text COLLATE "C" NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    deleted_at timestamptz,
    PRIMARY KEY(repository_id,binding_kind,binding_name),
    FOREIGN KEY(repository_id,txn_id) REFERENCES pgvn.dav_transactions ON DELETE CASCADE
);
CREATE INDEX dav_bindings_transaction ON pgvn.dav_bindings(repository_id,txn_id);
REVOKE ALL ON pgvn.dav_transactions,pgvn.dav_bindings FROM PUBLIC;
INSERT INTO pgvn.schema_versions(version,migration_sha256)
VALUES (6,current_setting('pgvn.migration_sha256'));
COMMIT;
