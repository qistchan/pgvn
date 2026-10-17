# PgVN Manager

Windows 原生配置与维护工具，使用 C# / WPF 和 .NET Framework 4.8。当前为内部验证版，和 PgVN 的生产就绪状态保持一致。

## 使用

1. 解压完整 PgVN Windows 运行包和 Manager 附加包。也可使用同时包含两者的 bundle。
2. 打开 `PgVNManager.exe`。bundle 中的程序位于 `manager` 子目录，会自动填入父目录；附加包需要手动选择完整运行包根目录。
3. 点击「加载配置」。程序校验原运行包的文件摘要，读取当前目录的配置及服务登记。
4. 在「概览」选择「以管理员身份重新打开」（普通会话可以加载包）。
5. 在「初始化与连接」填写 PostgreSQL 地址、端口、新数据库名称、新角色前缀、节点及监听端口，选择 SSL 模式。填写 DBA 用户与密码，先测试连接。测试后密码框会清空，创建前需重新输入。
6. 点击「创建数据库并初始化」，核对目标后执行。自动生成五个非超级用户账号、受保护的连接文件、数据库结构和部署密钥。
7. 在「仓库与用户」创建仓库，设置 SVN 账号（如需 DAV 勾选同步），读取当前访问规则，编辑 authz 为实际用户授权，再发布。发布使用读取时的配置代数，拒绝覆盖其他管理员的更新。
8. 在「服务管理」安装、启动服务，再运行健康检查。默认所有监听均在本机回环地址。SVN 示例：`svn://127.0.0.1:14690/svn/demo`；DAV：`http://127.0.0.1:14980/svn/demo`。

本工具连接已安装的 PostgreSQL，不安装 PostgreSQL，不修改 `postgresql.conf`、`pg_hba.conf`、防火墙或已有数据库。远程数据库推荐 `verify-full` 及 CA 证书；`require` 只加密，不校验服务器身份。DBA 用户需具备 CREATEDB 和 CREATEROLE 权限，默认维护数据库为 `postgres`。首次创建会拒绝已存在的同名数据库及任一同名角色，不重置已有角色密码。

### 日常配置及服务

加载配置后可以更改 SSL / CA、节点 ID、SVN / HTTP / 监控端口；保存前先停止当前目录的全部服务。原配置备份到受保护的 `admin/config-backup-*`。修改监控端口前需要卸载服务，保存后重新安装。数据库地址、库名、角色映射与密码迁移请使用独立目录和正式部署流程，此版不通过“保存配置”切换数据库。

安装的 Windows 服务使用 LocalService，默认手动启动。服务启停与卸载依赖本目录 `admin/services.json`，逐一核对服务 ImagePath；不接受任意服务名称，也不控制 PostgreSQL 服务。运行目录改变后必须先重新加载配置。

### 维护和恢复

- 数据库结构检查、仓库全历史校验、服务健康检查。
- 导出标准 SVN dump；导入仅允许 r0 空仓库，程序先禁用仓库，导入后保持禁用，校验通过后手动启用。失败后请检查日志并创建新仓库重试，历史导入不是整个操作一次性提交。
- 完整部署备份复用 `deploy/windows/Backup-PgVN.ps1`，需要本目录已有服务登记。选择运行目录之外的父目录，将生成独立新目录。脚本短暂停止本目录服务并恢复原运行状态；多节点写入需先协调停止。备份含密码及密钥，保持 ACL，不要上传到公共仓库。
- 完整部署恢复继续使用 `deploy/windows/Restore-PgVN.ps1`。先准备独立空目标数据库、恢复账号与连接文件，核对备份所需角色、密钥和 DAV 凭据，然后执行脚本，检查恢复报告并调整运行配置。脚本的自动 regrant 使用标准 `pgvn_admin/pgvn_service/pgvn_monitor` 名称；使用自定义角色前缀的部署需 DBA 核对并按实际角色完成授权，不可直接照搬默认 regrant。此版 GUI 不执行完整恢复。
- 导出会话操作日志；读取本目录服务日志的末尾。默认显示 `pgvnserve.log`、`apache-error.log`、`apache-access.log`，兼容 `error.log`、`access.log`（存在时）；自定义日志位置仍通过部署配置查看。

完整恢复及对外监听、TLS、备份和 Polarion 验证边界，参见完整运行包中的 Windows 部署指南。GUI 不改变现有 PgVN 内部验证定位。

### 故障处理

操作逐步执行，SQL 初始化和 Windows 服务安装无法共同组成事务。若中途失败，保留数据库、角色和目录供诊断，不自动删除数据。已成功创建数据库和密码文件、但数据库结构未完成时，重新加载后选择「继续初始化已有配置」。若创建数据库之前就失败，请 DBA 先核对创建到哪一步，使用新名称及新目录重试；不要手动删除未知来源的数据库或角色。

首次部署账号密码、运行密钥及日志文件保存在本地受保护目录。DBA 和 SVN 密码不会写入进程参数、请求临时文件或首选项；请求通过标准输入传递，日志做已知秘密脱敏。此机制不能代替机器管理员、操作系统及磁盘安全。操作进行时关闭窗口会被阻止；进程崩溃时自有后台进程随 Job 退出，可能留下需诊断的部分执行状态。

## 构建

Windows x64，已安装 .NET Framework 4.8 及 Windows PowerShell 5.1。使用系统自带 C# 编译器，无额外 .NET SDK、NuGet 或网络下载：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\pgvn-manager\Build-PgVNManager.ps1
```

输出默认在 `runtime/pgvn-manager`，附加包为相邻 `.zip`。如需将 GUI 与一个已验证的完整 CLI 运行包合并，传入 `-RuntimePackageZip <完整运行包.zip>`。组合包保留原有 runtime manifest，Manager 使用独立 `manager-manifest.json` 记录源码与文件摘要，不把新增 GUI 宣称为原冻结构建的门禁结果。也可用 Visual Studio / MSBuild 打开 `PgVN.Manager.csproj`，需要 .NET Framework 4.8 targeting pack。

版本信息统一读取仓库的 `src/pgvn/version.json`，由 `Write-PgVNVersion.ps1` 生成程序集版本、产品版本及进程清单。构建包包含对应的 `version.json`。GUI 显示运行包的产品版本，并在任何管理操作前检查平台、版本、API、schema 和所需后端能力；不匹配时拒绝操作。新增检查后的 GUI 需要配套重建的运行包，不能直接使用历史包补写版本信息。

`tests/pgvn/Test-VersionMetadata.ps1` 可单独验证版本生成和后端兼容性函数；Linux PowerShell 组件结果不替代 Windows PowerShell 5.1、WPF、服务与完整包回归。

开发源码新增「原生 SVN 权限模式」和当前 generation：先读取当前规则，再选择 enforce/bypass 并发布。bypass 只影响 svn://，固定要求认证并将 anonymous_access 设为 none；HTTP/DAV 继续执行原授权。发布保留当前 identity_domain、authenticated_access、groups 和 hooks，generation 冲突时拒绝覆盖。该入口需要 schema 7 的配套新构建，Windows GUI 实际验收按本轮要求暂缓。

## 代码结构

`MainWindow.xaml` 为界面；`App.cs` 管理表单、状态和异步操作；`BackendClient.cs` 使用固定 PowerShell 进程、UTF-8 标准输入协议及进程 Job；`Invoke-PgVNManager.ps1` 校验操作白名单、目标、配置和权限，调用当前运行包的部署脚本与 `pgvnadmin`。后台不接受任意 SQL、脚本路径或命令行。每个运行目录使用命名互斥锁串行化管理操作。

### 本地验证

`Test-PgVNManager.ps1` 校验编译产物、嵌入 XAML、参数引用及协议错误边界；`Test-PgVNManager.Integration.ps1` 使用显式指定的隔离 PostgreSQL 集群和新数据库 / 角色前缀，覆盖初始化、服务与 SVN 操作，保留隔离数据库供诊断。集成测试必须指向已获授权的专用测试集群，不能对生产库运行。
