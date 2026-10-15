param([string]$Root)
. (Join-Path $PSScriptRoot 'PgVN.Common.ps1')
if (-not $Root) { $Root = Get-PgVNRoot }
$manifest = Assert-PgVNPackage $Root
$count = 0
foreach ($group in $manifest.native_dependency_audit.PSObject.Properties) {
    foreach ($item in $group.Value.files.PSObject.Properties) {
        $path = Join-Path $Root $item.Value
        $stream = [IO.File]::OpenRead($path)
        $reader = New-Object IO.BinaryReader($stream)
        try {
            if ($reader.ReadUInt16() -ne 0x5a4d) { throw "无效的 PE 文件：$($item.Value)" }
            $stream.Position = 0x3c
            $offset = $reader.ReadUInt32()
            $stream.Position = $offset
            if ($reader.ReadUInt32() -ne 0x00004550 -or $reader.ReadUInt16() -ne 0x8664) {
                throw "不是 x64 产物：$($item.Value)"
            }
        } finally { $reader.Dispose(); $stream.Dispose() }
        $count++
    }
}
$savedPath = $env:PATH
try {
    $env:PATH = Join-Path $env:SystemRoot 'System32'
    $environment = Get-PgVNLibpqEnvironment $Root
    $server = Invoke-PgVNNative -FilePath (Join-Path $Root 'bin/pgvnserve.exe') -Arguments @('--version') -Environment $environment
    $identity = $server.Output | ConvertFrom-Json
    if ($identity.milestone -ne $manifest.milestone -or $identity.svn_base -ne '1.14.5' -or $identity.process_code_page -ne 65001) { throw '产物身份、UTF-8 代码页与清单不符。' }
    [void](Invoke-PgVNNative -FilePath (Join-Path $Root 'bin/pgvnadmin.exe') -Arguments @('--version'))
    [void](Invoke-PgVNNative -FilePath (Join-Path $Root 'apache/bin/pgvnhttpd.exe') -Arguments @('-v'))
    $hostIdentity = (Invoke-PgVNNative -FilePath (Join-Path $Root 'apache/bin/pgvnhttpd.exe') -Arguments @('--pgvn-runtime')).Output | ConvertFrom-Json
    if ($hostIdentity.product -ne 'PgVNHTTPD' -or $hostIdentity.process_code_page -ne 65001 -or $hostIdentity.apache_base -ne '2.4.68') { throw '专用 HTTP 宿主或 UTF-8 代码页不符合要求。' }
    [void](Invoke-PgVNNative -FilePath (Join-Path $Root 'tools/svn/svn.exe') -Arguments @('--version','--quiet'))
    [void](Invoke-PgVNNative -FilePath (Join-Path $Root 'tools/postgresql/pg_dump.exe') -Arguments @('--version'))
} finally { $env:PATH = $savedPath }
[pscustomobject]@{ Status='PASS'; PackageId=$manifest.package_id; NativeImages=$count;
    ValidationStatus=$manifest.validation_status; TestScope='Hashes, x64 PE headers and clean-PATH version startup; no clean-machine claim' } | ConvertTo-Json
