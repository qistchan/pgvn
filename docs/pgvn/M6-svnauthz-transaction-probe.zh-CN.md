# M6：svnauthz 待提交事务读取探针

日期：2026-10-17。独立候选在真实 pre-commit hook 中读取待提交事务，与 stock FSFS 对照通过。**这是补充组件证据；未执行原版 Python 测试函数，不表示完整套件或 M6 通过。** 详见[结构化证据](evidence/m6-svnauthz-transaction-probe.json)。

## 实际执行与结果

单次计划 `m6-authz-txn-probe-dc9a23cb7a89` 只使用专用集群 `127.0.0.1:16432` 的新隔离库 `pgvn_m6_net_dc9a23cb7a89`。顺序启动一个 stock FSFS SVN 服务和一个 PgVN SVN 服务，最多同时运行一个本探针自有服务。每侧依次提交三种 `A/authz` 内容，pre-commit 内执行五个原版 CLI 命令；均指定实际 transaction ID。权限查询固定为 `jrandom` 对 `/` 的访问。

| 待提交文件内容 | `validate` | `accessof` | `accessof --is rw` | `accessof --is r` | `validate A/missing.authz` |
| --- | --- | --- | --- | --- | --- |
| 有效用户授权：`jrandom = rw` | 0 | 0，输出 `rw` | 0 | 3 | 2 |
| 上述文件追加非法行 `x` | 1 | 1 | 1 | 1 | 2 |
| 有效内联组：`writers = jrandom`、`@writers = rw` | 0 | 0，输出 `rw` | 0 | 3 | 2 |

两后端的 **15 对退出码、stdout 和 stderr 原字节完全相同，不做路径或文本归一化**。共执行 30 个 `svnauthz` CLI 进程，另有 30 个管理和客户端命令。两侧均发布修订 1、2、3；每次通过网络核对 HEAD 与文件原字节。后两次提交前确认旧 HEAD 文件与待提交内容不同，防止误读 HEAD 而通过。

被测 `A/authz` 是版本化测试文件，服务实际发布的授权策略独立保持可写。hook 捕获 CLI 返回值后允许提交，与上游事务测试的捕获方式一致；非法版本化文件没有作为服务策略发布。

## 原生打开与冻结身份

候选由未改动的原版 `tools/server-side/svnauthz.c`、现有受保护构造器和公共核心编译，SHA-256 为 `62498787dc1b6ac8437536fc44f1fbce53f7082f2f40bf58b4dfd9df38976b7c`。本轮没有替换运行目录中的工具。

PgVN 的 15 个调用均记录按 `pgvn:<内部仓库 ID>` 打开，进程代码页为 65001；读取的 transaction ID 与真实 pre-commit 事件相同。没有创建 `authz-probe` 仓库别名目录或 `db`、`conf`、`format`、`activities.d`。全局配置、日志与测试输入文件仍保留在隔离工作目录。

构建清单保持 `5e3d4cc3f2f8491a27b6aaede96e1ec921b31f908ec4324574bc580ef323b1e9`，155 项注册输入及五个生产产物均未改；构建身份继续为 `c036339` dirty，不随文档提交回写。本轮没有新跑浏览器、M5 或重打包，历史验收继续绑定原记录。

## 独立复核、清理与留存

独立审计重新读取全部捕获和原始输出、校验命令参数与哈希，完整比较两侧字节，并只读核对专用集群身份及本隔离库。两个自有 Windows Job 均清空；所有者为非超级用户，监控令牌和该库残留客户端连接均为零。数据库、角色和诊断文件保留；没有创建临时 Windows 服务或监控令牌。这些清理值仅描述本探针，其他兼容性续跑独立进行。

首版审计误认为有效文件的权限不匹配命令也应无 stderr，实际 stock 的退出码 3 附带 `not read only` 诊断。依据上游命令源码修正该假设，并加强为完整 stderr 字节相等；保留首版脚本及失败说明，没有重跑或修改原始协议探针。

冻结目录：`runtime/pgvn/baselines/m6-svnauthz-transaction-dc9a23cb7a89`，共 195 份材料。索引 SHA-256：`f418eede583b4700d5244bb0f2b2af24888eae9ffcd82987d2c8f846ed39cc22`。包含计划、报告、脚本、两侧日志与原始输出，并引用此前冻结的候选和源输入；排除生成的 `credentials.key`。没有复制未结束队列的输出。

## 后续接入与未完成范围

正式 `--tools-bin` 绑定及正确的 `svnauthz-validate` 旧名称仍为未应用草案。当前兼容性续跑结束并冻结后，再应用、重建并为原版 `svnauthz_tests` 及其余管理工具套件准备新身份计划；原版断言、装饰器和结果必须保留。

本轮只验证 SVN 入口下的五命令、三种内容，未验证 HTTP/HTTPS、旧名称事务模式或全部原版函数。原先 patch69 的 FAIL 及[合并与 patch 首批检查点](M6-network-merge-patch-checkpoint.zh-CN.md)保持不变。PGVN-601、PGVN-602、M6、M7 均未完成；最终构建复跑、维护/真实历史备份、确认账本与跨故障域 HA、代表性性能和独立 Windows/Polarion 验收继续按[实施计划](../plans/2026-10-15-pgvn-implementation-plan.zh-CN.md)推进。
