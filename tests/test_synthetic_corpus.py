"""Full-history import and semantic comparison of a generated ALM-style repo."""
from datetime import datetime, timezone
import hashlib
import json
import subprocess
import unittest
import xml.etree.ElementTree as ET
from test_p1 import NativeTreeTests, ROOT, BIN


class SyntheticCorpusTests(unittest.TestCase):
    setUp = NativeTreeTests.setUp
    run_tool = NativeTreeTests.run_tool
    create = NativeTreeTests.create
    youngest = NativeTreeTests.youngest
    checkout = NativeTreeTests.checkout
    put = NativeTreeTests.put

    def commit(self, wc, message):
        self.run_tool("svn", "commit", wc, "-m", message)
        self.run_tool("svn", "update", wc)

    def revprops(self, repo, rev):
        document = ET.fromstring(self.run_tool("svnlook", "proplist", "--revprop", "--verbose",
                                               "--xml", "-r", rev, repo).stdout)
        return sorted((tuple(sorted(prop.attrib.items())), prop.text)
                      for prop in document.findall(".//property"))

    def test_every_revision_matches_fsfs_after_full_history_load(self):
        source, url = self.create("fsfs")
        wc = self.checkout(url, "source-wc")
        for i in range(32):
            xml = f'<work-item id="DEMO-{i}" revision="0"><title>需求 {i}</title><status>open</status></work-item>\n'
            self.put(wc, f"trunk/projects/demo/workitems/DEMO-{i}.xml", xml.encode("utf8"))
        self.put(wc, "trunk/.polarion/project.xml", b'<project id="demo"/>\n')
        self.put(wc, "trunk/projects/demo/modules/spec.xml", b'<module><item ref="DEMO-1"/></module>\n')
        payload = bytes(range(256)) * 4097
        self.put(wc, "trunk/projects/demo/attachments/data.bin", payload)
        (wc / "branches").mkdir()
        (wc / "releases").mkdir()
        self.run_tool("svn", "add", wc / "trunk", wc / "branches", wc / "releases")
        binary_property = self.fixture / "property.bin"
        binary_property.write_bytes(b"owner\0team\xff")
        self.run_tool("svn", "propset", "alm:binary", "-F", binary_property, wc / "trunk/projects/demo")
        self.commit(wc, "initial generated ALM content")
        for i in range(1, 11):
            path = f"trunk/projects/demo/workitems/DEMO-{i}.xml"
            self.put(wc, path, f'<work-item id="DEMO-{i}" revision="{i}"><status>approved</status></work-item>\n'.encode())
            self.run_tool("svn", "propset", "alm:state", "approved", wc / path)
            self.commit(wc, "work item " + str(i))
        self.run_tool("svn", "copy", url + "/trunk", url + "/branches/review", "-m", "review branch")
        branch = self.checkout(url + "/branches/review", "review-wc")
        self.put(branch, "projects/demo/attachments/data.bin", payload + b"review")
        self.run_tool("svn", "delete", branch / "projects/demo/workitems/DEMO-0.xml")
        self.put(branch, "projects/demo/workitems/DEMO-0.xml", b'<work-item id="replacement"/>\n')
        self.run_tool("svn", "add", branch / "projects/demo/workitems/DEMO-0.xml")
        self.run_tool("svn", "propset", "svn:mergeinfo", "/trunk:2-11", branch)
        self.commit(branch, "branch replacement, attachment and mergeinfo")
        self.run_tool("svn", "copy", url + "/branches/review", url + "/branches/nested", "-m", "nested copy")
        self.run_tool("svn", "copy", url + "/trunk@5", url + "/branches/legacy", "-m", "historical branch")
        self.run_tool("svn", "move", url + "/branches/review", url + "/releases/v1", "-m", "release move")
        self.run_tool("svn", "delete", url + "/trunk", "-m", "remove original copy source")
        self.run_tool("svn", "copy", url + "/trunk@7", url + "/trunk", "-m", "resurrect historical source")
        self.run_tool("svn", "update", wc)
        self.run_tool("svn", "propdel", "alm:binary", wc / "trunk/projects/demo")
        self.put(wc, "trunk/projects/demo/attachments/empty.bin", b"")
        self.run_tool("svn", "add", wc / "trunk/projects/demo/attachments/empty.bin")
        self.commit(wc, "remove binary property and add empty attachment")
        head = self.youngest(source)
        target, _ = self.create("pg")
        archive = self.fixture / "generated.dump"
        with archive.open("wb") as output:
            dump = subprocess.run([str(BIN / "svnadmin.exe"), "dump", "--deltas", str(source)],
                                  env=self.env, stdout=output, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(dump.returncode, 0, dump.stderr)
        with archive.open("rb") as input_file:
            load = subprocess.run([str(BIN / "svnadmin.exe"), "load", "--force-uuid", str(target)],
                                  env=self.env, stdin=input_file, capture_output=True, timeout=120)
        self.assertEqual(load.returncode, 0, load.stderr)
        self.assertEqual(self.youngest(target), head)
        self.assertEqual(self.run_tool("svnlook", "uuid", source).stdout,
                         self.run_tool("svnlook", "uuid", target).stdout)
        self.run_tool("svnadmin", "verify", target)
        snapshots = []
        for rev in range(head + 1):
            expected = self.run_tool("fs_probe", "snapshot", source, rev).stdout
            actual = self.run_tool("fs_probe", "snapshot", target, rev).stdout
            (self.fixture / f"fsfs-r{rev}.snapshot").write_bytes(expected)
            (self.fixture / f"pg-r{rev}.snapshot").write_bytes(actual)
            self.assertEqual(actual, expected, "Logical snapshot differs at revision " + str(rev))
            self.assertEqual(self.run_tool("svnlook", "changed", "--copy-info", "-r", rev, target).stdout,
                             self.run_tool("svnlook", "changed", "--copy-info", "-r", rev, source).stdout)
            self.assertEqual(self.revprops(target, rev), self.revprops(source, rev))
            snapshots.append({"revision": rev, "sha256": hashlib.sha256(expected).hexdigest(),
                              "nodes": sum(line.startswith(b"N|") for line in expected.splitlines())})
        report = {"fixture": str(self.fixture), "generated_data": True, "head": head,
                  "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                  "all_revisions_verified": head + 1, "all_revisions_compared": head + 1,
                  "snapshots": snapshots}
        (ROOT / "artifacts/tests/synthetic-corpus-fixture.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SyntheticCorpusTests))
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "tests": result.testsRun,
              "errors": len(result.errors), "failures": len(result.failures),
              "failed_tests": [test.id() for test, _ in result.errors + result.failures],
              "dll_sha256": hashlib.sha256((BIN / "libsvn_fs_pg-1.dll").read_bytes()).hexdigest()}
    (ROOT / "artifacts/tests/synthetic-corpus-results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    raise SystemExit(not result.wasSuccessful())
