param(
    [string]$Root,
    [string]$DatabaseHost = '127.0.0.1', [ValidateRange(1,65535)][int]$DatabasePort = 5432,
    [string]$DatabaseName = 'pgvn', [string]$NodeId = 'node-1',
    [ValidateRange(1,65535)][int]$SvnPort = 14690,
    [ValidateRange(1024,65535)][int]$MonitorPort = 14691,
    [ValidateRange(1,65535)][int]$HttpPort = 14980
)
. (Join-Path $PSScriptRoot 'PgVN.Common.ps1')
if (-not $Root) { $Root = Get-PgVNRoot }
$Root = [IO.Path]::GetFullPath($Root)
[void](Assert-PgVNPackage $Root)
Assert-PgVNAdministrator
foreach ($value in @($DatabaseHost,$DatabaseName,$NodeId,$Root)) {
    if (-not $value -or $value -match '[\r\n\x00]') { throw '配置值不能包含换行或 NUL。' }
}
if (@($SvnPort,$MonitorPort,$HttpPort | Select-Object -Unique).Count -ne 3) { throw '三个监听端口必须不同。' }
if ($DatabaseHost -notmatch '^[A-Za-z0-9.,:_-]+$' -or $DatabaseHost.Split(',') -contains '') {
    throw '数据库主机应为 DNS 名称或 IP；多个同端口主机以逗号分隔。'
}
if ($DatabaseName -ne $DatabaseName.Trim()) { throw '数据库名称不能以空白开头或结尾。' }
if ($NodeId -notmatch '^[A-Za-z0-9_.-]{1,80}$') { throw '节点 ID 仅接受 1–80 位字母、数字、下划线、点和连字符。' }
$sslMode = if ($DatabaseHost -in @('127.0.0.1','localhost','::1')) { 'disable' } else { 'verify-full' }
$tokens = @{
    '@PGVN_ROOT@'=$Root.Replace('\','/'); '@DB_HOST@'=$DatabaseHost; '@DB_PORT@'=[string]$DatabasePort;
    '@DB_NAME@'=$DatabaseName; '@NODE_ID@'=$NodeId; '@SVN_PORT@'=[string]$SvnPort;
    '@HTTP_PORT@'=[string]$HttpPort; '@SSL_MODE@'=$sslMode
}
foreach ($directory in @('config.local','secrets','admin','admin/secrets','logs','state','hooks')) {
    $path = Join-Path $Root $directory
    [void][IO.Directory]::CreateDirectory($path)
    if ($directory -eq 'admin' -or $directory.StartsWith('admin/')) { Set-PgVNProtectedDirectory $path }
    elseif ($directory -in @('logs','state')) { Set-PgVNProtectedDirectory $path -ServiceRead -ServiceRights M }
    elseif ($directory -eq 'hooks') { Set-PgVNProtectedDirectory $path -ServiceRead -ServiceRights RX }
    else { Set-PgVNProtectedDirectory $path -ServiceRead }
}
[void][IO.Directory]::CreateDirectory((Join-Path $Root 'state/tmp'))
$targets = @{
    'pgvnserve.ini.example'='config.local/pgvnserve.ini';
    'pgvn-apache.ini.example'='config.local/pgvn-apache.ini';
    'httpd.conf.example'='config.local/httpd.conf';
    'pgvn-admin.ini.example'='admin/admin.ini';
    'pgvn-migrate.ini.example'='admin/migrate.ini';
    'pgvn-restore.ini.example'='admin/restore.ini';
    'pg_service.runtime.conf.example'='config.local/pg_service.runtime.conf';
    'pg_service.conf.example'='admin/pg_service.conf';
    'runtime.pgpass.conf.example'='secrets/runtime.pgpass.conf';
    'pgpass.conf.example'='admin/secrets/pgpass.conf'
}
foreach ($entry in $targets.GetEnumerator()) {
    $path = Join-Path $Root $entry.Value
    if (Test-Path -LiteralPath $path) { Write-Output "保留现有配置：$($entry.Value)"; continue }
    $text = Get-Content -LiteralPath (Join-Path $Root ('config.templates/' + $entry.Key)) -Raw -Encoding UTF8
    if ($entry.Key -in @('pgpass.conf.example','runtime.pgpass.conf.example')) {
        $passwordLines = @()
        foreach ($line in ($text -split '\r?\n')) {
            if ($line.StartsWith('@DB_HOST_PASS@:')) {
                foreach ($hostName in $DatabaseHost.Split(',')) {
                    $lineTokens = $tokens.Clone()
                    $lineTokens['@DB_HOST_PASS@'] = $hostName.Replace('\','\\').Replace(':','\:')
                    $lineTokens['@DB_NAME_PASS@'] = $DatabaseName.Replace('\','\\').Replace(':','\:')
                    $passwordLines += [regex]::Replace($line,'@[A-Z_]+@', {
                        param($match)
                        if ($lineTokens.ContainsKey($match.Value)) { return $lineTokens[$match.Value] }
                        return $match.Value
                    })
                }
            } else { $passwordLines += $line }
        }
        $text = $passwordLines -join "`n"
    } else {
        $text = [regex]::Replace($text,'@[A-Z_]+@', {
            param($match)
            if ($tokens.ContainsKey($match.Value)) { return $tokens[$match.Value] }
            return $match.Value
        })
    }
    Write-PgVNUtf8 $path $text
}
$state = [ordered]@{ Version=1; Root=$Root; MonitorUrl="http://127.0.0.1:$MonitorPort/";
    SvnPort=$SvnPort; HttpPort=$HttpPort; NodeId=$NodeId; DatabaseHost=$DatabaseHost;
    DatabasePort=$DatabasePort; DatabaseName=$DatabaseName; SslMode=$sslMode }
$settings = Join-Path $Root 'admin/deployment.json'
if (-not (Test-Path -LiteralPath $settings)) { Write-PgVNUtf8 $settings ($state | ConvertTo-Json -Depth 4) }
Write-Output '配置文件已准备。分别填写管理员密码文件和服务密码文件，再执行数据库引导与密钥创建。'
