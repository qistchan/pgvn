# M2：pgvnadmin 管理指南

M2 交付 Windows x64 原生管理工具，不需要 Python、Node.js 或本地仓库目录。代码链接固定 SVN 1.14.5 的仓库层和 PgFS 核心；文件内容及标准 dump 流保持二进制安全。当前网络服务接入继续在 M3、M5 实施，中文业务监控页面在 M4 实施。

## 运行准备

运行目录使用 `runtime/pgvn/bin`，其中包含 `pgvnadmin.exe` 和匹配的 DLL。`--version` 显示产品、里程碑和依赖版本，`--help` 列出命令。开发构建和门禁仍需要 Python；原生运行不需要。

连接配置从 `PGVN_DSN` 环境变量或 `--config FILE` 读取，不能将密码作为命令行参数传入。配置文件使用 UTF-8：

```ini
[pgvn]
connection = service=pgvn-admin
```

`service=pgvn-admin` 由 libpq 的全局服务文件和凭据文件解析；也可填写不含密码的 libpq 连接参数。部署时保护全局配置、PGPASSFILE 和服务账户权限。诊断与错误输出不回显 DSN 或数据库原始错误参数。

数据库管理员预先创建目标数据库与登录角色，数据库所有者负责迁移。示意 SQL 不设置或显示密码：

```sql
CREATE ROLE pgvn_migration LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE pgvn_admin LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE pgvn_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE pgvn_monitor LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE pgvn OWNER pgvn_migration ENCODING 'UTF8' TEMPLATE template0;
```

使用迁移所有者的全局连接配置执行：

```powershell
pgvnadmin --config C:\PgVN\conf\migration.ini db init
pgvnadmin --config C:\PgVN\conf\migration.ini db check
pgvnadmin --config C:\PgVN\conf\migration.ini db grant --admin-role pgvn_admin --service-role pgvn_service --monitor-role pgvn_monitor
```

后续管理命令使用管理角色的连接配置。业务角色不能创建仓库、发布配置或管理令牌。迁移所有者和业务角色均可不具备 PostgreSQL 超级用户权限。

## 数据库初始化与升级

`db init` 和 `db migrate` 使用相同的有序、幂等迁移流程。SQL 及其 SHA-256 已编译进程序，不从工作目录扫描 SQL 文件。迁移在一个数据库事务内取得专用锁，多个初始化进程串行执行。

当前存储 schema 为 pgfs 1–4，控制 schema 为 pgvn 1–2。`db check` 检查连续版本、摘要和必要表；摘要不符、版本缺失或未知版本会拒绝继续。迁移不会默默改写已经安装的摘要。M1 二进制不接受新增的控制版本，不能直接用旧程序回退已升级的数据库；升级和回退需配套备份方案。

本地门禁使用独立 `127.0.0.1:16432` PostgreSQL 实例，并在该实例内生成独立测试数据库。fixture 的回环 trust 认证仅供隔离测试，部署认证由 PostgreSQL 管理员配置。

## 仓库、路由与导入导出

选择器可使用 `name:名称`、`id:内部UUID` 或不产生歧义的名称/ID。内部 ID 与公开 SVN UUID 分开，路由按完整路径段匹配且区分大小写。

```powershell
pgvnadmin repo create demo --route /svn/demo
pgvnadmin repo list --limit 50 --offset 0
pgvnadmin repo show name:demo
pgvnadmin repo disable name:demo
pgvnadmin load name:demo --file C:\Transfer\demo.dump
pgvnadmin verify name:demo
pgvnadmin dump name:demo --file C:\Backup\demo.dump --deltas
```

新建仓库初始为空，默认授权拒绝访问。load 要求目标先禁用并排空已有句柄；它对目标持独占会话租约。导入按标准 SVN 的每个修订原子发布，后续错误不会撤销已完成的修订；可用 look 查看 HEAD 与暂存事务，再决定恢复或清理。

省略 `--file` 时，load 从 stdin 读取，dump 写到 stdout。文件输出采用排他创建，已有备份文件不会被覆盖。dump 不接受 `--json`，避免混入非 dump 数据。文本查询默认 UTF-8，二进制内容及属性逐字节输出。

`-r START:END` 指定包含端点的修订范围。dump 的 `--incremental` 生成相对前一修订的首段，`--deltas` 输出标准 svndiff。load 支持修订过滤、`--ignore-uuid` 和 `--force-uuid`；缺省沿用上游规则，空仓库采纳 dump 中的 UUID。导入默认不执行 pre/post hooks；`--use-hooks` 显式启用当前策略。

既有 PgFS 历史可直接登记：

```powershell
pgvnadmin repo register-existing imported STORAGE-ID --route /svn/imported
pgvnadmin verify name:imported
```

登记保留原内部存储 ID、公开 SVN UUID 和全部历史，初始为禁用。旧文件适配器的 passwd/authz/hooks 不会自动迁入控制配置。迁移接入前应停止旧前端，配置 PgVN 策略并完成后续协议验收；旧前端不会自动遵守新的 PgVN 路由与权限代数。

## 查看历史与事务

| 命令 | 输出 |
| --- | --- |
| look youngest / uuid | HEAD 或公开 UUID；`--json` 返回结构化结果 |
| look info | 指定修订的 author/date/log；内容按 base64 表达，避免字节丢失 |
| look tree [PATH] | 目录树；`--full-paths` 输出完整路径，`--json` 返回路径与类型 |
| look cat PATH | 原始文件字节；`--json` 返回 base64 |
| look changed | 变更、类型与复制来源；`--json` 返回结构化结果 |
| look proplist [PATH] | 属性名；`--verbose --json` 包含 base64 值 |
| look propget NAME [PATH] | 原始属性字节；`--revprop` 选择修订或事务属性 |
| look transactions | 持久化的活动事务 ID |
| look txn-info --txn ID | 事务 author/date/log；其他适用查询也可使用 `--txn` |

look 使用 `-r REV` 或 `--txn ID`，两者不能并用；未指定时读取 HEAD。verify 执行后端结构/内容校验以及上游仓库层的修订语义校验，通过已有数据库句柄工作，不重新打开磁盘仓库。

## 配置和授权

```powershell
pgvnadmin config show name:demo
pgvnadmin authz validate --authz C:\PgVN\policy\demo.authz --groups C:\PgVN\policy\groups.conf
pgvnadmin config publish name:demo --expected-generation 2 --authz C:\PgVN\policy\demo.authz
pgvnadmin authz check name:demo alice /trunk write
pgvnadmin repo enable name:demo
```

先读取实际 generation，再发布。解析失败或代数冲突不会部分更新配置。未提供 `--groups`、`--hooks` 时保留原值；需要替换时显式传入文件，清空 hooks 使用内容为 `{}` 的文件。禁用/启用同样产生新代数，旧请求不能通过重新启用恢复过期的写权限。

M2 内置 hooks 支持已知事件的 `allow` / `reject`，例如 `{"pre-commit":"reject"}`。外部程序及 SVN 用户认证在 M3 实施。pre 拒绝阻止发布；post 错误保留已提交修订并报告失败。

## 维护与生命周期

```powershell
pgvnadmin maintenance pack name:demo
pgvnadmin maintenance recover name:demo
pgvnadmin maintenance hotcopy name:demo demo-backup --route /backup/demo
pgvnadmin maintenance hotcopy name:demo name:demo-backup --incremental
pgvnadmin maintenance freeze name:demo --timeout 300 -- C:\Tools\backup.exe ARGUMENTS
pgvnadmin maintenance abort-txn name:demo TXN-ID
```

hotcopy 仅支持同一个数据库。完整复制生成独立内部 ID，保留 SVN UUID、历史和配置，目标初始禁用。增量目标必须禁用、没有其他句柄/活动事务，且属于该来源并未产生分叉；冲突会回滚，不覆盖分叉数据。

pack 压实目录表示，recover 校验已发布根，不替代 PostgreSQL WAL 恢复。freeze 在回调期间持有仓库写入锁，使用无 shell 的 Windows 子进程参数传递；超时或取消会清理自己创建的进程树并释放锁。它保留子程序的标准流，不接受 `--json`。

物理清除是独立命令，必须先禁用、结束所有活动事务、排空句柄，并提供精确 ID：

```powershell
pgvnadmin maintenance purge name:demo --confirm-id STORAGE-ID
```

注册、配置和存储数据在同一事务清除。该命令永久删除仓库历史；命令返回不确定错误时，应先查询状态，不要切换到另一份同名数据继续写入。

## 监控令牌、诊断和退出码

```powershell
pgvnadmin monitor-token create --label operations --expires-in 86400
pgvnadmin monitor-token list
pgvnadmin monitor-token expire TOKEN-ID --expires-in 0
pgvnadmin monitor-token revoke TOKEN-ID
pgvnadmin diagnose --json
```

令牌使用 Windows CNG 生成 256 bit 随机值，原值只在创建时输出，数据库只保存 SHA-256。scope 固定为 `monitor:read`。列表不返回令牌或摘要；撤销幂等且不会因设置新过期时间恢复。M4 将接入监控认证，当前网页仍为 bootstrap。

元数据命令输出 JSON。`--json` 同时使错误及 look 查询结构化；原始二进制和外部子程序流按上文处理。退出码：0 成功、1 操作失败、2 参数/配置错误、3 数据库不可达、4 schema 不兼容、5 权限不足、6 不存在、7 冲突。

Release 验收与限制见 [M2 证据](evidence/m2.json) 和 [实施进展](progress.zh-CN.md)。便携目录验证证明当前机器上不依赖开发运行时；干净 Windows 机器、完整协议、HA 与 Polarion 应用验收继续在后续里程碑执行。
