"""SDK-free provenance tests; the formatter uses explicit APR/libpq test doubles.

These are not Windows, native Linux product, or W1/W2 acceptance tests.
Run: python -m unittest discover -s tests/pgvn -p test_build_metadata.py -v
"""
from pathlib import Path
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/pgvn"))
from build_metadata import (VERSION_PATH, check_pair, check_runtime, collect, migration_inventory,
                            object_sha256, read_version, sha256, source_inventory, write_metadata)
from check_pair import load_package


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pgvn-metadata-")
        self.work = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def identity(self, target="windows"):
        identity = collect(ROOT, target, "Release")["build_identity"]
        # A synthetic clean identity is used only for pairing unit fixtures.
        identity["git_worktree_dirty"] = False
        return identity

    def runtime(self, identity):
        return {**copy.deepcopy(identity), "apr_runtime": "1.7.6", "apu_runtime": "1.6.3", "libpq_version": 180006}

    def package(self, target):
        directory = self.work / target
        directory.mkdir()
        metadata = collect(ROOT, target, "Release")
        identity = self.identity(target)
        metadata["build_identity"] = identity
        products = {}
        for name in ("bin/pgvnserve", "bin/pgvnadmin", "apache/modules/mod_dav_pgvn.so", "apache/modules/mod_authz_pgvn.so"):
            if target == "windows" and name.startswith("bin/"):
                name += ".exe"
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("UNIT FIXTURE, NOT A NATIVE PRODUCT: " + name).encode())
            products["runtime/pgvn/" + name] = {"sha256": sha256(path)}
        reference = directory / "source-reference/version.json"
        reference.parent.mkdir()
        shutil.copyfile(ROOT / VERSION_PATH, reference)
        for namespace, migrations in metadata["migrations"].items():
            for name in migrations:
                target_path = directory / "schema-reference" / ("pgvn" if namespace == "pgvn" else "") / name
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / "sql" / ("pgvn" if namespace == "pgvn" else "") / name, target_path)
        build = {**metadata, "products": products,
                 "runtime_build_info": {name: self.runtime(identity) for name in ("pgvnserve", "pgvnadmin")}}
        if target == "windows":
            build["apache_host"] = {"runtime": {"build_identity": copy.deepcopy(identity)}}
        self.write_json(directory / "build-manifest.json", build)
        package = {"version": identity["version"], "git_base_commit": identity["git_commit"],
                   "build_identity": identity, "build_manifest_sha256": sha256(directory / "build-manifest.json")}
        self.save_package(directory, package)
        return directory

    def write_json(self, path, value):
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def save_package(self, directory, package):
        package["files"] = {
            path.relative_to(directory).as_posix(): {"sha256": sha256(path), "bytes": path.stat().st_size}
            for path in directory.rglob("*") if path.is_file()
            and path.name not in ("package-manifest.json", "package-manifest.json.sha256")}
        self.write_json(directory / "package-manifest.json", package)
        (directory / "package-manifest.json.sha256").write_text(
            sha256(directory / "package-manifest.json") + "  package-manifest.json\n", encoding="ascii")

    def mutate_build(self, directory, mutate):
        path = directory / "build-manifest.json"
        build = json.loads(path.read_text(encoding="utf-8"))
        mutate(build)
        self.write_json(path, build)
        package = json.loads((directory / "package-manifest.json").read_text(encoding="utf-8"))
        package["build_manifest_sha256"] = sha256(path)
        self.save_package(directory, package)

    def test_deterministic_shared_inputs_across_targets(self):
        first = write_metadata(ROOT, self.work / "linux", "linux", "Release")
        before = (self.work / "linux/pgvn_build_info.h").read_bytes()
        again = write_metadata(ROOT, self.work / "linux", "linux", "Release")
        windows = write_metadata(ROOT, self.work / "windows", "windows", "Release")
        self.assertEqual(first, again)
        self.assertEqual(before, (self.work / "linux/pgvn_build_info.h").read_bytes())
        linux_id, windows_id = copy.deepcopy(first["build_identity"]), copy.deepcopy(windows["build_identity"])
        linux_id.pop("target_os")
        windows_id.pop("target_os")
        self.assertEqual(linux_id, windows_id)
        self.assertEqual(first["sources"], windows["sources"])

    def test_inventory_covers_version_legacy_builder_and_patch_specs(self):
        inventory = source_inventory(ROOT)
        for name in (VERSION_PATH, "scripts/build.py", "scripts/pgvn/build_metadata.py",
                     "patches/pgvn/subversion-1.14.5/protocol.json"):
            self.assertEqual(inventory[name], sha256(ROOT / name))
        self.assertFalse(any("__pycache__" in name for name in inventory))

    def test_schema_and_explicit_migration_list_must_agree(self):
        version = read_version(ROOT)
        self.assertEqual(set(migration_inventory(ROOT, version)), {"pgvn", "pgfs"})
        for schema in ("catalog", "fs"):
            invalid = copy.deepcopy(version)
            invalid["schemas"][schema] += 1
            with self.subTest(schema=schema), self.assertRaisesRegex(ValueError, "Migration list/schema mismatch"):
                migration_inventory(ROOT, invalid)

    def test_unregistered_sql_migration_is_rejected(self):
        shutil.copytree(ROOT / "sql", self.work / "sql")
        (self.work / "sql/pgvn/007_unregistered.sql").write_text("BEGIN; COMMIT;\n")
        with self.assertRaisesRegex(ValueError, "Migration list/schema mismatch"):
            migration_inventory(self.work, read_version(ROOT))

    def test_version_descriptor_rejects_bad_versions_and_capabilities(self):
        version = read_version(ROOT)
        path = self.work / VERSION_PATH
        path.parent.mkdir(parents=True)
        for key, value in (("version", 'bad"version'), ("api_version", True),
                           ("cache_format_version", -1), ("capabilities", {"monitor": "full"})):
            invalid = copy.deepcopy(version)
            invalid[key] = value
            self.write_json(path, invalid)
            with self.subTest(key=key), self.assertRaises(ValueError):
                read_version(self.work)

    def test_runtime_identity_rejects_missing_or_changed_fields(self):
        identity = self.identity()
        check_runtime({**self.runtime(identity), "ready": False}, identity)
        for name in identity:
            invalid = self.runtime(identity)
            del invalid[name]
            with self.subTest(name=name), self.assertRaises(ValueError):
                check_runtime(invalid, identity)

    def test_runtime_rejects_unpinned_dependency_versions(self):
        identity = self.identity()
        for name, value in (("apr_runtime", "1.7.5"), ("apu_runtime", "1.6.1"), ("libpq_version", 170006)):
            invalid = self.runtime(identity)
            invalid[name] = value
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Runtime dependency version mismatch"):
                check_runtime(invalid, identity)

    def test_matching_clean_release_pair(self):
        check_pair(self.identity("windows"), self.identity("linux"))

    def test_pair_rejects_provenance_or_contract_differences(self):
        windows, linux = self.identity("windows"), self.identity("linux")
        changes = {"version": "0.2.0-dev", "git_commit": "1" * 40, "source_manifest_sha256": "1" * 64,
                   "migrations_sha256": "1" * 64, "schemas": {**linux["schemas"], "catalog": linux["schemas"]["catalog"]+1},
                   "api_version": 2, "cache_format_version": linux["cache_format_version"]+1,
                   "upstream": {**linux["upstream"], "subversion": "1.14.6"},
                   "capabilities": {**linux["capabilities"], "svn_authz_bypass": not linux["capabilities"]["svn_authz_bypass"]},
                   "configuration": "Debug", "git_worktree_dirty": True,
                   "target_os": "windows", "target_arch": "aarch64"}
        for name, value in changes.items():
            invalid = copy.deepcopy(linux)
            invalid[name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                check_pair(windows, invalid)

    def test_pair_rejects_missing_or_unknown_metadata(self):
        windows, linux = self.identity("windows"), self.identity("linux")
        for name in linux:
            invalid = copy.deepcopy(linux)
            del invalid[name]
            with self.subTest(name=name), self.assertRaises(ValueError):
                check_pair(windows, invalid)
        with self.assertRaises(ValueError):
            check_pair(windows, {**linux, "unknown_capability_format": 2})

    def test_package_pair_cli_and_file_integrity(self):
        windows, linux = self.package("windows"), self.package("linux")
        command = [sys.executable, str(ROOT / "scripts/pgvn/check_pair.py"), "--windows", str(windows), "--linux", str(linux)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["provenance_match"])
        self.assertEqual(report["functional_acceptance"], "not_checked")
        (linux / "bin/pgvnserve").write_bytes(b"changed binary")
        rejected = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("Package file mismatch", rejected.stderr)

    def test_package_rejects_rehashed_source_and_migration_inventories(self):
        for target, field in (("windows", "sources"), ("linux", "migrations")):
            directory = self.package(target)
            self.mutate_build(directory, lambda build: build[field].clear())
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "inventory mismatch"):
                load_package(directory)

    def test_package_rejects_rehashed_runtime_and_product_changes(self):
        directory = self.package("windows")
        self.mutate_build(directory, lambda build: build["runtime_build_info"]["pgvnadmin"].update(version="0.0.0"))
        with self.assertRaisesRegex(ValueError, "Runtime/build identity mismatch: version"):
            load_package(directory)
        directory = self.package("linux")
        self.mutate_build(directory, lambda build: build["products"]["runtime/pgvn/bin/pgvnserve"].update(sha256="0" * 64))
        with self.assertRaisesRegex(ValueError, "Package product mismatch"):
            load_package(directory)

    def test_package_rejects_rehashed_descriptor_or_migration_files(self):
        for target, relative in (("windows", "source-reference/version.json"),
                                 ("linux", "schema-reference/pgvn/004_protocol.sql")):
            directory = self.package(target)
            (directory / relative).write_bytes(b"changed reference file")
            package = json.loads((directory / "package-manifest.json").read_text(encoding="utf-8"))
            self.save_package(directory, package)
            with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, "Packaged (version descriptor|migration) mismatch"):
                load_package(directory)

    def test_package_rejects_manifest_checksum_and_path_escape(self):
        directory = self.package("windows")
        package = json.loads((directory / "package-manifest.json").read_text(encoding="utf-8"))
        self.write_json(directory / "package-manifest.json", {**package, "version": "0.0.0"})
        with self.assertRaisesRegex(ValueError, "Package manifest checksum mismatch"):
            load_package(directory)
        for relative in ("../outside", "/outside", "C:/outside", "bin\\outside"):
            self.save_package(directory, package)
            package["files"][relative] = {"sha256": "0" * 64, "bytes": 0}
            self.write_json(directory / "package-manifest.json", package)
            (directory / "package-manifest.json.sha256").write_text(sha256(directory / "package-manifest.json"))
            with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, "Invalid package path"):
                load_package(directory)

    def test_top_level_metadata_only_entry_needs_no_sdk(self):
        for target in ("windows", "linux"):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/pgvn/build.py"), "--metadata-only",
                                     "--target", target, "--configuration", "Release"],
                                    cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertFalse(output["products_built"])
            self.assertEqual(output["build_identity"]["target_os"], target)

    def test_linux_build_requires_all_verified_sdks(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/pgvn/build.py"), "--target", "linux"],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--linux-sdk, --linux-httpd-sdk and --linux-tools-sdk are required", result.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc"), "Linux host C compiler required")
    def test_production_formatter_roundtrip_and_buffer_bounds(self):
        # Only dependency functions are doubled. Compile and run the actual
        # formatter for both OS branches; this is not a cross-platform binary.
        (self.work / "apr_version.h").write_text("const char *apr_version_string(void);\n")
        (self.work / "apu_version.h").write_text("const char *apu_version_string(void);\n")
        (self.work / "libpq-fe.h").write_text("int PQlibVersion(void);\n")
        (self.work / "windows.h").write_text("unsigned int GetACP(void);\n")
        driver = self.work / "driver.c"
        driver.write_text('''#include <stdio.h>
#include <string.h>
#include "build_info.h"
const char *apr_version_string(void) { return "1.7.6"; }
const char *apu_version_string(void) { return "1.6.3"; }
int PQlibVersion(void) { return 180006; }
unsigned int GetACP(void) { return 65001; }
int main(void) {
    char output[PGVN_BUILD_JSON_SIZE], small[8];
    int length = pgvn_build_json(output, sizeof(output));
    if (length < 0 || (size_t)length >= sizeof(output) || strlen(output) != (size_t)length) return 1;
    memset(small, 'X', sizeof(small));
    if (pgvn_build_json(small, sizeof(small)) != length || small[7] != 0) return 2;
    if (pgvn_build_json(NULL, 0) != length) return 3;
    fputs(output, stdout);
    return 0;
}
''', encoding="ascii")
        for target in ("linux", "windows"):
            generated = self.work / target
            metadata = write_metadata(ROOT, generated, target, "Release")
            executable = generated / "formatter"
            command = ["cc", "-std=c17", "-Wall", "-Wextra", "-Werror", "-I", str(generated),
                       "-I", str(self.work), "-I", str(ROOT / "src/pgvn/core")]
            if target == "windows":
                command.append("-D_WIN32")
            command += [str(ROOT / "src/pgvn/core/build_info.c"), str(driver), "-o", str(executable)]
            compiled = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            output = subprocess.check_output([str(executable)], text=True, timeout=10)
            runtime = json.loads(output)
            check_runtime(runtime, metadata["build_identity"])
            self.assertEqual(runtime["process_code_page"], 65001 if target == "windows" else 0)
            self.assertFalse(runtime["ready"])

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc"), "Linux host C compiler required")
    def test_http_host_reports_same_build_identity(self):
        # Host-compiled unit probe of the Windows entry, with Windows API test
        # doubles. No Apache server, Windows executable, or listener is started.
        metadata = write_metadata(ROOT, self.work, "windows", "Release")
        (self.work / "windows.h").write_text('''#include <wchar.h>
#define SEM_FAILCRITICALERRORS 1
#define SEM_NOGPFAULTERRORBOX 2
#define CP_UTF8 65001
#define WC_ERR_INVALID_CHARS 128
void SetErrorMode(unsigned int mode);
unsigned int GetACP(void);
int WideCharToMultiByte(unsigned int a, unsigned int b, const wchar_t *c, int d, char *e, int f, void *g, void *h);
''', encoding="ascii")
        (self.work / "ap_release.h").write_text('#define AP_SERVER_BASEREVISION "2.4.68"\n', encoding="ascii")
        driver = self.work / "host.c"
        driver.write_text('''#include "windows.h"
int wmain(int argc, wchar_t **argv);
void SetErrorMode(unsigned int mode) { (void)mode; }
unsigned int GetACP(void) { return CP_UTF8; }
int WideCharToMultiByte(unsigned int a, unsigned int b, const wchar_t *c, int d, char *e, int f, void *g, void *h)
{ (void)a; (void)b; (void)c; (void)d; (void)e; (void)f; (void)g; (void)h; return 0; }
int pgvn_httpd_main(int argc, const char *const argv[]) { (void)argc; (void)argv; return 1; }
int main(void) { wchar_t *args[] = {L"pgvnhttpd", L"--pgvn-runtime"}; return wmain(2, args); }
''', encoding="ascii")
        executable = self.work / "host"
        result = subprocess.run(["cc", "-std=c17", "-D_WIN32", "-Wall", "-Wextra", "-Werror", "-I", str(self.work),
                                 str(ROOT / "src/pgvn/httpd/entry.c"), str(driver), "-o", str(executable)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = json.loads(subprocess.check_output([str(executable)], text=True, timeout=10))
        self.assertEqual(runtime["build_identity"], metadata["build_identity"])
        self.assertEqual(runtime["apache_base"], metadata["build_identity"]["upstream"]["httpd"])

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc"), "Linux host C compiler required")
    def test_header_rejects_compiler_os_mismatch(self):
        write_metadata(ROOT, self.work, "windows", "Release")
        driver = self.work / "target.c"
        driver.write_text('#include "pgvn_build_info.h"\nint main(void) { return 0; }\n', encoding="ascii")
        result = subprocess.run(["cc", "-I", str(self.work), "-c", str(driver), "-o", str(self.work / "target.o")],
                                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the compiler target OS", result.stderr)


if __name__ == "__main__":
    unittest.main()
