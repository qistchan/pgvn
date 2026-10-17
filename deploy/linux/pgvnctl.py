#!/usr/bin/env python3
"""Linux installation and offline recovery for PgVN development snapshots."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(directory, manifest_name="package-manifest.json"):
    directory = directory.resolve(strict=True)
    manifest = directory / manifest_name
    if digest(manifest) != (directory / (manifest_name + ".sha256")).read_text().split()[0]:
        raise ValueError("Manifest checksum mismatch")
    info = json.loads(manifest.read_text())
    if any(p.is_symlink() for p in directory.rglob("*")):
        raise ValueError("Verified directories must not contain symlinks")
    for relative, entry in info["files"].items():
        path = PurePosixPath(relative)
        target = directory / relative
        if path.is_absolute() or ".." in path.parts or "\\" in relative or not relative:
            raise ValueError("Invalid manifest path")
        if target.is_symlink() or not target.resolve().is_relative_to(directory):
            raise ValueError("Manifest path escapes directory")
        if not stat.S_ISREG(target.stat().st_mode) or digest(target) != entry["sha256"] or target.stat().st_size != entry["bytes"]:
            raise ValueError("File integrity check failed: " + relative)
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
    if actual != set(info["files"]) | {manifest_name, manifest_name + ".sha256"}:
        raise ValueError("Unexpected files in verified directory")
    return info


def run(args, env=None, capture=True):
    # Passwords and raw connection strings are never rendered in diagnostics.
    result = subprocess.run(list(map(str, args)), env=env, capture_output=capture, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError("Command failed: " + Path(str(args[0])).name + " (exit " + str(result.returncode) + ")")
    return result.stdout if capture else ""


def service_name(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        raise ValueError("Use a libpq service alias, not a raw connection string")
    return value


def safe_directory(path, mode=0o755):
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("Managed directories must not contain symlinks: " + str(component))
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if created: path.chmod(mode)
    if not path.is_dir():
        raise ValueError("Expected a directory")


def write_once(path, value, mode=0o640):
    with path.open("x") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(value)


def atomic_link(link, target):
    if link.exists() and not link.is_symlink():
        raise ValueError("Refusing to replace a real directory")
    temporary = link.with_name("." + link.name + "." + str(os.getpid()))
    try:
        temporary.symlink_to(target.name)
        temporary.replace(link)
    finally:
        temporary.unlink(missing_ok=True)


def install(options):
    info = verify(options.package)
    identity = info["build_identity"]
    if identity["target_os"] != "linux" or identity["target_arch"] != "x86_64":
        raise ValueError("Expected a Linux x86_64 package")
    base = options.root / "opt/pgvn"
    safe_directory(base)
    base.chmod(0o755)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,100}", identity["version"]):
        raise ValueError("Invalid installation version component")
    name = identity["version"] + "-" + digest(options.package / "package-manifest.json")[:16]
    destination = base / name
    if destination.exists():
        if verify(destination) != info:
            raise ValueError("Installed version differs")
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=base))
        try:
            shutil.copytree(options.package, temporary, dirs_exist_ok=True)
            if verify(temporary) != info:
                raise ValueError("Package changed while copying")
            for directory in (temporary, *temporary.rglob("*")):
                if directory.is_dir(): directory.chmod(0o755)
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    for relative in ("etc/pgvn", "etc/pgvn/hooks", "var/lib/pgvn", "var/log/pgvn", "etc/systemd/system"):
        safe_directory(options.root / relative)
    templates = destination / "deploy/linux"
    for name in ("pgvnserve.service", "pgvnhttpd.service"):
        target = options.root / "etc/systemd/system" / name
        content = (templates / name).read_text().replace("User=pgvn", "User=" + options.service_user).replace("Group=pgvn", "Group=" + options.service_user)
        if target.exists() and target.read_text() != content:
            raise ValueError("Existing unit differs; review and replace explicitly: " + str(target))
        if not target.exists():
            write_once(target, content, 0o644)
    current = base / "current"
    if current.is_symlink() and current.resolve() != destination:
        previous = current.resolve()
        # Before switching, the caller must have stopped ALL nodes sharing PG.
        if not options.nodes_stopped:
            raise ValueError("Stop all PgVN/Apache nodes before changing the active version")
        atomic_link(base / "previous", previous)
    atomic_link(current, destination)
    return {"installed": str(destination), "current": str(current), "systemd_reloaded": False}


def initialize(options):
    prefix = (options.root / "opt/pgvn/current").resolve(strict=True)
    verify(prefix)
    etc = options.root / "etc/pgvn"
    if (etc / "pgvn.ini").exists():
        return {"configuration": str(etc / "pgvn.ini"), "changed": False}
    account = pwd.getpwnam(options.service_user)
    safe_directory(etc)
    keys = etc / "keys"
    safe_directory(keys, 0o700)
    keys.chmod(0o700)
    key = keys / "current.key"
    if not key.exists():
        run([prefix / "bin/pgvnadmin", "key", "create", key, options.key_id])
    os.chown(keys, account.pw_uid, account.pw_gid)
    os.chown(key, account.pw_uid, account.pw_gid)
    config = (prefix / "deploy/linux/pgvn.ini.example").read_text()
    config = config.replace("service=pgvn-service", "service=" + options.service).replace("service=pgvn-monitor", "service=" + options.monitor_service)
    # Runtime paths always refer to the actual host layout, even for a staged root.
    write_once(etc / "pgvn.ini", config)
    os.chown(etc / "pgvn.ini", 0 if os.geteuid() == 0 else os.getuid(), account.pw_gid)
    write_once(etc / "environment", "PGSERVICEFILE=/etc/pgvn/pg_service.conf\nPGPASSFILE=/etc/pgvn/pgpass\n", 0o640)
    os.chown(etc / "environment", 0 if os.geteuid() == 0 else os.getuid(), account.pw_gid)
    for relative in ("var/lib/pgvn", "var/log/pgvn"):
        os.chown(options.root / relative, account.pw_uid, account.pw_gid)
    return {"configuration": str(etc / "pgvn.ini"), "key_created": True,
            "next": "Create protected pg_service.conf and pgpass, initialize/grant PG, then run check"}


def database_env(service):
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("PGPASSWORD", None)
    env["PGVN_DSN"] = "service=" + service
    return env


def backup(options):
    prefix = options.prefix.resolve(strict=True)
    verify(prefix)
    if not options.nodes_stopped:
        raise ValueError("Stop all shared-PG nodes and stabilize key/configuration rotation before offline backup")
    target = options.destination
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    target.chmod(0o700)
    env = database_env(options.service)
    try:
        schema = json.loads(run([prefix / "bin/pgvnadmin", "db", "check"], env))
        if not schema.get("ok"):
            raise ValueError("Database validation failed")
        run([prefix / "bin/pg_dump", "--dbname=service=" + options.service,
             "--format=custom", "--file=" + str(target / "database.dump")], env)
        state = options.state.resolve(strict=True)
        for path in state.rglob("*"):
            if path.is_symlink() or (not path.is_dir() and not path.is_file()):
                raise ValueError("Backup state must contain only regular files/directories")
        shutil.copytree(state, target / "state")
        for path in target.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        files = {p.relative_to(target).as_posix(): {"sha256": digest(p), "bytes": p.stat().st_size}
                 for p in sorted(target.rglob("*")) if p.is_file()}
        manifest = {"format": 1, "kind": "pgvn-offline-logical-backup", "files": files,
                    "build_identity": json.loads((prefix / "build-manifest.json").read_text())["build_identity"],
                    "schema_check": schema, "state_source": str(state), "cache": "ephemeral; restart all nodes on restore"}
        write_once(target / "backup-manifest.json", json.dumps(manifest, indent=2) + "\n", 0o600)
        write_once(target / "backup-manifest.json.sha256", digest(target / "backup-manifest.json") + "  backup-manifest.json\n", 0o600)
        verify(target, "backup-manifest.json")
    except Exception:
        # Keep incomplete evidence under a private directory; never label it valid.
        raise
    return {"backup": str(target), "files": len(files), "status": "complete"}


def restore(options):
    info = verify(options.backup, "backup-manifest.json")
    prefix = options.prefix.resolve(strict=True)
    package = verify(prefix)
    if not options.nodes_stopped:
        raise ValueError("Restore requires every node sharing the target database to be stopped")
    if info["build_identity"]["migrations_sha256"] != package["build_identity"]["migrations_sha256"]:
        raise ValueError("Restore with the matching schema package first; upgrade separately")
    if options.state_destination.exists():
        raise ValueError("State destination must be new; existing keys/configuration are never overwritten")
    env = database_env(options.service)
    count = run([prefix / "bin/psql", "--dbname=service=" + options.service, "-XAt", "-v", "ON_ERROR_STOP=1", "-c",
                 "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                 "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'"], env).strip()
    if count != "0":
        raise ValueError("Restore target must be an empty database; no drop/clean is performed")
    run([prefix / "bin/pg_restore", "--dbname=service=" + options.service, "--single-transaction",
         "--exit-on-error", "--no-owner", "--no-privileges", options.backup / "database.dump"], env)
    run([prefix / "bin/pgvnadmin", "db", "check"], env)
    shutil.copytree(options.backup / "state", options.state_destination)
    for path in (options.state_destination, *options.state_destination.rglob("*")):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return {"restored": True, "state": str(options.state_destination),
            "next": "Verify repositories; regrant service/monitor roles; install state with correct owner and paths; restart all nodes"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("install", "upgrade"):
        p = sub.add_parser(name)
        p.add_argument("--package", type=Path, required=True)
        p.add_argument("--root", type=Path, default=Path("/"))
        p.add_argument("--service-user", default="pgvn", type=service_name)
        p.add_argument("--nodes-stopped", action="store_true")
    p = sub.add_parser("init")
    p.add_argument("--root", type=Path, default=Path("/"))
    p.add_argument("--service-user", default="pgvn", type=service_name)
    p.add_argument("--service", type=service_name, default="pgvn-service")
    p.add_argument("--monitor-service", type=service_name, default="pgvn-monitor")
    p.add_argument("--key-id", default="linux-v1", type=service_name)
    for name in ("check", "diagnose", "start", "stop", "restart", "status", "rollback"):
        p = sub.add_parser(name)
        p.add_argument("--prefix", type=Path, default=Path("/opt/pgvn/current"))
        p.add_argument("--config", type=Path, default=Path("/etc/pgvn/pgvn.ini"))
        if name == "rollback":
            p.add_argument("--nodes-stopped", action="store_true")
        if name in ("start", "stop", "restart", "status"):
            p.add_argument("--unit", choices=("pgvnserve", "pgvnhttpd"), default="pgvnserve")
    for name in ("backup", "restore"):
        p = sub.add_parser(name)
        p.add_argument("--prefix", type=Path, default=Path("/opt/pgvn/current"))
        p.add_argument("--service", type=service_name, required=True)
        p.add_argument("--nodes-stopped", action="store_true")
        if name == "backup":
            p.add_argument("--destination", type=Path, required=True)
            p.add_argument("--state", type=Path, default=Path("/etc/pgvn"))
        else:
            p.add_argument("--backup", type=Path, required=True)
            p.add_argument("--state-destination", type=Path, required=True)
    options = parser.parse_args()
    if options.action in ("install", "upgrade"):
        result = install(options)
    elif options.action == "init":
        result = initialize(options)
    elif options.action in ("backup", "restore"):
        result = (backup if options.action == "backup" else restore)(options)
    elif options.action in ("start", "stop", "restart", "status"):
        if options.action == "start":
            run(["systemctl", "daemon-reload"])
        run(["systemctl", options.action, options.unit + ".service"], capture=False)
        result = {"unit": options.unit, "action": options.action}
    elif options.action == "rollback":
        if not options.nodes_stopped:
            raise ValueError("Stop all shared-PG nodes first")
        previous = options.prefix.parent / "previous"
        verify(previous.resolve(strict=True))
        # Use the old binary against the current DB: schema/hash incompatibility
        # fails before switching. SQL migrations are never reversed implicitly.
        run([previous / "bin/pgvnadmin", "--config", options.config, "db", "check"])
        atomic_link(options.prefix, previous.resolve())
        result = {"rollback": str(options.prefix.resolve()), "services_started": False}
    elif options.action == "check":
        verify(options.prefix.resolve(strict=True))
        run([options.prefix / "bin/pgvnserve", "--check-config", "--config", options.config])
        run([options.prefix / "bin/pgvnadmin", "--config", options.config, "db", "check"])
        result = {"configuration": "valid", "database": "valid"}
    else:
        verify(options.prefix.resolve(strict=True))
        # Native diagnose returns sanitized facts; environment and config bodies
        # are deliberately excluded from an exportable diagnostic report.
        result = json.loads(run([options.prefix / "bin/pgvnadmin", "--config", options.config, "diagnose"]))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        sys.exit(1)
