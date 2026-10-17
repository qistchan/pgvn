# M6：合并与 patch 的 HTTP 组件验证

日期：2026-10-17。九个套件的 HTTP 原版函数选择已全部结束，并完成 FSFS/PgVN 对照与独立审计。**此处只记录 HTTP 组件；M6 尚未完成，较早 SVN patch69 的 FAIL 保留。** 详见[结构化证据](evidence/m6-network-merge-patch-http.json)及[首批检查点](M6-network-merge-patch-checkpoint.zh-CN.md)。

## 范围与结果

15 个单次计划覆盖 merge、merge_automatic、merge_authz、merge_tree_conflict、merge_reintegrate、patch、shelf、shelf2 和 svnmucc。逐个核对固定上游源码的函数 ID、名称、装饰器与完整 test_list；共选择 377 个原版函数，无遗漏、重号、新增排除或断言改写。merge 分六组、patch 分两组执行。

| 两后端合计 | 结果 |
| --- | ---: |
| PASS | 596 |
| 原有 XFAIL | 22 |
| 原有 SKIP | 136 |
| FAIL / XPASS | 0 / 0 |
| 非 SKIP 结果 | 618 |

shelf/shelf2 在本轮默认配置中未启用，共贡献 126 个 SKIP；另有 10 个原有 SKIP。这些跳过不计后端实际进入，也不证明启用 shelve 后的行为。11 对原有 XFAIL 的完整预期/实际输出及异常，在仅归一各侧精确工作目录与本轮 URL 后一致；没有改写客户端输出。

HTTP patch69 的两侧结果均为 PASS。此前 SVN 组 `m6-network-9490cbbdee6e` 的两侧原始 FAIL 及第一队列 FAIL 汇总保持原身份，未用本次 HTTP 结果替代，仍需受控环境定点复测。

## 构建、清理与冻结

构建清单保持 `5e3d4cc3f2f8491a27b6aaede96e1ec921b31f908ec4324574bc580ef323b1e9`；155 项注册输入与五个生产产物未修改。构建元数据仍为 `c036339` dirty，不随本次文档提交回写。没有新跑浏览器、完整 M5 或重打包，已有验收继续绑定原构建记录。

独立审计重读每组计划、报告、原始日志和 fixture 事件，确认两侧原版结果相同、原生仓库别名目录未生成。75 个内层、15 个外层 Windows Job 均已清空。随后只读核对这 15 个隔离库：所有者均为非超级用户，监控令牌与残留客户端连接均为零；数据库及角色保留。清理数值仅描述上述 HTTP 计划，其他正在运行的测试独立记录。

原始材料冻结到 `runtime/pgvn/baselines/m6-merge-patch-http-complete-659b0c128b9b`，共 579 份；索引 SHA-256 为 `cda745b2efe9367c6c37423bdc9e2859f8bbfcad7b2c3eeb9c08252e4822b2b6`。另将审计脚本、完整审计、原始 FAIL 汇总和未执行计划身份冻结到 controls 目录，位置与摘要见结构化证据。未复制密钥，也未复制正在运行的 HTTPS 或历史验证输出。

首版汇总在审计报告的字段类型检查处中断：`pairs` 是逐对记录的列表，不能直接与整数比较。保留该版源码，改为核对列表长度及精确 test/case 集合；复用哈希未变的 PASS 原始审计，没有重跑原版测试。

## 尚未完成

PGVN-601、PGVN-602、M6、M7 保持未完成。HTTPS 续跑、启用的 shelf 配置、SVN patch69 受控环境复测、正式管理工具绑定与其余套件继续推进；最终构建复跑、维护/真实历史备份、确认账本、跨故障域 HA、代表性性能及独立 Windows/Polarion 验收仍按[实施计划](../plans/2026-10-15-pgvn-implementation-plan.zh-CN.md)执行。
