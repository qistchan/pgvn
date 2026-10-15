"""Source-grounded exclusions for tests that manipulate another FS's storage."""
UPSTREAM_EXCLUSIONS = {
    "svnadmin": {
        43: "Opens and corrupts db/revs/0/1 directly; this is an FSFS representation file, absent in PgFS",
    },
}

NATIVE_EXCLUSIONS = {
    "fs": {
        40: "Hardcodes format 3 and SVN 1.5 support for FSFS/BDB; PgFS format 1 is checked separately",
        65: "Requires the FSFS/FSX SQLite rep-cache file and its locking mechanism",
        67: "Overwrites the FSFS/FSX rep-cache and representation files to construct a SHA1 collision",
    },
    "repos": {
        21: "Assumes every non-FSX backend existed before SVN 1.4 and can create pre-1.4 repository formats",
    },
}
