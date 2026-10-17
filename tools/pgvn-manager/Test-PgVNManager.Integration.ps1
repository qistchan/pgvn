param(
    [Parameter(Mandatory=$true)][string]$BuildDirectory,
    [Parameter(Mandatory=$true)][string]$RuntimePackageZip,
    [Parameter(Mandatory=$true)][string]$WorkDirectory,
    [Parameter(Mandatory=$true)][ValidatePattern('^pgvn_m6_net_[0-9a-f]{12}$')][string]$DatabaseName,
    [Parameter(Mandatory=$true)][string]$DbaUser,
    [string]$DbaPassword='',
    [ValidateSet('127.0.0.1')][string]$DatabaseHost='127.0.0.1',
    [ValidateSet(16432)][int]$DatabasePort=16432
)
# Explicit isolated integration harness. Does not initialize/restart PostgreSQL,
# delete databases/roles, create monitor tokens or touch a Polarion installation.
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$encoding=New-Object Text.UTF8Encoding($false)
$testId=$DatabaseName.Substring('pgvn_m6_net_'.Length)
$WorkDirectory=[IO.Path]::GetFullPath($WorkDirectory)
if (Test-Path -LiteralPath $WorkDirectory) { throw 'Test work directory must be new.' }
[void][IO.Directory]::CreateDirectory($WorkDirectory)
$root=Join-Path $WorkDirectory 'runtime'
Expand-Archive -LiteralPath $RuntimePackageZip -DestinationPath $root
if (-not (Test-Path -LiteralPath (Join-Path $root 'package-manifest.json'))) {
    $candidates=@(Get-ChildItem -LiteralPath $root -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'package-manifest.json') })
    if ($candidates.Count -ne 1) { throw 'Expected exactly one extracted runtime root.' }
    $root=$candidates[0].FullName
}
[void][Reflection.Assembly]::LoadFrom((Join-Path $BuildDirectory 'PgVNManager.exe'))
$backend=New-Object PgVN.Manager.BackendClient((Join-Path $BuildDirectory 'Invoke-PgVNManager.ps1'))
. (Join-Path $root 'deploy/windows/PgVN.Common.ps1')
function Port {
    $listener=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,0)
    try { $listener.Start(); return $listener.LocalEndpoint.Port } finally { $listener.Stop() }
}
$svnPort=Port; do { $monitorPort=Port } while ($monitorPort -eq $svnPort)
do { $httpPort=Port } while ($httpPort -in @($svnPort,$monitorPort))
$prefix='pgvn_gui_' + $testId
$script:settings=@{root=$root;host=$DatabaseHost;dbPort=[string]$DatabasePort;database=$DatabaseName;rolePrefix=$prefix;node=('gui-' + $testId);svnPort=[string]$svnPort;monitorPort=[string]$monitorPort;httpPort=[string]$httpPort;sslMode='disable';caFile='';dbaUser=$DbaUser;dbaPassword=$DbaPassword;serviceName=('PgVN-GUI-' + $testId);davServiceName=('PgVNDAV-GUI-' + $testId);withDav=$true;repository='demo';route='/svn/demo'}
$script:checks=New-Object 'System.Collections.Generic.List[string]'
function Call([string]$Action,[hashtable]$Extra=@{}) {
    Write-Host ('Testing ' + $Action)
    $dict=New-Object 'System.Collections.Generic.Dictionary[string,object]'
    # Unwrap provider-added PSObject properties (for example Join-Path output).
    # The desktop's request fields are plain CLR strings too.
    foreach ($key in $script:settings.Keys) { if ($key -notmatch '(?i)password' -or $Action -in @('provision','connect')) { $dict[$key]=[string]$script:settings[$key] } }
    foreach ($key in $Extra.Keys) { $dict[$key]=[string]$Extra[$key] }
    $dict['action']=$Action
    $value=$backend.Invoke($dict,$null)
    $script:checks.Add($Action)
    return $value
}
function Assert([bool]$Value,[string]$Message) { if (-not $Value) { throw $Message } }
function Denied([string]$Action,[hashtable]$Extra,[string]$Expected) {
    $caught=$false
    try { [void](Call $Action $Extra) } catch { $caught=$true; if ($_.Exception.Message -notmatch $Expected) { throw } }
    Assert $caught ('Operation should be refused: ' + $Action)
    $script:checks.Add('refused:' + $Action)
}
$started=[DateTime]::UtcNow; $failure=$null; $cleanup=$null
try {
    $state=Call 'inspect'; Assert (-not $state['configured']) 'Fresh package unexpectedly configured'
    $connection=Call 'connect'; Assert ($connection['canCreateDatabase'] -and $connection['canCreateRole']) 'DBA permissions missing'
    $state=Call 'provision'; Assert $state['initialized'] 'Initialization marker missing'
    [void](Call 'db.check')
    Denied 'provision' @{} '已有配置'
    Denied 'configure' @{database=($DatabaseName + '_different')} '切换数据库地址'
    $roleCheck=Invoke-PgVNNative -FilePath (Join-Path $root 'tools/postgresql/psql.exe') -Arguments @('-X','-w','-A','-t','--dbname','service=pgvn_migrate','-f','-') -InputText ("SELECT json_build_object('count',count(*),'unsafe',bool_or(rolsuper OR rolcreatedb OR rolcreaterole)) FROM pg_roles WHERE rolname LIKE '" + $prefix + "\_%' ESCAPE '\';") -Environment (Get-PgVNLibpqEnvironment $root)
    $roles=$roleCheck.Output | ConvertFrom-Json
    Assert ($roles.count -eq 5 -and -not $roles.unsafe) 'Roles are missing or overprivileged'
    $keyHash=(Get-FileHash -LiteralPath (Join-Path $root 'secrets/credentials-v1.key')).Hash
    [void](Call 'initialize')
    Assert ((Get-FileHash -LiteralPath (Join-Path $root 'secrets/credentials-v1.key')).Hash -eq $keyHash) 'Resume rotated the credential key'
    $passwordFileHash=(Get-FileHash -LiteralPath (Join-Path $root 'secrets/runtime.pgpass.conf')).Hash
    $script:settings.node='gui-saved-' + $testId
    [void](Call 'configure')
    Assert ((Get-FileHash -LiteralPath (Join-Path $root 'secrets/runtime.pgpass.conf')).Hash -eq $passwordFileHash) 'Saving changed runtime passwords'
    [void](Call 'repo.create')
    $list=Call 'repo.list'; Assert ($list['items'].Count -eq 1) 'Repository list mismatch'
    Assert (@([PgVN.Manager.BackendClient]::Records($list['items'])).Count -eq 1) 'Desktop repository collection mismatch'
    $password='Gui-%$' + [char]0x4E2D + [char]0x6587 + '-"\' + $testId
    [void](Call 'user.set' @{userName='gui-writer';userPassword=$password;withDav=$true})
    [void](Call 'user.set' @{userName='gui-writer';userPassword=$password;withDav=$true})
    $policy=Call 'authz.read'
    $published=Call 'authz.publish' @{authz="[/]`ngui-writer = rw`n* =`n";expectedGeneration=$policy['generation']}
    Denied 'authz.publish' @{authz="[/]`n* = r";expectedGeneration=$policy['generation']} '访问规则已更新'
    Denied 'authz.publish' @{authz='[broken';expectedGeneration=$published['generation']} 'Invalid authorization'
    $installed=Call 'services.install'
    Assert (@([PgVN.Manager.BackendClient]::Records($installed['services'])).Count -eq 2) 'Desktop service collection mismatch'
    [void](Call 'services.start')
    [void](Call 'health')
    Denied 'configure' @{} '先停止'
    $svn=Join-Path $root 'tools/svn/svn.exe'
    $svnUrl="svn://127.0.0.1:$svnPort/svn/demo"
    $httpUrl="http://127.0.0.1:$httpPort/svn/demo"
    $clientArguments=@('--non-interactive','--no-auth-cache','--config-dir',(Join-Path $WorkDirectory 'client-config'),'--username','gui-writer','--password-from-stdin')
    [void](Invoke-PgVNNative -FilePath $svn -Arguments (@('mkdir',($svnUrl + '/trunk'),'-m','GUI integration seed') + $clientArguments) -InputText ($password + "`r`n"))
    $info=Invoke-PgVNNative -FilePath $svn -Arguments (@('info','--xml',$httpUrl) + $clientArguments) -InputText ($password + "`r`n")
    $xml=[xml]$info.Output; Assert ($xml.info.entry.revision -eq '1') 'DAV did not see SVN commit'
    $script:checks.Add('svn-commit-dav-read')
    [void](Call 'repo.verify')
    $dump=Join-Path $WorkDirectory 'demo.dump'
    [void](Call 'repo.dump' @{file=$dump})
    Assert ((Get-Item -LiteralPath $dump).Length -gt 100) 'Dump is empty'
    Denied 'repo.dump' @{file=$dump} '已存在'
    [void](Call 'repo.create' @{repository='imported';route='/svn/imported'})
    [void](Call 'repo.load' @{repository='imported';file=$dump})
    $repos=Call 'repo.list'; $imported=@($repos['items'] | Where-Object { $_['name'] -eq 'imported' })[0]
    Assert (-not $imported['enabled'] -and $imported['head'] -eq 1) 'Import should retain a disabled repository at r1'
    [void](Call 'repo.verify' @{repository='imported'})
    Denied 'repo.load' @{repository='imported';file=$dump} 'r0'
    [void](Call 'repo.disable' @{repository='imported'})
    [void](Call 'repo.enable' @{repository='imported'})
    [void](Call 'services.restart')
    [void](Call 'health')
    $backup=Join-Path $WorkDirectory 'backup'
    [void](Call 'backup' @{file=$backup})
    Assert (Test-Path -LiteralPath (Join-Path $backup 'backup-manifest.json')) 'Backup manifest missing'
    [void](Call 'health')
    $logs=Call 'logs'
    Assert (@([PgVN.Manager.BackendClient]::Records($logs['logs'])).Count -gt 0) 'Desktop log collection mismatch'
    [void](Call 'services.stop')
    [void](Call 'services.uninstall')
    foreach ($name in @($script:settings.serviceName,$script:settings.davServiceName)) { Assert (-not (Get-PgVNServiceImage $name)) ('Service leaked: ' + $name) }
} catch { $failure=$_.Exception.Message; Write-Host ('FAILED: ' + $failure) }
finally {
    try {
        if (Test-Path -LiteralPath (Join-Path $root 'admin/services.json')) { [void](Call 'services.uninstall') }
    } catch { $cleanup=$_.Exception.Message }
    $report=[ordered]@{status=$(if ($failure -or $cleanup) {'FAIL'} else {'PASS'});test_id=$testId;database=$DatabaseName;endpoint=($DatabaseHost + ':' + $DatabasePort);root=$root;started_utc=$started.ToString('o');finished_utc=[DateTime]::UtcNow.ToString('o');checks=@($script:checks);failure=$failure;cleanup_failure=$cleanup;database_and_roles_retained=$true;monitor_tokens_created=0;postgres_restarted=$false;runtime_package_sha256=(Get-FileHash -LiteralPath $RuntimePackageZip -Algorithm SHA256).Hash.ToLowerInvariant();gui_sha256=(Get-FileHash -LiteralPath (Join-Path $BuildDirectory 'PgVNManager.exe')).Hash.ToLowerInvariant();backend_sha256=(Get-FileHash -LiteralPath (Join-Path $BuildDirectory 'Invoke-PgVNManager.ps1')).Hash.ToLowerInvariant();manager_manifest_sha256=(Get-FileHash -LiteralPath (Join-Path $BuildDirectory 'manager-manifest.json')).Hash.ToLowerInvariant()}
    [IO.File]::WriteAllText((Join-Path $WorkDirectory 'report.json'),($report | ConvertTo-Json -Depth 8),$encoding)
}
if ($failure -or $cleanup) { throw 'GUI integration failed; inspect report.json.' }
$report | ConvertTo-Json -Depth 8
