param([Parameter(Mandatory=$true)][string]$BackupDirectory,[string]$Root)
. (Join-Path $PSScriptRoot 'PgVN.Common.ps1')
Assert-PgVNAdministrator
if (-not $Root) { $Root = Get-PgVNRoot }
$Root = [IO.Path]::GetFullPath($Root); $BackupDirectory = [IO.Path]::GetFullPath($BackupDirectory)
[void](Assert-PgVNPackage $Root)
$record = Get-Content -LiteralPath (Join-Path $BackupDirectory 'backup-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($record.Format -ne 1 -or $record.Id -notmatch '^[0-9a-f]{32}$') { throw '不支持的备份记录。' }
foreach ($entry in $record.Files.PSObject.Properties) {
    if ([IO.Path]::IsPathRooted($entry.Name) -or $entry.Name -match '(^|[/\\])\.\.([/\\]|$)') { throw '备份含非法路径。' }
    $path = [IO.Path]::GetFullPath((Join-Path $BackupDirectory $entry.Name))
    if (-not $path.StartsWith($BackupDirectory.TrimEnd('\') + '\',[StringComparison]::OrdinalIgnoreCase)) { throw '备份路径越界。' }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) { throw "备份摘要不符：$($entry.Name)" }
}
if ($record.Files.PSObject.Properties.Name -cnotcontains 'database.dump') { throw '备份缺少数据库归档条目。' }
# Resolve local configuration conflicts before mutating an empty target database.
$iniPath = Join-Path $Root 'config.local/pgvnserve.ini'
if (-not (Test-Path -LiteralPath $iniPath -PathType Leaf)) { throw '先初始化本目录配置，再执行恢复。' }
$keys = @($record.Keys)
if (@($keys | Where-Object { $_.ConfigKey -eq 'credential-key-file' }).Count -ne 1) { throw '备份必须包含一个当前部署密钥。' }
if (@($keys | Select-Object -ExpandProperty ConfigKey -Unique).Count -ne $keys.Count) { throw '备份包含重复密钥配置。' }
foreach ($key in $keys) {
    if ($key.ConfigKey -notin @('credential-key-file','credential-previous-key-file') -or $key.BackupPath -cne ('keys/' + $key.ConfigKey + '.key')) { throw '备份密钥条目无效。' }
    $item = $record.Files.PSObject.Properties[$key.BackupPath]
    if (-not $item -or $item.Value -cne $key.Sha256) { throw '备份密钥与文件清单不符。' }
    $targetKey = Join-Path $Root ('secrets/restored-' + $record.Id + '-' + $key.ConfigKey + '.key')
    if ((Test-Path -LiteralPath $targetKey) -and (Get-FileHash -LiteralPath $targetKey -Algorithm SHA256).Hash.ToLowerInvariant() -ne $key.Sha256) { throw '已有恢复密钥摘要不同。' }
}
$users = Join-Path $BackupDirectory 'configuration/secrets/users.htpasswd'
$targetUsers = Join-Path $Root 'secrets/users.htpasswd'
if (Test-Path -LiteralPath $users) {
    if ($record.Files.PSObject.Properties.Name -cnotcontains 'configuration/secrets/users.htpasswd') { throw 'DAV 密码文件未列入备份清单。' }
    if (Test-Path -LiteralPath $targetUsers) { throw '目标已存在 DAV 密码文件，未覆盖；请先在独立目录合并并复核。' }
}
$statePath = Join-Path $Root 'admin/services.json'
if (Test-Path -LiteralPath $statePath) {
    $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.Root -ne $Root) { throw '服务记录不属于此目录。' }
    $services = @(,@($state.ServiceName,$state.ServerImage))
    if ($state.DavInstalled) { $services += ,@($state.DavServiceName,$state.DavImage) }
    foreach ($entry in $services) {
        Assert-PgVNServiceOwner $entry[0] $entry[1]
        if (Get-PgVNServiceImage $entry[0]) {
            $controller = New-Object ServiceProcess.ServiceController($entry[0])
            try { if ($controller.Status -ne [ServiceProcess.ServiceControllerStatus]::Stopped) { throw '恢复前先停止本目录的服务。' } }
            finally { $controller.Dispose() }
        }
    }
}
$target = Invoke-PgVNSqlJson $Root 'pgvn_restore' @'
SELECT json_build_object('database',current_database(),'user',current_user,
 'relations',(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')),
 'functions',(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'));
'@
if ($target.relations -ne 0 -or $target.functions -ne 0) { throw '恢复目标必须为空数据库；未执行清除或覆盖。' }
$roleInfo = Invoke-PgVNSqlJson $Root 'pgvn_restore' 'SELECT json_agg(rolname) FROM pg_roles;'
$missing = @($record.Database.required_roles | Where-Object { $roleInfo -cnotcontains $_ })
if ($missing.Count -gt 0) { throw ('先由 DBA 创建备份所需角色（可为 NOLOGIN），再重试：' + ($missing -join ', ')) }
$environment = Get-PgVNLibpqEnvironment $Root
[void](Invoke-PgVNNative -FilePath (Join-Path $Root 'tools/postgresql/pg_restore.exe') -Arguments @(
    '--exit-on-error','--no-owner','--dbname','service=pgvn_restore',(Join-Path $BackupDirectory 'database.dump')) -TimeoutSeconds 7200 -Environment $environment)
# Retaining ACLs preserves the source's PUBLIC revocations. Regrant known roles
# through the native migration owner path rather than changing SQL by hand.
[void](Invoke-PgVNNative -FilePath (Join-Path $Root 'bin/pgvnadmin.exe') -Arguments @(
    '--config',(Join-Path $Root 'admin/restore.ini'),'db','check','--json') -Environment $environment)
[void](Invoke-PgVNNative -FilePath (Join-Path $Root 'bin/pgvnadmin.exe') -Arguments @(
    '--config',(Join-Path $Root 'admin/restore.ini'),'db','grant','--admin-role','pgvn_admin',
    '--service-role','pgvn_service','--monitor-role','pgvn_monitor','--json') -Environment $environment)
foreach ($key in $keys) {
    $source = Join-Path $BackupDirectory $key.BackupPath
    $targetKey = Join-Path $Root ('secrets/restored-' + $record.Id + '-' + $key.ConfigKey + '.key')
    if (Test-Path -LiteralPath $targetKey) {
        if ((Get-FileHash -LiteralPath $targetKey -Algorithm SHA256).Hash.ToLowerInvariant() -ne $key.Sha256) { throw '已有恢复密钥摘要不同。' }
    } else { [IO.File]::Copy($source,$targetKey,$false) }
    Set-PgVNKeyPermissions $targetKey
    Set-PgVNIniValue $iniPath $key.ConfigKey $targetKey.Replace('\','/')
}
if (@($keys | Where-Object { $_.ConfigKey -eq 'credential-previous-key-file' }).Count -eq 0) {
    Set-PgVNIniValue $iniPath 'credential-previous-key-file' ''
}
if (Test-Path -LiteralPath $users) {
    [IO.File]::Copy($users,$targetUsers,$false)
}
Set-PgVNProtectedDirectory (Join-Path $Root 'secrets') -ServiceRead
$report = [ordered]@{ Status='RestoredDatabase'; BackupId=$record.Id; Database=$target.database;
    RestoreOwner=$target.user; AclPreserved=$true; ServicesStarted=$false;
    Next='Review runtime/admin connection profiles for the restored database, restore external hook dependencies, then start services and run Test-PgVNDeployment.ps1.' }
Write-PgVNUtf8 (Join-Path $Root ('admin/restore-' + $record.Id + '.json')) ($report | ConvertTo-Json -Depth 5)
$report | ConvertTo-Json -Depth 5
