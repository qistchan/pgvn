# M6 上游网络测试入口

当前阶段：固定输入、静态目录与源码快照已建立。PGVN-601 保持未完成，执行用例数为 **0**。

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

## 后续适配边界

| 上游位置（固定 1.14.5） | 观察 | 后续工作 |
| --- | --- | --- |
| `svntest/main.py` 437、599 | 底层 `run_command` / `run_command_stdin` | 在明确的 admin/look 工具调用边界适配 argv、标准流及退出码 |
| `main.py` 1157、1213 | 建库和复制直接走工具调用；建库帮助函数还会先创建目录 | 替换 fixture 建库/复制帮助函数，将别名映射为仓库 ID，不生成 PgVN repo 目录 |
| `main.py` 1051、1350、1382 | 创建后设置、svnserve 配置和 authz 文件 | 通过 PgVN 配置发布和服务配置适配，保持用例断言原文 |
| `svntest/sandbox.py` 137、155、195、251、448 | 沙箱、额外仓库、复制和清理 | 统一仓库登记与生命周期账本，验证别名路径没有仓库壳 |

仅替换 `run_svnadmin()` 无法覆盖建库帮助函数的直接调用。静态工具操作和 repo 路径提示只是语法线索，间接变量及运行时路径仍需实测，不据此自动跳过测试。

接下来建立原生服务/管理 fixture，在导入测试模块之前绑定必要帮助函数；随后执行未经修改的原断言，按 SVN、HTTP、HTTPS 与 FSFS 对照记录结果和逐项适用性。当前旧 `tests/run_upstream.py`、`tests/upstream_entry.py`、`tests/upstream_http.py` 仍属于 PgFS 目录与 15432 配置，不作为 PgVN 执行入口。
