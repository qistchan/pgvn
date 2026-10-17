param(
    [string]$OutputDirectory,
    [string]$RuntimePackageZip
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
foreach ($file in @('LICENSE','NOTICE')) { if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $file) -PathType Leaf)) { throw ('缺少项目许可文件：' + $file) } }
if (-not $OutputDirectory) { $OutputDirectory=Join-Path $PSScriptRoot '../../runtime/pgvn-manager' }
$OutputDirectory=[IO.Path]::GetFullPath($OutputDirectory)
$framework=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319'
$compiler=Join-Path $framework 'csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw '需要 Windows x64 及 .NET Framework 4.8。无需安装 dotnet SDK 或 NuGet 包。' }
$release=(Get-ItemProperty 'HKLM:/SOFTWARE/Microsoft/NET Framework Setup/NDP/v4/Full' -Name Release).Release
if ($release -lt 528040) { throw '请先安装 .NET Framework 4.8 或更新版本。' }
[void][IO.Directory]::CreateDirectory($OutputDirectory)
$versionSource=Join-Path $projectRoot 'build/pgvn-manager/generated/PgVN.Version.cs'
& (Join-Path $PSScriptRoot 'Write-PgVNVersion.ps1') -OutputPath $versionSource
$productVersion=Get-Content -LiteralPath (Join-Path $projectRoot 'src/pgvn/version.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$references=@('System.dll','System.Core.dll','System.Xml.dll','System.Web.Extensions.dll','System.Windows.Forms.dll','System.Xaml.dll') | ForEach-Object { '/reference:' + (Join-Path $framework $_) }
$references += @('PresentationFramework.dll','PresentationCore.dll','WindowsBase.dll') | ForEach-Object { '/reference:' + (Join-Path $framework ('WPF/' + $_)) }
$exe=Join-Path $OutputDirectory 'PgVNManager.exe'
$arguments=@('/nologo','/target:winexe','/platform:x64','/optimize+','/warn:4','/warnaserror+','/codepage:65001',('/out:' + $exe),('/win32manifest:' + [IO.Path]::ChangeExtension($versionSource,'.manifest')),('/resource:' + (Join-Path $PSScriptRoot 'MainWindow.xaml') + ',PgVN.Manager.MainWindow.xaml')) + $references + @($versionSource,(Join-Path $PSScriptRoot 'App.cs'),(Join-Path $PSScriptRoot 'BackendClient.cs'))
& $compiler @arguments
if ($LASTEXITCODE -ne 0) { throw '编译失败。' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'App.config') -Destination ($exe + '.config') -Force
foreach ($file in @('Invoke-PgVNManager.ps1','README.zh-CN.md')) {
    $text=[IO.File]::ReadAllText((Join-Path $PSScriptRoot $file),[Text.Encoding]::UTF8)
    # Windows PowerShell 5.1 otherwise treats a non-BOM script as the system ANSI codepage.
    $fileEncoding=New-Object Text.UTF8Encoding($file.EndsWith('.ps1'))
    [IO.File]::WriteAllText((Join-Path $OutputDirectory $file),$text,$fileEncoding)
}
foreach ($file in @('LICENSE','NOTICE')) { Copy-Item -LiteralPath (Join-Path $projectRoot $file) -Destination (Join-Path $OutputDirectory $file) -Force }
Copy-Item -LiteralPath (Join-Path $projectRoot 'src/pgvn/version.json') -Destination (Join-Path $OutputDirectory 'version.json') -Force
$inputs=[ordered]@{}
$inputs['../../src/pgvn/version.json']=(Get-FileHash -LiteralPath (Join-Path $projectRoot 'src/pgvn/version.json') -Algorithm SHA256).Hash.ToLowerInvariant()
foreach ($file in @('App.cs','BackendClient.cs','MainWindow.xaml','App.config','app.manifest','PgVN.Manager.csproj','Invoke-PgVNManager.ps1','Build-PgVNManager.ps1','Write-PgVNVersion.ps1','README.zh-CN.md')) { $inputs[$file]=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $file) -Algorithm SHA256).Hash.ToLowerInvariant() }
foreach ($file in @('LICENSE','NOTICE')) { $inputs['../../' + $file]=(Get-FileHash -LiteralPath (Join-Path $projectRoot $file) -Algorithm SHA256).Hash.ToLowerInvariant() }
$files=[ordered]@{}
foreach ($file in @('PgVNManager.exe','PgVNManager.exe.config','Invoke-PgVNManager.ps1','version.json','README.zh-CN.md','LICENSE','NOTICE')) { $files[$file]=(Get-FileHash -LiteralPath (Join-Path $OutputDirectory $file) -Algorithm SHA256).Hash.ToLowerInvariant() }
$manifest=[ordered]@{format=1;product='PgVN Manager';version=$productVersion.version;api_version=$productVersion.api_version;schemas=$productVersion.schemas;platform='windows-x64';framework='.NET Framework 4.8';created_utc=[DateTime]::UtcNow.ToString('o');compiler=(Get-Item -LiteralPath $compiler).VersionInfo.FileVersion;sources=$inputs;files=$files}
$encoding=New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $OutputDirectory 'manager-manifest.json'),($manifest | ConvertTo-Json -Depth 6),$encoding)
$archive=$OutputDirectory.TrimEnd('\') + '.zip'
Compress-Archive -LiteralPath @($files.Keys | ForEach-Object { Join-Path $OutputDirectory $_ }) -DestinationPath $archive -Force
# Include build provenance alongside the executable payload.
Compress-Archive -LiteralPath (Join-Path $OutputDirectory 'manager-manifest.json') -DestinationPath $archive -Update
[IO.File]::WriteAllText(($archive + '.sha256'),((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + [IO.Path]::GetFileName($archive) + "`n"),$encoding)
if ($RuntimePackageZip) {
    $RuntimePackageZip=[IO.Path]::GetFullPath($RuntimePackageZip)
    if (-not (Test-Path -LiteralPath $RuntimePackageZip -PathType Leaf)) { throw '运行包 ZIP 不存在。' }
    $combined=$OutputDirectory.TrimEnd('\') + '-bundle-' + [Guid]::NewGuid().ToString('N').Substring(0,8)
    [void][IO.Directory]::CreateDirectory($combined)
    Expand-Archive -LiteralPath $RuntimePackageZip -DestinationPath $combined
    $bundleRoot=$combined
    if (-not (Test-Path -LiteralPath (Join-Path $bundleRoot 'package-manifest.json'))) {
        $candidates=@(Get-ChildItem -LiteralPath $combined -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'package-manifest.json') })
        if ($candidates.Count -ne 1) { throw '运行包必须包含唯一的完整部署根目录。' }
        $bundleRoot=$candidates[0].FullName
    }
    $runtimeManifest=Get-Content -LiteralPath (Join-Path $bundleRoot 'package-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $runtimeIdentity=$runtimeManifest.PSObject.Properties['build_identity']
    if (-not $runtimeIdentity -or $runtimeIdentity.Value.version -cne $productVersion.version -or $runtimeIdentity.Value.api_version -ne $productVersion.api_version -or $runtimeIdentity.Value.schemas.catalog -ne $productVersion.schemas.catalog) { throw '运行包与 GUI 的产品版本、API 或 schema 不匹配；请从同一源码重新构建。' }
    $manager=Join-Path $bundleRoot 'manager'; [void][IO.Directory]::CreateDirectory($manager)
    foreach ($file in @($files.Keys) + @('manager-manifest.json')) { Copy-Item -LiteralPath (Join-Path $OutputDirectory $file) -Destination (Join-Path $manager $file) }
    $bundleZip=$combined + '.zip'
    Compress-Archive -Path (Join-Path $bundleRoot '*') -DestinationPath $bundleZip
    [IO.File]::WriteAllText(($bundleZip + '.sha256'),((Get-FileHash -LiteralPath $bundleZip -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + [IO.Path]::GetFileName($bundleZip) + "`n"),$encoding)
    Write-Output ('完整运行包 + GUI：' + $bundleZip)
}
Write-Output ('GUI：' + $exe)
Write-Output ('GUI 附加包：' + $archive)
