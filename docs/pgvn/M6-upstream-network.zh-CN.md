# M6 上游网络测试入口

当前阶段：固定输入、静态目录与源码快照已建立；原版 CLI 命令体的 ID 绑定差分组件通过。PGVN-601 保持未完成，上游 Python 用例执行数为 **0**。

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

本结果仅覆盖上述命令。hotcopy、freeze、pack、recover 的适配代码尚未由此组件验收；recover 等待锁/通知语义待补，旧磁盘格式升级明确不支持。62 个原版 Python 套件尚未执行，SVN/HTTP/HTTPS 适用性和故障/性能出口继续推进。

## 后续适配边界

| 上游位置（固定 1.14.5） | 观察 | 后续工作 |
| --- | --- | --- |
| `svntest/main.py` 437、599 | 底层 `run_command` / `run_command_stdin` | 在明确的 admin/look 工具调用边界适配 argv、标准流及退出码 |
| `main.py` 1157、1213 | 建库和复制直接走工具调用；建库帮助函数还会先创建目录 | 替换 fixture 建库/复制帮助函数，将别名映射为仓库 ID，不生成 PgVN repo 目录 |
| `main.py` 1051、1350、1382 | 创建后设置、svnserve 配置和 authz 文件 | 通过 PgVN 配置发布和服务配置适配，保持用例断言原文 |
| `svntest/sandbox.py` 137、155、195、251、448 | 沙箱、额外仓库、复制和清理 | 统一仓库登记与生命周期账本，验证别名路径没有仓库壳 |

仅替换 `run_svnadmin()` 无法覆盖建库帮助函数的直接调用。静态工具操作和 repo 路径提示只是语法线索，间接变量及运行时路径仍需实测，不据此自动跳过测试。

接下来建立原生服务/管理 fixture，在导入测试模块之前绑定必要帮助函数；随后执行未经修改的原断言，按 SVN、HTTP、HTTPS 与 FSFS 对照记录结果和逐项适用性。当前旧 `tests/run_upstream.py`、`tests/upstream_entry.py`、`tests/upstream_http.py` 仍属于 PgFS 目录与 15432 配置，不作为 PgVN 执行入口。
