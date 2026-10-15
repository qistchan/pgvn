# PgVN Windows x64 部署指南

本包包含 `pgvnserve`、原生 DAV 模块、`pgvnadmin`、匹配的 DLL、Apache、SVN 客户端及 PostgreSQL 客户端工具。监控网页内嵌在 `pgvnserve.exe`，目标机器无需 Python、Node.js、编译器或现有 Polarion。PostgreSQL **服务端**另行部署；本轮固定依赖使用 PostgreSQL 18.6、SVN 1.14.5 和 Apache 2.4.68。

先查看 `package-manifest.json` 的 `validation_status`。`development-snapshot` 表示本机验证中的部署快照；M6 完整回归和代表性性能尚未全部验收。干净 Windows 安装、整机重启、跨主机故障切换及 Polarion 应用演练另行记录，不能从本包存在推导这些项目已通过。

本轮已通过带空格、中文、emoji、组合字符的安装目录，以及中文账号配合标准 `svn checkout/add/commit` 的本机双协议和备份恢复检查。Apache 入口改为自编译的 `apache/bin/pgvnhttpd.exe`；PgVN 自有进程和包内 PostgreSQL 客户端使用进程级 UTF-8 清单，无需改变机器的系统区域设置。目标系统需支持 Windows 10 1903 起的进程 UTF-8 机制，安装前运行下述包烟测。固定 SDK 的 `svnmucc` 中文账号提交断连仍是已知差异，已在原版 svnserve + FSFS 上复现；使用该客户端时采用英文账号。详细来源和实测范围见 [UTF-8 记录](../../docs/pgvn/Windows-UTF8.zh-CN.md)。

## 1. 校验与解压

在管理员 Windows PowerShell 5.1 或 PowerShell 中操作。将 ZIP 与 `.zip.sha256` 放在同一目录，比较摘要后解压至独立目录，例如 `C:\PgVN`。不要覆盖运行中的旧目录。

```powershell
$zip = 'C:\Transfer\PgVN-M5-windows-x64-实际包ID.zip'
$expected = (Get-Content -LiteralPath ($zip + '.sha256') -Raw).Split(' ')[0]
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ine $expected) { throw 'ZIP 摘要不符' }
Expand-Archive -LiteralPath $zip -DestinationPath C:\Deploy
# ZIP 内有一个 PgVN-M5-windows-x64-包ID 顶层目录，以下 $root 指向解压后的该目录。
$root = 'C:\Deploy\PgVN-M5-windows-x64-实际包ID'
& "$root\deploy\windows\Test-PgVNPackage.ps1"
```

脚本逐文件核对摘要、检查 x64 PE，并在 PATH 仅含 Windows System32 的条件下启动主要程序，同时要求 `pgvnserve` 和 `pgvnhttpd --pgvn-runtime` 实测报告 `process_code_page=65001`。摘要用于检查完整性，应通过可信渠道取得包和摘要。首次从互联网下载的 ZIP 如受系统阻止，先复核来源，再按组织的脚本执行策略处理文件；不必修改机器的全局执行策略。

## 2. 准备独立 PostgreSQL 数据库

由 DBA 在目标 PostgreSQL 上创建角色和数据库。下列名称与默认配置一致；角色应为专用账户，不能复用 PostgreSQL 超级用户。

```sql
CREATE ROLE pgvn_migrate LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE pgvn_admin LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE pgvn_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE pgvn_monitor LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE pgvn_restore LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE DATABASE pgvn OWNER pgvn_migrate ENCODING 'UTF8' TEMPLATE template0;
```

通过 psql 的 `\password 角色名` 交互设置各自密码，并配置 PostgreSQL 的网络监听、`pg_hba.conf` 和防火墙。远程数据库使用验证主机名的 TLS。业务连接必须保留 PostgreSQL 会话语义；不要使用会替换后端会话的事务池。

```powershell
& "$root\deploy\windows\Initialize-PgVNConfiguration.ps1" `
    -DatabaseHost 'db.example.internal' -DatabasePort 5432 -DatabaseName pgvn `
    -NodeId node-1 -SvnPort 14690 -MonitorPort 14691 -HttpPort 14980
```

本机回环地址默认 `sslmode=disable`，其他地址默认 `verify-full`。将数据库 CA 保存为 `config.local\postgresql-ca.pem`。多个同端口 PostgreSQL 主机可用逗号分隔；脚本生成逐主机密码行，DBA 应在连接配置中加入 `target_session_attrs=read-write`，并确保数据库集群具有可靠的选主和 fencing。PgVN 本身不执行 PostgreSQL 选主。

分别填写以下两个密码文件，保留脚本创建的受保护 NTFS 权限：

| 文件 | 内容和访问者 |
| --- | --- |
| `admin\pg_service.conf`、`admin\secrets\pgpass.conf` | 迁移、管理、备份、恢复连接；仅管理员/SYSTEM |
| `config.local\pg_service.runtime.conf`、`secrets\runtime.pgpass.conf` | 业务、监控连接；另授 LocalService 读取 |

密码文件中冒号写为 `\:`，反斜杠写为 `\\`。不要把管理密码写进服务专用文件。初始化脚本再次执行时保留已有配置；若需要改端口或数据库，先核对已有的 `admin\deployment.json`，不要以为新参数会覆盖旧值。

## 3. 初始化 schema、密钥和仓库

```powershell
$admin = "$root\deploy\windows\Invoke-PgVNAdmin.ps1"
& $admin -Profile Migrate -Arguments @('db','init','--json')
if ($LASTEXITCODE) { throw '数据库初始化失败' }
& $admin -Profile Migrate -Arguments @('db','check','--json')
& $admin -Profile Migrate -Arguments @('db','grant','--admin-role','pgvn_admin','--service-role','pgvn_service','--monitor-role','pgvn_monitor','--json')
& $admin -Arguments @('key','create',"$root\secrets\credentials-v1.key",'deployment-v1')

$password = Read-Host 'SVN/DAV 测试账号密码' -AsSecureString
& "$root\deploy\windows\Set-PgVNUser.ps1" -UserName alice -Password $password -AlsoDav
& $admin -Arguments @('repo','create','demo','--route','/svn/demo','--json')
```

当前控制 schema 为 6，存储 schema 为 pgfs 1–4，SQL 和摘要已编译进程序。`schema-reference` 仅供查阅，不需要手工顺序运行其中的 SQL。新建仓库默认拒绝访问。准备 `admin\demo.authz`，保存为 UTF-8 无 BOM：

```ini
[demo:/]
alice = rw
* =
```

```powershell
& $admin -Arguments @('config','show','name:demo','--json')
# 这里 1 仅对应刚创建且未修改的仓库；已有仓库使用上一步返回的真实 generation。
& $admin -Arguments @('config','publish','name:demo','--expected-generation','1','--authz',"$root\admin\demo.authz",'--json')
& $admin -Arguments @('authz','check','name:demo','alice','/','write','--json')
```

导入既有 SVN 历史时先禁用目标仓库，用 `load --file` 导入固定 dump，再执行完整 `verify`、发布访问策略并启用。不要让旧 PgFS 文件入口继续向同一存储库写入。管理工具支持标准二进制 dump；在 Windows PowerShell 中使用 `--file`，避免文本管道改变字节。

每个节点的 `hooks` 是全局外部程序目录。需要 hooks 时先向所有节点部署相同程序，再发布数据库 hook 策略；程序名只能为该目录下的 exe 文件名。外部程序依赖、证书和其他全局文件应纳入部署清单。每个仓库不需要本地 repo、conf 或 activities.d 目录。

## 4. 安装服务并检查网页和协议

```powershell
& "$root\deploy\windows\Install-PgVNServices.ps1"
& "$root\deploy\windows\Test-PgVNDeployment.ps1" `
    -RepositoryRoute /svn/demo -UserName alice -Password $password
```

默认创建 `PgVN`、`PgVNDAV` 两个 LocalService 服务，设置专用 libpq 环境和监控 URL ACL。服务程序及 hooks 可读/执行，运行配置与密钥可读，只有 `logs`、`state` 可写；管理目录不向 LocalService 开放。安装脚本记录实际服务路径，拒绝接管同名的其他服务。可用 `-StartupType Manual`、`-NoStart` 或 `-WithoutDav` 改变首次安装方式。

本机默认地址：SVN `svn://127.0.0.1:14690/svn/demo`，DAV `http://127.0.0.1:14980/svn/demo`，网页 `http://127.0.0.1:14691/monitor/`。只读探针为 `/health/live` 和 `/health/ready`。烟测脚本验证服务、就绪、内嵌页面，并可用 SecureString 密码读取两个协议的仓库 UUID；不创建提交。

```powershell
& $admin -Arguments @('monitor-token','create','--label','local-ops','--expires-in','3600')
```

令牌只返回一次，在网页中输入。网页支持中文、明暗主题、键盘图表、节点及 DAV 明细；重新加载需再次认证。不要把令牌放进 URL。监控默认只监听回环，远端运维通过受控隧道或统一 TLS 入口接入。

面向远端客户端时单独配置 Apache HTTPS、证书、监听地址、防火墙和访问控制，再验证客户端证书信任。默认模板中的 HTTP 和 SVN 回环入口供本机验收。CRAM-MD5 不加密 svn 协议数据，远程 SVN 入口需要受信隧道。SVNKit 1.10.1 部署采用 ASCII 仓库路由，已知中文路由修改修订属性的重复编码差异见 M5 指南。

## 5. 备份和恢复到空数据库

```powershell
& "$root\deploy\windows\Backup-PgVN.ps1" -Destination 'D:\PgVN-backups\20261015-01'
```

备份脚本只停止并恢复本目录登记的两个服务，以 `pgvn_admin` 执行 custom 格式逻辑备份；同时保存受保护的配置、当前/previous 密钥、DAV 密码文件和 `hooks`。归档及文件摘要写入 `backup-manifest.json`。备份含敏感信息，备份目录仅管理员/SYSTEM 可读。

`pg_dump` 提供数据库的一致快照；本机停服不代表其他节点已停写。多节点部署需要协调配置发布和密钥轮换，保留快照中仍使用的全部密钥及 hook 版本。安装目录之外的 hook 目录、外部程序依赖和证书由部署清单另外备份。

恢复采用独立解压目录和 **空** 数据库；示例目标为 `pgvn_restore`，所有者为同名的非超级用户恢复角色。DBA 先创建该数据库，并补齐备份 `Database.required_roles` 中的角色；不再用于登录的原所有者可建为 NOLOGIN。先运行初始化脚本，填写该目录的恢复连接和密码，保持服务停止，且不要先创建 DAV 用户文件。

```powershell
# $restoreRoot 指向独立解压目录；其 admin\pg_service.conf 中 [pgvn_restore] 指向空目标。
& "$restoreRoot\deploy\windows\Restore-PgVN.ps1" -BackupDirectory 'D:\PgVN-backups\20261015-01'
```

脚本在恢复前核对所有文件、角色和本地冲突，拒绝非空目标，不执行清除；`--no-owner` 令对象属于恢复角色，**保留 ACL**，避免丢失对 PUBLIC 的撤权。随后执行原生 `db check`、授权及密钥权限设置，保持服务停止。

在该独立目录复核/调整业务、监控、管理及备份连接，使其都指向恢复库；恢复 hooks 程序和外部依赖，确认连接密码、路由与策略后再安装并烟测。脚本不会直接覆盖源机器的连接地址或自动启动服务。以后迁移由实际对象所有者执行：可用 `Invoke-PgVNAdmin.ps1 -Profile Restore`；若切回迁移角色，先由 DBA 明确转移所有权。

恢复后比较仓库 UUID、HEAD、二进制文件和属性、锁、监控令牌及活动 DAV 事务。已有回执与未完成事务保存在数据库，不能把所有旧事务按时间删除。不要在恢复新库的同时继续使用旧库承接同一仓库的写请求。

## 6. 升级、回退与卸载

升级前记录当前包摘要和 schema，完成数据库、密钥和配置备份；用独立目录准备新包，先在隔离恢复库验证。停止本次安装登记的服务，再以数据库实际所有者执行 `db migrate`、`db check` 和授权，重新安装并烟测。多个节点的程序、配置、密钥和 schema 需协调升级，不声明旧程序与新增 schema 可混跑。

当前没有 schema 降级迁移。回退必须恢复到与旧程序匹配的完整数据库备份及密钥/配置，并处理备份点后的新提交；仅替换旧 exe/DLL 不能回退已升级的数据库。

```powershell
& "$root\deploy\windows\Uninstall-PgVNServices.ps1"
```

卸载仅移除安装记录中且程序路径匹配的服务及本次 URL 预留，保留数据库、密钥、配置和日志。整机重启后的自动启动、全新机器验证及 Polarion 隔离演练仍须在独立测试环境完成。
