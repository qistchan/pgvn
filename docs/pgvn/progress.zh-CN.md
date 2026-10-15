# PgVN 实施进展

目标仓库：`C:\Project\PgVN\pgvn`，远程 `git@github.com:qistchan/pgvn.git`。每个完成的里程碑提交一次代码。当前已完成 M0、M1、M2、M3，接下来实施 M4 内置监控；尚不能替代 Polarion 的 SVN 后端。

## M0：完成

交付原生 Windows x64 构建骨架：pgvnserve、pgvnadmin、mod_dav_pgvn 和 mod_authz_pgvn。公共核心静态链接，构建输入显式配置，输出独立于现有安装。固定 SVN/APR/libpq/Apache ABI、数据导入符号、路径改造清单、接口、认证/hooks 边界、schema 迁移和监控资源预算。

Release 验证命令：

```powershell
python scripts/pgvn/build.py --config config/build.local.json --configuration Release
python tests/pgvn/run_gates.py --milestone M0
```

五组门禁通过：二进制身份与依赖、非法配置拒绝、控制台 HTTP/重启、Apache 模块加载/请求/卸载、LocalService 低权限服务。并发测试为 16 客户端，每轮 128 次请求；服务测试两轮，检查真实后续请求头超时、慢首请求不占用工作线程、停止及同 URL 重启，最后删除临时服务和 URL ACL。

应用头超时配置 5 秒，内核实际断开有时钟粒度偏差，原始测量写入证据；不承诺精确 5 秒。首请求使用系统全局计时器，测试及程序不修改整机 HTTP 配置。详情见 [M0 契约](M0-contracts.zh-CN.md) 和 [路径审计](path-audit.zh-CN.md)。

可审阅的构建摘要和原始门禁记录见 [M0 证据](evidence/m0.json)。本机完整 manifest 与 SDK/source 摘要位于 `artifacts/pgvn/build`，完整日志在 `runtime/pgvn/tests`。同一固定路径与输入重复 Release 构建时，四个主二进制 SHA-256 相同；不据此宣称跨工具链或不同构建路径可复现。

能力边界：管理工具仅支持诊断；监控仅有 bootstrap 页、live 和固定失败的 ready。Apache 模块仅提供显式 bootstrap handler，尚未注册业务 DAV 或授权 provider。真实仓库协议、认证和中文业务监控页面继续按 M1–M5 推进。

## M1：完成

已实现按 ID 创建/绑定、注册路由、配置版本与无目录 svn_repos_t。修订发布在同一 SQL 事务中复核启用状态、权限代数和变更路径；SVN authz/groups 从内存解析，hooks 使用显式事件分派。连接固定到 FS 生命周期，计数及快照有界。

Release 的七组 M1 门禁通过，包含八进程并发创建、2 MiB 以上二进制历史、复制、回执查询、撤权、hooks、写流互斥/断连释放、32 连接上限、数据库重启及空工作目录；17 个传统 PgFS 原生用例通过。一个固定旧路径/端口的 restart 用例由独立 PgVN 重启检查替代，明确记录在证据中。M0 的模块/低权限服务门禁也对当前 Release 产物重跑。

实施细节及限制见 [M1 公共核心](M1-core.zh-CN.md)，证据见 [M1 记录](evidence/m1.json)。M1 尚未开放 SVN/DAV 网络服务，也未提供完整的正式管理 CLI。

## M2：完成

pgvnadmin 已实现数据库引导/迁移摘要检查、角色授权、仓库管理与既有 ID 注册、标准 dump/load、范围 verify、look 历史/属性/事务查询、pack/recover/freeze、完整与增量 hotcopy、排空清除、版本化授权配置、只读监控令牌和脱敏诊断。

最终 Release 的八组 M2 门禁通过，包含非超级用户数据库所有者的四进程初始化、未知版本/篡改摘要拒绝、五个修订与 FSFS 的完整快照和变更路径对照、二进制及属性、增量/文件/标准流、UUID 与 hooks、热复制冲突回滚、freeze 及子进程树超时清理、句柄排空和便携目录运行。M1 七组及 17 个传统 PgFS 用例、M0 五组也在该构建重新通过。

新增控制 schema 为版本 2，迁移及摘要编译进管理工具。M2 的令牌管理为 M4 接入准备，只读认证与中文业务监控页面尚未开放。当前命令和语义见 [M2 管理指南](M2-admin.zh-CN.md)，实际证据见 [M2 记录](evidence/m2.json)。

## M3：完成

已交付从固定上游源码构建的原生 svn 协议服务：数据库 ID 路由、CRAM-MD5、AES-256-GCM 密文账号、双密钥滚动轮换、版本化访问设置、标准路径授权、九种外部 hooks，以及修订发布、修订属性和锁操作在同一 SQL 事务中的撤权复核。程序不需要每库目录，支持控制台和 LocalService 服务。

最终 Release 的七组完整门禁通过（`m3-765f12d53a15`），同一构建前后核对源码和四个主产物摘要。包括 M0–M2 回归、凭据、标准 SVN/SVNmucc 与 SVNKit 1.10.1、hooks、真实低权限服务；覆盖双节点共享历史/锁/修订属性、在途撤权、滚动轮换、68,157,441 字节文件、URL 单次解码、30 秒空闲超时、数据库连接恢复、进程失联后从另一节点确认提交、外部程序超时及子进程树清理。

LocalService 完成二进制 checkout/delta update；停止期间取消 hooks、保持另一节点可用。16 个空闲 SVN 会话约 12.14 秒排空，同端口连续三轮重启通过；临时服务与 URL ACL 已清理。双节点测试为同机独立进程，不据此声明跨主机网络分区或 PostgreSQL 自动选主已验证。

控制 schema 当前为 4。服务配置、账号/密钥、hooks 和部署命令见 [M3 服务指南](M3-serve.zh-CN.md)，最终证据见 [M3 记录](evidence/m3.json)。`pgvnserve.exe` SHA-256 为 `625cf5cd170092b578be4ac7c825a0d803453e4c24346e65821af9e5394646ce`。监控仍为 bootstrap，ready=503；完整中文页面、只读令牌接入、后台采样和真实 readiness 在 M4 交付，Apache DAV 与 Polarion 应用验收仍未完成。

后续里程碑以 [推进计划](../plans/2026-10-15-pgvn-implementation-plan.zh-CN.md) 的出口为准。没有完成的能力不能沿用旧 PgFS 的测试成绩标为完成。
