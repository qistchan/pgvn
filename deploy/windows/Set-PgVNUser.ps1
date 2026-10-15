param(
    [Parameter(Mandatory=$true)][string]$UserName,
    [Parameter(Mandatory=$true)][Security.SecureString]$Password,
    [string]$IdentityDomain='pgvn', [switch]$AlsoDav
)
. (Join-Path $PSScriptRoot 'PgVN.Common.ps1')
$root = Get-PgVNRoot
$environment = Get-PgVNLibpqEnvironment $root
$keyPath = Get-PgVNIniValue (Join-Path $root 'config.local/pgvnserve.ini') 'credential-key-file'
if (-not $keyPath -or -not [IO.Path]::IsPathRooted($keyPath)) { throw '先配置当前部署密钥的绝对路径。' }
if ($AlsoDav -and $UserName -match '[:\r\n\x00]') { throw 'HTTP Basic 用户名不能包含冒号、换行或 NUL。' }
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    $result = Invoke-PgVNNative -FilePath (Join-Path $root 'bin/pgvnadmin.exe') -Arguments @(
        '--config',(Join-Path $root 'admin/admin.ini'),'user','set',$IdentityDomain,$UserName,
        '--key-file',$keyPath) -InputText ($plain + "`r`n") -Environment $environment
    [Console]::Out.Write($result.Output)
    if ($AlsoDav) {
        $hash = Invoke-PgVNNative -FilePath (Join-Path $root 'apache/bin/htpasswd.exe') -Arguments @(
            '-n','-i','-B','-C','12','pgvn-generated-password') -InputText ($plain + "`r`n") -Environment $environment
        $passwordHash = $hash.Output.Trim().Split(@(':'),2,[StringSplitOptions]::None)[1]
        $usersPath = Join-Path $root 'secrets/users.htpasswd'
        $lines = @()
        if (Test-Path -LiteralPath $usersPath) {
            $lines = @(Get-Content -LiteralPath $usersPath -Encoding UTF8 | Where-Object {
                -not $_.StartsWith($UserName + ':',[StringComparison]::Ordinal)
            })
        }
        $lines += ($UserName + ':' + $passwordHash)
        $temporary = Join-Path $root ('secrets/users-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        Write-PgVNUtf8 $temporary (($lines -join "`n") + "`n")
        if (Test-Path -LiteralPath $usersPath) { [IO.File]::Replace($temporary,$usersPath,$null) }
        else { [IO.File]::Move($temporary,$usersPath) }
        Write-Output 'DAV 密码文件已同步。'
    }
} finally {
    $plain = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}
