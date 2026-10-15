"""Apply checked, pinned transformations only to an isolated build copy."""
from pathlib import Path
import difflib
import hashlib
import json
import re
import shutil


def prepare(source, destination, specification):
    spec = json.loads(specification.read_text(encoding="utf-8"))
    for name, expected in spec["base_sha256"].items():
        actual = hashlib.sha256((source / name).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"Pinned SVN repository input changed: {name}")
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.is_file():
            shutil.copy2(item, destination / item.name)
    originals = {name: (destination / name).read_text(encoding="utf-8")
                 for name in spec["base_sha256"]}
    changed = dict(originals)
    for rule in spec["replacements"]:
        name = rule["file"]
        if changed[name].count(rule["old"]) != 1:
            raise RuntimeError(f"Patch anchor is not unique: {name}: {rule['old'][:60]}")
        changed[name] = changed[name].replace(rule["old"], rule["new"])
    for name, (event, user, txn, rev) in spec["hook_dispatch"].items():
        pattern = r"(svn_repos__hooks_" + name + r"\([^;{}]*\)\s*\{\n)"
        body = "  if (repos->pgvn_baton)\n    {\n"
        if name == "pre_lock":
            body += '      if (token) *token = "";\n'
        body += f'      return pgvn_repos_hook(repos->pgvn_baton, "{event}", {user}, {txn}, {rev}, pool);\n    }}\n'
        changed["hooks.c"], count = re.subn(pattern, lambda m: m[1] + body, changed["hooks.c"])
        if count != 1:
            raise RuntimeError(f"Hook patch anchor is not unique: {name}")
    diff = []
    for name, contents in changed.items():
        (destination / name).write_text(contents, encoding="utf-8")
        diff.extend(difflib.unified_diff(originals[name].splitlines(True), contents.splitlines(True),
                    fromfile="a/libsvn_repos/" + name, tofile="b/libsvn_repos/" + name))
    (destination.parent / "applied-repos.patch").write_text("".join(diff), encoding="utf-8")
    return {name: hashlib.sha256((destination / name).read_bytes()).hexdigest() for name in changed}
