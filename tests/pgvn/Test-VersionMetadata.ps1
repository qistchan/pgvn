param([Parameter(Mandatory=$true)][string]$WorkDirectory)
# Portable PowerShell component test. Windows PowerShell 5.1/WPF and complete
# Windows runtime acceptance remain separate gates.
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$managerRoot=Join-Path $projectRoot 'tools/pgvn-manager'
$productVersion=Get-Content -LiteralPath (Join-Path $projectRoot 'src/pgvn/version.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$checks=New-Object 'System.Collections.Generic.List[string]'
foreach ($name in @('Build-PgVNManager.ps1','Write-PgVNVersion.ps1','Invoke-PgVNManager.ps1','Test-PgVNManager.ps1')) {
    $tokens=$null; $parseErrors=$null
    [void][Management.Automation.Language.Parser]::ParseFile((Join-Path $managerRoot $name),[ref]$tokens,[ref]$parseErrors)
    if ($parseErrors.Count -ne 0) { throw ($parseErrors | Out-String) }
    $checks.Add('parse ' + $name)
}
if (Test-Path -LiteralPath $WorkDirectory) { throw 'Use a new component-test work directory.' }
[void][IO.Directory]::CreateDirectory($WorkDirectory)
$generated=Join-Path $WorkDirectory 'PgVN.Version.cs'
& (Join-Path $managerRoot 'Write-PgVNVersion.ps1') -OutputPath $generated
$assemblyPath=Join-Path $WorkDirectory 'PgVN.Version.dll'
Add-Type -Path $generated -OutputAssembly $assemblyPath
$assembly=[Reflection.Assembly]::LoadFile([IO.Path]::GetFullPath($assemblyPath))
$assemblyVersion=$productVersion.version.Split('-')[0] + '.0'
if ($assembly.GetName().Version.ToString() -cne $assemblyVersion) { throw 'Generated assembly version mismatch.' }
$informational=@($assembly.GetCustomAttributes([Reflection.AssemblyInformationalVersionAttribute],$false))[0].InformationalVersion
if ($informational -cne $productVersion.version) { throw 'Generated informational version mismatch.' }
$checks.Add('generated assembly and product version')
$manifest=New-Object Xml.XmlDocument
$manifest.Load([IO.Path]::ChangeExtension($generated,'.manifest'))
if ($manifest.SelectSingleNode('/*[local-name()="assembly"]/*[local-name()="assemblyIdentity"]').GetAttribute('version') -cne $assemblyVersion) { throw 'Generated process manifest version mismatch.' }
$checks.Add('generated process manifest version')

# Load only the production compatibility function, without running the Windows
# backend, importing deployment helpers, or starting a database/service.
$tokens=$null; $parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $managerRoot 'Invoke-PgVNManager.ps1'),[ref]$tokens,[ref]$parseErrors)
$definitions=@()
foreach ($name in @('Read-Json','Assert-BackendCompatibility','Merge-SvnAuthzSettings')) {
    $function=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name},$true)
    if (-not $function) { throw ('Missing production function: ' + $name) }
    $definitions += $function.Extent.Text
}
$componentPath=Join-Path $WorkDirectory 'compatibility.ps1'
$encoding=New-Object Text.UTF8Encoding($true)
[IO.File]::WriteAllText($componentPath,($definitions -join "`n"),$encoding)
Copy-Item -LiteralPath (Join-Path $projectRoot 'src/pgvn/version.json') -Destination (Join-Path $WorkDirectory 'version.json')
# Loading a real script preserves $PSScriptRoot, just as the installed backend.
. $componentPath
$identity=[pscustomobject]@{identity_format=1;product='PgVN';version=$productVersion.version;api_version=$productVersion.api_version;target_os='windows';target_arch='x86_64';schemas=$productVersion.schemas;capabilities=$productVersion.capabilities}
Assert-BackendCompatibility ([pscustomobject]@{build_identity=$identity})
$checks.Add('matching backend accepted')
foreach ($case in @('version','api','schema','fs','platform','architecture','capability','capability_type','missing')) {
    $invalid=$identity | ConvertTo-Json -Depth 14 | ConvertFrom-Json
    switch ($case) {
        'version' { $invalid.version='999.0.0' }
        'api' { $invalid.api_version=999 }
        'schema' { $invalid.schemas.catalog=999 }
        'fs' { $invalid.schemas.fs=999 }
        'platform' { $invalid.target_os='linux' }
        'architecture' { $invalid.target_arch='aarch64' }
        'capability' { $invalid.capabilities.repository_by_id=$false }
        'capability_type' { $invalid.capabilities.monitor=$true }
    }
    $package=[pscustomobject]@{build_identity=$invalid}
    if ($case -eq 'missing') { $package=[pscustomobject]@{} }
    $rejected=$false
    try { Assert-BackendCompatibility $package } catch { $rejected=$true }
    if (-not $rejected) { throw ('Incompatible backend accepted: ' + $case) }
    $checks.Add('incompatible backend rejected: ' + $case)
}
$original='{"identity_domain":"domain","anonymous_access":"read","authenticated_access":"none","svn_authz_mode":"enforce"}' | ConvertFrom-Json
$merged=Merge-SvnAuthzSettings $original 'bypass'
if ($merged.identity_domain -cne 'domain' -or $merged.authenticated_access -cne 'none' -or $merged.anonymous_access -cne 'none' -or $merged.svn_authz_mode -cne 'bypass') { throw 'Mode publication did not preserve settings or disable anonymous access.' }
if ($original.anonymous_access -cne 'read' -or $original.svn_authz_mode -cne 'enforce') { throw 'Mode merge mutated the read snapshot.' }
$checks.Add('bypass settings preserve domain/access and require authentication')
$checks.Add('mode merge preserves the original read snapshot')
$restored=Merge-SvnAuthzSettings ($merged | ConvertTo-Json | ConvertFrom-Json) 'enforce'
if ($restored.svn_authz_mode -cne 'enforce' -or $restored.anonymous_access -cne 'none') { throw 'Enforce restore unexpectedly opened anonymous access.' }
$checks.Add('enforce restore keeps anonymous access closed')
$rejected=$false
try { Merge-SvnAuthzSettings $original 'unknown' | Out-Null } catch { $rejected=$true }
if (-not $rejected) { throw 'Unknown SVN authorization mode accepted.' }
$checks.Add('unknown SVN mode rejected before publication')
@{status='PASS';scope='Portable version generation and backend compatibility component; Windows W1/W2 acceptance not run';powershell=$PSVersionTable.PSVersion.ToString();checks=@($checks)} | ConvertTo-Json -Depth 4
