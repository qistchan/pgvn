param(
    [string]$Root,
    [string]$RepositoryRoute = '/svn/demo',
    [string]$UserName,
    [Security.SecureString]$Password,
    [ValidateRange(1,120)][int]$ReadyTimeoutSeconds = 30
)
. (Join-Path $PSScriptRoot 'PgVN.Common.ps1')
if (-not $Root) { $Root = Get-PgVNRoot }
$Root = [IO.Path]::GetFullPath($Root)
$manifest = Assert-PgVNPackage $Root
$settings = Get-Content -LiteralPath (Join-Path $Root 'admin/deployment.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$state = Get-Content -LiteralPath (Join-Path $Root 'admin/services.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($settings.Root -ne $Root -or $state.Root -ne $Root -or $state.Uninstalled) { throw '本目录没有有效安装记录。' }
if ($RepositoryRoute -notmatch '^/svn/[^\s?#]+$' -or $RepositoryRoute -match '(^|/)\.\.(/|$)') { throw '仓库路由应为 /svn/ 下的绝对 URL 路径，不含查询、片段或上级目录。' }
$services = @(,@($state.ServiceName,$state.ServerImage))
if ($state.DavInstalled) { $services += ,@($state.DavServiceName,$state.DavImage) }
foreach ($entry in $services) {
    Assert-PgVNServiceOwner $entry[0] $entry[1]
    $controller = New-Object ServiceProcess.ServiceController($entry[0])
    try { if ($controller.Status -ne [ServiceProcess.ServiceControllerStatus]::Running) { throw "服务未运行：$($entry[0])" } }
    finally { $controller.Dispose() }
}
$base = [uri]$settings.MonitorUrl
if ($base.Scheme -ne 'http' -or $base.Host -ne '127.0.0.1' -or $base.AbsolutePath -ne '/') { throw '监控地址须为登记的本机回环地址。' }
Add-Type -AssemblyName System.Net.Http
$handler = New-Object Net.Http.HttpClientHandler
$handler.UseProxy = $false
$http = New-Object Net.Http.HttpClient($handler)
$http.Timeout = [TimeSpan]::FromSeconds(3)
try {
    $live = $http.GetStringAsync([uri]::new($base,'health/live')).GetAwaiter().GetResult() | ConvertFrom-Json
    if (-not $live.live) { throw '服务存活探针未通过。' }
    $deadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
    $ready = $false
    do {
        try {
            $value = $http.GetStringAsync([uri]::new($base,'health/ready')).GetAwaiter().GetResult() | ConvertFrom-Json
            $ready = [bool]$value.ready
        } catch { $ready = $false }
        if (-not $ready) { Start-Sleep -Milliseconds 500 }
    } while (-not $ready -and [DateTime]::UtcNow -lt $deadline)
    if (-not $ready) { throw '业务就绪探针未通过。' }
    $page = $http.GetStringAsync([uri]::new($base,'monitor/')).GetAwaiter().GetResult()
    if ($page -notmatch '<html' -or $page -notmatch 'monitor.js') { throw '内嵌监控页面不可读取。' }
} finally { $http.Dispose(); $handler.Dispose() }
$protocols = @()
if ($UserName -or $Password) {
    if (-not $UserName -or -not $Password) { throw '协议验证需要同时提供用户名和 SecureString 密码。' }
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        $urls = @("svn://127.0.0.1:$($settings.SvnPort)$RepositoryRoute")
        if ($state.DavInstalled) { $urls += "http://127.0.0.1:$($settings.HttpPort)$RepositoryRoute" }
        foreach ($url in $urls) {
            $result = Invoke-PgVNNative -FilePath (Join-Path $Root 'tools/svn/svn.exe') -Arguments @(
                'info','--xml','--non-interactive','--no-auth-cache','--username',$UserName,'--password-from-stdin',$url) -InputText ($plain + "`r`n")
            $info = [xml]$result.Output
            if (-not $info.info.entry.repository.uuid) { throw '协议返回缺少 SVN UUID。' }
            $protocols += [ordered]@{ Url=$url; Uuid=[string]$info.info.entry.repository.uuid; Revision=[long]$info.info.entry.revision }
        }
        if (@($protocols | ForEach-Object { $_.Uuid } | Select-Object -Unique).Count -ne 1) { throw 'SVN 与 DAV 没有访问同一公开仓库。' }
    } finally { $plain = $null; [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}
[ordered]@{ Status='PASS'; PackageId=$manifest.package_id; Live=$true; Ready=$true; EmbeddedPage=$true;
    Protocols=$protocols; Scope='Read-only local service, health and optional repository checks; reboot and clean-machine acceptance are separate' } | ConvertTo-Json -Depth 6
