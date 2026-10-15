param([Parameter(Mandatory=$true)][string]$Destination,[string]$Root)
. (Join-Path $PSScriptRoot 'PgVN.Common.ps1')
Assert-PgVNAdministrator
if (-not $Root) { $Root = Get-PgVNRoot }
$Root = [IO.Path]::GetFullPath($Root); $Destination = [IO.Path]::GetFullPath($Destination)
$package = Assert-PgVNPackage $Root
if ($Destination.StartsWith($Root.TrimEnd('\') + '\',[StringComparison]::OrdinalIgnoreCase) -or $Destination -eq $Root) {
    throw '备份目录必须在安装目录之外。'
}
if (Test-Path -LiteralPath $Destination) { throw '备份目录已存在，请使用新的目录。' }
$state = Get-Content -LiteralPath (Join-Path $Root 'admin/services.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($state.Root -ne $Root) { throw '服务记录不属于此目录。' }
$probe = Invoke-PgVNSqlJson $Root 'pgvn_backup' "SELECT json_build_object('database',current_database(),'catalog',to_regclass('pgvn.schema_versions') IS NOT NULL);"
if (-not $probe.catalog) { throw '备份连接不是 PgVN 数据库。' }
$services = @(,@($state.ServiceName,$state.ServerImage))
if ($state.DavInstalled) { $services += ,@($state.DavServiceName,$state.DavImage) }
$restart = @()
try {
    foreach ($entry in $services) {
        Assert-PgVNServiceOwner $entry[0] $entry[1]
        if (Get-PgVNServiceImage $entry[0]) {
            $controller = New-Object ServiceProcess.ServiceController($entry[0])
            try { if ($controller.Status -eq [ServiceProcess.ServiceControllerStatus]::Running) { $restart += $entry[0] } }
            finally { $controller.Dispose() }
            Stop-PgVNOwnedService $entry[0] $entry[1]
        }
    }
    [void][IO.Directory]::CreateDirectory($Destination)
    Set-PgVNProtectedDirectory $Destination
    [void](Invoke-PgVNNative -FilePath (Join-Path $Root 'tools/postgresql/pg_dump.exe') -Arguments @(
        '--format=custom','--file',(Join-Path $Destination 'database.dump'),'--dbname','service=pgvn_backup') -TimeoutSeconds 7200 -Environment (Get-PgVNLibpqEnvironment $Root))
    $metadataSql = @'
WITH objects AS (
 SELECT n.oid,n.nspowner AS owner FROM pg_namespace n WHERE n.nspname IN ('pgfs','pgvn','pgvn_monitor')
), principals AS (
 SELECT owner AS role_id FROM objects
 UNION SELECT c.relowner FROM pg_class c WHERE c.relnamespace IN (SELECT oid FROM objects)
 UNION SELECT p.proowner FROM pg_proc p WHERE p.pronamespace IN (SELECT oid FROM objects)
 UNION SELECT a.grantee FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) a WHERE n.oid IN (SELECT oid FROM objects)
 UNION SELECT a.grantee FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE c.relnamespace IN (SELECT oid FROM objects)
 UNION SELECT a.grantee FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a WHERE p.pronamespace IN (SELECT oid FROM objects)
)
SELECT json_build_object('database',current_database(),'server_version_num',current_setting('server_version_num')::integer,
 'catalog_version',(SELECT max(version) FROM pgvn.schema_versions),'required_roles',
 (SELECT coalesce(json_agg(rolname ORDER BY rolname),'[]'::json) FROM pg_roles WHERE oid IN (SELECT role_id FROM principals WHERE role_id<>0)));
'@
    $database = Invoke-PgVNSqlJson $Root 'pgvn_backup' $metadataSql
    $files = [ordered]@{}; $sourceHashes = @{}
    foreach ($directory in @('config.local','secrets','admin','hooks')) {
        $source = Join-Path $Root $directory
        if (-not (Test-Path -LiteralPath $source)) { continue }
        foreach ($file in Get-ChildItem -LiteralPath $source -File -Recurse -Force) {
            if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '备份目录含重解析点，请先提供普通文件副本。' }
            $relative = $file.FullName.Substring($Root.Length + 1).Replace('\','/')
            $target = Join-Path $Destination ('configuration/' + $relative)
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
            $before = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            [IO.File]::Copy($file.FullName,$target,$false)
            if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $before) { throw "复制期间文件变化：$relative" }
            $files['configuration/' + $relative] = $before
            $sourceHashes[$file.FullName] = $before
        }
    }
    $keys = @()
    foreach ($field in @('credential-key-file','credential-previous-key-file')) {
        $source = Get-PgVNIniValue (Join-Path $Root 'config.local/pgvnserve.ini') $field
        if (-not $source) { continue }
        if (-not [IO.Path]::IsPathRooted($source)) { throw '密钥路径必须为绝对路径。' }
        $relative = 'keys/' + $field + '.key'
        $target = Join-Path $Destination $relative
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
        [IO.File]::Copy($source,$target,$false)
        $hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        $files[$relative] = $hash; $sourceHashes[$source] = $hash
        $keys += [ordered]@{ ConfigKey=$field; BackupPath=$relative; Sha256=$hash }
    }
    foreach ($entry in $sourceHashes.GetEnumerator()) {
        if ((Get-FileHash -LiteralPath $entry.Key -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) { throw '备份期间配置或密钥变化。' }
    }
    $files['database.dump'] = (Get-FileHash -LiteralPath (Join-Path $Destination 'database.dump') -Algorithm SHA256).Hash.ToLowerInvariant()
    $record = [ordered]@{ Format=1; Id=[Guid]::NewGuid().ToString('N'); PackageId=$package.package_id;
        CreatedAt=[DateTime]::UtcNow.ToString('o'); SourceRoot=$Root; Database=$database; Files=$files; Keys=$keys;
        LocalServicesQuiesced=$true; RemoteNodesCoordinated=$false; RemoteWriterCoordination='Coordinate key rotation and configuration publication on other nodes separately';
        ExternalHooks='Root/hooks is copied. Dependencies or hook roots outside the installation require the operator deployment inventory.' }
    Write-PgVNUtf8 (Join-Path $Destination 'backup-manifest.json') ($record | ConvertTo-Json -Depth 12)
    Write-Output "备份完成：$Destination"
} finally {
    foreach ($name in $restart) {
        [void](Invoke-PgVNNative -FilePath "$env:SystemRoot\System32/sc.exe" -Arguments @('start',$name) -AllowedExitCodes @(0,1056))
    }
}
