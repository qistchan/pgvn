param([string]$Root)
. (Join-Path $PSScriptRoot 'PgVN.Common.ps1')
Assert-PgVNAdministrator
if (-not $Root) { $Root = Get-PgVNRoot }
$Root = [IO.Path]::GetFullPath($Root)
$recordPath = Join-Path $Root 'admin/services.json'
$record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($record.Root -ne $Root) { throw '服务记录不属于此目录。' }
Assert-PgVNServiceOwner $record.ServiceName $record.ServerImage
if ($record.DavInstalled) { Assert-PgVNServiceOwner $record.DavServiceName $record.DavImage }
if ($record.DavInstalled -and (Get-PgVNServiceImage $record.DavServiceName)) {
    Stop-PgVNOwnedService $record.DavServiceName $record.DavImage
    [void](Invoke-PgVNNative -FilePath (Join-Path $Root 'apache/bin/httpd.exe') -Arguments @('-k','uninstall','-n',$record.DavServiceName))
}
if (Get-PgVNServiceImage $record.ServiceName) {
    Stop-PgVNOwnedService $record.ServiceName $record.ServerImage
    [void](Invoke-PgVNNative -FilePath "$env:SystemRoot\System32/sc.exe" -Arguments @('delete',$record.ServiceName))
}
if ($record.UrlAclCreated -and -not $record.Uninstalled) {
    [void](Invoke-PgVNNative -FilePath "$env:SystemRoot\System32/netsh.exe" -Arguments @('http','delete','urlacl',('url=' + $record.MonitorUrl)))
}
$record.Uninstalled = $true
Write-PgVNUtf8 $recordPath ($record | ConvertTo-Json -Depth 4)
Write-Output '本目录登记的服务与 URL 预留已移除。数据库、配置、密钥和日志均保留。'
