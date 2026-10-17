# M6 合并与 patch 首批检查点

日期：2026-10-17。**本检查点不表示兼容性通过。** 45 个原版计划的首批 18 组已终止：17 组 PASS、1 组 FAIL，剩余 27 个尚未启动的原计划另行续跑。PGVN-601、PGVN-602、M6、M7 均保持未完成。详见[结构化证据](evidence/m6-network-merge-patch-checkpoint.json)。

## 原版结果与计数范围

九个套件共 377 个被选原版函数均已在 SVN 夹具产生两侧结果，其中部分套件另有 HTTP 结果。首批共 **830 个结果：666 PASS、26 原有 XFAIL、136 原有 SKIP、2 FAIL**；694 个为非 SKIP 结果。下表为单个后端计数，FSFS 与 PgVN 的原编号、函数名和结果一致；`merge_tests` 是六个原编号分段的合计。

| 原版套件 | SVN | 首批 HTTP | 首批 HTTPS |
| --- | --- | --- | --- |
| `merge_authz_tests` | 4 PASS | 4 PASS | 待执行 |
| `merge_automatic_tests` | 23 PASS / 2 XFAIL | 23 PASS / 2 XFAIL | 待执行 |
| `merge_reintegrate_tests` | 21 PASS / 1 SKIP | 待执行 | 待执行 |
| `merge_tests` | 137 PASS / 4 XFAIL / 2 SKIP | 待执行 | 待执行 |
| `merge_tree_conflict_tests` | 29 PASS | 待执行 | 待执行 |
| `patch_tests` | 74 PASS / 5 XFAIL / 2 SKIP / 1 FAIL | 待执行 | 待执行 |
| `shelf2_tests` | 31 SKIP | 待执行 | 待执行 |
| `shelf_tests` | 32 SKIP | 待执行 | 待执行 |
| `svnmucc_tests` | 9 PASS | 9 PASS | 待执行 |

`shelf_tests` 和 `shelf2_tests` 默认实验谓词未启用，贡献了 126 个 SKIP；这部分不能当作后端函数实际进入。另有 10 个原有 SKIP。启用官方 `shelf3` / `shelf2` 环境开关的六个新计划已准备，尚未执行。没有修改原版函数、装饰器、golden、test_list 或断言，没有新增排除。13 对原有 XFAIL 的完整诊断仅归一精确工作目录与本轮监听地址后相同。

## patch69：安全软件干扰，保留 FAIL

`m6-network-9490cbbdee6e` 的 patch42–82 两侧各为 33 PASS、5 XFAIL、2 SKIP、1 FAIL。唯一失败均为 `patch_add_remove_executable`（原编号 69），在写入上游原有 EICAR 测试内容后，`svn add` 报文件不存在；完整失败诊断相同。

只读提取火绒日志得到事件 261 与 262，精确对应两侧 `eicar.com` 路径、测试进程和执行时间。检测名称为 `TEST/AVEngTestFile!EICAR`，被检测字节 SHA-256 与原版函数静态常量解析结果一致。日志完整副本仅在私有目录查询，提取后已删除；只保存这两个相关事件，没有导出无关事件。安全软件的数字处置码保留原值，不擅自解释其含义。

**原报告和队列仍为 FAIL。** 未改安全设置，未信任测试目录，也未替换测试内容或把失败改为 SKIP。该用例仍需在明确允许 EICAR 的受控环境定点复测；不会将本次环境诊断直接记作兼容性通过。

## 冻结身份与有界清理

清单仍为 `5e3d4cc3f2f8491a27b6aaede96e1ec921b31f908ec4324574bc580ef323b1e9`，155 项注册输入和五个生产产物均未改。构建身份继续保留为 `c036339` dirty，文档提交不回写构建身份。五个生产产物与前一组件相同；本检查点没有新跑浏览器、M5 或重打包，历史结果继续绑定原记录。

在首批队列结束、续跑启动之前，75 个内层 Job 和 18 个外层 Job 已清空；18 个隔离库所有者均为非超级用户，监控令牌和残留客户端连接为零。带时间的 CIM 观察确认当时 runtime 进程及临时服务为零。专用 PostgreSQL、隔离库、角色及日志保留；这个零值不描述随后续跑期间的全局状态。

| 冻结材料 | 文件数 | 索引 SHA-256 |
| --- | --- | --- |
| `m6-merge-patch-initial-f1645be98e9b`：原输入、报告和日志 | 537 | `c3e573124cc988413ce75d1c8f97419fdd1c36ec824617e9325352ad503f5777` |
| `m6-merge-patch-initial-controls-f1645be98e9b`：队列、审计、相关安全事件及未执行计划 | 111 | `1fe7e74e00def61523aa7f9ed4bcf7e2212b479cfd469d83283e9f3914421585` |
| `m6-svnauthz-local-candidate-62498787dc1b`：独立候选与续跑启动输入 | 161 | `a1195e1444f6a8d2766a26c0ce5d5d3c1822855fb555b8a776f4b07e1b317e3a` |

三处目录均位于 `runtime/pgvn/baselines`，没有复制生成的私钥。当前覆盖账本不因本检查点而宣称九套件三协议通过。

## 继续执行与管理工具准备

续跑仅使用原 45 个计划中从未启动的 27 个，计划原字节不变；不重启首批已执行计划。续跑完整收集各组原版结果，任何 FAIL 都使总轮次保持 FAIL。身份不符、报告不完整或 Job 未清空会停止后续派发。续跑前用 18 份真实终态报告及 18 个损坏反例核对执行边界；这些离线检查不执行原版函数或连接数据库。早期未执行的调度候选及一次离线检查器的 Python 求值作用域错误均保留，实际运行器身份见结构化证据。

工具路径探针确认当前夹具缺少上游单独要求的 `--tools-bin`，使 `svnauthz`、`svnauthz-validate`、`svnmover` 保持不存在的默认路径。原版 stock 工具实际已在运行目录中；其中事务读取需要额外使用受保护的原生仓库构造器。

已在独立诊断目录用未修改的原版 `tools/server-side/svnauthz.c`、当前公共核心及现有受保护构造器编译候选，正常和旧名称模式的可执行文件字节相同。15 组本地命令的退出码、stdout、stderr 与 stock 相同，另有 7 项缺少守卫或不安全连接选项的拒绝检查通过，共执行 37 个本地 CLI 进程。没有建立有效数据库连接、执行原版套件或验证真实事务；该候选没有替换运行时工具。待冻结队列终止后，再将路径及原生工具绑定纳入正式构建并执行完整原版回归。

继续完成三协议套件及适用性、最终构建复跑、维护、638 修订真实历史与备份恢复、提交确认账本及跨故障域 HA、六类代表性性能与监控预算、Windows 高对比度视觉，以及独立 Windows/Polarion 验收。[实施计划](../plans/2026-10-15-pgvn-implementation-plan.zh-CN.md)的完整范围不变。
