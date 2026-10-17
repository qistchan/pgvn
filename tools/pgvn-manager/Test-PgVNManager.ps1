param([Parameter(Mandatory=$true)][string]$BuildDirectory,[Parameter(Mandatory=$true)][string]$RuntimeRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
[void][Reflection.Assembly]::LoadFrom((Join-Path $BuildDirectory 'PgVNManager.exe'))
$backend=New-Object PgVN.Manager.BackendClient((Join-Path $BuildDirectory 'Invoke-PgVNManager.ps1'))
function Call([hashtable]$Values) {
 $dict=New-Object 'System.Collections.Generic.Dictionary[string,object]'
 foreach($key in $Values.Keys){$dict[$key]=[string]$Values[$key]}
 return $backend.Invoke($dict,$null)
}
$state=Call @{action='inspect';root=$RuntimeRoot}
if(-not $state['packageId']){throw 'Missing package identity'}
$modulePath=$env:PSModulePath
try {
    $env:PSModulePath='C:\pgvn-manager-no-inherited-modules'
    $isolated=Call @{action='inspect';root=$RuntimeRoot}
    if($isolated['packageId'] -cne $state['packageId']){throw 'Inherited module path changed package inspection'}
} finally { $env:PSModulePath=$modulePath }
$failed=$false
try{Call @{action='unsupported';root=$RuntimeRoot} > $null}catch{$failed=$true}
if(-not $failed){throw 'Unsupported action accepted'}
$failed=$false
try{Call @{action='inspect';root='C:\not-a-package'} > $null}catch{$failed=$true}
if(-not $failed){throw 'Invalid root accepted'}
$temporary=Join-Path ([IO.Path]::GetTempPath()) ('pgvn-manager-test-' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($temporary)
try {
    # Test the actual Windows argument parser, including quoted paths ending in
    # a backslash. A request password is never passed on this command line.
    $echo=Join-Path $temporary 'arguments.exe'
    Add-Type -TypeDefinition 'using System; public class ManagerArgumentEcho { public static void Main(string[] args) { Console.OutputEncoding=new System.Text.UTF8Encoding(false); Console.Write(new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(args)); } }' -ReferencedAssemblies System.Web.Extensions -OutputAssembly $echo -OutputType ConsoleApplication
    $arguments=@('','plain','C:\space directory\','a"b','x\\"y',([string][char]0x4e2d + ' & $ value'))
    $start=New-Object Diagnostics.ProcessStartInfo
    $start.FileName=$echo; $start.Arguments=($arguments | ForEach-Object { [PgVN.Manager.BackendClient]::Quote($_) }) -join ' '
    $start.UseShellExecute=$false; $start.CreateNoWindow=$true; $start.RedirectStandardOutput=$true
    $start.StandardOutputEncoding=New-Object Text.UTF8Encoding($false)
    $process=[Diagnostics.Process]::Start($start)
    try { $actual=$process.StandardOutput.ReadToEnd() | ConvertFrom-Json; $process.WaitForExit(); if($process.ExitCode -ne 0 -or $actual.Count -ne $arguments.Count){throw 'Argument parser mismatch'}; for($i=0;$i -lt $arguments.Count;$i++){if($actual[$i] -cne $arguments[$i]){throw ('Argument mismatch at ' + $i)}} } finally { $process.Dispose() }
    # Reject a modified helper before importing it. The fixture cannot execute
    # even though its manifest itself has a valid checksum.
    $badRoot=Join-Path $temporary 'bad-runtime'; $deploy=Join-Path $badRoot 'deploy/windows'
    [void][IO.Directory]::CreateDirectory($deploy)
    $utf8=New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText((Join-Path $deploy 'PgVN.Common.ps1'),"throw 'UNTRUSTED_HELPER_EXECUTED'",$utf8)
    $manifest=Join-Path $badRoot 'package-manifest.json'
    [IO.File]::WriteAllText($manifest,(@{files=@{'deploy/windows/PgVN.Common.ps1'=@{sha256=('0' * 64)}}} | ConvertTo-Json -Depth 5),$utf8)
    [IO.File]::WriteAllText(($manifest + '.sha256'),((Get-FileHash -LiteralPath $manifest).Hash.ToLowerInvariant() + '  package-manifest.json'),$utf8)
    $failed=$false
    try { Call @{action='inspect';root=$badRoot} > $null } catch { if($_.Exception.Message -notmatch '部署公共脚本摘要不匹配'){throw}; $failed=$true }
    if(-not $failed){throw 'Modified bootstrap helper accepted'}
    # Supply an intact helper and correctly rehashed fixture manifest. Version,
    # schema, API and capability checks must reject it before state/DB access.
    Copy-Item -LiteralPath (Join-Path $RuntimeRoot 'deploy/windows/PgVN.Common.ps1') -Destination (Join-Path $deploy 'PgVN.Common.ps1') -Force
    $sourceManifest=Get-Content -LiteralPath (Join-Path $RuntimeRoot 'package-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $helperHash=(Get-FileHash -LiteralPath (Join-Path $deploy 'PgVN.Common.ps1') -Algorithm SHA256).Hash.ToLowerInvariant()
    foreach ($case in @('version','api','schema','capability','capability_type','missing')) {
        $identity=$sourceManifest.build_identity | ConvertTo-Json -Depth 14 | ConvertFrom-Json
        switch ($case) {
            'version' { $identity.version='999.0.0' }
            'api' { $identity.api_version=999 }
            'schema' { $identity.schemas.catalog=999 }
            'capability' { $identity.capabilities.repository_by_id=$false }
            'capability_type' { $identity.capabilities.monitor=$true }
        }
        $fixture=@{files=@{'deploy/windows/PgVN.Common.ps1'=@{sha256=$helperHash}};build_identity=$identity}
        if ($case -eq 'missing') { $fixture.Remove('build_identity') }
        [IO.File]::WriteAllText($manifest,($fixture | ConvertTo-Json -Depth 14),$utf8)
        [IO.File]::WriteAllText(($manifest + '.sha256'),((Get-FileHash -LiteralPath $manifest).Hash.ToLowerInvariant() + '  package-manifest.json'),$utf8)
        $failed=$false
        try { Call @{action='inspect';root=$badRoot} > $null } catch { if($_.Exception.Message -notmatch '版本|能力|schema|API'){throw}; $failed=$true }
        if(-not $failed){throw ('Incompatible backend accepted: ' + $case)}
    }
} finally {
    $resolved=[IO.Path]::GetFullPath($temporary)
    if(-not $resolved.StartsWith([IO.Path]::GetTempPath(),[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notmatch '^pgvn-manager-test-[0-9a-f]{32}$'){throw 'Unsafe temporary cleanup target'}
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
$assembly=[Reflection.Assembly]::LoadFrom((Join-Path $BuildDirectory 'PgVNManager.exe'))
$productVersion=Get-Content -LiteralPath (Join-Path $BuildDirectory 'version.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$informational=@($assembly.GetCustomAttributes([Reflection.AssemblyInformationalVersionAttribute],$false))[0].InformationalVersion
if($informational -cne $productVersion.version){throw 'GUI assembly/product version mismatch'}
Add-Type -AssemblyName PresentationFramework
$stream=$assembly.GetManifestResourceStream('PgVN.Manager.MainWindow.xaml')
try{$window=[Windows.Markup.XamlReader]::Load($stream);if($window.FindName('Navigation').Items.Count -ne 6){throw 'Navigation incomplete'};$window.Close()}finally{$stream.Dispose()}
@{status='PASS';scope='Package identity, version/API/schema/capability rejection, generated GUI version, inherited module isolation, allowlist, invalid target, bootstrap integrity before execution, WPF XAML and actual Windows argument parsing';package=$state['packageId']} | ConvertTo-Json
