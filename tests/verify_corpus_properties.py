"""Compare binary revision properties with the fixed, authorized SVN dump."""
from pathlib import Path
import base64
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "runtime/svn/bin"
ENV = dict(os.environ, PGFS_DSN="host=127.0.0.1 port=15432 dbname=pgfs_poc user=pgfs_test sslmode=disable")


def dump_revision_properties(archive):
    """Read record lengths, without interpreting binary node text as headers."""
    revisions = {}
    with Path(archive).open("rb") as source:
        while True:
            line = source.readline()
            while line == b"\n":
                line = source.readline()
            if not line:
                break
            headers = {}
            while line != b"\n":
                assert line.endswith(b"\n"), "Truncated dump header"
                name, value = line[:-1].split(b": ", 1)
                headers[name] = value
                line = source.readline()
                assert line, "Truncated dump header"
            length = int(headers.get(b"Content-length", b"0"))
            if b"Revision-number" not in headers:
                source.seek(length, 1)
                continue
            rev = int(headers[b"Revision-number"])
            assert rev not in revisions and b"Prop-delta" not in headers
            prop_length = int(headers.get(b"Prop-content-length", b"0"))
            assert prop_length == length
            data = source.read(length)
            assert len(data) == length, "Truncated dump revision properties"
            props, offset = {}, 0
            while data[offset:] != b"PROPS-END\n":
                key_end = data.index(b"\n", offset)
                kind, key_size = data[offset:key_end].split(b" ", 1)
                assert kind == b"K"
                key_start = key_end + 1
                key_end = key_start + int(key_size)
                key = data[key_start:key_end]
                assert data[key_end:key_end + 1] == b"\n"
                value_start = key_end + 1
                value_header_end = data.index(b"\n", value_start)
                kind, value_size = data[value_start:value_header_end].split(b" ", 1)
                assert kind == b"V" and key not in props
                value_start = value_header_end + 1
                value_end = value_start + int(value_size)
                props[key] = data[value_start:value_end]
                assert data[value_end:value_end + 1] == b"\n"
                offset = value_end + 1
            revisions[rev] = props
    return revisions


def xml_revision_properties(data):
    props = {}
    for item in ET.fromstring(data).findall(".//property"):
        name = item.attrib["name"].encode("utf8")
        assert name not in props
        text = item.text or ""
        encoding = item.attrib.get("encoding")
        assert encoding in (None, "base64"), "Unknown property encoding"
        props[name] = base64.b64decode(text) if encoding else text.encode("utf8")
    return props


def property_digest(props):
    digest = hashlib.sha256()
    for name, value in sorted(props.items()):
        for part in (name, value):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()


def verify_revision_properties(fixture, target, head):
    expected = dump_revision_properties(fixture / "source.dump")
    assert set(expected) == set(range(head + 1)), "Incomplete fixed dump revision range"
    records = []
    for rev, props in sorted(expected.items()):
        result = subprocess.run([str(BIN / "svnlook.exe"), "proplist", "--revprop", "--verbose", "--xml",
                                 "-r", str(rev), str(target)], env=ENV, capture_output=True, timeout=60)
        assert result.returncode == 0, "Cannot read imported revision properties at r" + str(rev)
        actual = xml_revision_properties(result.stdout)
        (fixture / ("pg-r" + str(rev) + ".revprops.xml")).write_bytes(result.stdout)
        assert actual == props, "Revision properties differ from fixed dump at r" + str(rev)
        records.append({"revision": rev, "sha256": property_digest(props), "properties": len(props)})
    return {"revisions_compared": head + 1, "source": "fixed_dump", "records": records}


if __name__ == "__main__":
    report_path = ROOT / "artifacts/tests/polarion-corpus-results.json"
    report = json.loads(report_path.read_text(encoding="utf8"))
    fixture, target = Path(report["fixture"]).resolve(), Path(report["target"]).resolve()
    assert report["passed"] and fixture.is_relative_to((ROOT / "runtime/corpus").resolve())
    assert target == fixture / "pg"
    report["revision_properties"] = verify_revision_properties(fixture, target, report["source_head_at_start"])
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print(json.dumps({"revision_properties_compared": report["revision_properties"]["revisions_compared"], "passed": True}))
