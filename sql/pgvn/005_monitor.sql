-- Read-only monitoring surfaces and bounded, non-authoritative telemetry.
BEGIN;
CREATE SCHEMA pgvn_monitor;
REVOKE ALL ON SCHEMA pgvn_monitor FROM PUBLIC;
CREATE TABLE pgvn_monitor.nodes (
    instance_id uuid PRIMARY KEY,
    node_id text COLLATE "C" NOT NULL CHECK (node_id ~ '^[A-Za-z0-9_.:-]{1,80}$'),
    component text NOT NULL CHECK (component IN ('svn','dav')),
    version text NOT NULL CHECK (octet_length(version)<=64),
    process_id integer NOT NULL CHECK (process_id>0),
    started_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    metrics jsonb NOT NULL CHECK (jsonb_typeof(metrics)='object' AND octet_length(metrics::text)<=4096)
);
CREATE INDEX nodes_seen ON pgvn_monitor.nodes(last_seen_at);
CREATE TABLE pgvn_monitor.samples (
    instance_id uuid NOT NULL REFERENCES pgvn_monitor.nodes ON DELETE CASCADE,
    sampled_at timestamptz NOT NULL,
    metrics jsonb NOT NULL CHECK (jsonb_typeof(metrics)='object' AND octet_length(metrics::text)<=4096),
    PRIMARY KEY (instance_id,sampled_at)
);
CREATE INDEX samples_age ON pgvn_monitor.samples(sampled_at);

-- Limit active-transaction scans; committed history is never scanned on refresh.
CREATE INDEX transactions_monitor_active ON pgfs.transactions(repository_id,txn_id)
    INCLUDE(base_rev,created_at) WHERE state='active';
CREATE FUNCTION pgvn_monitor.safe_revision_date(value bytea)
RETURNS text LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
    SELECT CASE WHEN octet_length(value) BETWEEN 20 AND 40 AND
        encode(value,'hex') ~ '^((3[0-9a])|54|5a|2[bde])+$'
        THEN convert_from(value,'UTF8') ELSE NULL END
$$;
REVOKE ALL ON FUNCTION pgvn_monitor.safe_revision_date(bytea) FROM PUBLIC;
CREATE VIEW pgvn_monitor.transactions AS
SELECT repository_id,txn_id,base_rev,created_at
FROM pgfs.transactions WHERE state='active';
CREATE VIEW pgvn_monitor.locks AS
SELECT repository_id,count(*) AS lock_count
FROM pgfs.locks WHERE expiration_date=0 OR expiration_date>(extract(epoch FROM clock_timestamp())*1000000)::bigint
GROUP BY repository_id;
CREATE VIEW pgvn_monitor.repositories AS
SELECT r.repository_id,r.canonical_name,r.enabled,r.generation,s.head_rev,
       s.svn_uuid,r.created_at,pgvn_monitor.safe_revision_date(p.value) AS last_commit_at,
       (SELECT count(*) FROM pgfs.transactions t WHERE t.repository_id=r.repository_id AND t.state='active') AS active_transactions,
       (SELECT count(*) FROM pgfs.locks l WHERE l.repository_id=r.repository_id AND
        (l.expiration_date=0 OR l.expiration_date>(extract(epoch FROM clock_timestamp())*1000000)::bigint)) AS lock_count
FROM pgvn.repositories r JOIN pgfs.repositories s USING(repository_id)
LEFT JOIN pgfs.revision_properties p ON p.repository_id=r.repository_id AND p.rev=s.head_rev AND p.name='svn:date';
CREATE VIEW pgvn_monitor.lock_items AS
SELECT repository_id,left(path,256) AS path,length(path)>256 AS path_truncated,
       left(owner,128) AS owner,length(owner)>128 AS owner_truncated,creation_date,expiration_date
FROM pgfs.locks WHERE expiration_date=0 OR expiration_date>(extract(epoch FROM clock_timestamp())*1000000)::bigint;
CREATE VIEW pgvn_monitor.recent_revisions AS
SELECT r.repository_id,r.rev,pgvn_monitor.safe_revision_date(p.value) AS committed_at
FROM pgfs.revisions r LEFT JOIN pgfs.revision_properties p
ON p.repository_id=r.repository_id AND p.rev=r.rev AND p.name='svn:date';
CREATE VIEW pgvn_monitor.configuration_health AS
SELECT NOT EXISTS(SELECT 1 FROM pgvn.repositories r LEFT JOIN pgvn.configurations c
    ON c.repository_id=r.repository_id AND c.generation=r.generation
    WHERE r.enabled AND (c.repository_id IS NULL OR NOT pgvn.valid_hooks(c.hooks) OR NOT pgvn.valid_settings(c.settings)
        OR NOT EXISTS(SELECT 1 FROM pgvn.routes x WHERE x.repository_id=r.repository_id))) AS healthy,
    (SELECT max(generation) FROM pgvn.repositories) AS max_configuration_generation;

CREATE FUNCTION pgvn_monitor.token_snapshot()
RETURNS TABLE(hash_hex text,remaining_seconds double precision)
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
    SELECT encode(token_hash,'hex'),extract(epoch FROM expires_at-clock_timestamp())::double precision
    FROM pgvn.monitor_tokens WHERE revoked_at IS NULL AND expires_at>clock_timestamp()
    ORDER BY token_id LIMIT 1001
$$;
REVOKE ALL ON FUNCTION pgvn_monitor.token_snapshot() FROM PUBLIC;

CREATE FUNCTION pgvn_monitor.publish(id uuid,node text,part text,ver text,pid integer,started timestamptz,body jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE stamp timestamptz:=clock_timestamp();
BEGIN
    IF node !~ '^[A-Za-z0-9_.:-]{1,80}$' OR part NOT IN ('svn','dav') OR pid<=0 OR
       octet_length(ver)>64 OR jsonb_typeof(body)<>'object' OR octet_length(body::text)>4096 OR
       started>stamp+interval '1 minute' OR started<stamp-interval '10 years' THEN
        RAISE EXCEPTION 'Invalid PgVN telemetry' USING ERRCODE='22023';
    END IF;
    -- Delete in bounded batches; these tables never participate in publication.
    DELETE FROM pgvn_monitor.samples WHERE (instance_id,sampled_at) IN
        (SELECT instance_id,sampled_at FROM pgvn_monitor.samples WHERE sampled_at<stamp-interval '24 hours' ORDER BY sampled_at LIMIT 2000);
    DELETE FROM pgvn_monitor.nodes WHERE instance_id IN
        (SELECT n.instance_id FROM pgvn_monitor.nodes n WHERE last_seen_at<stamp-interval '24 hours'
         AND NOT EXISTS(SELECT 1 FROM pgvn_monitor.samples s WHERE s.instance_id=n.instance_id)
         ORDER BY last_seen_at LIMIT 20);
    IF NOT EXISTS(SELECT 1 FROM pgvn_monitor.nodes WHERE instance_id=id) THEN
        PERFORM pg_advisory_xact_lock(1347849806,5);
        IF NOT EXISTS(SELECT 1 FROM pgvn_monitor.nodes WHERE instance_id=id) AND
           (SELECT count(*) FROM pgvn_monitor.nodes)>=1000 THEN
            RAISE EXCEPTION 'PgVN telemetry capacity reached' USING ERRCODE='54000';
        END IF;
    END IF;
    INSERT INTO pgvn_monitor.nodes VALUES(id,node,part,ver,pid,started,stamp,body)
    ON CONFLICT(instance_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,metrics=excluded.metrics;
    INSERT INTO pgvn_monitor.samples VALUES(id,to_timestamp(floor(extract(epoch FROM stamp)/5)*5),body)
    ON CONFLICT(instance_id,sampled_at) DO UPDATE SET metrics=excluded.metrics;
END $$;
REVOKE ALL ON FUNCTION pgvn_monitor.publish(uuid,text,text,text,integer,timestamptz,jsonb) FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA pgvn_monitor FROM PUBLIC;
INSERT INTO pgvn.schema_versions(version,migration_sha256)
VALUES (5,current_setting('pgvn.migration_sha256'));
COMMIT;
