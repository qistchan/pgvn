"""Isolated Apache transport using the pinned upstream test location rules."""
from pathlib import Path
import ast
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
APACHE = Path(r"C:\Polarion\bundled\apache")
BIN = ROOT / "runtime/svn/bin"


def command(directory, port, env):
    server_root = directory / "httpd"
    (server_root / "bin").mkdir(parents=True)
    # Each run owns its executable directory, so parallel HTTP suites do not
    # replace DLLs loaded by another server. Original Polarion files are read.
    for source in (APACHE / "bin").iterdir():
        if source.suffix.lower() in (".exe", ".dll"):
            shutil.copy2(source, server_root / "bin" / source.name)
    for source in BIN.glob("*.dll"):
        shutil.copy2(source, server_root / "bin" / source.name)
    password = server_root / "users"
    for index, (user, value) in enumerate((("jrandom", "rayjandom"),
                                         ("jconstant", "rayjandom"),
                                         ("JRANDOM", "rayjandom"),
                                         ("JCONSTANT", "rayjandom"),
                                         ("__dumpster__", "__loadster__"))):
        subprocess.run([str(server_root / "bin/htpasswd.exe"), "-bc" if index == 0 else "-b",
                        str(password), user, value], capture_output=True, check=True, timeout=10)
    groups = server_root / "groups"
    groups.write_text("random: jrandom\nconstant: jconstant\n", encoding="utf8")
    work = directory / "svn-test-work"
    work.mkdir()
    (work / "authz").write_text("[/]\n* = rw\n", encoding="utf8")
    (work / "dontdothat").write_text("[recursive-actions]\n/ = deny\n", encoding="utf8")

    # Compile only the pure location-generator methods from unchanged upstream
    # win-tests.py. Importing that script would execute its full build driver.
    source = ROOT / "vendor/subversion-1.14.5/win-tests.py"
    parsed = ast.parse(source.read_text(encoding="utf8"), filename=str(source))
    original = next(node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name == "Httpd")
    methods = [node for node in original.body if isinstance(node, ast.FunctionDef)
               and node.name in ("_quote", "_svn_repo", "_svn_authz_repo")]
    assert len(methods) == 3
    minimal = ast.Module(body=[ast.ClassDef(name="Httpd", bases=[], keywords=[],
                                         body=methods, decorator_list=[])], type_ignores=[])
    ast.fix_missing_locations(minimal)
    namespace = {"os": os, "CMDLINE_TEST_SCRIPT_NATIVE_PATH": ""}
    exec(compile(minimal, str(source), "exec"), namespace)
    locations = namespace["Httpd"]()
    locations.abs_builddir = directory.as_posix()
    locations.authz_file = (work / "authz").as_posix()
    locations.dontdothat_file = (work / "dontdothat").as_posix()
    locations.httpd_users = password.as_posix()
    locations.httpd_groups = groups.as_posix()
    locations.httpv2_option = "on"
    locations.path_authz_option = "on"
    locations.bulkupdates_option = "on"
    modules = ("authn_core", "authn_file", "authz_core", "authz_user", "auth_basic",
               "authz_groupfile", "authz_host", "access_compat", "alias", "dav",
               "dav_svn", "authz_svn", "dontdothat")
    config = server_root / "httpd.conf"
    text = (f'ServerRoot "{server_root.as_posix()}"\n'
            f'Listen 127.0.0.1:{port}\nServerName 127.0.0.1:{port}\n'
            'PidFile "httpd.pid"\nErrorLog "error.log"\nLogLevel warn\n'
            '<IfModule mpm_winnt.c>\nThreadsPerChild 16\n</IfModule>\n')
    for module in modules:
        path = APACHE / "modules" / ("mod_" + module + ".so")
        if not path.is_file():
            raise RuntimeError("Missing pinned Apache module: " + str(path))
        text += f'LoadModule {module}_module "{path.as_posix()}"\n'
    text += locations._svn_repo("repositories")
    text += locations._svn_repo("local_tmp")
    text += locations._svn_authz_repo()
    text += ('RedirectMatch permanent ^/svn-test-work/repositories/REDIRECT-PERM-(.*)$ /svn-test-work/repositories/$1\n'
             'RedirectMatch ^/svn-test-work/repositories/REDIRECT-TEMP-(.*)$ /svn-test-work/repositories/$1\n')
    config.write_text(text, encoding="utf8")
    program = server_root / "bin/httpd.exe"
    syntax = subprocess.run([str(program), "-t", "-f", str(config)], env=env,
                            capture_output=True, timeout=15)
    if syntax.returncode:
        raise RuntimeError(syntax.stderr.decode("utf8", "replace"))
    return [str(program), "-X", "-f", str(config)]
