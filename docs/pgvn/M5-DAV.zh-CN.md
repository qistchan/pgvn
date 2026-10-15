# M5 Apache DAV 接入与恢复

M5 提供原生 Windows x64 的 `mod_dav_pgvn.so` 和 `mod_authz_pgvn.so`，以 PostgreSQL 目录、策略和持久事务提供 SVN HTTP/HTTPS 服务。固定 Release 门禁 `m5-21229c13cbc1` 的 10 组检查全部通过，包含完整 M0–M4 回归和 14+6 项真实浏览器检查。范围以 [实施进展](progress.zh-CN.md) 和 [M5 证据](evidence/m5.json) 为准。

## 构建与数据库

模块基于固定 Subversion 1.14.5、Apache 2.4.68、APR 1.7.6/APR-util 1.6.3 和 PostgreSQL 18.6 的 Windows x64 ABI。使用同一构建的完整运行依赖，不将两个模块或旧 PgFS 的两个 DLL 单独复制到另一套 Apache。公共仓库核心链接进服务和模块，没有额外的仓库 RPC 进程。

控制 schema 为 6，存储 schema 仍为 pgfs 1–4。先备份，再使用数据库所有者配置执行 `db migrate`、`db check` 和角色授权。历史 M2/M4 文档中的 schema 2/5 是各里程碑的验收版本。M4 程序不接受 schema 6；当前没有降级迁移。

```powershell
.\bin\pgvnadmin.exe --config C:\PgVN\config\migration.local.ini db migrate
.\bin\pgvnadmin.exe --config C:\PgVN\config\migration.local.ini db check
.\bin\pgvnadmin.exe --config C:\PgVN\config\migration.local.ini db grant --admin-role pgvn_admin --service-role pgvn_service --monitor-role pgvn_monitor
```

业务角色连接同一数据库，需保留会话语义；暂不使用会替换后端会话的事务池。监控读取角色仍只读，不能读取原始 DAV 表。完整导入、备份和配置命令见 [管理指南](M2-admin.zh-CN.md)。

## 全局连接与 Apache 挂载

为独立 Apache 准备受保护的全局配置 `C:/PgVN/config/dav.local.ini`：

```ini
[pgvn]
connection = service=pgvn_service
node-id = dav-node-1
# 使用外部 hooks 时，指定与各服务节点一致的程序根目录。
# hook-root = C:/PgVN/hooks
```

libpq service/密码文件必须由实际 Apache 服务账户读取，密码不写入示例或命令行。`PgVNConfig` 只能全局指定一次；仓库通过数据库中最长匹配的路由定位。下面是供回环烟测使用的片段，假设 ServerRoot 指向运行包的 `apache`，日志目录已经准备：

```apache
Listen 127.0.0.1:14980
ServerName 127.0.0.1
LoadModule authn_core_module modules/mod_authn_core.so
LoadModule authn_file_module modules/mod_authn_file.so
LoadModule auth_basic_module modules/mod_auth_basic.so
LoadModule authz_core_module modules/mod_authz_core.so
LoadModule authz_user_module modules/mod_authz_user.so
LoadModule dav_module modules/mod_dav.so
LoadModule dav_pgvn_module modules/mod_dav_pgvn.so
LoadModule authz_pgvn_module modules/mod_authz_pgvn.so
PgVNConfig "C:/PgVN/config/dav.local.ini"
<Location /svn>
    DAV pgvn
    PgVNPathAuthz short_circuit
    PgVNAdvertiseV2Protocol On
    AuthType Basic
    AuthName "PgVN"
    AuthBasicProvider file
    AuthUserFile "C:/PgVN/config/users.htpasswd"
    Require valid-user
</Location>
```

```powershell
.\bin\pgvnadmin.exe --config C:\PgVN\config\admin.local.ini repo create demo --route /svn/demo
# policy.conf 使用 [demo:/] 和经认证的用户名；新建仓库默认拒绝访问。
.\bin\pgvnadmin.exe --config C:\PgVN\config\admin.local.ini config publish name:demo --expected-generation 1 --authz C:\PgVN\config\policy.conf
.\apache\bin\httpd.exe -t -f C:\PgVN\config\httpd.local.conf
.\apache\bin\httpd.exe -X -f C:\PgVN\config\httpd.local.conf
```

HTTP 身份认证由 Apache 的认证模块提供；SVN 路径权限和访问档位从数据库读取。`pgvnadmin user set` 管理的加密 CRAM-MD5 账号供 svn 协议使用，不会自动更新 Apache 的 htpasswd。使用相同用户名，分别维护认证源。远程 Basic 访问需要受信任 TLS；HTTPS 由配套 `mod_ssl` 和有效证书配置。

模块拒绝本地 `PgVNPath`、`PgVNParentPath`、`PgVNActivitiesDB`、`PgVNHooksEnv` 和授权文件指令；也拒绝关闭路径授权或权威授权。缺少配套 `mod_authz_pgvn` 时启动失败。服务节点仍需要程序、全局配置、日志、证书和外部 hook 程序，仓库历史与 activity/virtual 状态存于数据库，不创建每库 repo 或 activities.d 目录。

## 多进程提交与恢复

同一提交可逐请求切换 Apache 工作进程。SVN v1 activity 和 v2 virtual transaction 都按仓库、类型、名称和创建者隔离。创建将 FS 事务、初始属性及别名一并提交，再执行 start-commit hook，最后复核策略代数并激活。请求内缓存有界，并随请求释放；keep-alive 的下一请求重新获取策略。

已提交结果从 FS 事务回执推导，重复 MERGE 返回同一修订；取消已提交的绑定仍保留回执。hook 拒绝、撤权或进程退出均有明确状态。跨仓库 COPY 返回不支持；同仓库 copy/history 保留 SVN 语义。禁止依据时间批量删除 active 暂存事务。

管理员可安全检查并恢复失去创建者的 creating 事务：

```powershell
.\bin\pgvnadmin.exe --config C:\PgVN\config\admin.local.ini look transactions name:demo --json
.\bin\pgvnadmin.exe --config C:\PgVN\config\admin.local.ini maintenance recover-dav-creation name:demo TRANSACTION-ID --json
```

恢复命令由管理员角色执行。持有创建者租约时返回冲突（退出码 7）；只有确认创建者会话已丢失的 creating 状态才会原子中止。普通 active、已提交或已恢复事务返回 unchanged，不能用它清理正常未完成的提交。业务角色调用返回权限不足（退出码 5）。

## 工作进程监控

每个实际 Apache 工作进程发布独立 instance_id，每 5 秒写入有界心跳。通过 [内置监控](M4-monitor.zh-CN.md) 的节点页展开 DAV 行，查看 HTTP 方法计数、活动请求、SQL、修订发布/回执、遥测失败和最多 5 条近期错误。响应 `X-PgVN-Request-Id` 可关联近期错误。遥测失败不改变业务提交结果。

DAV readiness 的依据是工作进程已初始化业务上下文、可写主库及 catalog 检查，不是完整请求线程存活检测。超过 15 秒未更新显示失联；数据库样本不可用时显示未知。旧实例保留记录，新进程使用新 ID。正常 Windows 服务停止会发布 stopped；强制终止只能等待心跳失联。DAV 当前未上报 TCP/TLS 线字节或 CPU 百分比，不用 SVN 实例的对应数值代替。

## 验证范围与已知限制

原版 SVN 的中文路径/作者、二进制、属性、复制、历史、锁和两种 DAV 协议，以及 SVNKit 1.10.1 的 HTTP/受信 HTTPS 读写均有独立测试。双 Apache 轮转测试覆盖创建者退出后继续提交与重复 MERGE。授权负面测试覆盖递归复制、复制源、历史、继承属性、mergeinfo、在途撤权及数据库中断。

当前安装的 SVNKit 1.10.1 在中文仓库 URL 上修改 revision 属性会将路径中的百分号再次编码；同一操作在未修改的标准 mod_dav_svn + FSFS 上也失败。ASCII 路由可完成该测试，中文 URL 的原版 SVN 客户端通过。服务器不会二次解码绕过这个客户端差异，部署给此 SVNKit 版本时使用 ASCII 仓库路由。

本机独立进程不能证明跨主机或 PostgreSQL 自动选主。真实主备切换、代表性性能属于 M6；全新 Windows 安装/重启与隔离 Polarion 应用验收属于 M7。用户当前没有独立测试机，先交付本机验证和部署包，保留这些待验收项。
