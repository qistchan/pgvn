"""Local offline-package acceptance using owned SCM services and an isolated DB.

The input archive is immutable. No installed PostgreSQL/Polarion service is
modified. A pass proves local packaging mechanics, not a clean-machine/reboot
or Polarion application deployment.
"""
from pathlib import Path
import argparse
import ctypes
import datetime as dt
import hashlib
import http.client
import json
import os
import secrets
import socket
import subprocess
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
import zipfile
import winreg
from urllib.parse import urlsplit
from fixture import Fixture, ROOT, RUNTIME

TEST_ID = 'm7-package-' + uuid.uuid4().hex[:12]
WORK = RUNTIME / 'tests' / TEST_ID
RESULTS = ROOT / 'artifacts/pgvn/tests'
fixture = Fixture()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Acceptance:
    def __init__(self, archive, ascii_paths=False, ascii_user=False):
        self.archive = archive.resolve()
        self.ascii_paths = ascii_paths
        self.suffix = TEST_ID.rsplit('-', 1)[1]
        self.source_db = 'pgvn_pkg_' + self.suffix
        self.target_db = 'pgvn_pkg_restore_' + self.suffix
        self.owner = 'pgvn_pkg_owner_' + self.suffix
        self.restore_owner = 'pgvn_pkg_restore_owner_' + self.suffix
        self.private = WORK / 'private'
        self.ps = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        self.environment = dict(os.environ, PATH=str(Path(os.environ['SystemRoot']) / 'System32'))
        for name in ('PGVN_DSN', 'PGPASSWORD', 'PGSERVICEFILE', 'PGPASSFILE', 'PGSYSCONFDIR'):
            self.environment.pop(name, None)
        self.roots = []
        self.installed = []
        self.details = {}
        self.counter = 0
        self.password = secrets.token_urlsafe(36)
        self.username = 'package-writer' if ascii_user else '包验证用户'
        self.route = '/svn/package-demo'
        self.monitor_token = None

    def run(self, arguments, expected=0, input=None, timeout=120, sensitive=False):
        result = subprocess.run(list(map(str, arguments)), cwd=WORK, env=self.environment,
            input=input, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout)
        allowed = expected if isinstance(expected, tuple) else (expected,)
        if result.returncode not in allowed:
            # The command has paths/identifiers only; passwords are supplied on stdin.
            raise AssertionError((arguments[0], result.returncode,
                '[sensitive command output omitted]' if sensitive else result.stdout[-3000:].decode('utf-8', 'replace'),
                '[sensitive command error omitted]' if sensitive else result.stderr[-5000:].decode('utf-8', 'replace')))
        return result

    def powershell(self, script, parameters=None, expected=0, timeout=120, sensitive=False):
        self.counter += 1
        request = self.private / ('request-%03d.json' % self.counter)
        request.write_text(json.dumps({'Script': str(script), 'Parameters': parameters or {}}, ensure_ascii=False), encoding='utf-8')
        result = self.run([self.ps, '-NoProfile', '-File', self.private / 'launch.ps1', str(request)],
            expected=expected, timeout=timeout, sensitive=sensitive)
        (WORK / ('powershell-%03d.log' % self.counter)).write_bytes(b'[sensitive command output omitted]\n' if sensitive else result.stdout + result.stderr)
        return result

    def script(self, root, name, parameters=None, **kwargs):
        return self.powershell(root / 'deploy/windows' / name, parameters, **kwargs)

    def admin(self, root, *arguments, profile='Admin', expected=0):
        result = self.script(root, 'Invoke-PgVNAdmin.ps1', {'Profile': profile, 'Arguments': list(map(str, arguments))},
            expected=expected, sensitive=arguments[:2] == ('monitor-token', 'create'))
        return json.loads(result.stdout) if result.returncode == 0 else result

    def bridge(self, root, mode, **kwargs):
        return self.powershell(self.private / 'credential-bridge.ps1',
            {'Root': str(root), 'Mode': mode, 'SecretFile': str(self.private / 'credential.json'), 'RepositoryRoute': self.route}, **kwargs)

    def prepare(self):
        WORK.mkdir(parents=True)
        self.private.mkdir()
        self.run([Path(os.environ['SystemRoot']) / 'System32/icacls.exe', self.private, '/inheritance:r',
            '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'])
        (self.private / 'launch.ps1').write_text('''param([string]$RequestFile)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
try {
    $request = Get-Content -LiteralPath $RequestFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $parameters = @{}
    foreach ($entry in $request.Parameters.PSObject.Properties) { $parameters[$entry.Name] = $entry.Value }
    $global:LASTEXITCODE = 0
    & $request.Script @parameters
    if (-not $?) { exit 1 }
    exit $LASTEXITCODE
} catch { [Console]::Error.WriteLine(($_ | Out-String)); exit 1 }
''', encoding='utf-8-sig')
        (self.private / 'credential-bridge.ps1').write_text('''param([string]$Root,[string]$Mode,[string]$SecretFile,[string]$RepositoryRoute)
. (Join-Path $Root 'deploy/windows/PgVN.Common.ps1')
$secret = Get-Content -LiteralPath $SecretFile -Raw -Encoding UTF8 | ConvertFrom-Json
$password = ConvertTo-SecureString $secret.Password -AsPlainText -Force
if ($Mode -eq 'SetUser') {
    & (Join-Path $Root 'deploy/windows/Set-PgVNUser.ps1') -UserName $secret.UserName -Password $password -AlsoDav
} elseif ($Mode -eq 'Smoke') {
    & (Join-Path $Root 'deploy/windows/Test-PgVNDeployment.ps1') -RepositoryRoute $RepositoryRoute -UserName $secret.UserName -Password $password
} else { throw 'Unknown credential bridge operation' }
''', encoding='utf-8-sig')
        (self.private / 'credential.json').write_text(json.dumps({'UserName': self.username, 'Password': self.password}, ensure_ascii=False), encoding='utf-8')
        fixture.assert_ours()
        for role in (self.owner, self.restore_owner):
            fixture.sql(f'CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT', database='postgres')
        for database, owner in ((self.source_db, self.owner), (self.target_db, self.restore_owner)):
            fixture.sql(f"CREATE DATABASE {database} OWNER {owner} ENCODING 'UTF8' TEMPLATE template0", database='postgres')
        expected = self.archive.with_suffix('.zip.sha256').read_text().split()[0]
        assert sha(self.archive) == expected
        self.details.update(archive=str(self.archive), archive_sha256=expected,
            source_database=self.source_db, target_database=self.target_db, db_data_directory=str(RUNTIME / 'pg-primary'),
            database_authentication_scope='Existing isolated loopback trust fixture; production SCRAM/TLS configuration is a separate deployment check')

    def extract(self, label, database, owner):
        destination = WORK / (('deployment ' + label + ' space') if self.ascii_paths else ('部署 ' + label + ' space 中文'))
        destination.mkdir()
        with zipfile.ZipFile(self.archive) as archive:
            for name in archive.namelist():
                target = (destination / name).resolve()
                assert target.is_relative_to(destination.resolve()), name
            archive.extractall(destination)
        roots = [p for p in destination.iterdir() if p.is_dir()]
        assert len(roots) == 1
        root = roots[0]
        self.roots.append(root)
        manifest = json.loads((root / 'package-manifest.json').read_text(encoding='utf-8'))
        self.details['package_id'] = manifest['package_id']
        self.details['package_status'] = manifest['validation_status']
        assert manifest['clean_machine_acceptance'] is False and manifest['polarion_application_acceptance'] is False
        self.script(root, 'Test-PgVNPackage.ps1')
        ports = set()
        while len(ports) != 3:
            ports.add(free_port())
        svn_port, monitor_port, http_port = sorted(ports)
        self.script(root, 'Initialize-PgVNConfiguration.ps1', {'DatabaseHost': '127.0.0.1', 'DatabasePort': 16432,
            'DatabaseName': database, 'NodeId': 'package-' + label, 'SvnPort': svn_port,
            'MonitorPort': monitor_port, 'HttpPort': http_port})
        services = root / 'admin/pg_service.conf'
        text = services.read_text(encoding='utf-8').replace('user=pgvn_migrate', 'user=' + owner)
        text = text.replace('user=pgvn_restore', 'user=' + self.restore_owner).replace('dbname=' + database + '_restore', 'dbname=' + self.target_db)
        services.write_text(text, encoding='utf-8')
        for relative in ('admin/secrets/pgpass.conf', 'secrets/runtime.pgpass.conf'):
            path = root / relative
            text = path.read_text(encoding='utf-8')
            for placeholder in ('ADMIN', 'MIGRATION', 'RESTORE', 'SERVICE', 'MONITOR'):
                text = text.replace('CHANGE_ME_' + placeholder + '_PASSWORD', secrets.token_hex(24))
            text = text.replace(':pgvn_migrate:', ':' + owner + ':').replace(':pgvn_restore:', ':' + self.restore_owner + ':')
            text = text.replace(':' + database + '_restore:', ':' + self.target_db + ':')
            path.write_text(text, encoding='utf-8')
        return root

    def install(self, root, label):
        service = 'PgVNPkg-' + self.suffix + '-' + label
        dav = 'PgVNDAVPkg-' + self.suffix + '-' + label
        self.script(root, 'Install-PgVNServices.ps1', {'ServiceName': service, 'DavServiceName': dav, 'StartupType': 'Manual'})
        self.installed.append(root)
        return json.loads(self.bridge(root, 'Smoke').stdout)

    def svn(self, root, tool, *arguments, expected=0):
        try:
            return self.run([root / 'tools/svn' / (tool + '.exe'), *map(str, arguments), '--non-interactive',
                '--no-auth-cache', '--config-dir', WORK / 'svn-client-config', '--username', self.username,
                '--password-from-stdin'], expected=expected, input=(self.password + '\r\n').encode('utf-8'), timeout=90).stdout
        except Exception:
            diagnostic = {}
            state = json.loads((root / 'admin/services.json').read_text(encoding='utf-8'))
            for name in (state['ServiceName'], state['DavServiceName']):
                value = self.run([Path(os.environ['SystemRoot']) / 'System32/sc.exe', 'queryex', name])
                diagnostic[name] = value.stdout.decode('utf-8', 'replace')
            settings = json.loads((root / 'admin/deployment.json').read_text(encoding='utf-8'))
            for path in ('/health/live', '/health/ready', '/api/monitor/v1/events', '/api/monitor/v1/overview'):
                conn = http.client.HTTPConnection('127.0.0.1', urlsplit(settings['MonitorUrl']).port, timeout=5)
                try:
                    headers = {'Authorization': 'Bearer ' + self.monitor_token} if self.monitor_token else {}
                    conn.request('GET', path, headers=headers)
                    response = conn.getresponse()
                    diagnostic[path] = {'status': response.status, 'body': json.loads(response.read())}
                except Exception as error:
                    diagnostic[path] = {'error': type(error).__name__}
                finally:
                    conn.close()
            self.details['protocol_failure_diagnostics'] = diagnostic
            raise

    def urls(self, root):
        settings = json.loads((root / 'admin/deployment.json').read_text(encoding='utf-8'))
        return ('svn://127.0.0.1:' + str(settings['SvnPort']) + self.route,
            'http://127.0.0.1:' + str(settings['HttpPort']) + self.route)

    def checks(self):
        self.prepare()
        source = self.extract('source', self.source_db, self.owner)
        assert self.admin(source, 'db', 'init', '--json', profile='Migrate')['ok']
        self.admin(source, 'db', 'grant', '--admin-role', 'pgvn_admin', '--service-role', 'pgvn_service', '--monitor-role', 'pgvn_monitor', '--json', profile='Migrate')
        self.admin(source, 'key', 'create', source / 'secrets/credentials-v1.key', 'package-v1')
        self.bridge(source, 'SetUser')
        self.admin(source, 'repo', 'create', 'package-demo', '--route', self.route, '--json')
        authz = source / 'admin/demo.authz'
        authz.write_text('[package-demo:/]\n' + self.username + ' = rw\n* =\n', encoding='utf-8')
        hooks = source / 'admin/hooks.json'
        hooks.write_text('{"pre-revprop-change":"allow"}', encoding='utf-8')
        self.admin(source, 'config', 'publish', 'name:package-demo', '--expected-generation', '1', '--authz', authz, '--hooks', hooks, '--json')
        issued = self.admin(source, 'monitor-token', 'create', '--label', TEST_ID, '--expires-in', '600')
        self.monitor_token = issued['token']
        (source / 'hooks/deployment-inventory.txt').write_bytes(b'global hook dependencies are operator-managed\r\n')
        initial = self.install(source, 'source')
        assert initial['Ready'] and len(initial['Protocols']) == 2
        print('Package LocalService SVN/DAV and embedded monitor started', flush=True)
        # A second install must recognize only the services owned by this root.
        self.script(source, 'Install-PgVNServices.ps1', {'ServiceName': 'PgVNPkg-' + self.suffix + '-source',
            'DavServiceName': 'PgVNDAVPkg-' + self.suffix + '-source', 'StartupType': 'Manual'})
        svn_url, dav_url = self.urls(source)
        payload = bytes(range(256)) * 4097 + b'\x00\xff\r\npackage'
        data = self.private / 'payload.bin'
        data.write_bytes(payload)
        self.svn(source, 'svnmucc', '-m', 'package r1', 'put', data, svn_url + '/payload.bin')
        assert self.svn(source, 'svn', 'cat', dav_url + '/payload.bin') == payload
        property_file = self.private / 'revision-property.bin'
        property_file.write_bytes(b'\x00\xffbinary\r\nrevision property')
        self.svn(source, 'svn', 'propset', '--revprop', '-r', '1', 'package:binary', '-F', property_file, svn_url)
        self.svn(source, 'svn', 'lock', svn_url + '/payload.bin', '-m', 'package lock')
        lock = ET.fromstring(self.svn(source, 'svn', 'info', '--xml', svn_url + '/payload.bin')).findtext('.//lock/token')
        assert lock
        self.details.update(localservice_svn_dav=True, unicode_install_path=not self.ascii_paths, unicode_username=not self.username.isascii(),
            installer_idempotent=True, source_head=1, payload_sha256=sha(data), public_uuid=initial['Protocols'][0]['Uuid'])
        backup = WORK / 'backup-protected'
        self.script(source, 'Backup-PgVN.ps1', {'Destination': str(backup)}, timeout=180)
        assert json.loads(self.bridge(source, 'Smoke').stdout)['Ready']
        backup_record = json.loads((backup / 'backup-manifest.json').read_text(encoding='utf-8'))
        assert backup_record['LocalServicesQuiesced'] is True and backup_record['RemoteNodesCoordinated'] is False
        self.details['backup_manifest_sha256'] = sha(backup / 'backup-manifest.json')
        print('Packaged backup completed; original services recovered', flush=True)
        target = self.extract('restore', self.target_db, self.restore_owner)
        self.script(target, 'Restore-PgVN.ps1', {'BackupDirectory': str(backup)}, timeout=180)
        assert fixture.sql('SELECT rolsuper FROM pg_roles WHERE rolname=\'' + self.restore_owner + '\'', database=self.target_db) == 'f'
        assert fixture.sql("SELECT has_table_privilege('pgvn_monitor','pgvn.credentials','SELECT')", database=self.target_db) == 'f'
        assert fixture.sql("SELECT has_function_privilege('public','pgvn_monitor.publish(uuid,text,text,text,integer,timestamptz,jsonb)','EXECUTE')", database=self.target_db) == 'f'
        # The guide explicitly requires deployment of global hook files before activation.
        inventory = target / 'hooks/deployment-inventory.txt'
        inventory.write_bytes((backup / 'configuration/hooks/deployment-inventory.txt').read_bytes())
        restored = self.install(target, 'restore')
        assert restored['Protocols'][0]['Uuid'] == initial['Protocols'][0]['Uuid']
        target_svn, target_dav = self.urls(target)
        assert self.svn(target, 'svn', 'cat', target_svn + '/payload.bin') == payload
        assert self.svn(target, 'svn', 'cat', target_dav + '/payload.bin') == payload
        assert self.svn(target, 'svn', 'propget', '--strict', '--revprop', '-r', '1', 'package:binary', target_svn) == property_file.read_bytes()
        assert ET.fromstring(self.svn(target, 'svn', 'info', '--xml', target_svn + '/payload.bin')).findtext('.//lock/token') == lock
        settings = json.loads((target / 'admin/deployment.json').read_text(encoding='utf-8'))
        conn = http.client.HTTPConnection('127.0.0.1', urlsplit(settings['MonitorUrl']).port, timeout=5)
        try:
            conn.request('GET', '/api/monitor/v1/overview', headers={'Authorization': 'Bearer ' + issued['token']})
            response = conn.getresponse()
            assert response.status == 200
            assert json.loads(response.read())
        finally:
            conn.close()
        self.svn(target, 'svnmucc', '-m', 'package restored r2', 'put', data, target_dav + '/after-restore.bin')
        assert self.admin(target, 'look', 'youngest', 'name:package-demo', '--json')['youngest'] == 2
        self.admin(target, 'verify', 'name:package-demo')
        assert self.admin(source, 'look', 'youngest', 'name:package-demo', '--json')['youngest'] == 1
        self.details.update(restored_non_superuser=True, archive_acl_preserved=True, monitor_credentials_denied=True,
            public_telemetry_execute_denied=True, restored_binary_content_and_revprop=True, lock_preserved=True,
            monitor_token_preserved=True, restored_protocol_commit=True, restored_head=2, source_unchanged=True,
            protected_key_reconfigured=True, global_hook_inventory_copied=True)
        print('Restored package authenticated, preserved data/ACLs and committed over DAV', flush=True)
        original = (source / 'README.zh-CN.md').read_bytes()
        try:
            (source / 'README.zh-CN.md').write_bytes(original + b'\ntamper\n')
            result = self.script(source, 'Test-PgVNPackage.ps1', expected=1)
            assert b'README.zh-CN.md' in result.stderr
        finally:
            (source / 'README.zh-CN.md').write_bytes(original)
        self.details['tamper_rejected'] = True
        for root in self.roots:
            self.script(root, 'Test-PgVNPackage.ps1')
        assert sha(self.archive) == self.details['archive_sha256']
        self.details['package_files_unchanged'] = True

    @staticmethod
    def assert_removed(state):
        for name in (state['ServiceName'], state['DavServiceName']):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, 'SYSTEM\\CurrentControlSet\\Services\\' + name):
                    raise AssertionError('Owned test service still registered: ' + name)
            except FileNotFoundError:
                pass
        class ApiVersion(ctypes.Structure):
            _fields_ = [('major', ctypes.c_ushort), ('minor', ctypes.c_ushort)]
        class UrlKey(ctypes.Structure):
            _fields_ = [('prefix', ctypes.c_wchar_p)]
        class UrlQuery(ctypes.Structure):
            _fields_ = [('type', ctypes.c_int), ('key', UrlKey), ('token', ctypes.c_ulong)]
        api = ctypes.WinDLL('httpapi.dll')
        assert api.HttpInitialize(ApiVersion(2, 0), 2, None) == 0
        try:
            query = UrlQuery(0, UrlKey(state['MonitorUrl']), 0)
            required = ctypes.c_ulong()
            result = api.HttpQueryServiceConfiguration(None, 2, ctypes.byref(query), ctypes.sizeof(query),
                None, 0, ctypes.byref(required), None)
            assert result == 2, ('Owned URL reservation still present or query failed', result)
        finally:
            assert api.HttpTerminate(2, None) == 0

    def cleanup(self):
        errors = []
        for root in reversed(self.roots):
            record = root / 'admin/services.json'
            if record.exists():
                try:
                    self.script(root, 'Uninstall-PgVNServices.ps1')
                    self.assert_removed(json.loads(record.read_text(encoding='utf-8')))
                except Exception:
                    errors.append(traceback.format_exc())
        self.details['services_uninstalled'] = not errors
        self.details['url_reservations_removed'] = not errors
        if errors:
            raise RuntimeError('Owned service cleanup failed: ' + '\n'.join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--ascii-paths', action='store_true', help='Run an explicit ASCII-installation control; retain separate Unicode-path failures')
    parser.add_argument('--ascii-user', action='store_true', help='Run an explicit ASCII-username control; retain separate Unicode-username failures')
    args = parser.parse_args()
    test = Acceptance(args.archive, ascii_paths=args.ascii_paths, ascii_user=args.ascii_user)
    start = time.monotonic()
    error = None
    try:
        test.checks()
    except Exception:
        error = traceback.format_exc()
    finally:
        try:
            test.cleanup()
        except Exception:
            error = (error or '') + '\n' + traceback.format_exc()
    status = 'PASS' if error is None else 'FAIL'
    report = {'test_id': TEST_ID, 'component': 'M7 offline Windows package local acceptance', 'status': status,
        'milestone_complete': False, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'elapsed_seconds': time.monotonic() - start, 'test_source_sha256': sha(Path(__file__)),
        'details': test.details, 'error': error, 'work_directory': str(WORK),
        'scope': 'Current host, Windows PowerShell 5.1 and isolated PostgreSQL 16432 fixture only. No OS reboot, clean machine, cross-host HA or Polarion application acceptance.'}
    RESULTS.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, indent=2) + '\n'
    (RESULTS / (TEST_ID + '.json')).write_text(encoded, encoding='utf-8')
    (RESULTS / 'm7-package-latest.json').write_text(encoded, encoding='utf-8')
    if error:
        print(error, flush=True)
    print(json.dumps({'test_id': TEST_ID, 'status': status, 'elapsed_seconds': report['elapsed_seconds']}), flush=True)
    return 0 if error is None else 1


if __name__ == '__main__':
    raise SystemExit(main())
