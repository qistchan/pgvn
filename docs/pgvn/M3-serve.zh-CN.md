# M3：PgVN 原生 SVN 服务指南

PgVN M3 提供无本地仓库目录的 svn 协议服务、数据库凭据/配置、外部 hooks 和 Windows 服务生命周期。最终构建和七组完整门禁见 [M3 证据](evidence/m3.json)。完整中文监控、Apache DAV、扩展故障/性能及 Polarion 应用验收按 M4–M7 继续实施。

## 边界与数据库

服务端从固定 SVN 1.14.5 源码构建，私有 RA SVN 层静态编译；仅适配 build 下的副本，不修改 vendor 或现有 Polarion 安装。客户端保留标准 svn:// URL 和公开 SVN UUID。仓库按数据库路由/ID 打开，不读取每库 format、db、conf/passwd/authz 或 hooks。全局程序、配置、凭据、日志和临时文件仍可存在。

已验证 Windows x64、SVN 1.14.5、SVNKit 1.10.1、APR 1.7.6 和 PostgreSQL 18.6。双节点验收是同机两个独立进程，不代表跨主机网络分区或 PostgreSQL 自动选主已验收。传统 PgFS 文件入口不执行 PgVN 的策略检查，切换后不要让旧入口继续对同一存储库写入。

控制 schema 当前为 4，存储仍为 pgfs 1–4。版本 3 增加密文凭据和发布检查，版本 4 固定身份键的 C 排序规则并验证 hooks/协议设置。迁移及摘要嵌入 pgvnadmin，不依赖执行目录。

先创建数据库与数据库角色，再用数据库所有者/迁移角色执行：

```powershell
.\pgvnadmin.exe --config C:\PgVN\admin\migration.local.ini db init
.\pgvnadmin.exe --config C:\PgVN\admin\migration.local.ini db grant --admin-role pgvn_admin --service-role pgvn_service --monitor-role pgvn_monitor
```

数据库角色必须预先存在。迁移、管理配置和对应密码文件只交给部署管理员，服务使用独立的受保护连接配置。服务角色能读控制数据、读写存储并执行受控发布检查，不能修改账号或配置；监控角色不能读取凭据表。其他管理命令见 [M2 指南](M2-admin.zh-CN.md)。

可信本机 hooks 可使用服务数据库角色执行 pgvnadmin look，读取启用仓库的历史或活动事务。禁用仓库查询和其他管理命令仍要求管理权限；该本机工具信任数据库角色，不接收远端用户的授权声明。

## 部署密钥与账号

SVN 用户与 PostgreSQL 登录角色独立。数据库仅保存身份域、用户名、代数、启用状态、key_id、随机 nonce、AES-256-GCM 密文与认证标签，不保存部署密钥或明文 SVN 密码。身份域、用户名和 key_id 绑定为认证附加数据，大小写及 NFC/NFD 身份不合并。

```text
pgvnadmin key create FILE KEY_ID
pgvnadmin --config ADMIN_CONFIG user set DOMAIN USER --key-file FILE
pgvnadmin --config ADMIN_CONFIG user list [DOMAIN]
pgvnadmin --config ADMIN_CONFIG user disable DOMAIN USER
```

user set 的 argv 供受保护的凭据投递器使用，密码必须通过重定向标准输入传入 UTF-8；不支持交互控制台回显。末尾一次 LF 或 CRLF 被去掉，密码不放入 argv。key create 离线执行且不覆盖已有文件。

密钥文件使用受保护 DACL，仅授予当前用户、SYSTEM 和 Administrators；服务账号需要单独授予读取。加载时拒绝目录、文件重解析点、未保护 DACL 和常见宽泛主体的允许 ACE。服务按用户名临时解密，密码在 CRAM 计算后清零，部署密钥在进程池退出时清零。CRAM-MD5 不提供传输加密，远端接入须沿用 M0 契约的受信隧道保护。

滚动轮换：先创建不同 key_id 的新密钥并授权分发；逐节点配置新 current/旧 previous 密钥并重启；再逐账号执行：

```powershell
.\pgvnadmin.exe --config C:\PgVN\admin\admin.local.ini user rekey pgvn alice --key-file C:\PgVN\config\credentials-v2.key --old-key-file C:\PgVN\config\credentials-v1.key
```

rekey 不需要重新输入密码，保留禁用状态，以旧代数/key_id 做 CAS 更新，并递增代数使旧会话失效。并发改密/重加密不会被静默覆盖。确认 user list 中相关账号全部迁移后，才逐节点移除 previous-key 配置并重启；不要提前删除仍被账号使用的旧密钥。

## 服务与访问设置

```ini
[pgvn]
connection = service=pgvn_service
credential-key-file = C:/PgVN/config/credentials-v2.key
credential-previous-key-file = C:/PgVN/config/credentials-v1.key
identity-domain = pgvn
listen-host = 127.0.0.1
listen-port = 14690
hook-root = C:/PgVN/hooks
read-only = false
max-request-bytes = 67108864
max-response-bytes = 67108864
```

previous-key 和 hook-root 可以省略；使用外部 hooks 的节点必须部署对应全局程序。libpq 服务定义、密码文件及权限单独管理，不能把管理角色凭据混入服务专用配置。

```powershell
.\pgvnserve.exe --config C:\PgVN\config\server.local.ini --monitor-url http://127.0.0.1:14691/
```

默认每进程最多 16 个 SVN 会话，监听 backlog 32；连接数据库超时 3 秒，SQL 超时 5 秒，网络空闲超时 30 秒。每命令收发默认各限 64 MiB，max-*-bytes 的 0 表示显式不设字节限制。验收覆盖小预算拒绝及 68,157,441 字节文件的无限额完整往返；扩大预算仍需按负载配置资源。

config publish 新增 --settings FILE，仍需预期代数和 authz。未提供 settings、groups 或 hooks 时分别保留上一版内容：

```json
{"identity_domain":"pgvn","anonymous_access":"none","authenticated_access":"write"}
```

身份域可以覆盖节点缺省值。访问级别均接受 none/read/write；默认匿名 none、认证 write，实际路径还必须通过标准 SVN authz。匿名写入需要显式设置并取得 authz 授权；要求用户名的锁操作仍不能匿名执行。进程 read-only 会把写级别降为读。

每条顶层命令检查配置和账号代数，旧连接须在配置变更后重新打开。修订发布、修订属性修改及加解锁，在同一 SQL 事务中再次锁定/核对账号和配置。控制连接断开可在后续请求重建并复核 schema/主库状态，不在未完成的管理事务中换连接或重放写入。

退出码为 0 成功、1 失败、2 参数/配置、3 数据库不可用、4 schema、5 拒绝、6 不存在、7 冲突。Windows 服务在 SERVICE_SPECIFIC_EXIT_CODE 保留分类。协议中的后端错误映射为客户端认识的标准 SVN 错误，不回显 DSN。

## 外部 hooks

支持 start-commit、pre-commit、post-commit、pre/post-revprop-change、pre/post-lock、pre/post-unlock 九个事件。未配置 pre-revprop-change 时默认拒绝改修订属性。处理器为 allow/reject 或全局程序定义：

```json
{
  "pre-commit": {"executable":"check-change.exe","arguments":["--policy","strict"],"timeout_seconds":10},
  "post-commit": "allow"
}
```

程序名仅为 hook-root 下的 exe 文件名，不接受目录穿越或 shell 脚本入口。启动前核对最终路径并持有文件句柄，以明确 application/argv 调用；参数不交给 shell。先在所有节点部署程序，再发布数据库策略。

stdin 是 version=1 的 JSON：event、repository_id、username、transaction_id、revision、path、paths、property_name、action、property_value_base64、lock_token、lock_comment、force、capabilities。无值为 null，空属性值为空 Base64 字符串；pre-revprop 提供新值、post-revprop 提供旧值。pre-lock 成功 stdout 可返回自定义令牌，空输出使用默认令牌。

环境提供 PGVN_EVENT、PGVN_REPOSITORY_ID、PGVN_USER、PGVN_TRANSACTION、PGVN_REVISION、PGVN_CONFIG。仅继承少量系统变量及 PGSERVICEFILE/PGPASSFILE/PGSYSCONFDIR 文件引用，不继承 PGPASSWORD/PGVN_DSN。hooks 可用受保护的服务配置调用 pgvnadmin look；管理操作需要另外明确授权。

默认执行超时 30 秒，可设 1–60 秒。超时、服务取消或服务进程退出会清理所属 Job Object 的后代；正常完成也不保留后台后代。stdout/stderr 持续排空，错误文本有界。配置最多 16 KiB、16 个参数、每参数 2048 字节；命令行最多 32767 UTF-16 字符。JSON 事件最多 32 MiB，属性值最多 16 MiB，pre-lock 令牌最多 4096 字节；超限明确失败，不静默截断事件。

pre 失败阻止写入；post 失败报告已发布事实，不回滚修订。应答中断后先确认历史/事务结果，再处理重试。验收用另一节点的历史及按事务 ID 的内部回执共同确认发布结果，不宣称原版客户端任意重试自动幂等。

## Windows 服务与监控

服务账号只读取/执行程序及服务专用配置，仅对明确的日志、临时/事件目录取得写权限。不要对服务账号开放管理配置。以下命令在管理员 **cmd.exe** 中运行，假设目录和数据库权限已配置：

```bat
icacls C:\PgVN\config\credentials-v2.key /grant "*S-1-5-19:(R)"
icacls C:\PgVN\config\credentials-v1.key /grant "*S-1-5-19:(R)"
netsh http add urlacl url=http://127.0.0.1:14691/ sddl="D:(A;;GX;;;LS)"
sc.exe create PgVN binPath= "\"C:\PgVN\bin\pgvnserve.exe\" --service --service-name PgVN --config \"C:\PgVN\config\server.local.ini\" --monitor-url http://127.0.0.1:14691/" obj= "NT AUTHORITY\LocalService" start= demand
sc.exe start PgVN
sc.exe stop PgVN
```

停止时拒绝新连接/控制请求，取消外部 hooks，给既有会话 10 秒排空窗口，随后取消网络等待，并持续报告 SCM 进度；不强制终止工作线程。网络流采用可取消的非阻塞 poll，修复 Windows accept 不受接收超时约束及上游无限读的问题。关闭收发和等待完成需要分别处理，见 [微软 shutdown 语义](https://learn.microsoft.com/en-us/windows/win32/api/winsock/nf-winsock-shutdown)；实现见 [固定源代码适配](../../scripts/pgvn/prepare_protocol.py)。

M3 仍是 bootstrap 监控：live=200、ready 固定 503，简单页面沿用 M0。完整中文页面、令牌认证、采样器和真实 readiness 在 M4 实施。未指定 --config 时只启动 bootstrap 监控；静态能力位或 live 不代表完整业务 readiness。

## 完整门禁

```powershell
python scripts/pgvn/build.py --config config/build.local.json --configuration Release
$env:PGVN_TEST_JAVA_HOME = 'C:\Program Files\Eclipse Adoptium\jdk-21.0.11.10-hotspot'
$env:PGVN_TEST_SVNKIT_PLUGINS = 'C:\Polarion\polarion\plugins'
python tests/pgvn/run_gates.py --milestone M3
```

门禁在同一 Release 构建上顺序运行 M0–M2 回归及凭据、协议、hooks、LocalService 业务服务四组 M3 检查，前后复核源码和产物摘要。覆盖标准 SVN/SVNKit、大文件/工作副本更新、路由单次解码、授权及在途撤权、双节点锁/修订属性、滚动密钥轮换、输出/超时/子进程树、应答丢失、真实空闲超时、低权限排空及同端口重启。

所有数据库、端口、服务和 URL ACL 都是隔离测试资源，结束后清理临时服务/URL ACL，不修改 Polarion 原服务和仓库。完整新机打包和 Polarion 隔离应用验收在后续里程碑，继续按 [推进计划](../plans/2026-10-15-pgvn-implementation-plan.zh-CN.md) 执行。
