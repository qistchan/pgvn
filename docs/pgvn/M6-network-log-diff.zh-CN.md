# M6 原版 log/diff：三协议本机验证

日期：2026-10-16。原版 `log_tests` 1–46 与 `diff_tests` 1–96 在 SVN、HTTP、HTTPS 上完成 FSFS 对照。本组件结果见[结构化证据](evidence/m6-network-log-diff.json)；PGVN-601、M6、M7 仍未完成。

## 可复现入口

```powershell
python scripts/pgvn/build.py --config config/build.local.json --configuration Release
python scripts/pgvn/build_upstream_network.py
python scripts/pgvn/build_upstream_helpers.py
python scripts/pgvn/build_upstream_http.py
python tests/pgvn/test_m6_network_log.py
python tests/pgvn/test_m6_network_scripts.py
python tests/pgvn/test_m6_network_copy.py
python tests/pgvn/test_m6_hook_output.py
python tests/pgvn/test_m6_network_policy.py
$logs = python tests/pgvn/test_m6_network.py --plan --protocol svn --suite log_tests --cases 1:46 | ConvertFrom-Json
python tests/pgvn/test_m6_network.py --execute-plan $logs.plan
$diffs = python tests/pgvn/test_m6_network.py --plan --protocol svn --suite diff_tests --cases 1:96 | ConvertFrom-Json
python tests/pgvn/test_m6_network.py --execute-plan $diffs.plan
# HTTP/HTTPS 各生成新的 --protocol http/https 计划；已执行计划不能复用。
```

这些命令仅用于获批的 `127.0.0.1:16432` 专用集群与本仓库 runtime。每次执行使用唯一的 `pgvn_m6_net_<12位十六进制>` 数据库和非超级用户所有者；输入改变后重新生成计划。原版测试函数、装饰器、断言和 test_list 保持不变，网络入口边界见[指南](M6-upstream-network.zh-CN.md)。

## 完整结果

| 原版套件 | 传输 | 冻结运行 | PgVN 与 FSFS 各侧结果 |
| --- | --- | --- | --- |
| log | SVN | `m6-network-bafc99da3b5e` | 45 PASS、1 XFAIL |
| log | HTTP | `m6-network-ea54bd676979` | 45 PASS、1 XFAIL |
| log | HTTPS | `m6-network-c4e2b1eb2d05` | 45 PASS、1 XFAIL |
| diff | SVN | `m6-network-f983f90c6ce8` | 91 PASS、4 XFAIL、1 SKIP |
| diff | HTTP | `m6-network-d1d4796cc137` | 91 PASS、4 XFAIL、1 SKIP |
| diff | HTTPS | `m6-network-28a6e9a20383` | 91 PASS、4 XFAIL、1 SKIP |

六轮共 852 个结果、846 次实际进入（两侧、三协议，含原有 XFAIL，不含 SKIP）。唯一原版函数为 142 个，每侧每协议实际进入 141 个。

log 46 为原有 XFAIL（Issue 4711）；diff 77、90、92、94 为原有 XFAIL，diff 93 为 Windows 原有 SKIP。没有新增排除。原有 XFAIL 的诊断比较见结构化证据；它发生在运行结束后，不修改客户端输出或断言。

HTTPS 使用本轮私有 CA，验证证书链和主机名，拒绝未知 CA 及错误主机名，未改变机器信任库。已登记与显式声明的原生仓库别名均未生成本地目录。各轮 Job 已关闭，数据库、所有者及合成数据保留供诊断。

## 首轮失败与夹具适配

首轮三协议结果一致：FSFS 为 45 PASS、1 原有 XFAIL；PgVN 为 26 PASS、19 FAIL、1 原有 XFAIL。其中 18 个失败来自上游把提交说明写到 `repo_dir/log-msg`，另一个为 log 37 请求创建 SVN 1.5 格式的仓库。首轮源码、二进制、原始报告和日志已冻结。

`upstream_network_log.py` 仅在原生 `log_tests` 入口启用：

- `log-msg` 写入全局测试输入目录，调用原版 `file_write` 保持内容、编码和模式；对应原版 SVN 命令的 `-F`/`--file` 参数改为该输入路径。
- 映射要求已登记仓库、规范 UUID、缺失的原生别名及精确挂载父目录；核对单次计划摘要、隔离库、所有者、端口、数据目录与冻结输入。未写入的文件保持原行为；其他文件与工具不重定向。
- 每次输入和文件参数记录 ID、路径、字节数及 SHA-256。调用客户端前核对当前文件与最近写入记录；身份变化、文件被改写或别名已物化时拒绝继续。
- 历史准备从原生模板执行真实 dump，使用原版 `svnadmin create --compatible-version=1.5 --fs-type=fsfs` 创建全局临时 FSFS，load 并 verify 后再 dump，最后由原生原版命令体 load 到目标。实际 FSFS format 为 3，目标具有独立内部 ID、新公开 UUID、相同 HEAD，完整 verify 覆盖 r0–r1。
- log 37 随后由原测试继续提交 r2，并断言日志 XML 的文件/目录种类。旧 FSFS 的磁盘分支由 FSFS 对照覆盖；PgVN 侧验证旧输入经标准迁移后的原生协议行为，不声明 PgVN 实现旧 FSFS 磁盘格式。

首个定点候选的临时输入已通过，但对源模板要求没有 hooks 的限制过严。原版模板实际带有 `start-commit`、`pre-lock`、`pre-revprop-change`；原版 dump/load 读取历史并创建目标策略。修正夹具后，三协议 1/37 定点的两侧均为 2 PASS。该候选的 FAIL 与修正后的 PASS 均单独保留，未复用计划。

完整 SVN diff 的首个候选只在 48 失败：上游用通用脚本生成助手准备客户端外部 diff 参数探针，入口将它误当成仓库 hook。入口现按已登记的仓库/hook 路径分流；普通脚本在非仓库工作目录使用原版 Python/Windows batch 生成逻辑，保留路径、内容、字节输入和 cmd_alternative。该探针随后由客户端实际执行，参数顺序仍由原版断言验证。三协议定点及原失败均保留。

30 项 log 夹具、8 项脚本分流，以及 13 项复制、10 项 hook 输出、11 项策略边界检查通过。脚本分流检查提取当前入口及固定上游的实际函数，核对真实生成文件与策略分流。这 72 项离线检查不连接数据库、不启动子进程，也不计入原版套件执行数量。

## 构建与已有验收的关系

冻结清单为 `7b98c639a5b70e9d6ef7ae8e46aef6422f108b89eed51809f3e7eb11a15a98f4`，共 154 项本仓库源码/网页/SQL/构建测试输入，基于 `88f5f8a` 加当时工作区修复，以源码和产物摘要冻结。

本轮生产源码、网页资源与 SQL 未改变，改动为五个测试文件。五个生产产物、三份网页资源、67 项运行依赖和 84 项 Apache 依赖均与 [copy/prop 已验收组件](M6-network-copy-prop.zh-CN.md#匹配构建的完整回归与部署包)相同，构建输入路径也保持一致。

浏览器与完整 M5 的原始记录仍绑定此前 `dff1a26e649812903ce70f054fadc37a149ab9baa6424c419c21f4fe6e5dc7af` 清单。本轮依据产品字节相同复用其产品层验收，没有声称重新运行浏览器或完整 M5，也没有改写旧报告的构建身份。新增夹具由本轮离线检查、定点和完整原版套件验证。

## 归档与部署包

最终网络基线为 `runtime/pgvn/baselines/m6-log-diff-final-28a6e9a20383`，538 个文件，索引 SHA-256 `4eb6ed22df43b0b7c3d417ea273960ac210c59cd53c083d438007eb0f32ebd23`。历史候选各自归档，真实旧 FSFS、两份 dump、临时输入和生成的客户端脚本均保留。开发包及其完整性/最小 PATH 启动检查见[包旁校验记录](evidence/m7-package-log-diff.json)，该记录随 ZIP 单独提供。

部署包为 `development-snapshot`，本轮包检查及独立 ZIP 核对不代表干净机器或 Polarion 应用验收。网络运行可能与其他隔离网络运行并发，耗时仅用于功能诊断，不能作为吞吐、延迟或监控开销基准。

独立结果检查器核对原版用例元数据、逐项结果、原始日志/报告摘要、临时输入审计及真实历史输入。初版误将 `selected_cases` 当作 ID 列表，已保留并修正为其实际元数据结构；另修正派生汇总中 Job 数被文件摘要变量覆盖的问题，原始 Job 清理检查及测试报告未改变。XFAIL 的完整预期/实际输出与异常保留，比较精确归一本轮工作目录和已记录的监听地址；diff 77 的预期行包含不同随机端口，除此之外没有宽泛删减。

## 剩余出口

继续完成其余原版套件、完整适用性台账、最终产品上的早期套件复跑、维护语义和固定 638 修订/30 表备份恢复复核；之后完成确认提交账本、跨故障域主备/fencing、代表性持续性能与监控预算，以及独立 Windows 安装重启、实际高对比度视觉和 Polarion 隔离验收。上述项目未据本组件结果提前勾选。
