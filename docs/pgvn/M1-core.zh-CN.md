# M1 无目录公共核心

M1 的可用范围是进程内仓库核心。`core_probe.exe` 为测试驱动，不作为正式管理工具发布；pgvnadmin 的完整命令在 M2 实施，网络协议与完整监控继续按 M3–M5 推进。

## 仓库创建、绑定与历史

`pgvn_repo_create` 在一个 PostgreSQL 事务内写入存储记录、r0、注册、默认拒绝授权配置和路由。内部 repository_id 与 SVN UUID 分别生成；传入公开 UUID 时校验格式。名称或路由冲突回滚全部记录，八进程同名创建只允许一个成功。

`pgvn_catalog_resolve` 按完整路径段最长前缀匹配，禁用的最长路由不会回退到父仓库。使用精确大小写和 UTF-8，不折叠 NFC/NFD。库内路径与仓库标识分开返回，协议层仍负责一次解码及编码分隔符检查。

`pgvn_fs_bind` 直接取得数据库连接并绑定 PgFS vtable。FS 标识为 `pgvn:<uuid>`，配置文件列表为空；不会调用 FS loader 的路径识别。`pgvn_repos_open` 使用固定版本私有布局构建完整仓库对象，格式与能力表在内存初始化，conf/hooks/locks/db 路径为空。hook 分派在固定源码构建副本中适配，不扫描每库目录。

存储端新增按句柄的 pack、recover、hotcopy、delete 入口，原有文件适配器继续使用 pgfs.conf。M1 已通过 pack/recover 与历史快照一致性检查；热复制、删除的目录访问已从新入口拆开，其注册表生命周期和正式管理命令在 M2 一并验收，不把底层辅助入口等同于管理功能完成。

事务、二进制流、版本树 COW、锁及发布仍由同一 PgFS 实现处理。结束的事务继续遵守原 SVN FS 的不可重新打开语义；提交结果查询使用独立 `pgvn_transaction_receipt`，按仓库与事务键读取已落盘回执并校验用户身份，不重新执行提交。

## 权限、配置与连接

控制 schema 已实现仓库、路由、不可变版本快照及迁移摘要。配置发布先用 SVN 的流解析器校验 authz/groups，再按期望代数原子发布。缓存范围为一个仓库句柄：只解析一次、保持同一代数，不建立无限期的跨请求权限缓存。重开请求取得最新配置。

提交在同一个 SQL 发布事务中调用 `pgvn.guard_repository`，锁定配置行并检查 enabled/generation，然后用不可变 authz 快照检查变更路径及复制源；配置变更会拒绝旧请求。该受限 SECURITY DEFINER 函数固定 search_path，仅执行已限定的查询。业务角色因此不需要控制表 UPDATE 权限。

M1 hooks 提供内置 allow/reject 处理器并保留事件接口，未知处理器拒绝运行；外部程序和网络身份认证在 M3 实施。pre 拒绝不会发布修订，post 失败返回已提交修订及上游的 post-hook 错误。修订属性变更默认仍需显式策略。

每个上下文最多 32 条连接，包含控制连接；一个 FS 固定自己的连接及会话写流锁，关闭 pool 才释放。默认连接超时 3 秒、语句超时 5 秒。SQL 指标回调只更新原子计数；快照不查询数据库。M1 没有后台采样器，schema 检查不能使网络服务 ready=true。

## 隔离开发与验证

```powershell
python scripts/pgvn/build.py --config config/build.local.json --configuration Release
python tests/pgvn/fixture.py
python tests/pgvn/run_gates.py --milestone M1
python tests/pgvn/run_gates.py --milestone M0
```

fixture 固定到 `127.0.0.1:16432/pgvn_poc`，每次使用前核对 data_directory 为本仓库 `runtime/pgvn/pg-primary`。已安装的迁移摘要不符时拒绝重放。只有测试 fixture 使用回环 trust 认证；部署凭据和授权由后续安装/管理工具提供，不能把该测试认证配置复制到生产。

引导所有者与 `pgvn_admin`、`pgvn_service`、`pgvn_monitor` 分开。后两者没有超级用户权限；业务角色只能读控制元数据和调用配置锁函数，不能创建仓库或发布权限。监控角色仅取得明确表的读取权限。

M1 门禁覆盖路由与 Unicode、原子并发创建、二进制历史及复制、权限和组规则、旧代数请求拒绝、pre/post hooks、回执身份、跨进程写流锁、断连释放、32 连接上限、计数与资源回收、数据库重启及空工作目录。另运行 17 个原 PgFS 原生用例，不改断言。原 restart 用例固定旧目录和 15432 端口，明确排除，由核对目录后的 PgVN 专用重启检查替代。完整结果见 [M1 证据](evidence/m1.json)。

该结果不代表 SVN/HTTP 协议、全部官方套件、跨主机 HA 或 Polarion 应用已经验收；这些继续按后续里程碑执行。
