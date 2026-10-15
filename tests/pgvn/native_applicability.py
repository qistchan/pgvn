"""Case-level architectural boundaries, reviewed against pinned 1.14.5 sources.

Default raw runs include these cases and retain their failures. --applicable
selects only relevant cases and writes this full classification into results.
"""
CASE_COUNTS={'fs':70,'repos':32}
CASE_NAMES={'fs':{38:'delete_fs',40:'test_fs_info_format',65:'commit_with_locked_rep_cache',67:'test_rep_sharing_strict_content_check',70:'test_unrecognized_ioctl'},
    'repos':{8:'in_repo_authz',9:'in_repo_groups_authz',19:'test_delete_repos',21:'test_repos_info',22:'test_config_pool',24:'deprecated_access_context_api'}}
EXCLUSIONS={
    'fs':{
        38:'Asserts a physical fs path exists before svn_fs_delete_fs. PgVN IDs have no local directory; native purge uses pgvnadmin and is separately tested.',
        40:'For every non-FSFS/FSX backend asserts legacy BDB format 3 and SVN 1.5. PgVN storage format 1 and modern repository format are separately checked.',
        65:'Opens and locks the FSFS/FSX SQLite rep-cache.db, which does not exist in PostgreSQL storage.',
        67:'Overwrites FSFS/FSX representation files and SQLite rep-cache to inject a SHA1 collision; these are not PostgreSQL storage structures.',
        70:'The final subcase calls svn_fs_ioctl with NULL FS and type pg, requiring a globally registered legacy loader module. PgVN binds its FS directly by ID.'},
    'repos':{
        8:'Constructs file:// URLs from svn_repos_path and reads in-repository authz through local repository discovery. PgVN uses ID objects and network protocols.',
        9:'Constructs file:// URLs for in-repository authz/groups. Local file-RA discovery is outside the directory-free provider.',
        19:'Asserts a physical repository directory exists before svn_repos_delete; PgVN has no such directory and uses its purge API.',
        21:'Treats every non-FSX backend as pre-SVN-1.4 capable and requests legacy physical format 3. PgVN modern format/capabilities are separately checked.',
        22:'Loads repository config through a file:// URL and filesystem repository pool; PgVN configuration is versioned in PostgreSQL.',
        24:'Creates an on-disk pre-commit hook using a legacy path getter. PgVN path getters return NULL; versioned hooks and deprecated lock-token access commit are separately tested.'}}
