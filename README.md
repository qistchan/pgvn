# PgVN

基于 PostgreSQL 的专用 SVN 服务端。目标是按数据库 ID 管理仓库，服务节点不保留每库目录。

当前已完成 M0–M4：无目录公共核心、原生管理工具、svn 协议服务及内置中文只读监控。使用说明见 [M2 管理指南](docs/pgvn/M2-admin.zh-CN.md)、[M3 服务指南](docs/pgvn/M3-serve.zh-CN.md) 和 [M4 监控指南](docs/pgvn/M4-monitor.zh-CN.md)。Apache DAV、完整性能/故障验证与 Polarion 隔离验收继续按 [PgVN 计划](docs/plans/2026-10-15-pgvn-implementation-plan.zh-CN.md) 的 M5–M7 推进。当前尚不能据此替换 Polarion 生产后端，实际范围以 [进展与验证](docs/pgvn/progress.zh-CN.md) 为准。

## Windows 开发构建

准备固定 SVN 1.14.5、APR 1.7.6、APR-util 1.6.3 源码/头文件、匹配的 Windows x64 DLL、Apache 2.4.68 SDK、PostgreSQL 18.6 SDK 和 LLVM-MinGW。源码/SDK 可放在任意只读目录，不修改原安装。构建需要 Python 3，运行时无需 Python 或 Node.js。

1. 复制 `config/build.example.json` 为 `config/build.local.json`，填入实际 SDK 路径；本地配置不提交。
2. 执行 `python scripts/pgvn/build.py --config config/build.local.json --configuration Release`。
3. 配置测试 JDK/SVNKit，运行 `tests/pgvn/monitor_browser_fixture.py` 完成真实浏览器验收并保存摘要绑定的记录，再执行 `python tests/pgvn/run_gates.py --milestone M4`；它串行包含 M0–M3 及监控检查，使用专用 16432 实例。详见 M4 指南。
4. 执行 `python tests/pgvn/run_gates.py --milestone M1` 验证核心；它使用独立的 16432 测试数据库，包括重启该数据库。
5. 在管理员开发终端执行 `python tests/pgvn/run_gates.py --milestone M0` 验证服务骨架。该门禁会创建唯一命名的临时 LocalService 服务与回环 URL ACL，并在测试后清理。它不使用已有的 SVN、Apache 或 PostgreSQL 服务。

产物在 `runtime/pgvn`，构建清单在 `artifacts/pgvn/build/manifest.json`，测试原始结果在 `artifacts/pgvn/tests`。开发生成物不提交；每个里程碑提交代码与可审阅的证据摘要。

当前可执行 `runtime/pgvn/bin/pgvnserve.exe --version`、`pgvnadmin.exe diagnose`，或前台运行 `pgvnserve.exe --monitor-url http://127.0.0.1:14691/`。前台 Ctrl+C 停止。监听需要管理员权限或预先授予服务账户该 URL 的访问权限；部署要求见 [M0 契约](docs/pgvn/M0-contracts.zh-CN.md)。`/health/live` 返回 200，网络服务尚未就绪时 `/health/ready` 为 503。

## 代码与历史基线

`src/libsvn_fs_pg`、根目录 `sql/001`–`004`、原有 `scripts/*.py`/`tests/*` 和 PgFS 文档从已验证 PgFS 项目迁入，用于存储核心与回归参考。它们的历史验证结果属于原 PgFS 构建，不能作为 PgVN 当前构建的协议兼容结论。旧脚本可能使用旧 fixture 端口/路径；新开发统一使用 `scripts/pgvn`、`tests/pgvn` 与 `config/test.example.json` 的隔离环境。

独立测试数据库计划使用 `127.0.0.1:16432/pgvn_poc`，SVN/监控/HTTP 端口为 14690/14691/14980。M0 无需数据库运行；后续迁移与测试不得复用原 PgFS 或生产数据库进行故障注入。
