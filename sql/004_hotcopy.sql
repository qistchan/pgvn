BEGIN;
ALTER TABLE pgfs.repositories ADD COLUMN IF NOT EXISTS hotcopy_source uuid;
CREATE TABLE IF NOT EXISTS pgfs.hotcopy_objects (
    destination_repository_id uuid NOT NULL REFERENCES pgfs.repositories,
    source_repository_id uuid NOT NULL,
    object_kind text NOT NULL CHECK (object_kind IN ('node','content','properties','directory')),
    source_id bigint NOT NULL,
    target_id bigint NOT NULL,
    PRIMARY KEY (destination_repository_id,object_kind,source_id),
    UNIQUE (destination_repository_id,object_kind,target_id)
);
INSERT INTO pgfs.schema_versions(version) VALUES (4) ON CONFLICT DO NOTHING;
COMMIT;
