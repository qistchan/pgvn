Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -lt 5) { throw '需要 Windows PowerShell 5.1 或更高版本。' }
# PSModulePath inherited from PowerShell 7 can precede the Windows PowerShell
# modules. Resolve built-in modules against this process's own PSHOME.
foreach ($moduleName in @('Microsoft.PowerShell.Management','Microsoft.PowerShell.Utility','Microsoft.PowerShell.Security')) {
    Import-Module -Name (Join-Path $PSHOME ('Modules/' + $moduleName + '/' + $moduleName + '.psd1')) -ErrorAction Stop
}
if ($PSVersionTable.PSVersion.Major -le 5) { Add-Type -AssemblyName System.ServiceProcess }
else { Add-Type -AssemblyName System.ServiceProcess.ServiceController }

function Get-PgVNRoot {
    return [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
}

function ConvertTo-PgVNWindowsArgument {
    param([AllowEmptyString()][string]$Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ([int]$character -eq 92) { $slashes++; continue }
        if ($character -eq '"') {
            [void]$builder.Append([char]92, 2 * $slashes + 1)
        } else { [void]$builder.Append([char]92, $slashes) }
        [void]$builder.Append($character)
        $slashes = 0
    }
    [void]$builder.Append([char]92, 2 * $slashes)
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Join-PgVNWindowsArguments {
    param([string[]]$Values)
    return (($Values | ForEach-Object { ConvertTo-PgVNWindowsArgument $_ }) -join ' ')
}

function Invoke-PgVNNative {
    param([string]$FilePath, [string[]]$Arguments = @(), [AllowNull()][string]$InputText = $null,
        [int[]]$AllowedExitCodes = @(0), [int]$TimeoutSeconds = 120, [hashtable]$Environment = @{})
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $FilePath
    $start.Arguments = Join-PgVNWindowsArguments $Arguments
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.RedirectStandardInput = ($null -ne $InputText)
    $start.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $start.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    foreach ($entry in $Environment.GetEnumerator()) { $start.EnvironmentVariables[$entry.Key] = $entry.Value }
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw "无法启动 $([IO.Path]::GetFileName($FilePath))" }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if ($null -ne $InputText) {
            $inputBytes = (New-Object Text.UTF8Encoding($false)).GetBytes($InputText)
            $process.StandardInput.BaseStream.Write($inputBytes, 0, $inputBytes.Length)
            $process.StandardInput.Close()
            [Array]::Clear($inputBytes, 0, $inputBytes.Length)
        }
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            # Only the process launched by this call is addressed.
            try { $process.Kill() } catch { }
            $process.WaitForExit()
            throw "$([IO.Path]::GetFileName($FilePath)) 超时"
        }
        $output = $stdout.GetAwaiter().GetResult()
        $errorText = $stderr.GetAwaiter().GetResult()
        if ($AllowedExitCodes -notcontains $process.ExitCode) {
            # Authentication arguments and input are deliberately not echoed.
            throw "$([IO.Path]::GetFileName($FilePath)) 失败，退出码 $($process.ExitCode)：$errorText"
        }
        return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = $output; Error = $errorText }
    } finally { $process.Dispose() }
}

function Write-PgVNUtf8 {
    param([string]$Path, [string]$Text)
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Assert-PgVNAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '此操作需要管理员 PowerShell。'
    }
}

function Assert-PgVNPackage {
    param([string]$Root = (Get-PgVNRoot))
    $Root = [IO.Path]::GetFullPath($Root)
    $manifestPath = Join-Path $Root 'package-manifest.json'
    $expectedManifestHash = (Get-Content -LiteralPath ($manifestPath + '.sha256') -Raw).Split(' ')[0].Trim().ToLowerInvariant()
    if ((Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedManifestHash) {
        throw '包清单摘要不匹配。'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($entry in $manifest.files.PSObject.Properties) {
        if ([IO.Path]::IsPathRooted($entry.Name) -or $entry.Name -match '(^|[/\\])\.\.([/\\]|$)') {
            throw '清单含非法路径。'
        }
        $path = [IO.Path]::GetFullPath((Join-Path $Root $entry.Name))
        if (-not $path.StartsWith($Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw '清单路径超出安装目录。'
        }
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "缺少包文件：$($entry.Name)" }
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne $entry.Value.sha256) { throw "包文件摘要不匹配：$($entry.Name)" }
    }
    return $manifest
}

function Set-PgVNProtectedDirectory {
    param([string]$Path, [switch]$ServiceRead, [ValidateSet('R','RX','M')][string]$ServiceRights = 'R')
    $arguments = @($Path, '/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F')
    if ($ServiceRead) { $arguments += ('*S-1-5-19:(OI)(CI)' + $ServiceRights) }
    [void](Invoke-PgVNNative -FilePath "$env:SystemRoot\System32\icacls.exe" -Arguments $arguments)
}

function Get-PgVNLibpqEnvironment {
    param([string]$Root = (Get-PgVNRoot), [switch]$Runtime)
    if ($Runtime) {
        $serviceFile = Join-Path $Root 'config.local\pg_service.runtime.conf'
        $passwordFile = Join-Path $Root 'secrets\runtime.pgpass.conf'
    } else {
        $serviceFile = Join-Path $Root 'admin\pg_service.conf'
        $passwordFile = Join-Path $Root 'admin\secrets\pgpass.conf'
    }
    $values = @{ PGSERVICEFILE=$serviceFile; PGPASSFILE=$passwordFile;
        OPENSSL_CONF=(Join-Path $Root 'config.templates\openssl.cnf') }
    if ($Runtime) {
        $values['PATH'] = Join-Path $env:SystemRoot 'System32'
        $values['TEMP'] = Join-Path $Root 'state/tmp'
        $values['TMP'] = $values['TEMP']
    }
    return $values
}

function Set-PgVNKeyPermissions {
    param([string]$Path)
    if (-not [IO.Path]::IsPathRooted($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw '密钥必须为已存在的绝对文件路径。'
    }
    if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw '密钥不能为重解析点。'
    }
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetSecurityDescriptorSddlForm('D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;LS)',[Security.AccessControl.AccessControlSections]::Access)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Get-PgVNServiceImage {
    param([string]$Name)
    $path = 'HKLM:\SYSTEM\CurrentControlSet\Services\' + $Name
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    return [string](Get-ItemProperty -LiteralPath $path -Name ImagePath).ImagePath
}

function Assert-PgVNServiceOwner {
    param([string]$Name, [string]$ExpectedImage)
    $actual = Get-PgVNServiceImage $Name
    if ($actual -and -not [string]::Equals($actual,$ExpectedImage,[StringComparison]::OrdinalIgnoreCase)) {
        throw "服务 $Name 的程序路径与本次安装记录不同，保留该服务。"
    }
}

function Stop-PgVNOwnedService {
    param([string]$Name, [string]$ExpectedImage)
    Assert-PgVNServiceOwner $Name $ExpectedImage
    if (-not (Get-PgVNServiceImage $Name)) { return }
    $service = New-Object ServiceProcess.ServiceController($Name)
    try {
        $service.Refresh()
        if ($service.Status -ne [ServiceProcess.ServiceControllerStatus]::Stopped) {
            if ($service.Status -ne [ServiceProcess.ServiceControllerStatus]::StopPending) { $service.Stop() }
            $service.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped,[TimeSpan]::FromSeconds(45))
        }
    } finally { $service.Dispose() }
}

function Get-PgVNIniValue {
    param([string]$Path,[string]$Key)
    $inside = $false; $value = $null
    foreach ($line in [IO.File]::ReadAllLines($Path,[Text.Encoding]::UTF8)) {
        if ($line -match '^\s*\[([^]]+)\]') { $inside = ($Matches[1] -ieq 'pgvn'); continue }
        if ($inside -and $line -match ('^\s*' + [regex]::Escape($Key) + '\s*=(.*)$')) { $value = $Matches[1].Trim() }
    }
    return $value
}

function Set-PgVNIniValue {
    param([string]$Path,[string]$Key,[string]$Value)
    if ($Value -match '[\r\n\x00]') { throw 'INI 值含非法换行。' }
    $text = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    $lines = New-Object 'Collections.Generic.List[string]'
    $inside = $false; $written = $false; $found = $false
    foreach ($line in ($text -split '\r?\n')) {
        if ($line -match '^\s*\[([^]]+)\]') {
            if ($inside -and -not $written) { $lines.Add($Key + ' = ' + $Value); $written = $true }
            $inside = ($Matches[1] -ieq 'pgvn'); if ($inside) { $found = $true }
        }
        if ($inside -and $line -match ('^\s*' + [regex]::Escape($Key) + '\s*=')) {
            $lines.Add($Key + ' = ' + $Value); $written = $true
        } else { $lines.Add($line) }
    }
    if ($inside -and -not $written) { $lines.Add($Key + ' = ' + $Value); $written = $true }
    if (-not $found -or -not $written) { throw '缺少 pgVN 配置节。' }
    Write-PgVNUtf8 $Path ($lines -join "`n")
}

function Invoke-PgVNSqlJson {
    param([string]$Root,[string]$Service,[string]$Sql)
    $result = Invoke-PgVNNative -FilePath (Join-Path $Root 'tools/postgresql/psql.exe') -Arguments @(
        '-X','-w','-A','-t','--set=ON_ERROR_STOP=1','--dbname',('service=' + $Service),'-f','-') -InputText ($Sql + "`n") -Environment (Get-PgVNLibpqEnvironment $Root)
    return ($result.Output.Trim() | ConvertFrom-Json)
}
