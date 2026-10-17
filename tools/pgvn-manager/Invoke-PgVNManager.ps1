param([switch]$RequestStdIn)
# The desktop process sends one UTF-8 JSON request over stdin. Never accept SQL,
# command lines or secrets from arguments, and never persist the request.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# A desktop launched by PowerShell 7 can inherit its incompatible module path.
# Load this Windows PowerShell process's own modules before the bootstrap hash
# checks, which deliberately run before importing the selected runtime helper.
foreach ($moduleName in @('Microsoft.PowerShell.Management','Microsoft.PowerShell.Utility','Microsoft.PowerShell.Security')) {
    Import-Module -Name ([IO.Path]::Combine($PSHOME,'Modules',$moduleName,($moduleName + '.psd1'))) -ErrorAction Stop
}
[Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$script:secrets = New-Object 'System.Collections.Generic.List[string]'
$mutex = $null; $locked = $false
function Protect-Text([string]$Text) {
    foreach ($secret in $script:secrets) {
        if ($secret.Length -gt 0) {
            $Text = $Text.Replace($secret,'[REDACTED]').Replace($secret.Replace("'","''"),'[REDACTED]')
        }
    }
    $Text = [regex]::Replace($Text,'(?i)(password\s*[=:]\s*)(?:''(?:''''|[^''])*''|"[^"]*"|\S+)','$1[REDACTED]')
    return $Text
}
function Emit([string]$Kind, $Data) {
    if ($Data -is [string]) { $Data=Protect-Text $Data }
    $json = @{kind=$Kind; data=$Data} | ConvertTo-Json -Depth 14 -Compress
    foreach ($secret in $script:secrets) {
        if ($secret.Length -gt 0) {
            $escaped=ConvertTo-Json -InputObject $secret -Compress
            $json=$json.Replace($escaped.Substring(1,$escaped.Length-2),'[REDACTED]')
        }
    }
    [Console]::Out.WriteLine('PGVN-MANAGER/1 ' + $json)
}
function Note([string]$Text) { Emit 'log' $Text }
function Field([string]$Name,[string]$Default='') {
    $p = $script:request.PSObject.Properties[$Name]
    if ($null -eq $p -or $null -eq $p.Value) { return $Default }
    return [string]$p.Value
}
function Required([string]$Name) {
    $value = Field $Name
    if ([string]::IsNullOrWhiteSpace($value) -or $value -match '[\r\n\x00]') { throw "请填写有效的 $Name。" }
    return $value
}
function Identifier([string]$Value) {
    if ($Value -cnotmatch '^[a-z][a-z0-9_]{0,62}$') { throw '数据库及角色名称须以小写字母开头，仅含小写字母、数字、下划线，最多 63 字符。' }
    return '"' + $Value + '"'
}
function Literal([string]$Value) { return "'" + $Value.Replace("'","''") + "'" }
function Read-Json([string]$Path) { return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json) }
function Assert-PlainPath([string]$Path) {
    $item = $Path
    while ($item) {
        if ((Test-Path -LiteralPath $item) -and ((Get-Item -LiteralPath $item -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw '部署路径不能经过符号链接或目录联接。' }
        $item = [IO.Path]::GetDirectoryName($item)
    }
}
function Assert-Bootstrap([string]$Root,[string]$Common) {
    # Verify the helper before executing any code from the selected package.
    # These checks establish integrity against the supplied manifest, not a
    # publisher signature; obtain runtime archives from a trusted source.
    $manifestPath=Join-Path $Root 'package-manifest.json'
    Assert-PlainPath $manifestPath; Assert-PlainPath ($manifestPath + '.sha256'); Assert-PlainPath $Common
    $expected=(Get-Content -LiteralPath ($manifestPath + '.sha256') -Raw).Split(' ')[0].Trim().ToLowerInvariant()
    if ($expected -cnotmatch '^[0-9a-f]{64}$' -or (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expected) { throw '包清单摘要不匹配。' }
    $manifest=Read-Json $manifestPath
    $entry=$manifest.files.PSObject.Properties['deploy/windows/PgVN.Common.ps1']
    if (-not $entry -or $entry.Value.sha256 -cnotmatch '^[0-9a-f]{64}$' -or (Get-FileHash -LiteralPath $Common -Algorithm SHA256).Hash.ToLowerInvariant() -cne $entry.Value.sha256) { throw '部署公共脚本摘要不匹配。' }
}
function Validate-Settings {
    $script:hostName = Required 'host'
    if ($script:hostName -notmatch '^[A-Za-z0-9.:-]+$') { throw '主机应为单个 DNS 名称或 IP 地址。' }
    $script:database = Required 'database'; [void](Identifier $script:database)
    $script:prefix = Required 'rolePrefix'
    if ($script:prefix -cnotmatch '^[a-z][a-z0-9_]{0,45}$') { throw '角色前缀只接受 1–46 位小写字母、数字及下划线。' }
    $script:dbPort = 0; $script:svnPort = 0; $script:monitorPort = 0; $script:httpPort = 0
    foreach ($entry in @(@('dbPort',1),@('svnPort',1024),@('monitorPort',1024),@('httpPort',1024))) {
        $v = 0
        if (-not [int]::TryParse((Field $entry[0]),[ref]$v) -or $v -lt $entry[1] -or $v -gt 65535) { throw "端口无效：$($entry[0])。" }
        Set-Variable -Scope Script -Name $entry[0] -Value $v
    }
    if (@($script:svnPort,$script:monitorPort,$script:httpPort | Select-Object -Unique).Count -ne 3) { throw 'SVN、监控及 HTTP 端口不能重复。' }
    $script:node = Required 'node'
    if ($script:node -notmatch '^[A-Za-z0-9_.-]{1,80}$') { throw '节点 ID 格式无效。' }
    $script:ssl = Required 'sslMode'
    if ($script:ssl -notin @('disable','require','verify-full')) { throw '不支持的 SSL 模式。' }
    if ($script:ssl -eq 'disable' -and $script:hostName -notin @('127.0.0.1','localhost','::1')) { throw '远程 PostgreSQL 连接请选择 require 或 verify-full。' }
    $script:ca = Field 'caFile'
    if ($script:ca -match '[\r\n\x00]') { throw 'CA 文件路径无效。' }
    if ($script:ssl -eq 'verify-full' -and -not (Test-Path -LiteralPath $script:ca -PathType Leaf)) { throw 'verify-full 需要有效的 PostgreSQL CA 文件。' }
}
function Roles { return @('migrate','admin','service','monitor','restore') }
function Role([string]$Kind) { return $script:prefix + '_' + $Kind }
function Admin([string[]]$Arguments,[string]$Profile='admin',[AllowNull()][string]$InputText=$null,[int]$Timeout=120) {
    $result = Invoke-PgVNNative -FilePath (Join-Path $script:root 'bin/pgvnadmin.exe') -Arguments (@('--config',(Join-Path $script:root ('admin/' + $Profile + '.ini'))) + $Arguments) -InputText $InputText -Environment (Get-PgVNLibpqEnvironment $script:root) -TimeoutSeconds $Timeout
    return $result.Output
}
function Sql([string]$Text,[switch]$Dba) {
    $envs = Get-PgVNLibpqEnvironment $script:root
    if ($Dba) {
        $envs.PGHOST=$script:hostName; $envs.PGPORT=[string]$script:dbPort
        $envs.PGUSER=Required 'dbaUser'; $envs.PGDATABASE=Field 'maintenanceDatabase' 'postgres'
        $envs.PGPASSWORD=Field 'dbaPassword'; $envs.PGSSLMODE=$script:ssl; $envs.PGCONNECT_TIMEOUT='8'
        if ($script:ca) { $envs.PGSSLROOTCERT=$script:ca }
        $db = $envs.PGDATABASE
    } else { $db = 'service=pgvn_migrate' }
    return (Invoke-PgVNNative -FilePath (Join-Path $script:root 'tools/postgresql/psql.exe') -Arguments @('-X','-w','-A','-t','--set=ON_ERROR_STOP=1','--dbname',$db,'-f','-') -InputText $Text -Environment $envs).Output.Trim()
}
function Deployment-Script([string]$Name,[hashtable]$Parameters=@{}) {
    # Keep the signed-off deployment implementation and its ownership checks.
    & (Join-Path $script:root ('deploy/windows/' + $Name)) @Parameters | ForEach-Object { if ($null -ne $_) { Note ([string]$_) } }
}
function Service-State {
    $path = Join-Path $script:root 'admin/services.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $state = Read-Json $path
    if ($state.Root -ine $script:root) { throw '服务登记不属于此部署目录。' }
    return $state
}
function Service-Entries($State) {
    $entries = @(,[pscustomobject]@{name=$State.ServiceName;image=$State.ServerImage})
    if ($State.DavInstalled) { $entries += [pscustomobject]@{name=$State.DavServiceName;image=$State.DavImage} }
    return $entries
}
function Assert-Stopped {
    $state = Service-State
    if ($null -ne $state) {
        foreach ($entry in (Service-Entries $state)) {
            Assert-PgVNServiceOwner $entry.name $entry.image
            if (Get-PgVNServiceImage $entry.name) {
                $svc = New-Object ServiceProcess.ServiceController($entry.name)
                try { if ($svc.Status -ne 'Stopped') { throw '请先停止本目录的全部服务。' } } finally { $svc.Dispose() }
            }
        }
    }
}
function Read-State {
    $state = [ordered]@{root=$script:root;packageId=$script:manifest.package_id;configured=$false;initialized=$false;services=@();settings=$null;serviceName='PgVN';davServiceName='PgVNDAV';withDav=$true}
    $path = Join-Path $script:root 'admin/deployment.json'
    if (Test-Path -LiteralPath $path) {
        $d = Read-Json $path
        if ($d.Root -ine $script:root) { throw '配置登记属于另一个部署目录，请使用原目录或正式迁移。' }
        $state.configured=$true
        $ini = Join-Path $script:root 'config.local/pgvnserve.ini'
        $monitor = ([uri]$d.MonitorUrl).Port
        $runtime = Get-Content -LiteralPath (Join-Path $script:root 'config.local/pg_service.runtime.conf') -Raw -Encoding UTF8
        $rolePrefix='pgvn'
        if ($runtime -match '(?m)^user\s*=\s*([a-z][a-z0-9_]*)_service\s*$') { $rolePrefix=$Matches[1] }
        $caFile=''
        if ($runtime -match '(?m)^sslrootcert\s*=\s*(.*?)\s*$') { $caFile=$Matches[1] }
        $state.settings=@{host=$d.DatabaseHost;dbPort=[string]$d.DatabasePort;database=$d.DatabaseName;node=$d.NodeId;svnPort=[string]$d.SvnPort;monitorPort=[string]$monitor;httpPort=[string]$d.HttpPort;sslMode=$d.SslMode;rolePrefix=$rolePrefix;caFile=$caFile}
        $state.initialized=Test-Path -LiteralPath (Join-Path $script:root 'admin/manager-initialized.json')
        if ($state.initialized) { $state.initialized=(Read-Json (Join-Path $script:root 'admin/manager-initialized.json')).Database -ceq $d.DatabaseName }
    }
    $services = Service-State
    if ($services) {
        $state.serviceName=$services.ServiceName; $state.davServiceName=$services.DavServiceName; $state.withDav=[bool]$services.DavInstalled
        foreach ($entry in (Service-Entries $services)) {
            Assert-PgVNServiceOwner $entry.name $entry.image
            $status='未安装'
            if (Get-PgVNServiceImage $entry.name) {
                $svc=New-Object ServiceProcess.ServiceController($entry.name)
                try { $status=[string]$svc.Status } finally { $svc.Dispose() }
            }
            $state.services += @{name=$entry.name;status=$status}
        }
    }
    return $state
}
function Pass-Escape([string]$Value) { return $Value.Replace('\','\\').Replace(':','\:') }
function Write-Profiles([hashtable]$Passwords) {
    $slashRoot=$script:root.Replace('\','/')
    $cert=if ($script:ca) { $script:ca.Replace('\','/') } else { $slashRoot + '/config.local/postgresql-ca.pem' }
    $head="host=$script:hostName`nport=$script:dbPort`nsslmode=$script:ssl`nsslrootcert=$cert`nconnect_timeout=8`n"
    $adminText=''; $runtimeText=''; $adminPass=''; $runtimePass=''
    foreach ($kind in (Roles)) {
        $db=if ($kind -eq 'restore') { $script:database + '_restore' } else { $script:database }
        $profile='pgvn_' + $kind
        $text="[$profile]`n$head" + "dbname=$db`nuser=$(Role $kind)`n`n"
        if ($kind -in @('service','monitor')) { $runtimeText += $text } else { $adminText += $text }
        if ($kind -eq 'admin') { $adminText += "[pgvn_backup]`n$head" + "dbname=$db`nuser=$(Role $kind)`n`n" }
        if ($Passwords.ContainsKey($kind)) {
            $line=((@($script:hostName,[string]$script:dbPort,$db,(Role $kind),$Passwords[$kind]) | ForEach-Object { Pass-Escape $_ }) -join ':') + "`n"
            if ($kind -in @('service','monitor')) { $runtimePass += $line } else { $adminPass += $line }
        }
    }
    Write-PgVNUtf8 (Join-Path $script:root 'admin/pg_service.conf') $adminText
    Write-PgVNUtf8 (Join-Path $script:root 'config.local/pg_service.runtime.conf') $runtimeText
    if ($Passwords.Count -gt 0) {
        Write-PgVNUtf8 (Join-Path $script:root 'admin/secrets/pgpass.conf') $adminPass
        Write-PgVNUtf8 (Join-Path $script:root 'secrets/runtime.pgpass.conf') $runtimePass
    }
}
function Initialize-Catalog {
    Note '初始化数据库结构及最小权限……'
    Note (Admin @('db','init','--json') 'migrate')
    Note (Admin @('db','grant','--admin-role',(Role 'admin'),'--service-role',(Role 'service'),'--monitor-role',(Role 'monitor'),'--json') 'migrate')
    $key=Join-Path $script:root 'secrets/credentials-v1.key'
    if (-not (Test-Path -LiteralPath $key)) { Note (Admin @('key','create',$key,'deployment-v1')) }
    Set-PgVNKeyPermissions $key
    Note (Admin @('db','check','--json'))
    Write-PgVNUtf8 (Join-Path $script:root 'admin/manager-initialized.json') (@{Database=$script:database;CompletedUtc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json)
}
try {
    if (-not $RequestStdIn) { throw '仅支持 -RequestStdIn 协议。' }
    $raw=[Console]::In.ReadToEnd()
    if ($raw.Length -gt 131072) { throw '请求过大。' }
    $script:request=$raw | ConvertFrom-Json; $raw=$null
    foreach ($prop in $script:request.PSObject.Properties) { if ($prop.Name -match '(?i)password') { $script:secrets.Add([string]$prop.Value) } }
    $action=Required 'action'
    $allowed=@('inspect','connect','provision','initialize','configure','services.install','services.start','services.stop','services.restart','services.uninstall','health','db.check','repo.list','repo.create','repo.enable','repo.disable','repo.verify','repo.dump','repo.load','user.set','authz.read','authz.publish','backup','logs')
    if ($action -notin $allowed) { throw '不支持的操作。' }
    $script:root=[IO.Path]::GetFullPath((Required 'root')).TrimEnd('\')
    Assert-PlainPath $script:root
    foreach ($folder in @('admin','config.local','secrets','state','logs','deploy/windows')) { Assert-PlainPath (Join-Path $script:root $folder) }
    $common=Join-Path $script:root 'deploy/windows/PgVN.Common.ps1'
    if (-not (Test-Path -LiteralPath $common -PathType Leaf)) { throw '选择完整运行包的根目录（包含 bin、deploy、package-manifest.json）。' }
    Assert-Bootstrap $script:root $common
    . $common
    $script:manifest=Assert-PgVNPackage $script:root
    $sha=[Security.Cryptography.SHA256]::Create()
    try { $hash=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($script:root.ToLowerInvariant()))).Replace('-','') } finally { $sha.Dispose() }
    $mutex=New-Object Threading.Mutex($false,('Global\PgVN.Manager.' + $hash))
    try { $locked=$mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked=$true }
    if (-not $locked) { throw '另一个管理操作正在处理此目录，请稍后重试。' }
    if ($action -notin @('inspect','connect')) { Assert-PgVNAdministrator }
    $result=$null
    switch ($action) {
        'inspect' { $result=Read-State }
        'connect' {
            Validate-Settings
            $result=(Sql "SELECT json_build_object('database',current_database(),'user',current_user,'server',current_setting('server_version'),'canCreateDatabase',rolcreatedb OR rolsuper,'canCreateRole',rolcreaterole OR rolsuper) FROM pg_roles WHERE rolname=current_user;" -Dba) | ConvertFrom-Json
        }
        'provision' {
            Validate-Settings; Assert-Stopped
            if (Test-Path -LiteralPath (Join-Path $script:root 'admin/deployment.json')) { throw '目录已有配置。请加载已有配置，或将运行包解压到新的空目录后初始化。' }
            $roleNames=@(Roles | ForEach-Object { Role $_ })
            $collisions=(Sql ("SELECT json_build_object('database',EXISTS(SELECT FROM pg_database WHERE datname=" + (Literal $script:database) + "),'roles',(SELECT count(*) FROM pg_roles WHERE rolname IN (" + (($roleNames | ForEach-Object { Literal $_ }) -join ',') + "))); ") -Dba) | ConvertFrom-Json
            if ($collisions.database -or $collisions.roles -ne 0) { throw '数据库或角色前缀已存在。未修改现有数据库或账号；请选择新的数据库名与角色前缀。' }
            $capabilities=(Sql "SELECT json_build_object('db',rolcreatedb OR rolsuper,'role',rolcreaterole OR rolsuper) FROM pg_roles WHERE rolname=current_user;" -Dba) | ConvertFrom-Json
            if (-not $capabilities.db -or -not $capabilities.role) { throw 'DBA 账号需要 CREATEDB 及 CREATEROLE 权限。' }
            Deployment-Script 'Initialize-PgVNConfiguration.ps1' @{Root=$script:root;DatabaseHost=$script:hostName;DatabasePort=$script:dbPort;DatabaseName=$script:database;NodeId=$script:node;SvnPort=$script:svnPort;MonitorPort=$script:monitorPort;HttpPort=$script:httpPort}
            $passwords=@{}; $rng=[Security.Cryptography.RandomNumberGenerator]::Create()
            try { foreach ($kind in (Roles)) { $bytes=New-Object byte[] 32; $rng.GetBytes($bytes); $passwords[$kind]=[Convert]::ToBase64String($bytes); $script:secrets.Add($passwords[$kind]); [Array]::Clear($bytes,0,$bytes.Length) } } finally { $rng.Dispose() }
            Write-Profiles $passwords
            $d=Read-Json (Join-Path $script:root 'admin/deployment.json'); $d.SslMode=$script:ssl
            Write-PgVNUtf8 (Join-Path $script:root 'admin/deployment.json') ($d | ConvertTo-Json)
            $sql="BEGIN;`n"
            foreach ($kind in (Roles)) { $sql += 'CREATE ROLE ' + (Identifier (Role $kind)) + ' LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD ' + (Literal $passwords[$kind]) + ";`n" }
            $sql += "COMMIT;`nCREATE DATABASE " + (Identifier $script:database) + ' OWNER ' + (Identifier (Role 'migrate')) + " ENCODING 'UTF8' TEMPLATE template0;`n"
            $sql += 'REVOKE ALL ON DATABASE ' + (Identifier $script:database) + " FROM PUBLIC;`nGRANT CONNECT ON DATABASE " + (Identifier $script:database) + ' TO ' + (($roleNames | ForEach-Object { Identifier $_ }) -join ',') + ";`n"
            Note '创建专用角色及数据库；密码仅写入受保护的密码文件。'
            [void](Sql $sql -Dba); $sql=$null; $passwords.Clear()
            Initialize-Catalog; $result=Read-State
        }
        'initialize' {
            Validate-Settings; Assert-Stopped
            $d=Read-Json (Join-Path $script:root 'admin/deployment.json')
            if ($d.DatabaseName -cne $script:database -or $d.DatabaseHost -cne $script:hostName -or [string]$d.DatabasePort -cne [string]$script:dbPort) { throw '先重新加载磁盘上的配置，再继续初始化。' }
            $actual=(Read-State).settings.rolePrefix
            if ($actual -cne $script:prefix) { throw '角色前缀与磁盘配置不同。' }
            Initialize-Catalog; $result=Read-State
        }
        'configure' {
            Validate-Settings; Assert-Stopped
            $state=Read-State
            if (-not $state.configured) { throw '先执行首次初始化。' }
            if ($script:prefix -cne $state.settings.rolePrefix) { throw '已有部署不允许通过保存配置重命名角色。' }
            if ($script:database -cne $state.settings.database -or $script:hostName -cne $state.settings.host -or [string]$script:dbPort -cne $state.settings.dbPort) { throw '切换数据库地址需要更新角色密码文件。请使用新的运行目录初始化，或按部署指南在受保护的连接文件中迁移，再重新加载。' }
            $services=Service-State
            if ($services -and -not $services.Uninstalled -and [string]$script:monitorPort -cne $state.settings.monitorPort) { throw '修改监控端口前请卸载本目录服务，保存配置后重新安装。' }
            $snapshot=Join-Path $script:root ('admin/config-backup-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff'))
            [void][IO.Directory]::CreateDirectory($snapshot); Set-PgVNProtectedDirectory $snapshot
            foreach ($relative in @('config.local/pgvnserve.ini','config.local/pgvn-apache.ini','config.local/httpd.conf','config.local/pg_service.runtime.conf','admin/pg_service.conf','admin/deployment.json')) {
                [IO.File]::Copy((Join-Path $script:root $relative),(Join-Path $snapshot ($relative.Replace('/','_'))),$false)
            }
            Write-Profiles @{}
            foreach ($file in @('pgvnserve.ini','pgvn-apache.ini')) { Set-PgVNIniValue (Join-Path $script:root ('config.local/' + $file)) 'node-id' $script:node }
            Set-PgVNIniValue (Join-Path $script:root 'config.local/pgvnserve.ini') 'listen-port' ([string]$script:svnPort)
            $http=Join-Path $script:root 'config.local/httpd.conf'
            $text=Get-Content -LiteralPath $http -Raw -Encoding UTF8
            $text=[regex]::Replace($text,'(?m)^Listen\s+127\.0\.0\.1:\d+\s*$','Listen 127.0.0.1:' + $script:httpPort)
            $text=[regex]::Replace($text,'(?m)^ServerName\s+localhost:\d+\s*$','ServerName localhost:' + $script:httpPort)
            Write-PgVNUtf8 $http $text
            $d=Read-Json (Join-Path $script:root 'admin/deployment.json'); $d.NodeId=$script:node; $d.SvnPort=$script:svnPort; $d.HttpPort=$script:httpPort; $d.SslMode=$script:ssl; $d.MonitorUrl="http://127.0.0.1:$script:monitorPort/"
            Write-PgVNUtf8 (Join-Path $script:root 'admin/deployment.json') ($d | ConvertTo-Json)
            Note ('配置已保存；原配置保存在 ' + $snapshot); $result=Read-State
        }
        'services.install' {
            Deployment-Script 'Install-PgVNServices.ps1' @{Root=$script:root;ServiceName=(Required 'serviceName');DavServiceName=(Required 'davServiceName');StartupType='Manual';WithoutDav=((Field 'withDav') -ne 'True');NoStart=$true}
            $result=Read-State
        }
        {$_ -in @('services.start','services.stop','services.restart')} {
            $state=Service-State
            if (-not $state -or $state.Uninstalled) { throw '先安装本目录服务。' }
            $entries=@(Service-Entries $state)
            foreach ($entry in $entries) { Assert-PgVNServiceOwner $entry.name $entry.image; if (-not (Get-PgVNServiceImage $entry.name)) { throw ('登记的服务不存在：' + $entry.name) } }
            if ($action -in @('services.stop','services.restart')) { for ($i=$entries.Count-1;$i -ge 0;$i--) { Stop-PgVNOwnedService $entries[$i].name $entries[$i].image; Note ($entries[$i].name + ' 已停止') } }
            if ($action -in @('services.start','services.restart')) {
                foreach ($entry in $entries) {
                    $svc=New-Object ServiceProcess.ServiceController($entry.name)
                    try { if ($svc.Status -ne 'Running') { $svc.Start(); $svc.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running,[TimeSpan]::FromSeconds(45)) }; Note ($entry.name + ' 已启动') } finally { $svc.Dispose() }
                }
            }
            $result=Read-State
        }
        'services.uninstall' { Deployment-Script 'Uninstall-PgVNServices.ps1' @{Root=$script:root}; $result=Read-State }
        'health' { Deployment-Script 'Test-PgVNDeployment.ps1' @{Root=$script:root}; $result=@{message='本机服务、存活、就绪及内嵌网页检查通过。'} }
        'db.check' { $result=(Admin @('db','check','--json')) | ConvertFrom-Json }
        'repo.list' { $result=(Admin @('repo','list','--limit','1000','--json')) | ConvertFrom-Json }
        'repo.create' {
            $name=Required 'repository'; $route=Required 'route'
            if ($name -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$' -or $route -notmatch '^/svn/[A-Za-z0-9][A-Za-z0-9_./-]*$' -or $route -match '(^|/)\.\.(/|$)') { throw '仓库名称或 /svn/ 路由格式无效。' }
            $result=(Admin @('repo','create',$name,'--route',$route,'--json')) | ConvertFrom-Json
        }
        {$_ -in @('repo.enable','repo.disable')} { $result=(Admin @('repo',($action.Split('.')[1]),('name:' + (Required 'repository')),'--json')) | ConvertFrom-Json }
        'repo.verify' { $result=(Admin @('verify',('name:' + (Required 'repository')),'--json') 'admin' $null 7200) | ConvertFrom-Json }
        'repo.dump' {
            $file=[IO.Path]::GetFullPath((Required 'file')); Assert-PlainPath $file
            if (Test-Path -LiteralPath $file) { throw '导出文件已存在；请选择新的文件名。' }
            [void](Admin @('dump',('name:' + (Required 'repository')),'--file',$file,'--deltas') 'admin' $null 7200)
            $result=@{message='SVN 历史导出完成。';file=$file}
        }
        'repo.load' {
            $file=[IO.Path]::GetFullPath((Required 'file'))
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw '导入文件不存在。' }
            $repo='name:' + (Required 'repository'); $info=(Admin @('repo','show',$repo,'--json')) | ConvertFrom-Json
            if ($info.head -ne 0) { throw 'GUI 导入仅允许空仓库（r0），请创建一个新仓库。' }
            if ($info.enabled) { [void](Admin @('repo','disable',$repo,'--json')); Note '导入前已禁用仓库。完成后保持禁用，请校验历史再手动启用。' }
            $result=(Admin @('load',$repo,'--file',$file,'--json') 'admin' $null 7200) | ConvertFrom-Json
        }
        'user.set' {
            $user=Required 'userName'; $password=Field 'userPassword'
            $withDav=(Field 'withDav') -eq 'True'
            $byteCount=[Text.Encoding]::UTF8.GetByteCount($password)
            if ($user -notmatch '^[A-Za-z0-9][A-Za-z0-9_.@-]{0,79}$' -or $password.Length -lt 8 -or $password -match '[\r\n\x00]' -or $byteCount -gt 4096) { throw '用户名格式无效；密码须至少 8 位、不超过 4096 UTF-8 字节，且不含换行。' }
            if ($withDav -and $byteCount -gt 72) { throw '同步 DAV 时密码不能超过 bcrypt 的 72 UTF-8 字节限制。' }
            $ini=Join-Path $script:root 'config.local/pgvnserve.ini'
            $domain=Get-PgVNIniValue $ini 'identity-domain'
            if (-not $domain) { $domain='pgvn' }
            $key=Get-PgVNIniValue $ini 'credential-key-file'
            $users=Join-Path $script:root 'secrets/users.htpasswd'; Assert-PlainPath $users
            $hash=$null
            if ($withDav) {
                $generated=Invoke-PgVNNative -FilePath (Join-Path $script:root 'apache/bin/htpasswd.exe') -Arguments @('-n','-i','-B','-C','12','pgvn-generated-password') -InputText ($password + "`r`n")
                $hash=$generated.Output.Trim().Split(@(':'),2,[StringSplitOptions]::None)[1]
            }
            Note (Admin @('user','set',$domain,$user,'--key-file',$key) 'admin' ($password + "`r`n"))
            if ($withDav) {
                $lines=@()
                if (Test-Path -LiteralPath $users) { $lines=@(Get-Content -LiteralPath $users -Encoding UTF8 | Where-Object { -not $_.StartsWith($user + ':',[StringComparison]::Ordinal) }) }
                $lines += $user + ':' + $hash
                $temporary=Join-Path $script:root ('secrets/users-' + [Guid]::NewGuid().ToString('N') + '.tmp')
                try {
                    Write-PgVNUtf8 $temporary (($lines -join "`n") + "`n")
                    # NullString preserves a real null for Framework File.Replace;
                    # PowerShell 5.1 otherwise coerces $null to an empty backup path.
                    if (Test-Path -LiteralPath $users) { [IO.File]::Replace($temporary,$users,[Management.Automation.Language.NullString]::Value) }
                    else { [IO.File]::Move($temporary,$users) }
                } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
            }
            $password=$null
            $result=@{message='用户凭据已保存。请为用户发布仓库访问权限。'}
        }
        'authz.read' { $result=(Admin @('config','show',('name:' + (Required 'repository')),'--json')) | ConvertFrom-Json }
        'authz.publish' {
            $repo='name:' + (Required 'repository'); $rules=Field 'authz'
            if ([string]::IsNullOrWhiteSpace($rules) -or $rules.Length -gt 65536) { throw '请填写不超过 64 KiB 的 SVN authz 规则。' }
            $current=(Admin @('config','show',$repo,'--json')) | ConvertFrom-Json
            $expected=0L
            if (-not [long]::TryParse((Field 'expectedGeneration'),[ref]$expected) -or $expected -lt 1) { throw '请先读取当前仓库规则，再编辑并发布。' }
            if ($current.generation -ne $expected) { throw '访问规则已更新，请重新读取当前规则后编辑；未覆盖其他管理员的修改。' }
            $path=Join-Path $script:root ('admin/authz-' + [Guid]::NewGuid().ToString('N') + '.conf')
            $groupsPath=$path + '.groups'
            try {
                Write-PgVNUtf8 $path $rules
                $validation=@('authz','validate','--authz',$path,'--json')
                if (-not [string]::IsNullOrEmpty([string]$current.groups)) { Write-PgVNUtf8 $groupsPath $current.groups; $validation += @('--groups',$groupsPath) }
                [void](Admin $validation)
                $result=(Admin @('config','publish',$repo,'--expected-generation',([string]$expected),'--authz',$path,'--json')) | ConvertFrom-Json
            } finally { foreach($temp in @($path,$groupsPath)) { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force } } }
        }
        'backup' { $destination=[IO.Path]::GetFullPath((Required 'file')); Assert-PlainPath $destination; Deployment-Script 'Backup-PgVN.ps1' @{Root=$script:root;Destination=$destination}; $result=@{message='完整备份完成。备份含密钥及密码，请保持访问控制。';file=$destination} }
        'logs' {
            $items=@()
            foreach ($name in @('pgvnserve.log','apache-error.log','apache-access.log','error.log','access.log')) {
                $path=Join-Path $script:root ('logs/' + $name)
                if (Test-Path -LiteralPath $path -PathType Leaf) { $items += @{name=$name;text=(Protect-Text ((Get-Content -LiteralPath $path -Tail 80 -Encoding UTF8) -join "`n"))} }
            }
            $result=@{logs=$items}
        }
    }
    Emit 'result' $result
    exit 0
} catch {
    Emit 'error' $_.Exception.Message
    exit 1
} finally {
    if ($locked -and $mutex) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
    $script:request=$null; $script:secrets.Clear()
}
