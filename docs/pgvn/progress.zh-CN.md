# PgVN 实施进展

目标仓库：`C:\Project\PgVN\pgvn`，远程 `git@github.com:qistchan/pgvn.git`。每个完成的里程碑提交一次代码。当前已完成 M0–M5，继续进行 M6 本机验证和 M7 部署包；尚不能替代 Polarion 的生产 SVN 后端。

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

M3 验收时控制 schema 为 4。服务配置、账号/密钥、hooks 和部署命令见 [M3 服务指南](M3-serve.zh-CN.md)，最终证据见 [M3 记录](evidence/m3.json)。`pgvnserve.exe` SHA-256 为 `625cf5cd170092b578be4ac7c825a0d803453e4c24346e65821af9e5394646ce`。监控仍为 bootstrap，ready=503；完整中文页面、只读令牌接入、后台采样和真实 readiness 在 M4 交付，Apache DAV 与 Polarion 应用验收仍未完成。

## M4：完成

交付内嵌中文概览、节点、仓库搜索/分页/详情、数据库和请求错误页面，同源只读 API、受限只读采样连接、版本/路由/写主就绪校验、Windows 进程资源、有界趋势和事件、Prometheus。令牌只在页面内存，撤销清空详情；正常停止、失联、数据库未知和过期状态分别处理。正式配置见 [M4 指南](M4-monitor.zh-CN.md)，证据见 [M4 记录](evidence/m4.json)。

固定 Release 门禁 `m4-a9f0cf1bddb0` 八组脚本和 15 项真实浏览器检查通过。M0–M3 七组回归重新通过，包括原版 SVN/SVNKit、双进程共享库、授权、hooks、原子提交与服务排空。独立 TCP 账本精确对应实际协议字节；真实 PostgreSQL COMMIT 应答丢失后只发布一个 revision，新发布与回执确认分开计数。打开写流、连接租约及 SQL 结果分类平衡。

1000 个合成仓库的分页与采样通过，额外私有内存 7.43 MiB。采样 SQL 超时、遥测发布容量不足不改变真实提交结果。冷启动恢复、网络黑洞、双慢响应、正常停止状态和低权限 URL 重启通过。构建前后核对源码、页面、主产物与运行依赖摘要；临时服务、URL ACL、浏览器 fixture 已清理。

短时 CLI 负载测量保留全部关闭/开启/关闭轮次，尚不能宣称 M6 的吞吐/P95 预算达标。Apache DAV、多节点 HTTP 状态与工作进程遥测为 M5；完整性能/故障域验证为 M6；干净机器安装包和 Polarion 隔离应用验收为 M7。当前仍不具备生产替换 Polarion 后端的结论。

后续里程碑以 [推进计划](../plans/2026-10-15-pgvn-implementation-plan.zh-CN.md) 的出口为准。

## M5：完成

交付真实 DAV provider、配套数据库授权和控制 schema 6。仓库按 ID 打开，activity/virtual 状态保存在 PostgreSQL；创建、取消、创建者租约、外部 hook 可见性、撤权和失去创建者后的安全恢复均有组件及协议验证。真实 M4 schema 5→6 升级保留身份、完整测试历史、二进制与旧摘要，旧程序拒绝新结构。

固定 Release 门禁 `m5-21229c13cbc1` 的 10 组检查全部通过，耗时 552.593 秒；其中 M0–M4 完整回归为 `m4-ecb6a432ecae`。同一构建前后核对源码、四个主产物、运行库和 Apache 依赖摘要。真实浏览器完成原监控 14 项及 DAV 6 项检查，包括方法计数、错误请求 ID、刷新保留展开/焦点、失联/新实例、窄屏、数据库未知与令牌撤销。临时网页进程、服务和 URL ACL 已清理。

HTTP v1/v2、原版 SVN 中文路径及作者、二进制/属性/复制历史/锁、SVNKit 1.10.1 HTTP 与验证证书的 HTTPS 读写通过。两个独立 Apache 逐请求轮转，创建者进程退出后由另一个继续提交，重复 MERGE 只产生一个修订。路径授权负面检查覆盖递归根目录复制、源/目标策略、历史、继承属性、mergeinfo、keep-alive 和在途撤权。数据库故障返回 503，恢复后能读写。

Apache 实际工作进程独立上报有界心跳、HTTP 方法、SQL、修订/回执和近期错误；计数与访问日志对应。遥测写权限被撤销期间仍能提交；工作进程失联、重启以及真实 LocalService 正常停止状态通过。修复数据库流式策略的文件缓存假设和根目录递归权限判断，并对共享核心重跑完整回归。

SVNKit 中文仓库 URL 修改 revision 属性的重复编码失败已在未修改的标准 mod_dav_svn + FSFS 上复现；该版本使用 ASCII 路由，服务器不二次解码。跨仓库 COPY 不支持。本机双进程不代表跨主机 HA，DAV readiness 也不代表完整工作线程存活探测。配置、恢复命令及限制见 [M5 DAV 指南](M5-DAV.zh-CN.md)，摘要见 [M5 证据](evidence/m5.json)；早期组件结果保留在 [开发记录](M5-development.zh-CN.md)。

## 后续验证与部署

继续完成固定真实历史、协议回归、故障账本、备份恢复和代表性性能测量，生成可校验的 Windows 部署包与中文新机指南。用户目前没有独立测试机，已明确先完成本机验证和部署包；全新 Windows 安装、整机重启、跨故障域和隔离 Polarion 应用演练仍待验收，M6/M7 不据此提前完成。
