#!/usr/bin/env python3
"""Lock the Linux-visible storage changes without requiring the SVN SDK."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
streams = (ROOT / "src/libsvn_fs_pg/pgfs_streams.inc").read_text()
tree = (ROOT / "src/libsvn_fs_pg/pgfs_tree.inc").read_text()
assert "SELECT data FROM pgfs.content_chunks" in streams
assert "pgvn_chunk_length_ok" in streams
assert "encode(data,'hex') FROM pgfs.content_chunks" not in streams
assert "n.node_id,n.copy_id,n.owner_txn" in tree
directory = tree.split("static svn_error_t *pgfs_dir_entries", 1)[1].split("static svn_error_t *pgfs_dir_order", 1)[0]
assert "pgfs_load_node" not in directory
pins = (ROOT / "deploy/linux/pinned-dependencies.json").read_text()
assert '"svn": "1.14.5"' in pins and '"apache": "2.4.68"' in pins
print("source checks passed")
