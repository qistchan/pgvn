"""Embed ordered, checksummed SQL migrations into the native administrative CLI."""
import hashlib
import json

STORAGE = ("001_initial.sql", "002_version_tree.sql", "003_locks.sql", "004_hotcopy.sql")
CATALOG = ("001_catalog.sql", "002_admin.sql", "003_credentials.sql", "004_protocol.sql", "005_monitor.sql", "006_dav_state.sql", "007_linux_hooks.sql")


def embed(root, output):
    rows = []
    arrays = ["/* Generated from the explicit migration list. Do not edit. */"]
    for namespace, directory, names in (("pgfs", root / "sql", STORAGE), ("pgvn", root / "sql/pgvn", CATALOG)):
        for version, name in enumerate(names, 1):
            path = directory / name
            data = path.read_bytes()
            source = data.decode("utf-8")
            lines = source.splitlines(True)
            if sum(line.strip() == "BEGIN;" for line in lines) != 1 or sum(line.strip() == "COMMIT;" for line in lines) != 1:
                raise RuntimeError(f"Migration must have one explicit transaction wrapper: {path}")
            body = "".join(line for line in lines if line.strip() not in ("BEGIN;", "COMMIT;"))
            variable = f"pgvn_sql_{namespace}_{version}"
            encoded = body.encode("utf-8") + b"\0"
            arrays.append(f"static const unsigned char {variable}[]={{" + ",".join(str(v) for v in encoded) + "};")
            rows.append("{" + ",".join((json.dumps(namespace), str(version), json.dumps(hashlib.sha256(data).hexdigest()),
                                        "(const char *)" + variable, json.dumps(path.relative_to(root).as_posix()))) + "}")
    arrays.append("static const admin_migration pgvn_migrations[]={\n" + ",\n".join(rows) + "\n};\n")
    output.write_text("\n".join(arrays), encoding="ascii")
