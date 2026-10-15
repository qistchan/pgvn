BEGIN;
CREATE TABLE IF NOT EXISTS pgfs.locks (
    repository_id uuid NOT NULL REFERENCES pgfs.repositories,
    path text COLLATE "C" NOT NULL,
    token text COLLATE "C" NOT NULL,
    owner text NOT NULL,
    comment text,
    is_dav_comment boolean NOT NULL DEFAULT false,
    creation_date bigint NOT NULL,
    expiration_date bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (repository_id,path),
    UNIQUE (repository_id,token)
);
INSERT INTO pgfs.schema_versions(version) VALUES (3) ON CONFLICT DO NOTHING;
COMMIT;
