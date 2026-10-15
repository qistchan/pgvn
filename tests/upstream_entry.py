"""Bind upstream test helpers without changing upstream test assertions."""
from pathlib import Path
import importlib
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "vendor/subversion-1.14.5/subversion/tests/cmdline"
suite = sys.argv.pop(1)
sys.path.insert(0, str(SOURCE))
import svntest
svntest.main.entriesdump_binary = str(ROOT / "runtime/svn/bin/entries-dump.exe")
svntest.main.lock_helper_binary = str(ROOT / "runtime/svn/bin/lock-helper.exe")
svntest.main.atomic_ra_revprop_change_binary = str(ROOT / "runtime/svn/bin/atomic-ra-revprop-change.exe")
svntest.main.wc_lock_tester_binary = str(ROOT / "runtime/svn/bin/wc-lock-tester.exe")
svntest.main.wc_incomplete_tester_binary = str(ROOT / "runtime/svn/bin/wc-incomplete-tester.exe")
module = importlib.import_module(suite + "_tests")
sys.argv[0] = str(SOURCE / (suite + "_tests.py"))
svntest.main.run_tests(module.test_list, serial_only=getattr(module, "serial_only", False))
