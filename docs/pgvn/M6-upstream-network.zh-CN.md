# M6 上游网络测试入口

当前阶段：原版 SVN 网络入口已运行，完整基础套件与一个带到期时间的锁用例在 PgVN/FSFS 两侧结果一致。每侧 67 PASS、1 个原有 XFAIL、4 个原有 SKIP；PGVN-601 仍未完成，HTTP/HTTPS、其余套件和完整适用性矩阵待推进。

## 可复现入口

在仓库根目录执行：

```powershell
python scripts/pgvn/build.py --config config/build.local.json --configuration Release
python scripts/pgvn/catalog_upstream_network.py --require-frozen-inputs --snapshot
```

工具读取构建清单中配置的 Subversion 源码目录，默认归档为相邻的 `subversion-git-1.14.5.zip`。支持 `--source-root`、`--archive` 指定镜像；摘要仍须与提交的源码锁一致。`--output` 限于 `artifacts/pgvn/tests`。

`tests/pgvn/upstream_network_lock.py` 为纯数据锁，由 AST 字面量解析，不导入执行；它和目录脚本均纳入构建源码清单。锁覆盖 233 个原始命令行测试文件及 `svn_version.h`，并固定归档 SHA-256。配置归档与本地文件逐字节一致，未声称验证发行签名。

输出含 62 个套件、1,984 个静态测试编号、原始装饰器和 fixture 调用位置。状态为 `CATALOG_ONLY`，`tests_executed=0`，不自动生成排除项。报告保存在 `artifacts/pgvn/tests/m6-network-catalog-*.json`。

`--snapshot` 在 `runtime/pgvn/upstream-source/<本次 ID>` 写入新源码快照和清单，234 个文件重新校验，旧 vendor 字节码缓存不带入。未来测试入口应从该快照执行，并禁止产生/消费旧缓存；快照创建本身不执行上游模块。

## 已验证的输入保护

本轮验证三种拒绝条件：脚本/源码锁未登记到冻结构建、隔离副本中的源码被改动、归档副本被追加字节。全部在目录生成前拒绝；原始源码和归档保持一致。五个原生产品与 `77948d7` 焦点修复基线逐字节相同。固定摘要和原始报告见 [目录证据](evidence/m6-network-catalog.json)。

## 已完成的 CLI 差分组件

`build_upstream_network.py` 将原版 `svnadmin.c`、`svnlook.c` 链接到当前生产公共核心，只通过编译时头文件替换仓库构造及维护入口。命令体逐字节核对源码锁指定的原始 ZIP；没有重写命令输出。工具位于私有 `runtime/pgvn/upstream-tools`，不会覆盖原版客户端或生产 `pgvnadmin`。

```powershell
python scripts/pgvn/build_upstream_network.py
python tests/pgvn/test_m6_network_tools.py --offline
python tests/pgvn/test_m6_network_tools.py --plan
# 将上一条输出的完整 plan.json 路径传入；每个计划仅执行一次。
python tests/pgvn/test_m6_network_tools.py --execute-plan <plan.json路径>
```

执行范围为已授权的 `127.0.0.1:16432` 专用集群和仓库 runtime，库名强制为 `pgvn_m6_net_<12位十六进制>`。计划冻结输入摘要，启动前核对真实数据目录，每次创建独立非超级用户所有者和测试库。原版 CLI 适配器在连接前核对 host、port、dbname、user，拒绝 service 重定向。此组件不启动网络服务、不创建监控令牌。

离线 `m6-network-tools-offline-65c373e9982e` 的 9 组检查通过。隔离差分 `m6-network-tools-8b32fcd51584` 的 19 组检查通过：中文及空格仓库别名无磁盘目录、两修订合成历史包含二进制/属性/复制、原版 load/verify、16 组 svnlook 输出与原版 FSFS 一致，原生 dump 恢复到原版 FSFS 后再次 verify 和比对。20 条实际构造/打开账本均为 UTF-8 进程。

首轮 `m6-network-tools-299c93399a2d` 在测试适配器中访问未填充的路由字段而崩溃；已改为查询精确路由并核对仓库 ID，FAIL 原样保留。五个生产二进制摘要与网页验收基线相同。固定摘要、结果与边界见 [CLI 组件证据](evidence/m6-network-tools.json)。

本结果仅覆盖上述命令。hotcopy、freeze、pack、recover 的适配代码尚未由此组件验收；recover 等待锁/通知语义待补，旧磁盘格式升级明确不支持。62 个原版 Python 套件的完整协议矩阵、故障/性能出口继续推进。

## SVN 网络入口与本轮结果

```powershell
python scripts/pgvn/build_upstream_network.py
python scripts/pgvn/build_upstream_helpers.py
python tests/pgvn/test_m6_network.py --plan --suite basic_tests --cases 1:71
python tests/pgvn/test_m6_network.py --execute-plan <上一条生成的plan.json路径>
python tests/pgvn/test_m6_network.py --plan --suite lock_tests --cases 53
python tests/pgvn/test_m6_network.py --execute-plan <新的plan.json路径>
```

每个计划仅执行一次，冻结产品、入口、C 工具、私有 WC DLL、原版客户端及解释器摘要。验证原始归档后创建新源码快照，以 `-I -B` 导入；运行前后复核 234 个原始文件、无字节码缓存和原 `test_list`。用例函数、装饰器、预期输出均未改。入口强制串行，避免上游并行子入口绕过原生 fixture。

先以原版 svnserve + FSFS 执行，再以 pgvnserve 执行相同选择。两侧只监听回环临时端口，使用 Job Object 管理自有进程；结束时确认任务内进程为 0。PgVN 仓库别名始终没有磁盘目录，创建、dump/load、策略发布、外部 Python hook 和清理均进入明确的 fixture 边界。常规工作副本、日志、全局 hook/策略文件仍为真实文件。

原版基础用例 38 将本地仓库 `format` 当任意上传样本。原生工具调用边界只对 `svnmucc put` 的该输入改用全局 fixture 文件，字节来自已完成的 FSFS 对照中的同一文件并记录摘要；仓库 URL、目标名称、协议操作和断言不改，也不生成 PgVN 仓库目录。Hook 桥接执行原帮助函数提供的 Python 体，Windows `cmd_alternative` 尚未覆盖，后续套件须逐项验证。

| 最终候选运行 | PgVN | 原版 FSFS |
| --- | --- | --- |
| `m6-network-8bbee1e5274c`，basic 1–71 | 66 PASS、1 XFAIL、4 SKIP | 66 PASS、1 XFAIL、4 SKIP |
| `m6-network-769bbbc41c57`，lock 53 | 1 PASS | 1 PASS |

XFAIL 为原用例 63；SKIP 为 52、67、70、71，均由原始装饰器/条件产生，没有新增排除。锁用例实际调用原版 lock-helper 的 ID 绑定入口，验证带到期时间的锁信息、工作副本状态、强制解锁和重锁。每侧报告 72 个结果，实际进入 68 个用例；CLI/辅助工具检查与重复尝试不累加为新的上游覆盖。

## 本轮发现与修复

首个完整基础运行 `m6-network-be6b9be8677f` 保留 FAIL：Windows 在 hook 读取期间短暂拒绝上下文文件替换，以及上述本地样本缺失。上下文改为内容未变化时不重写、完整文件替换并有限重试；同时记录样本重定向。

随后 `m6-network-2a1cfc9b9714` 到达真实协议差异：合法节点名称中的反斜杠被路由注册校验拒绝。公共核心现在区分严格路由和逻辑库内路径；目录解析、路径授权及管理查询允许反斜杠字面字符，注册路由仍拒绝它，UTF-8、控制字符及 `/` 路径段约束保留。原用例 38 不经修改完整通过。

`test_m6_catalog_paths.py --database <已有pgvn_m6_net_库>` 在核对所属计划、数据库所有者和真实集群目录后，只运行 M1 的建库/路由契约，不引导或重启集群。`m6-catalog-paths-6a2cf9cb566e` 通过：字面路径保留、非法路由没有部分创建、大小写/Unicode 区分，以及禁用最长路由不回退。原 M1 门禁同时补充相应回归断言。

最终候选还重跑 CLI 差分 19 组、辅助工具 14 组、离线保护 9 组，全部通过。完整运行和组件原始记录、两次失败、当前源/产物摘要及 172 文件冻结基线见 [网络组件证据](evidence/m6-network-entry.json)。测试耗时为功能检查墙钟，不作为性能结论。

这次共享核心修复改变了 pgvnserve、pgvnadmin 和 mod_dav_pgvn 的二进制摘要；网页三个资源未改。既有网页验收和 `m5-57cb4d03584a` 仍绑定旧产品，新候选尚需新的浏览器证据及完整 M5 回归，不能沿用为本次验收。M6 的剩余网络矩阵、维护语义、故障账本、主备隔离和性能，以及 M7 独立环境验收保持未完成。

## 后续适配边界

### 上游辅助工具已具备本机组件验证

```powershell
python scripts/pgvn/build_upstream_helpers.py
python tests/pgvn/test_m6_upstream_helpers.py
```

构建原版 entries-dump、lock-helper、atomic-ra-revprop-change、wc-lock-tester、wc-incomplete-tester。SDK 缺少部分工作副本私有导出，因此从固定归档对应的 WC C/头文件/SQL 和 SQL 生成器构建测试私有 DLL，保留原 SDK 的 305 个导出；原版客户端仍从其原目录加载原 SDK DLL。所有额外文件均在 `runtime/pgvn/upstream-helpers`，原安装及 vendor 未改。

`m6-upstream-helpers-b5b75e59dac7` 的 14 组本机检查通过，覆盖真实工作副本条目/目录树、写锁与 cleanup、不完整目录与 update、原子修订属性成功/冲突、FSFS 过期锁及原生保护拒绝。早期两个检查脚本的解析错误保留为 FAIL。私有 WC DLL 追加字节时，身份校验在执行测试之前拒绝，随后恢复原字节；原 SDK 未修改。

lock-helper 原版命令体追加虚拟 `db` 分量，由测试构造入口解析为仓库 ID；FSFS 对照另构建不带该绑定的同一命令体。该前置组件只验收原生入口的缺少连接保护拒绝，实际原生锁随后在上文原用例 53 验证。扩展后的 CLI 另以新隔离库重跑 19 组差分及 9 组离线检查通过。该组件的固定摘要与范围见 [辅助工具证据](evidence/m6-upstream-helpers.json)，它自身不运行上游 Python 套件。

| 上游位置（固定 1.14.5） | 观察 | 后续工作 |
| --- | --- | --- |
| `svntest/main.py` 437、599 | 底层 `run_command` / `run_command_stdin` | 在明确的 admin/look 工具调用边界适配 argv、标准流及退出码 |
| `main.py` 1157、1213 | 建库和复制直接走工具调用；建库帮助函数还会先创建目录 | 替换 fixture 建库/复制帮助函数，将别名映射为仓库 ID，不生成 PgVN repo 目录 |
| `main.py` 1051、1350、1382 | 创建后设置、svnserve 配置和 authz 文件 | 通过 PgVN 配置发布和服务配置适配，保持用例断言原文 |
| `svntest/sandbox.py` 137、155、195、251、448 | 沙箱、额外仓库、复制和清理 | 统一仓库登记与生命周期账本，验证别名路径没有仓库壳 |

仅替换 `run_svnadmin()` 无法覆盖建库帮助函数的直接调用。静态工具操作和 repo 路径提示只是语法线索，间接变量及运行时路径仍需实测，不据此自动跳过测试。

当前 SVN 入口已在导入用例前绑定服务/管理 fixture，后续扩展剩余帮助函数和 HTTP/HTTPS，并与 FSFS 对照记录逐项适用性。旧 `tests/run_upstream.py`、`tests/upstream_entry.py`、`tests/upstream_http.py` 仍属于 PgFS 目录与 15432 配置，不作为 PgVN 执行入口。
