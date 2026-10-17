param([Parameter(Mandatory=$true)][string]$OutputPath)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$version=Get-Content -LiteralPath (Join-Path $projectRoot 'src/pgvn/version.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($version.format -ne 1 -or $version.product -cne 'PgVN' -or $version.version -cnotmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$') { throw 'Invalid shared PgVN version descriptor.' }
$numbers=$version.version.Split('-')[0].Split('.')
foreach ($number in $numbers) { if ([int]$number -gt 65534) { throw 'Product version exceeds .NET assembly version limits.' } }
$assemblyVersion=($numbers -join '.') + '.0'
$OutputPath=[IO.Path]::GetFullPath($OutputPath)
[void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($OutputPath))
$encoding=New-Object Text.UTF8Encoding($false)
$source=@"
// Generated from src/pgvn/version.json. Do not edit.
using System.Reflection;
[assembly: AssemblyVersion("$assemblyVersion")]
[assembly: AssemblyFileVersion("$assemblyVersion")]
[assembly: AssemblyInformationalVersion("$($version.version)")]
"@
[IO.File]::WriteAllText($OutputPath,$source,$encoding)
$manifest=New-Object Xml.XmlDocument
$manifest.Load((Join-Path $PSScriptRoot 'app.manifest'))
$identity=$manifest.SelectSingleNode('/*[local-name()="assembly"]/*[local-name()="assemblyIdentity"]')
$identity.SetAttribute('version',$assemblyVersion)
$settings=New-Object Xml.XmlWriterSettings
$settings.Encoding=$encoding
$writer=[Xml.XmlWriter]::Create([IO.Path]::ChangeExtension($OutputPath,'.manifest'),$settings)
try { $manifest.Save($writer) } finally { $writer.Dispose() }
