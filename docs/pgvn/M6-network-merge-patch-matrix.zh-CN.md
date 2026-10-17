# M6：三协议合并与 patch 矩阵执行完成，原失败保留

日期：2026-10-17。原定 45 个单次计划现已全部结束，覆盖 SVN、HTTP、HTTPS 的九个原版套件。**矩阵总结果仍为 FAIL；执行完成不等于兼容性验收通过。** 第一队列 FAIL 与所有失败用例原样保留。详见[结构化证据](evidence/m6-network-merge-patch-matrix.json)和[首批检查点](M6-network-merge-patch-checkpoint.zh-CN.md)。

## 完整范围与结果

每种协议均选择全部 377 个原版函数，包含 merge、merge_automatic、merge_authz、merge_tree_conflict、merge_reintegrate、patch、shelf、shelf2 和 svnmucc；两后端合计 2,262 个结果。按固定上游源码逐一核对全部函数 ID、名称和完整 test_list，无漏号、重号、新增排除或原断言改写。

| 协议，两后端合计 | PASS | 原有 XFAIL | 原有 SKIP | FAIL | XPASS |
| --- | ---: | ---: | ---: | ---: | ---: |
| SVN | 594 | 22 | 136 | 2 | 0 |
| HTTP | 596 | 22 | 136 | 0 | 0 |
| HTTPS | 596 | 22 | 136 | 0 | 0 |

全矩阵非 SKIP 结果为 1854。33 对原有 XFAIL 中，32 对完整诊断在仅归一精确工作目录及本轮 URL 后一致；HTTPS `merge_tests` #49 的严格文本比较仍为 FAIL。两侧打印的四个节点及属性相同，完整异常栈和结果行相同；首个报告的冲突节点分别为 `tfile1`、`tfile2`，树的打印顺序也相应不同。上游 `compare_trees` 按子节点顺序在首个缺失节点处抛异常，与该现象一致；此项分析不能证明原断言之后的合并行为正确。首轮严格比较失败、原始日志、两份诊断及单独分析均保留，未扩大归一规则或重写结果。

默认未启用的 shelf/shelf2 共贡献 378 个 SKIP，不计后端实际进入，启用配置另行执行。所有两侧原版结果分类相同，但相同的失败仍记为 FAIL。

| 协议 | 计划 | 原版用例 | 后端 | 原结果 |
| --- | --- | --- | --- | --- |
| SVN | `m6-network-9490cbbdee6e` | `patch_tests` #69 | fsfs | FAIL |
| SVN | `m6-network-9490cbbdee6e` | `patch_tests` #69 | pgvn | FAIL |

此前 SVN patch69 的 EICAR 文件消失有对应火绒事件与原始字节/进程/时间关联证据，详见首批记录。本轮未更改杀毒设置、信任列表或原测试内容；其他协议的结果不能替代该 SVN 原失败，也不能将其重分类为跳过或通过。

## 调度、隔离与冻结

第一队列保留 18 组终态和 FAIL 汇总；续跑只执行原先未启动的 27 个计划，未重启任何已经启动的计划。首轮汇总因严格 XFAIL 诊断差异退出，第二版汇总复用已有审计结果，保留该 FAIL 并补充分析，没有为此重跑任何测试。续跑自身状态为 `PASS`，与第一队列 FAIL 分别保存。审计重读全部计划、原始日志、两侧结果和报告摘要，对每种协议的完整原版函数选择独立核对；通过组另执行完整原始报告审计，失败组保持原始日志与分类。

210 个内层和 45 个外层自有 Windows Job 均已排空；45 个隔离数据库的终态只读审计确认所有者均为非超级用户，监控令牌和残留客户端连接均为零。原生仓库别名目录未生成，数据库及角色保留。本段仅描述这 45 组，不描述独立运行的真实历史任务。

冻结构建保持 `5e3d4cc3…`，155 项注册输入、五个生产产物和依赖未改变，构建元数据仍为 `c036339` dirty。未新增浏览器、完整 M5 或运行包验收。网络原始材料共 1164 份冻结到 `runtime/pgvn/baselines/m6-merge-patch-complete-matrix-d919eae71cd6`，索引 SHA-256 为 `d847ae34ec2a5935372f32838565c2893c50f31c13aee9b656102cf76418561a`；另有 29 份调度、审计与原失败控制材料冻结到 `runtime/pgvn/baselines/m6-merge-patch-matrix-controls-d919eae71cd6`。未复制密钥或运行中的真实历史输出。

## 未完成项

原 SVN 失败仍需受控环境复测；启用 shelf/shelf2、正式管理工具绑定及其余套件继续推进。PGVN-601、PGVN-602、M6、M7 保持未完成，真实历史数据库备份、最终构建门禁、确认账本、跨故障域验证、代表性性能及独立 Windows/Polarion 验收仍按[实施计划](../plans/2026-10-15-pgvn-implementation-plan.zh-CN.md)执行。
