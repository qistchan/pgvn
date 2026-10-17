# M6：启用实验 shelf 配置的三协议原版回归

日期：2026-10-17。六组完整原版套件在 SVN、HTTP、HTTPS 完成 FSFS 与 PgVN 对照，六个调度报告均为 PASS；严格 XFAIL 文本诊断审计为 FAIL，保留为未验收组件。共 63 个原版函数、378 个两后端协议结果：330 PASS、48 个原有 XFAIL，零 SKIP、FAIL 或 XPASS。详见[结构化证据](evidence/m6-network-shelf-enabled.json)。

## 实际进入的范围

| 原版套件 | 子进程环境 | 原版启用条件 | 每协议函数数 | 两后端、三协议结果 |
| --- | --- | --- | ---: | --- |
| shelf2_tests | `SVN_EXPERIMENTAL_COMMANDS=shelf2` | `shelf2_enabled()` | 31 | 186 PASS |
| shelf_tests | `SVN_EXPERIMENTAL_COMMANDS=shelf3` | `shelf3_enabled()` | 32 | 144 PASS、48 原有 XFAIL |

仅添加上游源码定义的环境变量，原函数、装饰器、test_list、预期结果及断言均未修改。审计逐一核对全套 ID 与名称、原始源码与条件函数 AST 摘要，独立计算原启用条件为真，并确认每个函数都产生非 SKIP 结果。24 对原有 XFAIL 的完整诊断，仅归一精确工作目录与本轮 URL 后有 22 对完全一致、2 对不同，严格审计保持 FAIL。独立只读审计从原始日志重取诊断，以 AST 解析全部预期及实际状态树：HTTP shelf_tests #30 的实际树、HTTPS #27 的预期树仅打印键序不同，各树的全部 21 个节点与属性、树外诊断前缀、完整异常及 XFAIL 行均相同。两后端仍未达到原版对 A/mu 冲突状态的预期：#30 期望 ` C` 而实际 ` M`，#27 期望 `C ` 而实际 `M `。该解释不改严格归一规则或失败状态，也不证明断言之后的行为。XFAIL 仍为 XFAIL，不计通过。

这 63 个函数与此前默认未启用配置选择的是同一批函数，不增加唯一函数覆盖数。此次补充的是启用状态下的真实执行证据。实验 shelf 客户端仍有上游原有 XFAIL，本轮不表示所有 shelve/unshelve 行为均已实现。

## 独立调度与隔离

保留原六个单次计划及其字节摘要。原未执行调度脚本要求此前矩阵全部成功；新版仅要求之前 45 组已经终态、选择完整、冻结身份一致且 Job 排空，再独立运行这六组。这项前置条件调整未改变任何原版用例或通过标准：零 SKIP 仍为必要条件，首个失败停止新派发，已启动驱动自然结束。旧脚本、新脚本、变更理由及先前两个队列的报告均冻结。

此前[合并/patch 矩阵](M6-network-merge-patch-matrix.zh-CN.md)及首队列仍为 FAIL，原 SVN patch 失败和严格诊断差异未重分类，本组件不能替代它们的验收。

六个隔离库均为非超级用户所有者，终态只读审计为零监控令牌、零残留客户端连接；28 个内层和 6 个外层自有 Windows Job 均排空。没有创建每库仓库别名目录，未创建监控令牌或临时 Windows 服务，数据库和角色保留。

## 冻结与后续

155 项注册输入、五个生产产物及依赖保持冻结构建 `5e3d4cc3…`，构建元数据仍为 `c036339` dirty。原始网络材料 384 份冻结到 `runtime/pgvn/baselines/m6-enabled-shelf-f428753c6813`，索引 SHA-256 为 `027608a1e0fc033a299b6a3cc4308ff886758742cc4b951ccece580b236b7deb`；另有 40 份启用条件、计划和调度审计材料冻结到 `runtime/pgvn/baselines/m6-enabled-shelf-controls-f428753c6813`，索引 SHA-256 为 `b40562fa1b4f1a68895bfcb0a373ddb6bb6704c51c68cbf80662ccb4ae0ab21f`。未复制密钥或其他运行中任务输出，未新增浏览器、完整 M5 或包验收。

PGVN-601、PGVN-602、M6、M7 保持未完成，继续正式管理工具绑定、其余原版套件、真实历史数据库备份、协议确认账本、跨故障域与代表性性能，以及独立 Windows/Polarion 验收，详见[实施计划](../plans/2026-10-15-pgvn-implementation-plan.zh-CN.md)。
