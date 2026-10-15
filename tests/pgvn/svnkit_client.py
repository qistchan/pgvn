"""Compile/run the installed, pinned SVNKit client against isolated test nodes."""
from pathlib import Path
import concurrent.futures
import hashlib
import json
import os
import subprocess


class SvnKit:
    def __init__(self, work, urls, user, password):
        self.work, self.urls, self.user, self.password = work, urls, user, password
        self.java = Path(os.environ["PGVN_TEST_JAVA_HOME"]) / "bin"
        plugins = Path(os.environ["PGVN_TEST_SVNKIT_PLUGINS"])
        patterns = ("org.tmatesoft.svnkit_*.jar", "com.trilead.ssh2_*.jar", "org.tmatesoft.sqljet_*.jar",
                    "antlr-runtime_*.jar", "com.jcraft.jsch_*.jar", "com.sun.jna_*")
        dependencies = [path for pattern in patterns for path in sorted(plugins.glob(pattern))]
        assert len(list(plugins.glob(patterns[0]))) == 1
        self.classes = work / "java-classes"
        self.classes.mkdir()
        source = Path(__file__).with_name("SvnKitProbe.java")
        self.classpath = os.pathsep.join(map(str, [self.classes, *dependencies]))
        result = subprocess.run([str(self.java / "javac.exe"), "-encoding", "UTF-8", "-cp", self.classpath,
            "-d", str(self.classes), str(source)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        assert result.returncode == 0, result.stderr
        self.evidence = {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "dependencies": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in dependencies if path.is_file()}}

    def arguments(self, mode, content=None):
        return [str(self.java / "java.exe"), "-cp", self.classpath, "SvnKitProbe", mode,
                *self.urls, self.user, str(self.work / "svnkit-config"), str(content or "unused")]

    def roundtrip(self, content):
        result = subprocess.run(self.arguments("roundtrip", content), input=self.password + b"\n",
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=45)
        assert result.returncode == 0, result.stderr[:4000]
        return json.loads(result.stdout)

    def expect_database_failure(self):
        result=subprocess.run(self.arguments("expect-database-failure"),input=self.password+b"\n",
            capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
        assert result.returncode==0,result.stderr[:4000]
        assert json.loads(result.stdout)["code"]==160000

    def revoke(self, mode, action):
        process = subprocess.Popen(self.arguments(mode), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            process.stdin.write(self.password + b"\n")
            process.stdin.flush()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(process.stdout.readline)
                try:
                    line = future.result(timeout=15)
                    assert line.strip() == b"READY", (line, process.poll())
                except Exception:
                    process.kill()
                    raise
            action()
            output, error = process.communicate(b"resume\n", timeout=30)
            assert process.returncode == 0, error[:4000]
            assert json.loads(output)["denied"] is True
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
