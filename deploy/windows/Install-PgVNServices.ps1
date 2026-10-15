param(
    [string]$Root,
    [ValidatePattern('^[A-Za-z0-9_.-]{1,80}$')][string]$ServiceName='PgVN',
    [ValidatePattern('^[A-Za-z0-9_.-]{1,80}$')][string]$DavServiceName='PgVNDAV',
    [ValidateSet('Automatic','Manual')][string]$StartupType='Automatic',
    [switch]$WithoutDav, [switch]$NoStart
)
. (Join-Path $PSScriptRoot 'PgVN.Common.ps1')
Assert-PgVNAdministrator
if (-not $Root) { $Root = Get-PgVNRoot }
$Root = [IO.Path]::GetFullPath($Root)
$manifest = Assert-PgVNPackage $Root
if ($ServiceName -eq $DavServiceName) { throw '两个服务名称必须不同。' }
$settings = Get-Content -LiteralPath (Join-Path $Root 'admin/deployment.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($settings.Root -ne $Root -or $settings.MonitorUrl -notmatch '^http://127\.0\.0\.1:[0-9]+/$') {
    throw '配置目录或监控 URL 不符合本机安装记录。'
}
foreach ($relative in @('config.local/pgvnserve.ini','config.local/pg_service.runtime.conf','secrets/runtime.pgpass.conf')) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $relative) -PathType Leaf)) { throw "缺少运行配置：$relative" }
}
if (-not $WithoutDav -and -not (Test-Path -LiteralPath (Join-Path $Root 'secrets/users.htpasswd'))) {
    throw '先用 Set-PgVNUser.ps1 -AlsoDav 创建测试账号，再安装 DAV 服务。'
}
if (Select-String -LiteralPath (Join-Path $Root 'secrets/runtime.pgpass.conf') -Pattern 'CHANGE_ME_' -Quiet) {
    throw '请先填写运行账户的数据库密码文件。'
}
Set-PgVNProtectedDirectory $Root -ServiceRead -ServiceRights RX
Set-PgVNProtectedDirectory (Join-Path $Root 'admin')
Set-PgVNProtectedDirectory (Join-Path $Root 'admin/secrets')
Set-PgVNProtectedDirectory (Join-Path $Root 'config.local') -ServiceRead
Set-PgVNProtectedDirectory (Join-Path $Root 'secrets') -ServiceRead
foreach ($relative in @('logs','state')) { Set-PgVNProtectedDirectory (Join-Path $Root $relative) -ServiceRead -ServiceRights M }
Set-PgVNProtectedDirectory (Join-Path $Root 'hooks') -ServiceRead -ServiceRights RX
$activeKey = Get-PgVNIniValue (Join-Path $Root 'config.local/pgvnserve.ini') 'credential-key-file'
if (-not $activeKey) { throw '缺少当前部署密钥配置。' }
Set-PgVNKeyPermissions $activeKey
$previousKey = Get-PgVNIniValue (Join-Path $Root 'config.local/pgvnserve.ini') 'credential-previous-key-file'
if ($previousKey) { Set-PgVNKeyPermissions $previousKey }
$runtimeEnvironment = Get-PgVNLibpqEnvironment $Root -Runtime
$sc = Join-Path $env:SystemRoot 'System32/sc.exe'
$netsh = Join-Path $env:SystemRoot 'System32/netsh.exe'
$httpd = Join-Path $Root 'apache/bin/pgvnhttpd.exe'
$serverImage = Join-PgVNWindowsArguments @((Join-Path $Root 'bin/pgvnserve.exe'),'--service','--service-name',$ServiceName,
    '--config',(Join-Path $Root 'config.local/pgvnserve.ini'),'--monitor-url',$settings.MonitorUrl)
$davImage = Join-PgVNWindowsArguments @($httpd,'-k','runservice','-n',$DavServiceName,'-f',(Join-Path $Root 'config.local/httpd.conf'))
$recordPath = Join-Path $Root 'admin/services.json'
$existing = $null
if (Test-Path -LiteralPath $recordPath) {
    $existing = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($existing.Root -ne $Root -or $existing.ServiceName -ne $ServiceName -or $existing.DavServiceName -ne $DavServiceName -or
        $existing.ServerImage -ne $serverImage -or $existing.DavImage -ne $davImage -or $existing.MonitorUrl -ne $settings.MonitorUrl -or
        $existing.DavInstalled -ne (-not $WithoutDav)) {
        throw '现有服务记录与本次参数不同。请先按原记录卸载。'
    }
    if ($existing.Uninstalled) { $existing = $null }
}
foreach ($entry in @(@($ServiceName,$serverImage),@($DavServiceName,$davImage))) {
    if (Get-PgVNServiceImage $entry[0]) {
        if (-not $existing) { throw "服务 $($entry[0]) 已存在，未改动。" }
        Assert-PgVNServiceOwner $entry[0] $entry[1]
    }
}
$createdServer = $false; $createdDav = $false; $reserved = $false
try {
    if (-not $existing) {
        [void](Invoke-PgVNNative -FilePath $netsh -Arguments @('http','add','urlacl',('url=' + $settings.MonitorUrl),'sddl=D:(A;;GX;;;LS)'))
        $reserved = $true
    }
    $startType = if ($StartupType -eq 'Automatic') { 'auto' } else { 'demand' }
    if (-not (Get-PgVNServiceImage $ServiceName)) {
        [void](Invoke-PgVNNative -FilePath $sc -Arguments @('create',$ServiceName,'binPath=',$serverImage,
            'obj=','NT AUTHORITY\LocalService','start=',$startType))
        $createdServer = $true
    }
    if (-not $WithoutDav -and -not (Get-PgVNServiceImage $DavServiceName)) {
        [void](Invoke-PgVNNative -FilePath $httpd -Arguments @('-t','-f',(Join-Path $Root 'config.local/httpd.conf')) -Environment $runtimeEnvironment)
        [void](Invoke-PgVNNative -FilePath $httpd -Arguments @('-k','install','-n',$DavServiceName,'-f',(Join-Path $Root 'config.local/httpd.conf')) -Environment $runtimeEnvironment)
        $createdDav = $true
        [void](Invoke-PgVNNative -FilePath $sc -Arguments @('config',$DavServiceName,'binPath=',$davImage,
            'obj=','NT AUTHORITY\LocalService','start=',$startType))
    }
    $names = @($ServiceName)
    if (-not $WithoutDav) { $names += $DavServiceName }
    foreach ($name in $names) {
        $environment = @($runtimeEnvironment.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value })
        [void](New-ItemProperty -LiteralPath ('HKLM:\SYSTEM\CurrentControlSet\Services\' + $name) -Name Environment -PropertyType MultiString -Value $environment -Force)
    }
    $record = [ordered]@{ Version=1; Root=$Root; PackageId=$manifest.package_id; ServiceName=$ServiceName;
        DavServiceName=$DavServiceName; ServerImage=$serverImage; DavImage=$davImage; MonitorUrl=$settings.MonitorUrl;
        DavInstalled=(-not $WithoutDav); UrlAclCreated=($reserved -or ($existing -and $existing.UrlAclCreated)); Uninstalled=$false }
    Write-PgVNUtf8 $recordPath ($record | ConvertTo-Json -Depth 4)
    if (-not $NoStart) {
        foreach ($name in $names) {
            [void](Invoke-PgVNNative -FilePath $sc -Arguments @('start',$name) -AllowedExitCodes @(0,1056))
            $controller = New-Object ServiceProcess.ServiceController($name)
            try { $controller.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running,[TimeSpan]::FromSeconds(30)) }
            finally { $controller.Dispose() }
        }
    }
    Write-Output '服务已注册。运行 Test-PgVNDeployment.ps1 检查业务就绪与协议连接。'
} catch {
    if ($createdDav) {
        try { Stop-PgVNOwnedService $DavServiceName $davImage; [void](Invoke-PgVNNative -FilePath $httpd -Arguments @('-k','uninstall','-n',$DavServiceName)) } catch { }
    }
    if ($createdServer) {
        try { Stop-PgVNOwnedService $ServiceName $serverImage; [void](Invoke-PgVNNative -FilePath $sc -Arguments @('delete',$ServiceName)) } catch { }
    }
    if ($reserved) { try { [void](Invoke-PgVNNative -FilePath $netsh -Arguments @('http','delete','urlacl',('url=' + $settings.MonitorUrl))) } catch { } }
    if (-not $existing -and (Test-Path -LiteralPath $recordPath)) {
        try {
            $failedRecord = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $failedRecord.Uninstalled = $true
            $failedRecord.UrlAclCreated = $false
            Write-PgVNUtf8 $recordPath ($failedRecord | ConvertTo-Json -Depth 4)
        } catch { }
    }
    throw
}
