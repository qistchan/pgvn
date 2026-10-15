[CmdletBinding()]
param(
    [string] $PostgresHome = 'C:\Program Files\PostgreSQL\18',
    [ValidateRange(1024, 65535)] [int] $Port = 15432,
    [ValidatePattern('^[a-z][a-z0-9_]*$')] [string] $Database = 'pgfs_poc'
)

$ErrorActionPreference = 'Stop'
$taskProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskRuntimeRoot = Join-Path $taskProjectRoot 'runtime'
$taskDataPath = Join-Path $taskRuntimeRoot 'pg-primary'
$taskBinPath = Join-Path $PostgresHome 'bin'
$taskInitDb = Join-Path $taskBinPath 'initdb.exe'
$taskPgCtl = Join-Path $taskBinPath 'pg_ctl.exe'
$taskPsql = Join-Path $taskBinPath 'psql.exe'
$taskReady = Join-Path $taskBinPath 'pg_isready.exe'

foreach ($taskProgram in @($taskInitDb, $taskPgCtl, $taskPsql, $taskReady)) {
    if (-not (Test-Path -LiteralPath $taskProgram -PathType Leaf)) {
        throw "Required PostgreSQL executable is missing: $taskProgram"
    }
}

function Invoke-TestPsql {
    param([string] $TargetDatabase, [string[]] $Arguments)
    $taskResult = & $taskPsql -X -w -h 127.0.0.1 -p $Port -U pgfs_test `
        -d $TargetDatabase --set=ON_ERROR_STOP=1 @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Test PostgreSQL command failed: $($taskResult -join [Environment]::NewLine)"
    }
    return $taskResult
}

New-Item -ItemType Directory -Path $taskRuntimeRoot -Force | Out-Null
& $taskReady -h 127.0.0.1 -p $Port -t 2 2>&1 | Out-Null
$taskWasReady = $LASTEXITCODE -eq 0

if ($taskWasReady) {
    $taskActualDataPath = (Invoke-TestPsql -TargetDatabase postgres `
        -Arguments @('-A', '-t', '-c', 'SHOW data_directory;') | Out-String).Trim()
    if ([IO.Path]::GetFullPath($taskActualDataPath) -ne $taskDataPath) {
        throw "Port $Port belongs to another PostgreSQL data directory. Refusing to reuse it."
    }
} else {
    if (Test-Path -LiteralPath (Join-Path $taskDataPath 'postmaster.pid')) {
        & $taskPgCtl -D $taskDataPath status 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            throw 'The test PostgreSQL process is running but the TCP probe failed. Inspect the existing process; do not start another server.'
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $taskDataPath 'PG_VERSION'))) {
        if ((Test-Path -LiteralPath $taskDataPath) -and
            (Get-ChildItem -LiteralPath $taskDataPath -Force | Select-Object -First 1)) {
            throw "The test data directory is nonempty and uninitialized: $taskDataPath"
        }
        & $taskInitDb -D $taskDataPath -U pgfs_test --encoding=UTF8 --locale=C `
            --auth-local=trust --auth-host=trust --no-instructions
        if ($LASTEXITCODE -ne 0) { throw 'Test PostgreSQL initialization failed.' }
    }

    & $taskPgCtl -D $taskDataPath -l (Join-Path $taskRuntimeRoot 'pg-primary.log') `
        -o "-p $Port -h 127.0.0.1 -c shared_buffers=128MB -c max_connections=100" -w -t 30 start
    if ($LASTEXITCODE -ne 0) { throw 'The dedicated PostgreSQL test instance did not start.' }

    $taskActualDataPath = (Invoke-TestPsql -TargetDatabase postgres `
        -Arguments @('-A', '-t', '-c', 'SHOW data_directory;') | Out-String).Trim()
    if ([IO.Path]::GetFullPath($taskActualDataPath) -ne $taskDataPath) {
        throw 'Started server has an unexpected data directory.'
    }
}

$taskExists = (Invoke-TestPsql -TargetDatabase postgres -Arguments @(
    '-A', '-t', '-c', "SELECT 1 FROM pg_database WHERE datname = '$Database';"
) | Out-String).Trim()
if ($taskExists -ne '1') {
    Invoke-TestPsql -TargetDatabase postgres -Arguments @(
        '-c', "CREATE DATABASE $Database ENCODING 'UTF8' TEMPLATE template0;"
    ) | Out-Host
}

Get-ChildItem -LiteralPath (Join-Path $taskProjectRoot 'sql') -Filter '*.sql' |
    Sort-Object Name | ForEach-Object {
        Invoke-TestPsql -TargetDatabase $Database -Arguments @('-f', $_.FullName) | Out-Host
    }

Invoke-TestPsql -TargetDatabase $Database -Arguments @(
    '-A', '-t', '-c',
    "SELECT json_build_object('version', version(), 'port', inet_server_port(), 'database', current_database(), 'schema_version', (SELECT max(version) FROM pgfs.schema_versions));"
) | Out-Host
