# M6 工作副本状态、冲突与重定位

日期：2026-10-17。固定上游的 10 个完整套件、163 个函数在 SVN、HTTP、HTTPS 完成原版 FSFS 与 PgVN 差分。结果与原始报告摘要见[结构化证据](evidence/m6-network-wc-state.json)，全部 62 套件的逐函数状态见[覆盖账本](M6-network-coverage.zh-CN.md)。本组件不宣告 PGVN-601、M6 或 M7 完成。

## 原版结果

| 套件 / 范围 | SVN | HTTP | HTTPS |
| --- | --- | --- | --- |
| stat 1–45 | 43 PASS / 1 XFAIL / 1 SKIP (`a14f75b67ca9`) | 43 PASS / 1 XFAIL / 1 SKIP (`0525ad142cff`) | 43 PASS / 1 XFAIL / 1 SKIP (`478869e186cf`) |
| resolve 1–8 | 7 PASS / 1 SKIP (`e23c702f76ce`) | 7 PASS / 1 SKIP (`0a4188bf41f0`) | 7 PASS / 1 SKIP (`084a33399994`) |
| relocate 1–7 | 7 PASS (`0a4f73c5d58f`) | 7 PASS (`3d48540b1f60`) | 7 PASS (`0851476fcd54`) |
| tree_conflict 1–26 | 26 PASS (`8bb28bbfa8c3`) | 26 PASS (`f91b55a3060c`) | 26 PASS (`dc5abfcb6f7a`) |
| move 1–15 | 15 PASS (`80d87a1b1cf2`) | 15 PASS (`b3dba6cf2a2d`) | 15 PASS (`2cb85071b84c`) |
| schedule 1–16 | 15 PASS / 1 SKIP (`a3ddc50866a8`) | 15 PASS / 1 SKIP (`4ae1e365efb0`) | 15 PASS / 1 SKIP (`bfe29405a020`) |
| changelist 1–18 | 17 PASS / 1 XFAIL (`9d797840f1be`) | 17 PASS / 1 XFAIL (`e6998c962164`) | 17 PASS / 1 XFAIL (`5420dd547f5e`) |
| wc 1–18 | 9 PASS / 9 SKIP (`160862077720`) | 9 PASS / 9 SKIP (`6168a9c5eab2`) | 9 PASS / 9 SKIP (`04b61416d35b`) |
| svnversion 1–7 | 7 PASS (`0f87c267a9d7`) | 7 PASS (`c226a7b9bb2b`) | 7 PASS (`dc9b7878c774`) |
| entries 1–3 | 3 PASS (`72922c91d237`) | 3 PASS (`f61f71998c42`) | 3 PASS (`0f666711e191`) |

每格展示单个后端的计数；FSFS 与 PgVN 逐项结果一致，两后端三协议合计 **978 个结果：894 PASS / 12 XFAIL / 72 SKIP**，实际进入 **906 次**（含原有 XFAIL，不含 SKIP）。没有增加排除或修改原函数、装饰器、test_list、客户端输出和预期断言。原版 XFAIL 的 6 对完整诊断仅精确归一各自工作目录及监听地址后相同。

POSIX 符号链接/可执行位、特权条件和原版无条件 SKIP 保持原决策；这些是原有平台条件，不表示相应函数实际执行成功。账本保留 PASS、XFAIL、SKIP 及未取得当前构建结果的区别。

## 夹具和验证边界

本轮没有修改生产代码或测试适配器。stat33 的旧 `.svn/format`、wc 套件的缺失/损坏 `wc.db` 及锁定工作副本都操作真实客户端目录。entries 使用固定原版 entries-dump 辅助程序读取真实工作副本，没有替换为模拟输出。

relocate 的复制使用此前已有的受限生产 hotcopy 夹具，保留公开 UUID、生成独立内部 ID；源仓库删除通过数据库注册生命周期处理。原来的重定位、过期属性写入拒绝、切换子树和相对 externals 断言不变。所有原生仓库别名在入口前后均没有生成本地仓库目录。

HTTPS 各轮实际验证私有 CA 证书链和主机名，并拒绝未知 CA 和错误主机名。信任仅作用于本轮私有客户端配置，未修改机器信任库。内层 140 个 Job、外层 30 个 Job 关闭前活动进程均为零；30 个隔离数据库及其非超级用户所有者逐一核对并保留，监控令牌行数为零，未启动临时 Windows 服务。最终只读 CIM 检查确认 runtime 进程和临时测试服务均为零。

原调度器在记录 20 组完成后中断，另 3 组没有终态报告、7 组尚未启动。续跑前已确认原驱动与服务进程不存在，并恢复同一专用集群；为 3 组中断任务各创建新 UUID 计划，其余 7 个未执行计划首次启动。累计派发 33 次，接纳其中完整的 30 次。3 份中断记录保留于 `runtime/pgvn/baselines/m6-wc-state-interrupted-20261017`（索引 `e28d3be89f9c237c7671b44e82b63535bee79d182fdbbc870d48750fd6050301`），不计 PASS，也不以事后进程消失替代原 Job 正常清理证据；原计划未重复执行。它们的数据库、角色与诊断记录同样保留。

## 同一冻结构建的既有验收

当前清单仍为 `e63c40be2a119368e6060d4e4637dfc2ee8936d1a8dc45106cfa1aa0e56bb997`，154 项注册输入、5 个生产产物及运行依赖全部未变。构建时基线仍为 `c638127` 的 dirty 构建身份，不改写为之后的提交号。

因此本轮复用真实浏览器 15+6 项与完整 M5 `m5-0587e0a9e03b` 十组的原始报告；它们与本轮是同一个冻结构建。没有重复运行浏览器或全 M5，也没有把原报告改写为新运行。原报告、源码/产品摘要和复用依据均在结构化证据中列出；详见[同构建验收](M6-network-wc-workflows.zh-CN.md#冻结构建与完整回归)。

## 覆盖账本与剩余范围

账本逐项校验固定 62 个源文件、1,984 个函数编号、原始快照、58 份既有已接纳运行与本轮 30 份新报告。只采用各组件的 final/runs 分支；定点重复、被拒绝尝试与失败记录不累加为新覆盖。早期缺失字段只依据原始 URL 和已记录的别名核对补证，并明确标注，没有改写历史报告。

当前构建在三协议均有原版结果的函数从 **125 增至 288**，覆盖 13 个完整套件的选中结果；另有 **676** 个仅有旧构建结果，**1020** 个尚无已接纳运行引用。原有 SKIP 与实际进入数单独保存。checkout14 的 `file:///`/SUBST 架构边界保留原始 stock PASS/native FAIL，未改成 SKIP/PASS，也不套用到其他函数。

继续其余套件和完整适用性审查、最终构建的早期套件复跑、维护语义、638 修订真实历史与 30 表备份恢复复核、提交确认与跨故障域主备/fencing、代表性持续性能与监控预算。实际 Windows 高对比度、独立机器安装重启和隔离 Polarion 应用验收仍待完成。功能套件执行耗时不作为性能基准。

## 可复现入口与部署包

只用于已获批的 `127.0.0.1:16432` 专用集群及本仓库 runtime；每次创建新计划，不复用已经启动的计划。下例的套件与范围可按上表替换，HTTP/HTTPS 分别生成新计划。

```powershell
$run = python tests/pgvn/test_m6_network.py --plan --protocol svn --suite stat_tests --cases 1:45 | ConvertFrom-Json
python tests/pgvn/test_m6_network.py --execute-plan $run.plan
```

原始输入、报告和日志冻结于 `runtime/pgvn/baselines/m6-wc-state-full-0f666711e191`，索引 `f09a4d1d5e3e242ddace0a00ef2766487c95e718b838f8c5b0e6e8c85da2406d`，共 863 个文件。匹配开发包的完整性、95 个 x64 镜像和 Windows PowerShell 5.1 清空 PATH 后的启动检查见[包旁记录](evidence/m7-package-wc-state.json)。包仍为 development-snapshot，不宣告干净机器或 Polarion 验收。
