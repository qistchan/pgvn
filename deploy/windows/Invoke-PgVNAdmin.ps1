param(
    [Alias('Profile')][ValidateSet('Admin','Migrate','Restore')][string]$ConnectionProfile = 'Admin',
    [Parameter(Position=0,ValueFromRemainingArguments=$true)][string[]]$Arguments
)
. (Join-Path $PSScriptRoot 'PgVN.Common.ps1')
$root = Get-PgVNRoot
$environment = Get-PgVNLibpqEnvironment $root
$configurations = @{ Admin='admin/admin.ini'; Migrate='admin/migrate.ini'; Restore='admin/restore.ini' }
$start = New-Object Diagnostics.ProcessStartInfo
$start.FileName = Join-Path $root 'bin/pgvnadmin.exe'
$start.Arguments = Join-PgVNWindowsArguments (@('--config',(Join-Path $root $configurations[$ConnectionProfile])) + $Arguments)
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
foreach ($entry in $environment.GetEnumerator()) { $start.EnvironmentVariables[$entry.Key] = $entry.Value }
$process = New-Object Diagnostics.Process
$process.StartInfo = $start
try {
    if (-not $process.Start()) { throw '无法启动 pgvnadmin。' }
    # Preserve binary cat/dump output rather than decoding and re-encoding it.
    $stdout = $process.StandardOutput.BaseStream.CopyToAsync([Console]::OpenStandardOutput())
    $stderr = $process.StandardError.BaseStream.CopyToAsync([Console]::OpenStandardError())
    $process.WaitForExit()
    [void]$stdout.GetAwaiter().GetResult()
    [void]$stderr.GetAwaiter().GetResult()
    $exitCode = $process.ExitCode
} finally { $process.Dispose() }
exit $exitCode
