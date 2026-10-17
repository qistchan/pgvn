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

## 监控改版：本机验证完成

已按 [页面重设计计划](../plans/2026-10-15-pgvn-monitor-redesign.zh-CN.md) 更新三个内嵌资源，交付明暗主题、健康摘要、趋势 KPI、可键盘读取的多线型图表、固定表头与独立滚动、事件/SQL 可视化和自适应 DAV 明细。断开及撤销同步清空页头和内容，新鲜度时钟与错误重绘遵守视图代数。

固定 Release 的完整 M4 门禁 `m4-297b2d21dc76` 八组通过，双主题真实浏览器 15 项及 DAV 6 项通过。另验证延迟响应竞争、减少动效计算样式和 38 项对比度；Windows 高对比度实际视觉检查待补。截图、源/产物摘要、原始验收记录与边界见 [改版证据](evidence/m4-monitor-redesign.json)。本次产品源码仅三个网页资源，M6/M7 范围保持进行中。

## 监控第二轮：本机验证完成（2026-10-16）

修复暂停期间心跳年龄不增长、健康条沿用旧计数和过期服务入口仍显示就绪的问题；增强键盘滚动、图表读数和选中状态，并收紧窄屏节点行高。真实 375×812 下双主题展开 DAV 详情均无整页横向溢出，主表自动刷新保留焦点、展开及滚动位置。

同一冻结 Release 的 M5 `m5-9cc4f3a5b58d` 十组通过，含 M4 `m4-f80d7e26b175` 八组及 M0–M3；浏览器 15+6 项、38 项计算对比度与静态契约通过。源码只改两个网页资源，只有 `pgvnserve.exe` 产品摘要变化。截图、原始发现和范围见 [第二轮证据](evidence/m4-monitor-polish.json)。实际 Windows 高对比度、多个 DAV 历史实例的内层表格焦点边界继续列为后续工作；M6/M7 及原部署包的版本边界保持独立。

## 多实例焦点修复：浏览器及完整回归通过（2026-10-16）

修复两个在线 DAV 实例内层表格共享焦点键的问题；新增双 DAV fixture，保留旧构建的实际串位记录。桌面/375px、明暗主题、历史实例并列的五组真实自动刷新检查，以及失联后回退 `main` 均通过。每次要求页面可见、采样记录确实改变。仅修改三个 JS 位置和测试 fixture；产品中仅 `pgvnserve.exe` 摘要改变。

随后按用户明确授权，在原隔离库创建一枚 600 秒只读令牌，补验自动撤销后焦点回到令牌输入、清空所有状态数据；令牌已撤销且进程清理。原记录与缺项补验在相同资源/产物摘要下组成浏览器 15+6 项 PASS。一次获批的完整 M5 `m5-57cb4d03584a` 十组通过，含 M4 `m4-bd6125edca4f` 八组和 M0–M3，冻结构建前后校验通过。

原始 PARTIAL、门禁拒绝和诊断截图保留在 [组件原记录](evidence/m4-monitor-focus.json)，新的可核对截图与来源见 [最终验收](evidence/m4-monitor-focus-acceptance.json)。实际 Windows 高对比度视觉仍待补；M6/M7 及旧部署包保持各自版本边界。

## M6 组件和 M7 本机部署包

固定真实历史组件 `m6-history-7dec7f595eb4` 通过：完整 verify 638 个修订，逐项比较全部变更路径/修订属性、五个完整逻辑快照，原生 dump 回灌 FSFS 后一致，导出摘要与输入相同。不同非超级用户所有者的备份恢复组件 `m6-backup-4e45713029fa` 通过：30 个表/序列账本一致，ACL、凭据/令牌、锁、回执及未完成 DAV 事务可恢复。原始失败与修正原因保留，范围见 [M6 组件验证](M6-validation.zh-CN.md)。

离线包生成器收集匹配的 x64 原生依赖和随附许可，输出可重复的 ZIP、逐文件来源/摘要以及 Windows PowerShell 5.1 安装、烟测、备份恢复和卸载脚本。首轮英文路径（含空格）与英文账号的本机 LocalService 双协议、恢复后继续提交和清理已验证。本轮进一步定位并修复中文安装路径的 DAV 连接问题：自编译 `pgvnhttpd.exe`，为自有进程链接 UTF-8 清单，包内无签名 PostgreSQL 客户端仅变换私有副本并记录来源；原 SDK 签名文件不改。中文、emoji 和组合字符目录以及中文账号配合标准 svn 的完整安装/备份恢复已通过，固定 SDK 的 svnmucc 中文账号差异仍保留。实际包记录见 [M7 本机部署](M7-package-local.zh-CN.md)，机制及验证边界见 [UTF-8 记录](Windows-UTF8.zh-CN.md)。

当前仅生成明确标记的 development-snapshot；正式包要求同一冻结构建完成 M6。完整上游协议入口、故障域和长窗口性能仍在进行，M6/M7 未完成。干净机器、整机重启及 Polarion 应用演练继续待独立环境。

## M6 原生上游入口组件

新增固定上游 FS/仓库 C 套件入口，以生产公共核心静态归档及按 ID 对象替换创建/重开夹具。上游用例与断言保持原样，测试自身保留 C 断言；辅助文件的生成副本仅重命名四个构造函数定义，其他辅助调用会进入 PgVN 夹具。适用矩阵 PgVN 87 PASS、4 SKIP、11 项架构边界；原版 FSFS 全套 101 PASS、1 SKIP，同一修复核心的传统目录模式仓库组 31 PASS、1 SKIP。

修复 16 个旧路径 getter 在无目录对象上可能发生的空指针崩溃，验证返回 NULL、原目录模式行为、旧锁令牌 API 真实提交和测试依赖篡改拒绝。原始失败、排除理由、源码/归档/私有 DLL 摘要及本轮功能回归记录见 [M6 原生入口](M6-native.zh-CN.md)。M6 仍在进行；完整上游网络协议入口、故障域/账本和性能测量未据此完成。此前部署包与网页结果仍属于原构建，本轮没有复用为新二进制的完整验收。

## 后续验证与部署

继续完成固定真实历史、协议回归、故障账本、备份恢复和代表性性能测量，生成可校验的 Windows 部署包与中文新机指南。用户目前没有独立测试机，已明确先完成本机验证和部署包；全新 Windows 安装、整机重启、跨故障域和隔离 Polarion 应用演练仍待验收，M6/M7 不据此提前完成。

## M6 网络入口输入：目录与快照完成（2026-10-16）

固定 Subversion 1.14.5 归档与 234 个输入文件，解析 62 个上游套件、1,984 个静态测试编号，生成无旧字节码的源码快照。未登记输入、源码副本改动和归档副本改动均被拒绝；实际原生产品摘要保持不变。执行用例数为 0，PGVN-601 未完成。下一步是绑定原生协议/admin fixture 并执行原断言，说明见 [网络测试入口](M6-upstream-network.zh-CN.md)，固定记录见 [目录证据](evidence/m6-network-catalog.json)。

## M6 原版 CLI 命令绑定：差分组件通过（2026-10-16）

将未修改的原版 svnadmin/svnlook 命令体链接当前公共核心，以受限 ID 夹具代替仓库目录构造；构建同时核对原始归档与命令体字节。离线 9 组、隔离差分 19 组通过，包含二进制/属性/复制、原版 load/verify、16 组 svnlook 与 FSFS 对照，以及 dump 回灌原版 FSFS。首轮适配器空指针崩溃的 FAIL 和修复重跑均保留。

五个生产产品摘要不变。该组件尚未执行上游 Python 网络用例，也未验收全部维护入口；PGVN-601 和 M6 保持进行中。命令、隔离范围及证据见 [网络测试入口](M6-upstream-network.zh-CN.md) 和 [CLI 组件记录](evidence/m6-network-tools.json)。

## M6 上游辅助工具：本机组件通过（2026-10-16）

从固定源码构建五种原版 C 辅助工具和私有工作副本 DLL，保留 SDK 全部 305 个 WC 导出，标准客户端 DLL 未替换。14 组实际 FSFS/WC 检查通过；私有 DLL 篡改在测试执行前被拒绝并恢复，原检查脚本两次解析 FAIL 保留。扩展后的 CLI 19 组差分和 9 组离线检查重跑通过。

这些是网络入口的前置组件，上游 Python 用例数仍为 0，PGVN-601 未完成。原生 lock-helper 的真实锁路径及原版断言的网络运行继续推进。范围见 [网络入口](M6-upstream-network.zh-CN.md)，摘要见 [辅助工具证据](evidence/m6-upstream-helpers.json)。

## M6 原版 SVN 网络入口：基础套件与锁用例通过（2026-10-16）

原版基础 1–71 与 lock 53 在独立 PgVN 和原版 svnserve + FSFS 上执行，原函数/断言/装饰器不改。最终每侧为 67 PASS、1 个原有 XFAIL、4 个原有 SKIP，未新增排除；仓库别名没有本地目录，测试 Job 内进程结束后均为 0。命令、适配边界和冻结证据见 [网络入口](M6-upstream-network.zh-CN.md) 与 [本轮记录](evidence/m6-network-entry.json)。

本轮修复 hook 上下文的 Windows 文件发布竞态，并将旧用例的任意上传样本放入全局 fixture 目录。随后原用例发现反斜杠节点名被路由规则拒绝；核心已区分严格路由和逻辑路径，授权及管理查询同步支持字面反斜杠。M1 路由边界在已授权 M6 库通过，最终候选的 CLI 19 组、辅助工具 14 组、离线 9 组也通过。原始失败保留。

pgvnserve、pgvnadmin 和 mod_dav_pgvn 产品已变化，网页资源未改；新的浏览器和完整 M5 回归尚待执行，旧验收不作为本构建结果。其余 SVN 套件、HTTP/HTTPS、完整适用性、故障域/确认账本及代表性性能继续推进，PGVN-601 与 M6/M7 均未完成。

## M6 原版 HTTP/HTTPS 网络入口：基础套件与锁用例通过（2026-10-16）

原版 HTTP 与 HTTPS 各完成 basic 1–71 和 lock 53–54，在 PgVN 和固定原版 DAV + FSFS 两侧结果一致；各协议每侧 69 PASS、1 个原有 XFAIL、3 个原有 SKIP。原用例 52 在 HTTP/HTTPS 下适用并通过，比 SVN 组少一个跳过。原断言/装饰器保持不变，没有新增排除。HTTPS 使用私有 CA 验证证书链与主机名，未知 CA/错误主机名均拒绝，异常缓存为空；直接 DAV 锁请求也通过。

保留首轮 IP URL 的证书主机名 FAIL，改用 localhost；同时修正 IPv4 单独监听导致的本机 IPv6 回退等待，并在最终输入上重跑。源码仅涉及测试，五个生产产品保持 492cced 核心修复候选的字节；所有本轮 Job 进程清空，库及所有者保留，未创建监控令牌或 Windows 临时服务。命令及边界见 [网络入口](M6-upstream-network.zh-CN.md)，冻结原始证据见 [HTTP/HTTPS 组件记录](evidence/m6-network-http.json)。

剩余套件与完整适用性、授权挂载/hook 边界、当前核心产品的全 M5/浏览器回归、故障与性能出口继续推进；PGVN-601 和 M6/M7 保持未完成。

## M6 原版授权套件与匹配的完整回归（2026-10-16）

原版 authz 1–35 三协议均与 FSFS 对照一致：每侧 31 PASS；SVN 另有 3 个原有 XFAIL、1 个原有 SKIP，HTTP/HTTPS 各有 2 个原有 XFAIL、2 个原有 SKIP，无新增排除。补齐精确仓库名映射、全局配置/groups、无效文件夹具、旧历史 dump/load，以及原版 dontdothat 模块/额外挂载。所有原生仓库别名均不存在本地目录，11 项离线边界保护通过，原始失败保留。

生产只修复无效 authz 被归为 HTTP 503 的分类：现返回 403，真实数据库故障仍返回 503。新浏览器 15+6 项和完整 M5 `m5-7e3dbdd48f38`（含 M4 `m4-0041b28dd2df` 八组、M0–M3 七组）通过，均绑定清单 `612a8df7a539a427294e49a6b089c5577e082a56dc04d5be2a9234a2af5329ce`；本轮令牌已撤销，自有夹具已停止。详见[授权套件与回归](M6-network-authz.zh-CN.md)和[冻结证据](evidence/m6-network-authz.json)。

此前记录中的“当前核心的新浏览器/完整 M5 待执行”已由本轮完成；各早期结果仍保留原构建身份。PGVN-601 的剩余协议矩阵、维护语义、最终核心的真实历史/备份复核、跨故障域和确认账本、代表性性能继续推进。M6/M7 仍未完成，实际 Windows 高对比度视觉、干净机器、重启和 Polarion 应用验收待独立环境。

## 2026-10-16：原版 commit/update 三协议及匹配构建完整回归

原版 commit 1–74 在 SVN、HTTP、HTTPS 各侧 71 PASS/2 原有 XFAIL/1 原有 SKIP；update 1–86 各侧 84 PASS/2 原有 XFAIL，与原版 FSFS 对照逐项一致。全量共 960 个结果、954 次实际进入用例，未新增排除；原有 XFAIL 的诊断在消除工作目录差异后也一致。修复标准 hook 退出文本与 DAV start-commit 反馈覆盖；hooks.log 临时文件及 Windows file:// 准备写入仅作夹具适配，原断言和被测 HTTP/HTTPS 更新保持不变。

冻结构建 `8f094d1e6502bc53fd099e989b8b004e3b236a8ef101e892bbd1cb9d7dd5faa5` 上，新浏览器 15+6 项及完整 M5 `m5-d2be426ed5d1`（10 组，600.047 秒，内含 M4 `m4-e1bac01cdf67` 八组及 M3 `m3-dbc36e7240ca` 七组）通过。六轮全量和六轮定点各 28 个 Job 已清理，门禁 22 个隔离库的 7 枚令牌均已撤销，自有运行进程与临时服务均为 0；数据库保留。

同步生成开发包 `7ed4abd2a273bd16`，203 个文件、ZIP 205 个条目及 95 个 x64 原生镜像位数核对通过，三个主入口、svn 和 pg_dump 通过清空 PATH 后的版本启动检查。该包仍为 development-snapshot，不宣告干净机器或 Polarion 验收。原始 FAIL、修复确认、全部源代码与产物及浏览器/M5/清理/部署包证据均已冻结，见[组件记录](M6-network-commit-update.zh-CN.md)与[结构化证据](evidence/m6-network-commit-update.json)。

PGVN-601 与 M6/M7 保持未完成。剩余套件/适用性、维护语义、最终构建的早期套件与真实历史/备份复核、跨故障域与确认账本、代表性持续性能及独立环境验收继续推进。较早结果保留其原构建身份，网络用例执行耗时不作为性能基准。

## 2026-10-16：原版 copy/prop 三协议、表单清理及匹配构建完整回归

原版 copy 1–118 在 SVN、HTTP、HTTPS 各侧 116 PASS/1 原有 SKIP/1 原有 XFAIL；prop 1–46 各侧 45 PASS/1 原有 SKIP，与 FSFS 对照逐项一致。六轮共 984 个结果、972 次实际进入，未新增排除；XFAIL 诊断仅消除工作目录差异后也一致。复制准备严格限定已登记源、声明目标和默认策略，通过生产 hotcopy 创建独立内部 ID，保留公开 UUID、HEAD 并完整 verify；原 relocate 断言保持不变。另补全 revprop 拒绝消息中的标准 hook 名称，原错误码和策略不变。

完整浏览器验收发现清空 value 的候选在重载再连接后仍有自动填充残留，原始 FAIL、定点 smoke 与中断会话均保留。最终保留早期清空，并在显示登录和恢复焦点后执行原生表单 reset；完整 15+6 项、手动断开及完整故障/恢复/重载后的自动撤销均通过。输入为空、占位符可见、自动填充标记为 false、数据清空、焦点回到令牌框，AX 观察前后结果一致。

冻结清单 `dff1a26e649812903ce70f054fadc37a149ab9baa6424c419c21f4fe6e5dc7af` 上，34 项离线边界、六轮网络全量和完整 M5 `m5-279992c5cea3`（10 组，560.437 秒；内含 M4 `m4-32a12d8202c5` 八组及 M3 `m3-27a08f1333ea` 七组）通过。真实数据库故障仍返回 503 并恢复 200。最终网络 28 个 Job 清空，门禁 22 个库的 7 枚令牌及本组件五枚浏览器令牌均已撤销，自有进程和临时服务为 0；库及角色保留。

匹配开发包 `789ce2453389afee` 的 207 个负载文件、209 个 ZIP 条目和 95 个 x64 原生镜像已独立核对，清空 PATH 的版本启动通过。其状态仍为 development-snapshot。原始失败、全部候选、151 项最终冻结输入、产物、浏览器/M5/清理/包证据均按摘要归档，见[组件记录](M6-network-copy-prop.zh-CN.md)与[结构化证据](evidence/m6-network-copy-prop.json)。网络运行耗时不作为性能基准。

PGVN-601、M6、M7 保持未完成。继续其余套件和完整适用性、最终产品的早期套件/真实历史/备份复核、维护语义、跨故障域/确认账本、代表性性能及独立环境验收；实际 Windows 高对比度视觉仍待补。

## 2026-10-16：原版 log/diff 三协议

原版 log 1–46 与 diff 1–96 在 SVN、HTTP、HTTPS 完成 FSFS 对照；每侧 log 为 45 PASS/1 原有 XFAIL，diff 为 91 PASS/4 原有 XFAIL/1 Windows 原有 SKIP。六轮共 852 个结果、846 次实际进入，142 个唯一原版函数，未新增排除。原版 XFAIL 的完整预期/实际输出及异常，经精确归一工作目录与本轮监听地址后匹配；客户端输出和原断言未改。

补齐 log-msg 临时输入及其 SVN 文件参数、真实 SVN 1.5 兼容 FSFS 经 dump/load 的历史准备，以及客户端通用 Python/batch 脚本与原生仓库 hook 的分流。72 项无数据库/无子进程的离线边界检查、三协议定点与全量均通过；源模板 hooks 限制及 diff 48 原失败、已执行计划和未执行旧计划分别保留。

冻结清单 `7b98c639a5b70e9d6ef7ae8e46aef6422f108b89eed51809f3e7eb11a15a98f4` 含 154 项本仓库输入，改动为五个测试文件。五个生产产物、三份网页资源、67 项运行依赖和 84 项 Apache 依赖与此前 copy/prop 产品验收构建相同。浏览器和完整 M5 复用其产品层验收，原记录仍绑定原清单；本轮没有把它们改写为新运行。原生仓库别名均未生成目录，最终 28 个 Job 已关闭，未创建监控令牌或临时 Windows 服务；库及角色保留。

详细结果与修复边界见[log/diff 组件](M6-network-log-diff.zh-CN.md)和[结构化证据](evidence/m6-network-log-diff.json)，开发包的完整性与最小 PATH 检查见[包旁记录](evidence/m7-package-log-diff.json)。包为 development-snapshot。PGVN-601、M6、M7 保持未完成；剩余套件/适用性、最终构建复跑、维护、真实历史/备份复核、跨故障域/确认账本、代表性性能和独立环境验收继续推进。

## 2026-10-16：仓库工作流、会话容量与监控一致性

原版 export、import、cat、blame、history、info 六组完整套件及 checkout1–13 网络用例在 SVN、HTTP、HTTPS 完成 FSFS 对照，共612个结果、606次实际进入、102个唯一被选函数。6对原有XFAIL输出一致；原有Windows SKIP保留。checkout14的物理file://提供器边界保留首轮stockPASS/nativeFAIL记录，未改成SKIP或PASS，也不据此自动排除其他用例。

修复原版info21暴露的固定16会话接纳问题：`max-connections`缺省64，可设1–64；接纳、排空和取消使用相同上限。网页容量来自监听器实际值，API、心跳及Prometheus一致；配置改2→8而未重启时仍报告并执行2，重启后才为8。默认64/显式1、2、额外连接拒绝、释放后认证及7类非法配置均通过；64空闲会话的LocalService排空为12.250秒。

冻结清单`fb66ece247011e7940fdbd991a09397786b04c906204c5587051498ef6387611`下，真实浏览器15+6项及完整M5`m5-ba210e325efc`十组通过，含M4`m4-d4c29065dec3`八组及M0–M3。首轮网页硬编码16的FAIL保留；外层Job即时计数非零的尝试未接纳，以新库重跑后与此前正常关闭的结果核对。后续调度记录PID并最多等待5秒自然排空，关闭前仍须为0。最终runtime进程、临时服务和本轮未撤销监控令牌均为0，库及角色保留。

详见[仓库工作流与容量修复](M6-network-workflows.zh-CN.md)及[结构化证据](evidence/m6-network-workflows.json)，开发包校验见[包旁记录](evidence/m7-package-workflows.json)。PGVN-601、M6、M7仍未完成；其余套件/适用性、最终构建复跑、维护与真实历史备份、跨故障域/确认账本、代表性性能及独立Windows/Polarion验收继续推进。

## 2026-10-16：工作副本语义与仓库不存在错误

原版 switch 1–39、depth 1–50、revert 1–36 在 SVN、HTTP、HTTPS 完成 FSFS 对照：125 个唯一函数、750 个实际进入结果，714 PASS、36 个原有 XFAIL；18 对 XFAIL 诊断一致，未新增排除。修复 SVN 通用仓库不存在文本及 DAV URI 翻译提前返回，未知/停用仓库现统一为 404 XML/E180001；既有路径与真实数据库故障分类保留，COPY 缺失目标不得产生修订。

冻结清单 `e63c40be2a119368e6060d4e4637dfc2ee8936d1a8dc45106cfa1aa0e56bb997` 下，浏览器 15+6 项和完整 M5 `m5-0587e0a9e03b` 十组通过，耗时 578.641 秒，含 M4 `m4-77ebedd6492f` 八组和 M0–M3。375×812、双详情刷新焦点、故障恢复及令牌撤销清屏均实测；原始网络失败、三个候选、浏览器部分尝试和漏带测试环境变量的一轮 FAIL 保留。最终网络 42/9 个内外层 Job 清空，运行进程、临时服务和本轮有效监控令牌均为零；库与角色保留。

见[工作副本组件](M6-network-wc-workflows.zh-CN.md)和[结构化证据](evidence/m6-network-wc-workflows.json)，匹配开发包校验见[包旁记录](evidence/m7-package-wc-workflows.json)。包仍为 development-snapshot。PGVN-601、M6、M7 保持未完成；旧组件绑定各自构建，继续剩余适用性/套件、最终构建复跑、维护与真实历史备份、跨故障域/确认账本、代表性性能及独立 Windows/Polarion 验收。

## 2026-10-17：工作副本状态与完整覆盖账本

原版 stat、resolve、relocate、tree_conflict、move、schedule、changelist、wc、svnversion、entries 十个完整套件在 SVN、HTTP、HTTPS 完成 FSFS 对照：163 个唯一函数、978 个结果，894 PASS、12 个原有 XFAIL、72 个原有 SKIP；实际进入 906 次。无新增排除或断言改写，原有 XFAIL 诊断逐对一致。

冻结清单仍为 `e63c40be2a119368e6060d4e4637dfc2ee8936d1a8dc45106cfa1aa0e56bb997`，154 项输入与五个生产产物均未修改；明确复用同构建浏览器 15+6 项及完整 M5 `m5-0587e0a9e03b` 原始验收，不改写为新运行。140 个内层和 30 个外层 Job 清空，30 个隔离库所有者均为非超级用户、监控令牌行数为零；最终运行进程和临时服务为零，库与角色保留。

原调度器中断后保留 20 组完整结果，3 组缺少终态报告的尝试单独冻结且不计通过。续跑为这 3 组创建新 UUID，首次执行另 7 个原未启动计划，未重复执行已启动计划。上述 140/30 个 Job 仅对应最终接纳的完整 30 组；原中断尝试不宣称正常关闭，其隔离库、角色和记录继续保留。

新增[覆盖账本](M6-network-coverage.zh-CN.md)核对全部 62 套件、1,984 个函数与已接纳报告/快照。当前构建三协议有结果的函数从 125 增至 288；676 个仅有旧构建结果，1020 个尚无已接纳引用，原有 SKIP 不计实际进入。细节见[状态与冲突组件](M6-network-wc-state.zh-CN.md)和[结构化证据](evidence/m6-network-wc-state.json)，开发包检查见[包旁记录](evidence/m7-package-wc-state.json)。PGVN-601、M6、M7 保持未完成，继续剩余适用性/套件、最终产品复跑、维护/真实历史备份、跨故障域/确认账本、代表性性能及独立 Windows/Polarion 验收。

## 2026-10-17：自动属性、继承与外部引用

原版 autoprop、getopt、input_validation、pegrev_parse、trans、special、mergeinfo、iprop、iprop_authz、externals 十个完整套件在 SVN、HTTP、HTTPS 完成 FSFS/PgVN 对照：280 个唯一函数、1,680 个结果，1512 PASS、126 个原有 XFAIL、42 个原有 SKIP；实际进入 1638 次。原函数、装饰器、golden 文件和断言未修改，没有新增排除。63 对 XFAIL 中 50 对归一后文本相同，另 13 对仅状态树字典条目顺序不同，路径键值及块外诊断完全相同；原严格文本比较的 FAIL 记录保留，专项复核拒绝全部 78 个内容变更反例，详见[诊断复核](M6-network-client-properties.zh-CN.md#xfail-诊断复核)。getopt 和部分输入/peg 校验可在客户端结束，协议夹具结果不等于这些用例访问过 PostgreSQL。

本轮 30 个新计划均首次执行完成，140 个内层、30 个外层 Job 关闭前活动进程为零；30 个隔离库所有者均为非超级用户，监控令牌与残留客户端连接为零，最终 runtime 进程和临时测试服务为零。隔离库、角色及原始报告保留。HTTPS 两侧均核对证书链、主机名及未知 CA/错误主机名拒绝对照。

当前冻结构建三协议结果从 288 增至 **568 个函数、23 个完整套件**；另有 676 个函数仅有旧构建结果、740 个尚无已接纳引用。最新[覆盖账本](M6-network-coverage.zh-CN.md)指向新的不可变 JSON，上一版 JSON 与旧组件哈希保持不变；原有 3 份中断尝试继续保留且不计通过。详见[本轮组件](M6-network-client-properties.zh-CN.md)和[结构化证据](evidence/m6-network-client-properties.json)。

154 项注册输入、五个产物和依赖保持原冻结构建，明确复用浏览器 15+6 项及完整 M5 `m5-0587e0a9e03b` 的原验收。现有开发包 `fa6fd390be872efa` 的运行时相同，本轮只复核原包记录与哈希，没有重建运行包；包内文档早于本组件，见[原包验收](evidence/m7-package-wc-state.json)。PGVN-601、M6、M7 仍未完成，继续剩余套件/适用性、最终构建复跑、维护/真实历史备份、确认账本与跨故障域 HA、代表性性能及独立 Windows/Polarion 验收。

## 2026-10-17：旧工作副本升级与 Windows 搬移夹具

原版 upgrade 1–35 及 authz24 在 SVN、HTTP、HTTPS 完成 FSFS/PgVN 对照，36 个唯一函数、216 个结果全部 PASS。补齐锁定 wc.h、允许 shelf2 套件名；两侧旧工作副本准备使用相同的有界同卷 rename 重试，逐项保留字节和 Windows 属性。原版函数、断言和结果未改，没有新增排除。21 项离线边界检查通过，含真实目录共享锁释放及永久拒绝；228 次搬移签名两侧一致，记录 5 次临时 WinError 5 重试成功。

冻结清单 `5e3d4cc3f2f8491a27b6aaede96e1ec921b31f908ec4324574bc580ef323b1e9` 含 155 项输入，五个生产产物、网页和运行依赖与此前产品验收构建相同；原浏览器/M5 仍绑定原记录，本轮未重跑或重打包。首次网络失败、两轮离线失败及旧计划分别保留，原失败占用句柄的进程未知。28 个内层及 6 个外层 Job 清空，六个隔离库非超级用户所有者、零令牌和零残留连接；最终 runtime 进程及临时服务为零，库与角色保留。

PGVN-601、M6、M7 保持未完成。继续另外 45 个新计划及完整适用性、最终构建复跑、维护/真实历史备份、确认账本与跨故障域 HA、代表性性能、独立 Windows/Polarion 验收。

详见[升级夹具组件](M6-network-upgrade-fixtures.zh-CN.md)和[结构化证据](evidence/m6-network-upgrade-fixtures.json)。

## 2026-10-17：合并与 patch 首批检查点

原版合并、patch、shelf/shelf2 与 svnmucc 的首批 18 组结束：17 组 PASS、1 组 FAIL；830 个结果为 666 PASS、26 原有 XFAIL、136 原有 SKIP、2 FAIL。patch69 两侧 EICAR 文件消失由对应火绒事件、相同原始字节哈希和进程/时间关联确认，原失败不改写；需要受控环境复测。默认未启用的 shelf 贡献 126 个 SKIP，不计后端实际进入。13 对原有 XFAIL 诊断相同，没有修改原断言或新增排除。

155 项输入及五个生产产物保持 `5e3d4cc3…`；首批 75/18 个内外层 Job 清空，18 个隔离库无超级用户所有者、监控令牌或残留连接。首批结束时的零进程/服务观察与随后续跑分开记录。仅恢复从未启动的 27 个原计划，全部失败继续保留，任何失败使总轮次为 FAIL。独立 svnauthz 候选完成 15 组本地 stock 对照及 7 项连接前守卫检查，尚未纳入正式工具绑定或验证事务；当前工具路径缺口已确认。

PGVN-601、PGVN-602、M6、M7 均保持未完成。剩余矩阵、EICAR 定点复测、启用的 shelf 配置、正式管理工具绑定、最终构建复跑、维护/历史备份、确认账本与跨故障域 HA、代表性性能及独立 Windows/Polarion 验收继续推进。

详见[首批检查点](M6-network-merge-patch-checkpoint.zh-CN.md)及其结构化证据；原始队列、失败与所有未执行计划保持原身份。

## 2026-10-17：svnauthz 待提交事务读取探针

独立候选在真实 pre-commit hook 中读取三种待提交 authz 内容，两侧共 30 个 svnauthz CLI 进程，15 对退出码、stdout、stderr 原字节完全一致；有效用户、非法语法、内联组、权限不匹配与缺失文件路径均覆盖。两侧修订 1–3 及文件字节通过 SVN 核对，PgVN 记录 15 次按内部 ID 打开且无本地仓库别名目录。此为补充组件探针，原版 Python 函数执行数为零，不合并进原套件覆盖率。

195 份材料已冻结并排除生成的密钥，独立审计保留首版对退出码 3 的 stderr 假设错误及修正。两个自有 Job 清空，单个隔离库非超级用户所有者、零令牌、零残留客户端连接；不描述其他正在续跑的进程。155 项注册输入及五个产物保持 `5e3d4cc3…`，正式工具绑定尚未应用，未新增浏览器/M5/包验收。

PGVN-601、PGVN-602、M6、M7 保持未完成；等待当前队列结束并冻结后接入工具草案，再执行原版管理工具三协议矩阵，其余原计划范围保持。详见[事务探针](M6-svnauthz-transaction-probe.zh-CN.md)及[结构化证据](evidence/m6-svnauthz-transaction-probe.json)。
