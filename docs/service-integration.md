# PgFS 原生 SVN 服务接入指南

全新 Windows 机器的运行包准备、数据库初始化、服务注册及迁移恢复步骤，参见[新机器部署指南](deployment-guide.zh-CN.md)。

PgFS 是 SVN 文件系统提供程序，对应的动态库为 `libsvn_fs_pg-1.dll`。
仓库内容存储在 PostgreSQL 中；原生 SVN 的仓库层和协议层继续负责授权、
钩子（hooks）、增量编辑器和客户端请求的处理。

已验证的运行平台为 Windows x64、SVN 1.14.5、APR 1.7.6、APR-util 1.6.3
和 PostgreSQL 18.6。通过网络访问的客户端无需安装此提供程序。
已使用生成的测试仓库，验证了已安装的 Polarion SVN 1.14.5 客户端和
SVNKit 1.10.1 通过 svnserve 及 Apache mod_dav_svn 访问仓库的能力。

## 运行时与数据库

使用 `C:\Project\PgFS\runtime\svn\bin` 中完整的隔离运行时。
该运行时的 FS 加载器由未修改的 SVN 1.14.5 源码重新构建，以启用文件系统提供程序的
动态共享对象（DSO）加载；随附的原版 Windows 加载器禁用了此功能。
构建过程会检查加载器是否保留所有原有的公共导出符号。
更换 SVN 的 FS 私有 ABI 或 APR 运行时时，需要重新构建后端。

创建仓库前，先对目标数据库应用编号为 1–4 的 SQL 迁移。
`scripts/initialize-test-db.ps1` 只用于初始化 `runtime/pg-primary` 下的专用开发数据库集群，
监听回环地址的 15432 端口。数据库名为 `pgfs_poc`，测试角色为 `pgfs_test`。

仓库配置和钩子仍保存在仓库目录中。
`db/pgfs.conf` 包含 libpq 连接信息和独立的存储标识。
仓库树、文件全文内容、属性、修订版本、事务和锁存储在 PostgreSQL 中。
为便于恢复服务，应将数据库、仓库配置和钩子一起备份。

## 原生 svnserve 开发环境接入

使用文件系统提供程序名称 `pg` 创建新仓库：

```powershell
$env:PGFS_DSN = 'host=127.0.0.1 port=15432 dbname=pgfs_poc user=pgfs_test sslmode=disable'
& 'C:\Project\PgFS\runtime\svn\bin\svnadmin.exe' create --fs-type pg 'C:\Project\PgFS\runtime\repos\demo'
```

配置 `demo/conf/svnserve.conf`，指定密码库和路径授权（authz）：

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

将需要使用的开发测试用户写入 `conf/passwd`，并按照标准 Subversion 配置语法，
在 `conf/authz` 中授予用户相应的路径权限。使用隔离运行时启动 svnserve：

```powershell
& 'C:\Project\PgFS\runtime\svn\bin\svnserve.exe' --daemon --foreground `
    --listen-host 127.0.0.1 --listen-port 13690 --root 'C:\Project\PgFS\runtime\repos'
```

客户端访问地址为 `svn://127.0.0.1:13690/demo`。标准 SVN 客户端可以通过现有的
SVN 服务层执行检出、更新、提交、复制/移动、查看日志、逐行追溯（blame）、
合并、加锁/解锁，以及修改修订版本属性等操作。
标准仓库钩子仍可拒绝操作并发出通知。

## Apache mod_dav_svn

服务集成验证流程会创建独立的 Apache 2.4.68 运行时，并将经过测试的
SVN FS 加载器、PgFS 和 libpq 依赖文件覆盖部署到其可执行文件目录中。
该运行时加载与之匹配的原版 `mod_dav_svn` 和 `mod_authz_svn` 模块。

核心的 Location 配置采用标准 Subversion 语法：

```apache
<Location /svn>
  DAV svn
  SVNParentPath "C:/Project/PgFS/runtime/repos"
  AuthType Basic
  AuthName "PgFS demo"
  AuthBasicProvider file
  AuthUserFile "C:/Project/PgFS/runtime/http.passwd"
  AuthzSVNAccessFile "C:/Project/PgFS/runtime/http.authz"
  Require valid-user
</Location>
```

Apache 还需要常规的 `Listen`、`ServerRoot`、日志路径配置，以及所需的
认证和 DAV 模块的 `LoadModule` 指令。`tests/test_services.py` 包含完整、可执行的测试环境配置；
`tests/upstream_http.py` 使用固定版本的上游回归测试中的 Location 规则，提供隔离的 Apache 实例。

运行可重复执行的验证流程：

```powershell
python C:\Project\PgFS\tests\test_services.py
python C:\Project\PgFS\tests\run_compatibility.py services
```

验证过程中创建的监听服务均为临时服务，仅绑定回环地址，并由对应的测试进程负责停止。
测试会读取 Polarion 原始二进制文件和已安装的 SVNKit 组件包；
不会替换 Polarion 文件，也不会启动 Polarion 应用。

## 仓库管理与当前限制

`svnadmin dump/load`、`svnlook`、仓库校验、冻结（freeze）、恢复校验、
修订版本属性更新，以及完整/增量热复制（hotcopy）均有专门的测试。
热复制会在同一个 PostgreSQL 数据库中分配独立的存储标识，同时保留 SVN UUID。
跨数据库迁移使用原生 dump/load。PgFS 的 pack 操作会压缩目录覆盖层，
不会改变逻辑修订历史。

该提供程序当前使用格式版本 1，报告的最低原生格式兼容版本为 SVN 1.14。
BDB 日志操作及其他后端针对物理存储的控制不属于该提供程序的存储模型范围。
未知的 ioctl 请求会返回标准的“无法识别 ioctl”错误。

现有验证证据、跳过项和存储实现相关的排除项记录在
`artifacts/tests/compatibility-evidence.json` 中。经授权导出的真实 Polarion 历史截至 r637，
全部 638 个修订版本均通过完整性校验，并完成了所有修订版本的变更路径和修订版本属性比对，
以及 r0、r1、r318、r636 和 r637 的完整逻辑快照比对。
本次验证未更改原始仓库和 Polarion 服务配置。
Polarion 应用的实际接入、具有代表性的性能测试，以及 PostgreSQL 高可用（HA）
属于后续里程碑。

在获得源仓库与隔离目标的使用授权后，单独运行 `python tests/test_corpus.py`；
该测试独立于生成数据的验证流程。若要复用已获授权的导出数据及已完成的导入，
可使用 `--resume`，并指定 `runtime/corpus` 下对应的测试环境目录。
测试在读取导入的历史前，会检查目标 DSN、仓库类型和固定的修订版本范围。

服务测试还会启动两个独立的 svnserve 进程访问同一个仓库，
检查双方的提交和修订版本属性更新能否立即被对方看到，并验证共享锁的约束和读取权限限制。
所有监听服务均使用回环地址，并由对应的测试进程负责停止。

参见[需求审计](svn-compatibility-audit.md)，了解当前目标的验收检查项，
以及已记录的存储实现和版本相关排除项。
