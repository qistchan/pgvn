"""Audit source integrity and complete, matching-build SVN compatibility runs."""
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
import ast
import hashlib
import json
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts/tests"
sys.path.insert(0, str(ROOT / "tests"))
from run_compatibility import FILE_SUITES, SERVICE_SUITES
from compatibility_matrix import UPSTREAM_EXCLUSIONS, NATIVE_EXCLUSIONS
from verify_corpus_properties import dump_revision_properties, xml_revision_properties, property_digest


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_count(suite):
    path = ROOT / "vendor/subversion-1.14.5/subversion/tests/cmdline" / (suite + "_tests.py")
    parsed = ast.parse(path.read_text(encoding="utf8"))
    tests = next(node.value for node in parsed.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "test_list" for target in node.targets))
    assert isinstance(tests, ast.List), suite + " has a nonliteral test list; audit it explicitly"
    return len(tests.elts) - 1, path


def audit_log(path, expected, label):
    output = path.read_text(encoding="utf8", errors="replace")
    results = re.findall(r"^(PASS|FAIL|SKIP|XFAIL|XPASS):\s+\S+\s+(\d+):.*$", output, re.M)
    numbers = [int(number) for status, number in results]
    assert len(numbers) == len(set(numbers)) and set(numbers) == expected, label + " has missing/duplicate test results"
    counts = dict(Counter(status for status, number in results))
    assert not counts.get("FAIL") and not counts.get("XPASS"), label + " has unexpected results"
    exceptions = [line for line in output.splitlines() if line.startswith(("SKIP:", "XFAIL:"))]
    return counts, exceptions


module = ROOT / "runtime/svn/bin/libsvn_fs_pg-1.dll"
digest = sha(module)
manifest = json.loads((ROOT / "artifacts/build/manifest.json").read_text())
assert manifest["dll_sha256"] == digest, "Build manifest does not match module"
assert all(sha(ROOT / path) == expected for path, expected in manifest["sources"].items()), "Native source differs from tested build"
assert len(manifest["callbacks"]) == 78
assert sum(item["status"] == "implemented" for item in manifest["callbacks"]) == 77
assert all(not (item.get("implementation") or "").startswith("pgfs_stub_") for item in manifest["callbacks"])
assert [(item["vtable"], item["callback"], item["status"]) for item in manifest["callbacks"]
        if item["status"] != "implemented"] == [("root_vtable_t", "report_changes", "emulated")]

# All upstream C, headers and Python test sources must match the pinned archive.
archive = ROOT / "vendor/subversion-git-1.14.5.zip"
with zipfile.ZipFile(archive) as source_zip:
    prefix = source_zip.namelist()[0].split("/")[0] + "/"
    checked = 0
    for name in source_zip.namelist():
        if not name.startswith(prefix) or Path(name).suffix not in (".c", ".h", ".py"):
            continue
        relative = name[len(prefix):]
        path = ROOT / "vendor/subversion-1.14.5" / relative
        assert path.is_file() and path.read_bytes() == source_zip.read(name), "Upstream source modified: " + relative
        checked += 1
assert checked == 1195, "Pinned source archive scope changed"

custom = {}
for name, expected_count in (("p0", 7), ("p1", 7), ("p2", 4), ("admin", 7), ("faults", 2),
                             ("services", 3), ("recovery", 2), ("synthetic-corpus", 1)):
    data = json.loads((ARTIFACTS / (name + "-results.json")).read_text())
    count = data.get("tests", data.get("tests_run"))
    assert count == expected_count and not data["failures"] and not data["errors"], name + " is incomplete or failed"
    assert data["dll_sha256"] == digest, name + " tests a different module"
    custom[name] = {"tests": count, "passed": True, "recorded_at": data.get("timestamp", data.get("time_utc"))}
service = json.loads((ARTIFACTS / "services-results.json").read_text())
original_svn = Path(r"C:\Polarion\bundled\svn/bin/svn.exe")
svnkit = Path(r"C:\Polarion\polarion/plugins/org.tmatesoft.svnkit_1.10.1.r10777.jar")
assert service["original_svn_client_sha256"] == sha(original_svn)
assert service["svnkit_sha256"] == sha(svnkit)
dual_service = json.loads((ARTIFACTS / "dual-service-fixture.json").read_text())
assert dual_service["independent_frontends"] == 2 and dual_service["head"] == 2
assert len(set(dual_service["ports"])) == 2
assert Path(dual_service["repository"]).resolve().is_relative_to((ROOT / "runtime/repos").resolve())
assert all(dual_service[name] is True for name in ("commits_visible_both_directions", "revprops_visible_both_directions",
                                                "shared_lock_enforcement", "restricted_read_denied"))

upstream = {}
matrix = [(suite, "file") for suite in FILE_SUITES] + list(SERVICE_SUITES) + [("svnadmin", "file")]
assert len(matrix) == len(set(matrix)), "Duplicate matrix entries"
for suite, transport in matrix:
    suffix = "" if transport == "file" else "-" + transport
    label = suite + "/" + transport
    data_path = ARTIFACTS / ("upstream-" + suite + "-pg" + suffix + ".json")
    data = json.loads(data_path.read_text())
    total, source = source_count(suite)
    exclusions = UPSTREAM_EXCLUSIONS.get(suite, {})
    expected = set(range(1, total + 1)) - set(exclusions)
    assert data["exit_code"] == 0 and data["dll_sha256"] == digest, label + " failed or tests another module"
    assert not data.get("requested_selection", data["selection"]), label + " is only a partial selection"
    if exclusions:
        assert data["applicable_matrix"]
        assert data["selection"] == [str(i) for i in sorted(expected)]
        assert data["backend_specific_exclusions"] == {str(i): reason for i, reason in exclusions.items()}
    else:
        assert not data["selection"], label + " unexpectedly selects individual cases"
    if "source_sha256" in data:
        assert data["source_sha256"] == sha(source), label + " source changed after running"
    assert data.get("transport", "file") == transport
    counts, exceptions = audit_log(data_path.with_suffix(".log"), expected, label)
    upstream[label] = {"counts": counts, "upstream_total": total,
                       "excluded": {str(i): reason for i, reason in exclusions.items()},
                       "skips_and_expected_failures": exceptions,
                       "workspace": data["workspace"], "recorded_at": data["timestamp"],
                       "source_sha256": sha(source)}

native = {}
native_manifest = json.loads((ROOT / "artifacts/build/native-test-manifest.json").read_text())
for suite, total in (("fs", 70), ("repos", 32)):
    data_path = ARTIFACTS / ("native-" + suite + "-pg.json")
    data = json.loads(data_path.read_text())
    exclusions = NATIVE_EXCLUSIONS[suite]
    expected = set(range(1, total + 1)) - set(exclusions)
    assert data["exit_code"] == 0 and data["dll_sha256"] == digest, "Native " + suite + " failed or tests another module"
    assert data["applicable_matrix"] and not data["requested_selection"]
    assert data["selection"] == [str(i) for i in sorted(expected)]
    assert data["backend_specific_exclusions"] == {str(i): reason for i, reason in exclusions.items()}
    binary = ROOT / "runtime/svn/bin" / (suite + "-test.exe")
    source = ROOT / "vendor/subversion-1.14.5/subversion/tests" / ("libsvn_" + suite) / (suite + "-test.c")
    assert data["test_binary_sha256"] == native_manifest[suite + "-test"]["binary_sha256"] == sha(binary)
    assert native_manifest[suite + "-test"]["source_sha256"] == sha(source)
    counts, exceptions = audit_log(data_path.with_suffix(".log"), expected, "native " + suite)
    assert data["counts"] == counts
    native[suite] = {"counts": counts, "upstream_total": total, "excluded": data["backend_specific_exclusions"],
                     "skips_and_expected_failures": exceptions, "workspace": data["workspace"],
                     "recorded_at": data["timestamp"]}

corpus = json.loads((ARTIFACTS / "synthetic-corpus-fixture.json").read_text())
assert corpus["generated_data"] and corpus["all_revisions_compared"] == corpus["all_revisions_verified"] == corpus["head"] + 1
fixture = Path(corpus["fixture"]).resolve()
assert fixture.is_relative_to((ROOT / "runtime/repos").resolve())
assert corpus["archive_sha256"] == sha(fixture / "generated.dump")
assert [item["revision"] for item in corpus["snapshots"]] == list(range(corpus["head"] + 1))
for item in corpus["snapshots"]:
    source = fixture / ("fsfs-r" + str(item["revision"]) + ".snapshot")
    target = fixture / ("pg-r" + str(item["revision"]) + ".snapshot")
    assert source.read_bytes() == target.read_bytes() and sha(source) == item["sha256"]

real_corpus = None
real_report = ARTIFACTS / "polarion-corpus-results.json"
if real_report.is_file():
    real = json.loads(real_report.read_text(encoding="utf8"))
    assert real["passed"] and real["dll_sha256"] == digest, "Real corpus did not pass on this module"
    real_fixture = Path(real["fixture"]).resolve()
    assert real_fixture.is_relative_to((ROOT / "runtime/corpus").resolve())
    assert Path(real["target"]).resolve() == real_fixture / "pg"
    assert Path(real["source"]).resolve() == Path(r"C:\Polarion\data\svn\repo").resolve()
    real_head = real["source_head_at_start"]
    assert real["source_head_at_end"] >= real_head
    assert real["all_revisions_verified"] == real["changed_path_revisions_compared"] == real_head + 1
    assert real["archive_sha256"] == sha(real_fixture / "source.dump")
    assert real["archive_bytes"] == (real_fixture / "source.dump").stat().st_size
    provenance = real["import_provenance"]
    assert provenance["revisions_loaded"] == real_head + 1
    assert provenance["archive_sha256"] == real["archive_sha256"]
    import_digest = provenance["dll_sha256"]
    assert re.fullmatch("[0-9a-f]{64}", import_digest)
    if import_digest != digest:
        historical_modules = list((ROOT / "artifacts/history").glob("*-" + import_digest[:12] + "/libsvn_fs_pg-1.dll"))
        assert any(sha(path) == import_digest for path in historical_modules), "Import module provenance unavailable"
    assert [item["revision"] for item in real["snapshots"]] == sorted({0, 1, real_head // 2, max(0, real_head - 1), real_head})
    for item in real["snapshots"]:
        old_snapshot = real_fixture / ("source-r" + str(item["revision"]) + ".snapshot")
        new_snapshot = real_fixture / ("pg-r" + str(item["revision"]) + ".snapshot")
        assert old_snapshot.read_bytes() == new_snapshot.read_bytes() and sha(old_snapshot) == item["sha256"]
        assert item["nodes"] == sum(line.startswith(b"N|") for line in old_snapshot.read_bytes().splitlines())
    archived_props = dump_revision_properties(real_fixture / "source.dump")
    assert set(archived_props) == set(range(real_head + 1))
    property_report = real["revision_properties"]
    assert property_report["revisions_compared"] == real_head + 1 and property_report["source"] == "fixed_dump"
    assert [item["revision"] for item in property_report["records"]] == list(range(real_head + 1))
    for item in property_report["records"]:
        expected_props = archived_props[item["revision"]]
        actual_props = xml_revision_properties((real_fixture / ("pg-r" + str(item["revision"]) + ".revprops.xml")).read_bytes())
        assert actual_props == expected_props
        assert item["sha256"] == property_digest(expected_props) and item["properties"] == len(expected_props)
    real_corpus = {"head": real_head, "revisions_verified": real_head + 1,
                   "import_dll_sha256": import_digest, "verification_dll_sha256": digest,
                   "changed_path_revisions_compared": real_head + 1,
                   "revision_property_revisions_compared": real_head + 1,
                   "full_snapshot_revisions": [item["revision"] for item in real["snapshots"]],
                   "snapshot_node_counts": {str(item["revision"]): item["nodes"] for item in real["snapshots"]},
                   "fixture": str(real_fixture), "archive_bytes": real["archive_bytes"],
                   "archive_sha256": real["archive_sha256"], "source_head_at_end": real["source_head_at_end"]}

report = {"timestamp": datetime.now(timezone.utc).isoformat(),
          "objective_status": "svn_compatibility_and_services_verified" if real_corpus else "in_progress",
          "dll_sha256": digest, "custom_passed": sum(item["tests"] for item in custom.values()),
          "upstream_matrix_runs": len(upstream),
          "upstream_passed": sum(item["counts"].get("PASS", 0) for item in upstream.values()),
          "upstream_skipped": sum(item["counts"].get("SKIP", 0) for item in upstream.values()),
          "upstream_expected_failures": sum(item["counts"].get("XFAIL", 0) for item in upstream.values()),
          "native_passed": sum(item["counts"].get("PASS", 0) for item in native.values()),
          "native_skipped": sum(item["counts"].get("SKIP", 0) for item in native.values()),
          "native_excluded": sum(len(item["excluded"]) for item in native.values()),
          "custom": custom, "upstream": upstream, "native": native,
          "callback_inventory": {"implemented":77,"loader_emulated":1,"unbound_error_callbacks":0},
          "two_frontend_consistency": dual_service,
          "polarion_corpus": real_corpus,
          "synthetic_corpus": {"head": corpus["head"], "revisions_compared": corpus["all_revisions_compared"], "fixture": str(fixture)},
          "upstream_source_integrity": {"files_checked": checked, "archive_sha256": sha(archive)},
          "sql_migrations": {p.name: sha(p) for p in sorted((ROOT / "sql").glob("*.sql"))},
          "remaining": (["Real Polarion fixed-history comparison is still running"] if real_corpus is None else []) +
                       ["Actual Polarion application integration, representative performance and PostgreSQL HA are later milestones"]}
(ARTIFACTS / "compatibility-evidence.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
print(json.dumps({key: report[key] for key in ("objective_status", "custom_passed", "upstream_matrix_runs", "upstream_passed",
                                             "upstream_skipped", "upstream_expected_failures", "native_passed", "native_skipped",
                                             "native_excluded", "dll_sha256")}, indent=2))
