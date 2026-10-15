# M5 开发记录

M5 固定 Release 门禁 m5-21229c13cbc1 已通过，控制 schema 6，dav_protocol=true。正式入口和限制见 [M5 DAV 指南](M5-DAV.zh-CN.md)，最终记录见 [M5 证据](evidence/m5.json)。以下保留早期状态核心与候选构建的开发记录，早期证据不能替代最终固定构建验收。

已通过的 M4 程序与构建清单保存在 runtime/pgvn/baselines/m4-b67d041，用于真实的 5→6 升级检查。

## 已验证的状态核心

私有接口 pgfs_native_begin_bound_txn 在同一 SQL 事务中创建 SVN 暂存事务、初始属性和绑定。回调只能在给定连接上执行绑定 SQL，不能开始/提交 SQL 事务或调用 FS 修改接口。入口拒绝嵌套调用，仅在成功后修改输出。对应 abort_bound_txn 将中止及绑定状态更新放在同一单元内，不改变原有 FS 单元的嵌套或写流语义。

006_dav_state.sql 添加真实 DAV 事务登记和 activity/virtual 绑定。绑定以仓库 ID、种类和名称隔离，并验证创建者身份；提交结果直接从 pgfs.transactions 推导，保留回执和删除墓碑。service 得到必要 DML 权限，monitor 不得读取原始 DAV 状态表。

创建先持久化暂存数据，再运行 start-commit hook，最后在配置代数守卫下激活。外部 hook 能查到已保存的事务及属性。创建者持有会话 advisory lease，恢复程序只能接管已经失去持有者的 creating 状态，不按时间清理 active 事务。hook 拒绝和 hook 期间撤权留下 failed/aborted 状态。

UUID 在进入原生仓库层前统一为小写，避免不同文本形式绕过基于字符串的写流或创建者互斥。

通过的独立进程检查包括：

- 回调失败整体回滚、输出不变、二进制属性、中文与引号、并发唯一键竞争和提交前进程退出。
- 同名跨仓库/种类隔离、跨用户拒绝、只读/匿名创建拒绝、重复绑定无孤儿。
- 其他进程提交后读取同一个 revision，删除保留回执，不复用墓碑名称。
- 外部 hook 等待时暂存数据可见，创建者存活时不能恢复；创建者与 hook 进程丢失后能够恢复。
- hook 期间撤权拒绝激活，年久 active 事务不被误清理，缺少必需表时拒绝服务。
- 完整 verify，真实 M4 程序创建的 schema 5 升级到 6：身份、历史、二进制内容、旧迁移摘要保留；旧程序拒绝新结构，篡改迁移摘要被拒绝。

## 早期候选回归

schema 6 候选完成 M0–M3 七组回归（m3-9bc37556b100），包括标准 SVN/SVNKit、凭据、hooks、LocalService 和原有核心/传统插件用例；M4 smoke 与 API 检查也通过。产品摘要和测试编号见 [开发证据](evidence/m5-development.json)。这些记录对应当时的候选构建，后续 Apache 适配及授权缓存修复仍须对最终候选重新回归。

可分别运行 tests/pgvn/test_m5_creation.py、test_m5_registry.py、test_m5_upgrade.py，均明确输出 milestone_complete=false。

## Apache 接入边界

固定 Subversion 1.14.5 DAV 源码只在构建副本中适配。模块注册 provider pgvn，使用独立 dav_pgvn/authz_pgvn C 符号、过滤器和授权旁路 provider。全局 PgVNConfig 提供数据库连接与可选 hook 根目录；仓库和路由从数据库解析，不使用 SVNPath、SVNParentPath、activities.d 或每库配置目录。

仓库与 FS 绑定属于请求池，避免 keep-alive 请求长期持有旧策略。请求内授权策略缓存最多 32 个仓库，复制源和目标分别加载数据库策略。请求、历史和递归路径检查由固定上游协议实现调用。

HTTP 验证发现并修正 Location 配置为空、数据库流式授权误用文件缓存、根目录递归权限，以及原 handler/过滤器依赖本地 SVNPath 才启用的假设。最终构建已经完成真实 HTTP/HTTPS v1/v2、路径授权负面场景、多节点逐请求切换与恢复、Apache 工作进程遥测和完整回归，详见最终证据。
