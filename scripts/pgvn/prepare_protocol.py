"""Adapt the pinned SVN protocol engine in build copies, leaving vendor untouched."""
import difflib
import hashlib
import json
import re
import shutil


def prepare_protocol(source, destination, specification):
    spec = json.loads(specification.read_text(encoding="utf-8"))
    for name, expected in spec["base_sha256"].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Pinned SVN protocol input changed: " + name)
    for directory in ("svnserve", "libsvn_ra_svn"):
        target = destination / directory
        target.mkdir(parents=True, exist_ok=True)
        for item in (source / directory).iterdir():
            if item.is_file():
                shutil.copy2(item, target / item.name)
    original = {name: (source / name).read_text(encoding="utf-8") for name in spec["base_sha256"]}
    changed = dict(original)

    def replace(name, old, new):
        if changed[name].count(old) != 1:
            raise RuntimeError(f"Protocol patch anchor is not unique: {name}: {old[:70]}")
        changed[name] = changed[name].replace(old, new)

    fields = """  struct pgvn_repository *pgvn;
  struct pgvn_context *pgvn_context;
  struct pgvn_credential_key *pgvn_key;
  const char *pgvn_domain;
  apr_int64_t pgvn_auth_generation;
"""
    replace("svnserve/server.h", "typedef struct repository_t {\n", "typedef struct repository_t {\n" + fields)
    replace("svnserve/server.h", "typedef struct serve_params_t {\n", "typedef struct serve_params_t {\n" + fields)
    replace("svnserve/serve.c", '#include "logger.h"', '#include "logger.h"\n#include "pgvn_protocol.h"')
    replace("svnserve/serve.c", "  const char *client_url, *ra_client_string, *client_string, *canonical_url;",
            "  const char *client_url, *ra_client_string, *client_string, *canonical_url;\n  svn_string_t *client_url_wire;")
    replace("svnserve/serve.c", '"nlc?c(?c)",\n                                 &ver, &caplist, &client_url,',
            '"nls?c(?c)",\n                                 &ver, &caplist, &client_url_wire,')
    replace("svnserve/serve.c", "  if (ver != 2)\n", """  if (client_url_wire->len > 16384
      || memchr(client_url_wire->data, 0, client_url_wire->len)
      || strstr(client_url_wire->data, "%00"))
    return svn_error_create(SVN_ERR_BAD_URL, NULL, "PgVN URL contains a NUL or exceeds its limit");
  client_url = client_url_wire->data;
  if (ver != 2)
""")
    # Replace exactly the complete disk repository locator, not just open3().
    text = changed["svnserve/serve.c"]
    start = text.index("\nfind_repos(const char *url,")
    body = text.index("\n{", start) + 1
    end = text.index("\n/* Compute the authentication name EXTERNAL", body)
    assert text[body:end].rstrip().endswith("}")
    changed["svnserve/serve.c"] = text[:body] + """{
  return pgvn_svn_find(repository, url, read_only, result_pool, scratch_pool);
}
""" + text[end:]
    replace("svnserve/serve.c", "  b->repository->username_case = params->username_case;", """  b->repository->pgvn_context = params->pgvn_context;
  b->repository->pgvn_key = params->pgvn_key;
  b->repository->pgvn_domain = params->pgvn_domain;
  b->repository->username_case = params->username_case;""")
    replace("svnserve/serve.c", """      SVN_ERR(svn_ra_svn_cram_server(conn, scratch_pool, b->repository->pwdb,
                                     &user, success));
      b->client_info->user = apr_pstrdup(b->pool, user);""", """      SVN_ERR(pgvn_cram_server(conn, scratch_pool, b->repository, &user, success));
      b->client_info->user = *success ? apr_pstrdup(b->pool, user) : NULL;
      if (*success)
        SVN_ERR(pgvn_svn_authenticated(b->repository, user, b->pool));""")
    # Only top-level repository commands use server_baton_t. Editor commands
    # have other batons; do not attach a global marshaller callback to those.
    text = changed["svnserve/serve.c"]
    start = text.index("static const svn_ra_svn__cmd_entry_t main_commands[] = {")
    end = text.index("\n};", start) + 3
    table = text[start:end]
    commands = re.findall(r'\{\s*"([^"]+)",\s*(\w+)', table)
    handlers = [handler for name, handler in commands]
    assert len(handlers) == 32 and len(set(handlers)) == len(handlers)
    wrappers = []
    for name, handler in commands:
        wrappers.append(f"""static svn_error_t *pgvn_cmd_{handler}(svn_ra_svn_conn_t *conn,
    apr_pool_t *pool, svn_ra_svn__list_t *params, void *baton)
{{
  server_baton_t *b = baton;
  apr_time_t started = pgvn_command_start();
  svn_error_t *err = pgvn_svn_command(b->repository, b->client_info->user, pool);
  if (err) err = svn_error_create(SVN_ERR_RA_SVN_CMD_ERR, err, NULL);
  else err = {handler}(conn, pool, params, baton);
  pgvn_command_finish("{name}", started, err);
  return err;
}}
""")
        table = re.sub(r'(\{\s*"[^"]+",\s*)' + handler + r'\b', r'\1pgvn_cmd_' + handler, table)
    changed["svnserve/serve.c"] = text[:start] + "\n".join(wrappers) + table + text[end:]

    replace("libsvn_ra_svn/cram.c", '#include "ra_svn.h"', '#include "ra_svn.h"\n#include "pgvn_protocol.h"')
    replace("libsvn_ra_svn/cram.c", """svn_error_t *svn_ra_svn_cram_server(svn_ra_svn_conn_t *conn, apr_pool_t *pool,
                                    svn_config_t *pwdb, const char **user,
                                    svn_boolean_t *success)""", """svn_error_t *pgvn_cram_server(svn_ra_svn_conn_t *conn, apr_pool_t *pool,
                                    repository_t *repository, const char **user,
                                    svn_boolean_t *success)""")
    replace("libsvn_ra_svn/cram.c", "  svn_config_get(pwdb, &password, SVN_CONFIG_SECTION_USERS, *user, NULL);",
            "  SVN_ERR(pgvn_svn_password(&password, repository, *user, pool));")
    replace("libsvn_ra_svn/cram.c", """  compute_digest(sdigest, challenge, password);
  if (memcmp(cdigest, sdigest, sizeof(sdigest)) != 0)""", """  compute_digest(sdigest, challenge, password);
  SecureZeroMemory((void *)password, strlen(password));
  unsigned char difference = 0;
  for (size_t i = 0; i < sizeof(sdigest); ++i) difference |= cdigest[i] ^ sdigest[i];
  SecureZeroMemory(sdigest, sizeof(sdigest));
  if (difference)""")
    replace("libsvn_ra_svn/cram.c", "  apr_md5_final(digest, &ctx);\n}",
            "  apr_md5_final(digest, &ctx);\n  SecureZeroMemory(secret, sizeof(secret));\n  SecureZeroMemory(&ctx, sizeof(ctx));\n}")
    replace("libsvn_ra_svn/cram.c", '"Username not found"', '"Authentication failed"')
    replace("libsvn_ra_svn/cram.c", '"Password incorrect"', '"Authentication failed"')
    replace("libsvn_ra_svn/marshal.c", "return conn->block_handler ? 0 : -1;", "return conn->block_handler ? 0 : 30 * APR_USEC_PER_SEC;")
    replace("libsvn_ra_svn/marshal.c", '#include "ra_svn.h"', '#include "ra_svn.h"\n#include "pgvn_protocol.h"')
    replace("libsvn_ra_svn/marshal.c", "  char buffer[128];\n  SVN_ERR(writebuf_write_literal(conn, pool, \"( failure ( \"));",
        "  char buffer[128];\n  pgvn_command_failure(err);\n  SVN_ERR(writebuf_write_literal(conn, pool, \"( failure ( \"));")
    replace("libsvn_ra_svn/marshal.c", "(apr_uint64_t) err->apr_err,", "(apr_uint64_t) pgvn_svn_wire_error(err->apr_err),")
    replace("libsvn_ra_svn/streams.c", '#include "ra_svn.h"', '#include "ra_svn.h"\n#include "pgvn_protocol.h"')
    replace("libsvn_ra_svn/streams.c", "  apr_socket_t *sock;\n  apr_pool_t *pool;",
            "  apr_socket_t *sock;\n  apr_pool_t *pool;\n  apr_interval_time_t write_timeout;")
    for callback, expression in (
        ("sock_read_cb", "pgvn_svn_socket_read(b->sock, b->pool, buffer, len)"),
        ("sock_write_cb", "pgvn_svn_socket_write(b->sock, b->pool, buffer, len, b->write_timeout != 0)")):
        pattern = r"(" + callback + r"\([^;{}]*\)\s*\{\n).*?\n\}"
        changed["libsvn_ra_svn/streams.c"], count = re.subn(pattern,
            lambda match: match[1] + "  sock_baton_t *b = baton;\n  return " + expression + ";\n}",
            changed["libsvn_ra_svn/streams.c"], flags=re.S)
        if count != 1:
            raise RuntimeError("Pinned socket callback changed: " + callback)
    replace("libsvn_ra_svn/streams.c", "  apr_socket_timeout_set(b->sock, interval);", "  b->write_timeout = interval;")
    replace("libsvn_ra_svn/streams.c", "  b->pool = svn_pool_create(result_pool);",
            "  b->pool = svn_pool_create(result_pool);\n  b->write_timeout = 30 * APR_USEC_PER_SEC;")

    diff = []
    for name, contents in changed.items():
        (destination / name).write_text(contents, encoding="utf-8")
        diff.extend(difflib.unified_diff(original[name].splitlines(True), contents.splitlines(True),
            fromfile="a/" + name, tofile="b/" + name))
    (destination / "applied-protocol.patch").write_text("".join(diff), encoding="utf-8")
    return {name: hashlib.sha256((destination / name).read_bytes()).hexdigest() for name in changed}
