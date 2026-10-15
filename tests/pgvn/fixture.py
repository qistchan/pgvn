"""Dedicated, loopback-only PgVN development cluster. Never controls installed services."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import socket
import subprocess

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime/pgvn"
DATA = RUNTIME / "pg-primary"
PORT = 16432
DATABASE = "pgvn_poc"


class Fixture:
    def __init__(self, config=None):
        config = config or ROOT / "config/build.local.json"
        self.pg = Path(json.loads(Path(config).read_text(encoding="utf-8"))["postgres"])

    def run(self, args, expected=0, timeout=45, input=None):
        result = subprocess.run([str(a) for a in args], cwd=ROOT, capture_output=True, input=input,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout)
        if result.returncode != expected:
            raise RuntimeError(f"Fixture command failed: {args}\n{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    def sql(self, statement, database=DATABASE, role="pgvn_owner"):
        return self.run([self.pg / "bin/psql.exe", "-X", "-w", "-A", "-t", "-h", "127.0.0.1",
                         "-p", PORT, "-U", role, "-d", database, "--set=ON_ERROR_STOP=1", "-f", "-"], input=statement)

    def dsn(self, role="pgvn_service"):
        return f"host=127.0.0.1 port={PORT} dbname={DATABASE} user={role} sslmode=disable"

    def assert_ours(self):
        actual = Path(self.sql("SHOW data_directory", database="postgres")).resolve()
        if actual != DATA.resolve():
            raise RuntimeError(f"Port {PORT} is not the dedicated PgVN data directory; refusing to use it")

    def initialize(self):
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with socket.socket() as connection:
            in_use = connection.connect_ex(("127.0.0.1", PORT)) == 0
        if in_use:
            self.assert_ours()
        else:
            if (DATA / "postmaster.pid").exists():
                status = subprocess.run([str(self.pg / "bin/pg_ctl.exe"), "-D", str(DATA), "status"],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
                if status.returncode == 0:
                    raise RuntimeError("Dedicated server is running but TCP is unavailable; inspect before restarting")
            if not (DATA / "PG_VERSION").exists():
                if DATA.exists() and any(DATA.iterdir()):
                    raise RuntimeError("Nonempty uninitialized data directory; refusing to overwrite")
                self.run([self.pg / "bin/initdb.exe", "-D", DATA, "-U", "pgvn_owner", "--encoding=UTF8",
                          "--locale=C", "--auth-local=trust", "--auth-host=trust", "--no-instructions"])
            # A detached Windows postgres can retain inherited pipe handles.
            # Use a file for launcher output so EOF is not tied to server exit.
            with (RUNTIME / "pg-launch.log").open("a", encoding="utf-8") as log:
                result = subprocess.run([str(self.pg / "bin/pg_ctl.exe"), "-D", str(DATA), "-l", str(RUNTIME / "pg-primary.log"),
                          "-o", f"-p {PORT} -h 127.0.0.1 -c shared_buffers=128MB -c max_connections=100",
                          "-w", "-t", "30", "start"], cwd=ROOT, stdout=log, stderr=log,
                          creationflags=subprocess.CREATE_NO_WINDOW, timeout=40)
            if result.returncode:
                raise RuntimeError("Dedicated PostgreSQL startup failed; inspect runtime/pgvn/pg-launch.log")
            self.assert_ours()
        if self.sql("SELECT 1 FROM pg_database WHERE datname='pgvn_poc'", database="postgres") != "1":
            self.sql("CREATE DATABASE pgvn_poc ENCODING 'UTF8' TEMPLATE template0", database="postgres")
        migrations = [ROOT / "sql" / name for name in
            ("001_initial.sql", "002_version_tree.sql", "003_locks.sql", "004_hotcopy.sql")]
        catalog = ROOT / "sql/pgvn/001_catalog.sql"
        exists = self.sql("SELECT to_regclass('pgvn.schema_versions') IS NOT NULL") == "t"
        if exists:
            installed = self.sql("SELECT migration_sha256 FROM pgvn.schema_versions WHERE version=1")
            if installed != hashlib.sha256(catalog.read_bytes()).hexdigest():
                raise RuntimeError("Installed PgVN migration checksum differs; do not edit installed migrations")
            for number, migration in enumerate(migrations, 1):
                installed = self.sql(f"SELECT migration_sha256 FROM pgvn.storage_migrations WHERE version={number}")
                if installed != hashlib.sha256(migration.read_bytes()).hexdigest():
                    raise RuntimeError(f"Installed storage migration {number} checksum differs")
        else:
            arguments = [self.pg / "bin/psql.exe", "-X", "-w", "-h", "127.0.0.1", "-p", PORT,
                         "-U", "pgvn_owner", "-d", DATABASE, "--set=ON_ERROR_STOP=1"]
            for migration in migrations:
                self.run([*arguments, "-f", migration])
            checksum = hashlib.sha256(catalog.read_bytes()).hexdigest()
            self.run([*arguments, "-c", f"SELECT set_config('pgvn.migration_sha256','{checksum}',false)", "-f", catalog])
            for number, migration in enumerate(migrations, 1):
                checksum = hashlib.sha256(migration.read_bytes()).hexdigest()
                self.sql(f"INSERT INTO pgvn.storage_migrations VALUES({number},'{checksum}')")
        for role in ("pgvn_admin", "pgvn_service", "pgvn_monitor"):
            if self.sql(f"SELECT 1 FROM pg_roles WHERE rolname='{role}'") != "1":
                self.sql(f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT")
        self.sql("""GRANT USAGE ON SCHEMA pgfs,pgvn TO pgvn_admin,pgvn_service,pgvn_monitor;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA pgfs TO pgvn_admin,pgvn_service;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA pgfs TO pgvn_admin,pgvn_service;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA pgvn TO pgvn_admin;
GRANT SELECT ON pgvn.repositories,pgvn.configurations,pgvn.routes,pgvn.schema_versions,pgvn.storage_migrations TO pgvn_service;
GRANT EXECUTE ON FUNCTION pgvn.guard_repository(uuid,bigint) TO pgvn_service,pgvn_admin;
GRANT SELECT ON pgfs.repositories,pgvn.repositories,pgvn.routes,pgvn.schema_versions TO pgvn_monitor;""")
        return {"data_directory": str(DATA), "port": PORT, "database": DATABASE,
                "pgfs_schema": 4, "pgvn_schema": int(self.sql("SELECT max(version) FROM pgvn.schema_versions")),
                "roles": ["pgvn_admin", "pgvn_service", "pgvn_monitor"]}

    def stop(self):
        self.assert_ours()
        self.run([self.pg / "bin/pg_ctl.exe", "-D", DATA, "-m", "fast", "-w", "-t", "30", "stop"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    fixture = Fixture(args.config)
    if args.stop:
        fixture.stop()
    else:
        print(json.dumps(fixture.initialize()))
