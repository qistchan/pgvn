# PgFS Windows 新机器部署指南

适用状态：2026 年 10 月 15 日。目标是在全新的 Windows x64 机器上，用当前 PgFS 构建提供 SVN 服务，先完成独立仓库部署与验收，再安排 Polarion 应用接入。

推荐流程为：在现有构建机准备完整运行包，在新机安装 PostgreSQL 和系统运行库，初始化业务数据库，创建 PgFS 仓库，配置认证与授权，最后注册 SVN 服务。目标机使用预编译运行包时，无需安装 Python、编译器或 Polarion。

## 1 当前可部署范围

| 项目 | 当前状态 |
| --- | --- |
| 平台 | 已在现有 Windows x64 环境验证；其他 Windows 版本需要执行本指南的验收步骤 |
| SVN 服务端 | 固定 SVN 1.14.5，APR 1.7.6，APR-util 1.6.3 |
| PostgreSQL | 已验证 18.6 |
| 服务入口 | 原生 svnserve、Apache 2.4.68 的 mod_dav_svn |
| 客户端 | 已验证原版 Polarion SVN 1.14.5 客户端及 SVNKit 1.10.1，通过网络访问时无需安装 PgFS |
| 数据验证 | 真实 Polarion 仓库 r0–r637，共 638 个版本完整性校验通过；全部变更路径与修订属性一致，5 个完整历史快照一致 |
| 后续工作 | Polarion 应用实际运行、生产性能、数据库复制与故障切换 |

现有验证覆盖独立服务进程。下文的新机目录、独立数据库角色及 Windows 服务注册流程，需要在目标机验收；目前尚未在空白虚拟机上完整重跑这套安装流程。

PgFS 不是单独替换一个 DLL 即可使用。必须配套部署支持第三方后端加载的 `libsvn_fs-1.dll`、`libsvn_fs_pg-1.dll` 和经过验证的依赖。当前构建使用 SVN 私有 FS ABI，更换 SVN、APR 或运行库后需要重新构建并验证。

## 2 部署布局与前置条件

以下统一使用 `C:\PgFS`，避免依赖开发机的 `C:\Project\PgFS` 和 `C:\Polarion` 路径。

```text
C:\PgFS\
  bin\                  完整的 SVN 与 PgFS 运行时
  sql\                  001 至 004 数据库迁移
  docs\                 部署及兼容性文档
  artifacts\build\      构建清单
  artifacts\tests\      兼容性证据
  package-sha256.json    运行包文件校验清单
  repos\                SVN 仓库目录，保留配置、hooks 和数据库连接信息
  secrets\              PostgreSQL 密码文件及可选证书
  logs\                 SVN 和 Apache 日志
  work\                 独立验收工作副本
  backup\               备份暂存目录，正式备份还应复制到独立介质
  apache\               可选，配套 Apache 的 bin 和 modules
```

仓库内容、历史、事务和锁存储在 PostgreSQL；`repos` 目录仍是服务恢复的必要组成部分。数据库数据目录使用 PostgreSQL 安装时配置的位置，不放进运行包。

新机准备以下组件：

1. Windows x64；安装软件和注册服务时使用管理员权限。
2. PostgreSQL 18.6 x64，包括 `psql`、`pg_dump`、`pg_restore`。本例通过安装程序创建数据库集群及 Windows 服务，监听本机 5432 端口。
3. Microsoft Visual C++ x64 Redistributable。当前目录未携带部分依赖使用的 `MSVCP140.dll` 和 `VCRUNTIME140_1.dll`，需由系统运行库提供。安装来源见 [Microsoft 官方运行库说明](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170)。
4. 64 位 PowerShell 7，用于执行本指南的示例，见 [Windows 安装说明](https://learn.microsoft.com/en-us/powershell/scripting/install/install-powershell-on-windows)。
5. 下节制作的运行包。磁盘空间需覆盖全部历史内容、索引、WAL 和备份；当前文件内容按 1 MiB 分块存储，尚无版本间增量压缩或去重。

本例端口：PostgreSQL `127.0.0.1:5432`，svnserve `127.0.0.1:3690`，可选 Apache HTTP `127.0.0.1:13980`。开发测试集群使用的 15432 端口不用于本例业务部署。

## 3 在现有构建机准备运行包

当前项目没有自动安装程序。以下命令在已经通过兼容性验证的构建机执行，使用全新目录，打包完整运行时、SQL、文档和证据。不要复制整个 `runtime`，其中还包含测试数据库和真实仓库导出数据。

```powershell
$ErrorActionPreference = 'Stop'
$SourceRoot = 'C:\Project\PgFS'
$PackageRoot = 'C:\Project\PgFS\artifacts\deployment\pgfs-win-x64-svn1.14.5-20261015'
if (Test-Path -LiteralPath $PackageRoot) { throw '打包目录已存在，请选择全新目录。' }
New-Item -ItemType Directory -Path $PackageRoot | Out-Null
Copy-Item -LiteralPath "$SourceRoot\runtime\svn\bin" -Destination $PackageRoot -Recurse
Copy-Item -LiteralPath "$SourceRoot\sql" -Destination $PackageRoot -Recurse
Copy-Item -LiteralPath "$SourceRoot\docs" -Destination $PackageRoot -Recurse
New-Item -ItemType Directory -Path "$PackageRoot\artifacts\build", "$PackageRoot\artifacts\tests" | Out-Null
Copy-Item -LiteralPath "$SourceRoot\artifacts\build\manifest.json" -Destination "$PackageRoot\artifacts\build"
Copy-Item -LiteralPath "$SourceRoot\artifacts\tests\compatibility-evidence.json" -Destination "$PackageRoot\artifacts\tests"

# 需要 HTTP 接入时加入已经完成依赖覆盖的隔离 Apache 运行时。
New-Item -ItemType Directory -Path "$PackageRoot\apache" | Out-Null
Copy-Item -LiteralPath "$SourceRoot\runtime\apache\bin" -Destination "$PackageRoot\apache" -Recurse
Copy-Item -LiteralPath "$SourceRoot\runtime\apache\modules" -Destination "$PackageRoot\apache" -Recurse

$PackageFiles = @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -File | ForEach-Object {
    [pscustomobject]@{
        Path = $_.FullName.Substring($PackageRoot.Length + 1)
        SHA256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }
})
$PackageFiles | ConvertTo-Json -Depth 3 |
    Set-Content -LiteralPath "$PackageRoot\package-sha256.json" -Encoding utf8NoBOM
```

同时保留所用 SVN、APR、PostgreSQL、Apache 发行包的 LICENSE、NOTICE 和第三方组件说明。将包目录内容传到新机 `C:\PgFS`；确认最终存在 `C:\PgFS\bin\svnadmin.exe`，没有额外嵌套一层包名目录。

当前已验证的关键文件 SHA-256 如下。以后重新构建时，使用新构建的清单和对应验证证据，不再套用这两个值。

| 文件 | SHA-256 |
| --- | --- |
| `bin/libsvn_fs_pg-1.dll` | `a06a79ba5efb9cf9bace026fdda1dfe820e3df13f23ae1f3f08403a2b121bb5c` |
| `bin/libsvn_fs-1.dll` | `dede060b2c8ff553b2594de236c894f1199d65fe893b76ea6a0d3390b5c044c5` |

## 4 在新机校验文件并准备命令环境

后续 PowerShell 命令在同一管理终端执行。另开终端时，先重新设置本节变量和函数。

```powershell
$ErrorActionPreference = 'Stop'
$DeployRoot = 'C:\PgFS'
$SvnBin = "$DeployRoot\bin"
$PgBin = 'C:\Program Files\PostgreSQL\18\bin'
$Repo = "$DeployRoot\repos\demo"
function Assert-NativeSuccess([string] $Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step 失败，退出码：$LASTEXITCODE" }
}

$PackageFiles = Get-Content -LiteralPath "$DeployRoot\package-sha256.json" -Raw | ConvertFrom-Json
foreach ($File in $PackageFiles) {
    $Actual = (Get-FileHash -LiteralPath (Join-Path $DeployRoot $File.Path) -Algorithm SHA256).Hash
    if ($Actual -ne $File.SHA256) { throw "运行包校验失败：$($File.Path)" }
}
foreach ($Directory in @('repos', 'secrets', 'logs', 'work', 'backup')) {
    New-Item -ItemType Directory -Path (Join-Path $DeployRoot $Directory) -Force | Out-Null
}
& "$SvnBin\svnadmin.exe" --version
Assert-NativeSuccess 'SVN 版本检查'
& "$PgBin\psql.exe" --version
Assert-NativeSuccess 'PostgreSQL 工具检查'
```

`svnadmin --version` 的可用后端列表可能只列出内置的 FSFS 和 FSX。第三方 `pg` 是否可用，以第 7 节实际创建仓库并通过校验为准。

## 5 初始化独立业务数据库

通过 PostgreSQL 安装程序完成实例初始化，记下管理员 `postgres` 的密码和 Windows 服务名。先检查实际服务名：

```powershell
Get-Service -Name '*postgres*'
$PgServiceName = 'postgresql-x64-18'  # 按实际安装结果修改。
$PgAdminArgs = @('-X', '-W', '-h', '127.0.0.1', '-p', '5432', '-U', 'postgres', '--set=ON_ERROR_STOP=1')
& "$PgBin\psql.exe" @PgAdminArgs -d postgres
```

在打开的 psql 中执行以下命令。`\password` 会交互输入密码，不把密码写进 SQL 历史或命令参数。

```sql
CREATE ROLE pgfs_owner NOLOGIN;
CREATE ROLE pgfs_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
SET password_encryption = 'scram-sha-256';
\password pgfs_app
CREATE DATABASE pgfs OWNER pgfs_owner ENCODING 'UTF8' TEMPLATE template0;
\q
```

回到 PowerShell，按顺序应用全部四个迁移。迁移对象由 `pgfs_owner` 持有，服务使用权限独立的 `pgfs_app`。

```powershell
$MigrationArgs = @($PgAdminArgs) + @('-d', 'pgfs', '-c', 'SET ROLE pgfs_owner;')
foreach ($Migration in @('001_initial.sql', '002_version_tree.sql', '003_locks.sql', '004_hotcopy.sql')) {
    $MigrationArgs += @('-f', "$DeployRoot\sql\$Migration")
}
& "$PgBin\psql.exe" @MigrationArgs
Assert-NativeSuccess 'SQL 迁移'

$GrantSql = @'
GRANT CONNECT ON DATABASE pgfs TO pgfs_app;
GRANT USAGE ON SCHEMA pgfs TO pgfs_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA pgfs TO pgfs_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA pgfs TO pgfs_app;
ALTER DEFAULT PRIVILEGES FOR ROLE pgfs_owner IN SCHEMA pgfs
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pgfs_app;
ALTER DEFAULT PRIVILEGES FOR ROLE pgfs_owner IN SCHEMA pgfs
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO pgfs_app;
'@
& "$PgBin\psql.exe" @PgAdminArgs -d pgfs -c $GrantSql
Assert-NativeSuccess '业务角色授权'
& "$PgBin\psql.exe" @PgAdminArgs -d pgfs -c 'SELECT version FROM pgfs.schema_versions ORDER BY version;'
Assert-NativeSuccess '迁移版本检查'
```

预期迁移版本依次为 `1、2、3、4`。后续迁移也应由对象所有者执行，并复核业务角色权限；不要只检查 `max(version)`。

检查 PostgreSQL 配置文件的真实位置及持久化设置：

```powershell
& "$PgBin\psql.exe" @PgAdminArgs -d postgres -c 'SHOW data_directory; SHOW config_file; SHOW hba_file; SHOW listen_addresses; SHOW port; SHOW fsync; SHOW full_page_writes; SHOW synchronous_commit;'
Assert-NativeSuccess '数据库配置检查'
```

本机部署确保 IPv4 回环地址可连接，端口为 5432，`fsync`、`full_page_writes`、`synchronous_commit` 保持开启。在实际 `pg_hba.conf` 中确认以下规则位于其他可能匹配的宽泛规则之前，保留管理员原有连接方式：

```text
host  pgfs  pgfs_app  127.0.0.1/32  scram-sha-256
```

`pg_hba.conf` 使用第一条匹配规则；Windows 上的修改会对后续新连接生效。若修改了 `listen_addresses` 或端口，则需要重启对应实例。见 [PostgreSQL 认证配置说明](https://www.postgresql.org/docs/18/auth-pg-hba-conf.html)。

`scripts/initialize-test-db.ps1` 只初始化专用开发集群 `runtime/pg-primary`，使用 15432、`pgfs_poc` 和本机 trust 认证。业务部署使用本节流程，不用该脚本代替正式数据库初始化。

## 6 配置数据库凭据与服务目录权限

本例服务账户使用 Windows 内置 `LocalService`。先限制密码目录权限，只允许管理员、SYSTEM 和该账户读取；管理员与 SYSTEM 可修改。命令中的 SID 可避免中英文 Windows 账户名称差异。

```powershell
& icacls.exe "$DeployRoot\secrets" /inheritance:r /grant:r `
    '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-19:(OI)(CI)RX'
Assert-NativeSuccess '密码目录权限设置'
```

在 `C:\PgFS\secrets\pgpass.conf` 中保存以下一行，使用 UTF-8 无 BOM，将密码占位符替换为刚才设置的真实密码：

```text
127.0.0.1:5432:pgfs:pgfs_app:REPLACE_WITH_DATABASE_PASSWORD
```

密码中如果包含 `:` 或 `\`，按 libpq 规则用反斜杠转义。DSN 显式指定绝对 `passfile`，避免交互用户和 Windows 服务账户的用户目录不同。见 [PostgreSQL 密码文件说明](https://www.postgresql.org/docs/18/libpq-pgpass.html)。

```powershell
$Dsn = 'host=127.0.0.1 port=5432 dbname=pgfs user=pgfs_app passfile=C:/PgFS/secrets/pgpass.conf connect_timeout=5 sslmode=disable'
& "$PgBin\psql.exe" -X -w --dbname $Dsn --set=ON_ERROR_STOP=1 `
    -c 'SELECT current_database(), current_user; SELECT version FROM pgfs.schema_versions ORDER BY version;'
Assert-NativeSuccess '业务账户连接检查'

& icacls.exe $DeployRoot /grant '*S-1-5-19:(RX)'
Assert-NativeSuccess '服务根目录访问权限'
& icacls.exe $SvnBin /grant '*S-1-5-19:(OI)(CI)RX'
Assert-NativeSuccess '运行时读取和执行权限'
foreach ($Directory in @('repos', 'logs')) {
    & icacls.exe (Join-Path $DeployRoot $Directory) /grant '*S-1-5-19:(OI)(CI)M'
    Assert-NativeSuccess "$Directory 服务访问权限"
}
```

这里的 `sslmode=disable` 限于同机回环连接。数据库分离部署时，改用经过验证的数据库域名、`sslmode=verify-full`、可信 CA 及限制到 SVN 主机的认证规则和防火墙；密码文件主机字段也随之修改。所有 PgFS 请求连接可写主库。当前实现持有会话级 advisory lock，应先直接连接 PostgreSQL，不接入事务级连接池。

## 7 创建仓库并配置 SVN 用户

```powershell
if (Test-Path -LiteralPath $Repo) { throw '仓库目录已存在，请检查后选择新目录。' }
$env:PGFS_DSN = $Dsn
& "$SvnBin\svnadmin.exe" create --fs-type pg $Repo
Assert-NativeSuccess 'PgFS 仓库创建'
Get-Content -LiteralPath "$Repo\db\fs-type"
& "$SvnBin\svnlook.exe" youngest $Repo
Assert-NativeSuccess '初始版本检查'
& "$SvnBin\svnadmin.exe" verify $Repo
Assert-NativeSuccess '新仓库校验'
```

预期 `fs-type` 为 `pg`，初始 HEAD 为 `0`。创建时，`PGFS_DSN` 会原样写入 `db/pgfs.conf` 的 `connection`，并生成独立的 `repository` 存储标识。后续打开仓库读取此文件，修改服务进程的 `PGFS_DSN` 不会覆盖已有仓库的连接配置。

将 `C:\PgFS\repos\demo\conf\svnserve.conf` 设置为：

```ini
[general]
anon-access = none
auth-access = write
password-db = passwd
authz-db = authz
realm = PgFS demo
[sasl]
use-sasl = false
```

将 `conf/passwd` 设置为以下内容，替换两个密码占位符。SVN 用户与 PostgreSQL 角色是两套账户。

```ini
[users]
alice = REPLACE_WITH_ALICE_PASSWORD
reader = REPLACE_WITH_READER_PASSWORD
```

将 `conf/authz` 设置为：

```ini
[/]
alice = rw
reader = r
* =
```

以上配置文件使用 UTF-8 无 BOM。配置完成后限制包含 SVN 密码的目录权限：

```powershell
& icacls.exe "$Repo\conf" /inheritance:r /grant:r `
    '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-19:(OI)(CI)RX'
Assert-NativeSuccess '仓库配置权限'
& "$SvnBin\svnauthz.exe" validate "$Repo\conf\authz"
Assert-NativeSuccess 'SVN 授权语法检查'
```

按实际业务部署 hooks 及其脚本依赖。修订属性修改仍遵循标准 SVN 钩子规则；需要时配置相应 `pre-revprop-change`，避免直接启用允许所有修改的测试钩子。

## 8 启动 svnserve 并注册 Windows 服务

先在前台验证配置：

```powershell
& "$SvnBin\svnserve.exe" --daemon --foreground --listen-host 127.0.0.1 `
    --listen-port 3690 --root "$DeployRoot\repos" --log-file "$DeployRoot\logs\svnserve.log"
```

另一个终端运行 `svn info svn://127.0.0.1/demo --username alice --no-auth-cache`，按提示输入 SVN 密码。成功后，在前台服务终端按 Ctrl+C 停止实例，确认端口已经释放，再注册持久服务。

以下命令以管理员运行。`$PgServiceName` 应对应本机数据库服务；数据库在远端时，从依赖列表去掉该项。

```powershell
if (Get-Service -Name 'PgFS-SVN' -ErrorAction SilentlyContinue) { throw 'PgFS-SVN 服务已存在。' }
$ListenAddress = '127.0.0.1'
$ServiceCommand = '"C:\PgFS\bin\svnserve.exe" --service --listen-host ' + $ListenAddress +
    ' --listen-port 3690 --root "C:\PgFS\repos" --log-file "C:\PgFS\logs\svnserve.log"'
New-Service -Name 'PgFS-SVN' -DisplayName 'PgFS Subversion' -BinaryPathName $ServiceCommand `
    -StartupType Automatic -DependsOn @('Tcpip', $PgServiceName)
& sc.exe config PgFS-SVN obj= 'NT AUTHORITY\LocalService'
Assert-NativeSuccess 'SVN 服务账户配置'
Start-Service -Name 'PgFS-SVN'
Get-Service -Name 'PgFS-SVN'
```

服务模式使用 `--service`，不能同时携带 `--daemon` 或 `--foreground`。参见 [SVN Windows 服务说明](https://svnbook.red-bean.com/en/1.8/svn.serverconfig.svnserve.html)。

需要远端访问时，将 `$ListenAddress` 改为机器实际内网 IPv4 地址，再生成服务命令。已有服务应停止后更新其启动命令。防火墙只放行所需客户端网段；例如在替换为实际网段后执行：

```powershell
New-NetFirewallRule -DisplayName 'PgFS SVN trusted clients' -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort 3690 -RemoteAddress '10.0.0.0/24' -Profile Domain,Private
```

客户端地址变为 `svn://服务器地址/demo`。基础 `svn://` 不加密仓库内容；需要传输加密的访问应使用 HTTPS 或已有加密通道。

## 9 用独立仓库验收部署

在业务数据导入前创建 `smoke` 仓库，提交操作只影响该验收仓库。以下操作需要 svnserve 已启动；命令会交互提示 SVN 密码。

```powershell
$SmokeRepo = "$DeployRoot\repos\smoke"
$SmokeWc = "$DeployRoot\work\smoke-wc"
$SmokeUrl = 'svn://127.0.0.1/smoke'  # 绑定内网地址时同步修改。
if ((Test-Path -LiteralPath $SmokeRepo) -or (Test-Path -LiteralPath $SmokeWc)) {
    throw '验收目录已存在，请使用新目录。'
}
$env:PGFS_DSN = $Dsn
& "$SvnBin\svnadmin.exe" create --fs-type pg $SmokeRepo
Assert-NativeSuccess '验收仓库创建'
foreach ($Name in @('svnserve.conf', 'passwd', 'authz')) {
    Copy-Item -LiteralPath "$Repo\conf\$Name" -Destination "$SmokeRepo\conf\$Name"
}
& icacls.exe "$SmokeRepo\conf" /inheritance:r /grant:r `
    '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-19:(OI)(CI)RX'
Assert-NativeSuccess '验收仓库配置权限'

& "$SvnBin\svn.exe" checkout $SmokeUrl $SmokeWc --username alice --no-auth-cache
Assert-NativeSuccess '网络检出'
'PgFS deployment smoke test' | Set-Content -LiteralPath "$SmokeWc\probe.txt" -Encoding utf8NoBOM
& "$SvnBin\svn.exe" add "$SmokeWc\probe.txt"
Assert-NativeSuccess '添加文件'
& "$SvnBin\svn.exe" commit $SmokeWc -m 'deployment smoke test' --username alice --no-auth-cache
Assert-NativeSuccess '网络提交'
& "$SvnBin\svn.exe" copy "$SmokeUrl/probe.txt" "$SmokeUrl/copy.txt" -m 'copy smoke test' --username alice --no-auth-cache
Assert-NativeSuccess '带历史复制'
& "$SvnBin\svn.exe" lock "$SmokeWc\probe.txt" -m 'lock smoke test' --username alice --no-auth-cache
Assert-NativeSuccess '加锁'
& "$SvnBin\svn.exe" unlock "$SmokeWc\probe.txt" --username alice --no-auth-cache
Assert-NativeSuccess '解锁'
& "$SvnBin\svn.exe" cat "$SmokeUrl/probe.txt" -r 1 --username reader --no-auth-cache
Assert-NativeSuccess '只读用户读取历史'
& "$SvnBin\svn.exe" mkdir "$SmokeUrl/denied" -m 'must be denied' --username reader --no-auth-cache
if ($LASTEXITCODE -eq 0) { throw '只读用户意外获得写权限。' }
& "$SvnBin\svnadmin.exe" verify $SmokeRepo
Assert-NativeSuccess '验收仓库完整性校验'
```

预期提交和复制后 HEAD 为 `2`，r1 文件内容正确，reader 的创建目录请求因授权被拒绝。随后重启 `PgFS-SVN` 并再次读取历史，确认服务账户可读取密码文件；重启机器后再验证自动启动和读写。另用实际客户端完成一次检出、更新和提交，检查业务钩子及其拒绝策略。

这些检查是新机部署验收，不替代项目的完整兼容性测试。

## 10 可选 Apache HTTP 接入

使用运行包中的独立 `C:\PgFS\apache`；它的 `bin` 已覆盖为与 PgFS 配套的 DLL，不要用另一个 Apache 发行包随意替换。HTTP 用户密码文件由 Apache 管理，用户名称应与 authz 一致。

```powershell
$ApacheRoot = "$DeployRoot\apache"
New-Item -ItemType Directory -Path "$ApacheRoot\conf" -Force | Out-Null
New-Item -ItemType Directory -Path "$DeployRoot\logs\apache" -Force | Out-Null
& icacls.exe "$ApacheRoot\conf" /inheritance:r /grant:r `
    '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-19:(OI)(CI)RX'
Assert-NativeSuccess 'HTTP 配置目录权限'
& "$ApacheRoot\bin\htpasswd.exe" -c "$ApacheRoot\conf\http.passwd" alice
Assert-NativeSuccess 'HTTP 首个用户创建'
& "$ApacheRoot\bin\htpasswd.exe" "$ApacheRoot\conf\http.passwd" reader
Assert-NativeSuccess 'HTTP 只读用户创建'
Copy-Item -LiteralPath "$Repo\conf\authz" -Destination "$ApacheRoot\conf\authz"
```

`htpasswd -c` 仅在首次创建密码文件时使用。将以下内容保存为 `C:\PgFS\apache\conf\httpd.conf`，编码为 UTF-8 无 BOM：

```apache
ServerRoot "C:/PgFS/apache"
Listen 127.0.0.1:13980
ServerName 127.0.0.1:13980
PidFile "C:/PgFS/logs/apache/httpd.pid"
ErrorLog "C:/PgFS/logs/apache/error.log"
LogLevel warn
LoadModule authn_core_module modules/mod_authn_core.so
LoadModule authn_file_module modules/mod_authn_file.so
LoadModule authz_core_module modules/mod_authz_core.so
LoadModule authz_user_module modules/mod_authz_user.so
LoadModule auth_basic_module modules/mod_auth_basic.so
LoadModule dav_module modules/mod_dav.so
LoadModule dav_svn_module modules/mod_dav_svn.so
LoadModule authz_svn_module modules/mod_authz_svn.so
<Location /svn>
  DAV svn
  SVNParentPath "C:/PgFS/repos"
  AuthType Basic
  AuthName "PgFS"
  AuthBasicProvider file
  AuthUserFile "C:/PgFS/apache/conf/http.passwd"
  AuthzSVNAccessFile "C:/PgFS/apache/conf/authz"
  Require valid-user
</Location>
```

这里的 authz 采用第 7 节的全局路径规则；多仓库分别授权时改用 `[demo:/]`、`[smoke:/]` 等限定仓库名称的规则。

```powershell
& icacls.exe $ApacheRoot /grant '*S-1-5-19:(OI)(CI)RX'
Assert-NativeSuccess 'Apache 读取和执行权限'
& "$ApacheRoot\bin\httpd.exe" -t -f "$ApacheRoot\conf\httpd.conf"
Assert-NativeSuccess 'Apache 配置检查'
& "$ApacheRoot\bin\httpd.exe" -X -f "$ApacheRoot\conf\httpd.conf"
```

另一个终端使用 `svn info http://127.0.0.1:13980/svn/demo --username alice --no-auth-cache`，并对 `smoke` 重复网络读写、只读用户拒绝及钩子验收。`Syntax OK` 只证明配置可解析，不能代替实际数据库访问。

前台检查完成后按 Ctrl+C 停止，再注册独立 Windows 服务：

```powershell
& "$ApacheRoot\bin\httpd.exe" -k install -n 'PgFS-HTTP' -f "$ApacheRoot\conf\httpd.conf"
Assert-NativeSuccess 'Apache 服务注册'
& sc.exe config PgFS-HTTP obj= 'NT AUTHORITY\LocalService'
Assert-NativeSuccess 'Apache 服务账户配置'
& sc.exe config PgFS-HTTP depend= "Tcpip/$PgServiceName"
Assert-NativeSuccess 'Apache 服务依赖配置'
Set-Service -Name 'PgFS-HTTP' -StartupType Automatic
Start-Service -Name 'PgFS-HTTP'
```

Windows 服务安装参数见 [Apache 官方说明](https://httpd.apache.org/docs/2.4/platform/windows.html)。数据库在远端时仅保留 TCP/IP 依赖。本节明文 HTTP 仅绑定回环地址；远端正式访问需要配置 HTTPS 或由可信反向代理终止 TLS，再开放相应接口。当前项目已验证 HTTP DAV 协议，TLS 证书及反向代理配置需单独验收。

## 11 将现有 SVN 或 Polarion 仓库迁入

先迁入独立的 `polarion-stage` 仓库。演练可只读导出固定 HEAD；正式切换需停止源端提交和修订属性修改，确保最终导出及配置备份对应同一状态。

创建和加载目标仓库前，先停止新机的 PgFS 前端，配置完目标仓库的认证、授权和 hooks 后再启动。`SVNParentPath` 会自动暴露目录下的新仓库，不能让未完成授权配置的真实历史提前开放访问。

在源机器上使用原有 SVN 工具导出。以下路径对应现有 Polarion 安装，导出目录应事先创建：

```powershell
$SourceSvnBin = 'C:\Polarion\bundled\svn\bin'
$SourceRepo = 'C:\Polarion\data\svn\repo'
$DumpFile = 'C:\PgFS-export\repo.dump'
$SourceHead = & "$SourceSvnBin\svnlook.exe" youngest $SourceRepo
if ($LASTEXITCODE -ne 0) { throw '无法读取源仓库 HEAD。' }
& "$SourceSvnBin\svnadmin.exe" dump $SourceRepo -r "0:$SourceHead" --file $DumpFile
if ($LASTEXITCODE -ne 0) { throw '源仓库导出失败。' }
Get-FileHash -LiteralPath $DumpFile -Algorithm SHA256
```

传输 dump 到新机并核对 SHA-256。使用原生 `--file` 读写转储，避免通过 PowerShell 文本管道或重定向改变二进制数据。

```powershell
$TargetRepo = "$DeployRoot\repos\polarion-stage"
$DumpFile = 'C:\PgFS-import\repo.dump'
if (Test-Path -LiteralPath $TargetRepo) { throw '迁移目标必须是全新仓库。' }
$MigrationFrontends = @(Get-Service -Name 'PgFS-SVN', 'PgFS-HTTP' -ErrorAction SilentlyContinue |
    Where-Object Status -eq 'Running')
$MigrationFrontends | Stop-Service
$env:PGFS_DSN = $Dsn
& "$SvnBin\svnadmin.exe" create --fs-type pg $TargetRepo
Assert-NativeSuccess '迁移目标创建'
& "$SvnBin\svnadmin.exe" load $TargetRepo --file $DumpFile --force-uuid
Assert-NativeSuccess '历史导入'
& "$SvnBin\svnadmin.exe" verify $TargetRepo
Assert-NativeSuccess '迁入历史校验'
& "$SvnBin\svnlook.exe" youngest $TargetRepo
Assert-NativeSuccess '迁入 HEAD 检查'
& "$SvnBin\svnlook.exe" uuid $TargetRepo
Assert-NativeSuccess '迁入 UUID 检查'
```

导入后，HEAD 和 SVN UUID 应与导出源一致。`repository` 存储标识仍属于新建的 PgFS 仓库，不应手工改成源 UUID。dump 不包含认证配置、hooks 和活动锁；这些内容要单独盘点、迁移或重建，并调整机器路径、账户、凭据与外部脚本依赖。默认 load 不执行提交钩子，业务侧外部通知或索引不能视为已经回放。

为目标仓库配置认证和授权后，再通过 `$MigrationFrontends | Start-Service` 恢复原先运行的目标前端。对关键历史文件、修订属性、复制历史、权限和钩子进行业务验收。

现有 SVNKit 验证仅覆盖网络协议。如果 Polarion 还存在 `file://` 或直接访问仓库物理格式的调用，需要单独实现或验证该路径，不能据此认定直接更换仓库目录就能运行应用。Polarion 应用应使用隔离环境验证读写、索引、项目和工作项操作，再安排维护窗口。

## 12 备份恢复与回退

需要一起保留数据库、仓库目录、凭据及实际运行版本。仅复制 `repos\demo` 不会复制数据库内容；将该目录另起名称也不会形成独立仓库，两者会连接同一存储标识。

完整或增量 `svnadmin hotcopy` 可产生独立存储标识，当前限于同一 PostgreSQL 数据库。跨数据库迁移可用 SVN dump/load。数据库整体恢复则使用 PostgreSQL 备份，并保留原来的仓库配置和存储标识。

### 12.1 单机逻辑备份示例

暂停对应 PgFS 前端，确保没有其他服务、管理工具或 Polarion 继续写入相同仓库，并保持配置文件不变。以下只停止已安装的 PgFS 服务，不停止 PostgreSQL。

```powershell
$RunningFrontends = @(Get-Service -Name 'PgFS-SVN', 'PgFS-HTTP' -ErrorAction SilentlyContinue |
    Where-Object Status -eq 'Running')
$RunningFrontends | Stop-Service
$BackupPath = Join-Path "$DeployRoot\backup" (Get-Date -Format 'yyyyMMdd-HHmmss')
New-Item -ItemType Directory -Path $BackupPath | Out-Null
& icacls.exe $BackupPath /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F'
Assert-NativeSuccess '备份目录权限设置'
& "$PgBin\pg_dump.exe" -h 127.0.0.1 -p 5432 -U postgres -W -d pgfs `
    --format=custom --file "$BackupPath\pgfs.dump"
Assert-NativeSuccess '数据库逻辑备份'
Copy-Item -LiteralPath "$DeployRoot\repos" -Destination $BackupPath -Recurse
Copy-Item -LiteralPath "$DeployRoot\secrets" -Destination $BackupPath -Recurse
Copy-Item -LiteralPath "$DeployRoot\package-sha256.json" -Destination $BackupPath
if (Test-Path -LiteralPath "$DeployRoot\apache\conf") {
    Copy-Item -LiteralPath "$DeployRoot\apache\conf" -Destination "$BackupPath\apache-conf" -Recurse
}
$RunningFrontends | Start-Service
```

备份目录包含密码，应预先设置受限访问权限，并与对应运行包、数据库角色定义及 hooks 外部依赖一起安全转存；复制后复核目标文件 ACL。备份过程失败时先检查原因，再手工恢复原本运行的服务。

`pg_dump` 提供单个数据库的一致性逻辑导出，不包含集群全局角色；恢复时需要先创建 `pgfs_owner` 和 `pgfs_app`。它适合本阶段迁移与恢复演练；长期生产备份还需设计物理备份、WAL 归档和恢复点策略。见 [PostgreSQL pg_dump 说明](https://www.postgresql.org/docs/18/app-pgdump.html)。

### 12.2 恢复顺序

1. 准备相同版本的运行包和 PostgreSQL，保持目标 PgFS 前端停止。
2. 创建 `pgfs_owner`、`pgfs_app` 和空数据库，设置新机密码。恢复路线不预先执行 SQL 迁移，避免与 dump 中的 schema 冲突。
3. 对空数据库执行以下恢复命令，确认无错误。

```powershell
& "$PgBin\pg_restore.exe" -h 127.0.0.1 -p 5432 -U postgres -W -d pgfs `
    --exit-on-error 'C:\PgFS-restore\pgfs.dump'
Assert-NativeSuccess '数据库恢复'
```

4. 恢复 `repos` 和服务配置，保留每个 `db/pgfs.conf` 中原有的 `repository`。只按新环境调整 `connection` 的地址、数据库名、密码文件路径；恢复后不要重新运行 `svnadmin create` 覆盖仓库。
5. 配置新机的密码文件及 Windows ACL，检查数据库迁移版本和业务角色权限；恢复 Apache 配置、hooks 及其依赖。
6. 对业务仓库执行 `svnadmin verify`，核对 HEAD、UUID 和关键历史内容，再启动服务。使用独立 smoke 仓库验收网络读写，避免向已恢复的业务历史加入测试提交。

### 12.3 切换后的回退

目标仓库尚无新增写入时，可以停止目标入口并恢复源入口。目标已接收新提交或修订属性修改后，应先停写、保全新增数据并确定反向迁移方案；直接切回旧仓库会丢失这些变更。源仓库与原始备份保留到新环境完成验收。

## 13 需要从源码重建时

源码构建不是新机使用预编译运行包的前置条件。若要在新构建机重建，需要额外准备：

| 输入 | 当前要求 |
| --- | --- |
| Python | 已使用 Python 3.11.9 x64 |
| 编译器 | 项目 `toolchains/llvm-mingw-20261006-ucrt-x86_64` |
| SVN 源码 | `vendor/subversion-1.14.5` |
| APR 源码 | `vendor/apr-1.7.6` |
| APR-util 源码 | `vendor/apr-util-1.6.3` |
| PostgreSQL 开发文件 | 安装目录中的 `include`、`lib/libpq.lib` 和 `bin` DLL |
| 原始 SVN 二进制 | 当前验证使用 `C:\Polarion\bundled\svn\bin` 中配套的发行文件 |

`vendor`、`toolchains`、`runtime` 和 `artifacts` 均被 `.gitignore` 排除，只复制源码目录或仅取得 Git 内容不能得到完整构建输入。

```powershell
Set-Location 'C:\Project\PgFS'
$env:PGFS_POSTGRES_HOME = 'C:\Program Files\PostgreSQL\18'
$env:PGFS_SVN_BIN = 'C:\Polarion\bundled\svn\bin'
python .\scripts\build.py
if ($LASTEXITCODE -ne 0) { throw 'PgFS 构建失败。' }
```

`PGFS_SVN_BIN` 可指向已准备的同一发行版目录，不要求构建机启动 Polarion 应用。构建会生成 PgFS DLL、启用 DSO 的 FS 加载器、回归测试所需 WC DLL 及工具，并校验原有公共导出符号。当前编译参数含 `-O0 -g`，这套构建的兼容性结果不能直接作为优化发布版的性能结论。

完整开发回归使用专用环境：

```powershell
& .\scripts\initialize-test-db.ps1
python .\scripts\build_native_tests.py
python .\tests\run_gates.py
```

这些测试固定使用开发数据库 `127.0.0.1:15432/pgfs_poc`，包含专用集群重启和故障测试。服务测试还固定依赖 `C:\Polarion\bundled\apache`、原版 SVN 客户端、Polarion 的 SVNKit JAR 和指定 JDK 21 路径。因此，全新业务机器只安装运行包后，不能直接把这组开发命令当作便携安装验收；需要复现完整测试依赖或先改造测试配置。

真实历史数据验证独立执行，继续遵循源仓库和隔离目标的授权范围。完整回归及其证据审计方法见[实现状态](implementation-status.md)和[兼容性审计](svn-compatibility-audit.md)。

## 14 常见问题

| 现象 | 检查方向 |
| --- | --- |
| `create --fs-type pg` 提示后端不可用 | 是否使用包内 `svnadmin.exe`，以及成套的重建 FS 加载器和 PgFS DLL；不要只看 `--version` 的内置后端列表 |
| 找不到 DLL，或启动错误 126、193 | DLL 依赖、VC++ x64 运行库、进程与 DLL 位数，以及是否混用其他发行版 |
| 交互执行正常，Windows 服务连接数据库失败 | `pgfs.conf` 中的绝对 passfile 路径、LocalService 的目录与文件权限，以及 pgpass 的主机、端口、库名和角色匹配情况 |
| 修改 `PGFS_DSN` 后旧仓库仍连接原数据库 | 已有仓库读取 `db/pgfs.conf`，需要调整其 `connection` 并重新打开仓库或重启前端 |
| SQL 提示表或序列权限不足 | 检查 schema 使用权限、全部表的读写权限及全部序列权限；同时确认迁移由预期所有者执行 |
| HTTP 403 或 svnserve 拒绝访问 | 分别检查 HTTP 和 SVN 用户名、authz 的仓库名称与路径规则，以及钩子返回错误 |
| 本机可访问，其他机器无法访问 | 监听地址是否仍为回环地址、服务启动命令、防火墙范围、客户端端口与 URL |
| 复制仓库目录后两个仓库一起变化 | 两份配置仍指向同一存储标识；使用 hotcopy 创建同库独立副本，或 dump/load 创建跨库副本 |
| 计划多前端或数据库 HA | 两个 svnserve 进程共享仓库的可见性和锁已验证；还需统一仓库配置、hooks、凭据并验证故障切换。libpq 的主库选择参数不等于完整 HA，见[连接参数说明](https://www.postgresql.org/docs/18/libpq-connect.html) |

服务配置细节见[服务接入指南](service-integration.md)，现有验证结果见[中文兼容性报告](compatibility-report.zh-CN.md)。
