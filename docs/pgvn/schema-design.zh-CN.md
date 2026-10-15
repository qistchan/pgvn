# 控制 schema 迁移设计

M0 设计，尚未声称已安装。历史数据继续使用 pgfs 的四个迁移，控制状态用 pgvn，遥测用 pgvn_monitor。所有仓库外键使用内部 repository_id，SVN UUID 只作为对外身份。

| 表 | 主键和约束 | 生命周期 |
| --- | --- | --- |
| pgvn.schema_versions | version 主键，migration_sha256 非空 | 串行事务迁移，安装后不改写摘要 |
| pgvn.repositories | repository_id 外键 pgfs.repositories，canonical_name 唯一且 C 排序，enabled，generation>0 | 创建与 r0/配置/路由同事务；禁用不删历史 |
| pgvn.routes | route_prefix 唯一，repository_id 外键 | 规范 UTF-8 绝对路径，完整段匹配；并发注册由唯一约束裁决 |
| pgvn.configurations | (repository_id,generation)，authz_text、groups_text、settings JSON、hooks JSON、published_by/time | 不可变快照；仓库 generation 为已发布版本，复合外键约束其存在 |
| pgvn.credentials | (identity_domain,username,generation)，key_id、加密载荷、enabled | 不存可读密码或部署密钥；代数失配拒绝认证，密钥轮换显式执行 |
| pgvn.dav_activities | (repository_id,activity_id)，txn_id、状态 active/committed/aborted、commit_rev、owner/version | 原子关联存储事务；已提交回执保留，回收要检查活跃持有者 |
| pgvn.monitor_tokens | token_hash 主键、scope、expires_at、revoked_at | 只读 scope；验证失败不回退匿名业务信息 |
| pgvn_monitor.instances | instance_id 主键、node_id、component、版本、pid、started_at、heartbeat_at、config_generation、state | 心跳使用数据库时钟，进程退出尽力标记，失联由时效判定 |
| pgvn_monitor.samples | (instance_id,sampled_at,sequence)，有界指标批次 | 24 小时保留，按时间索引，限批清理 |

配置发布必须在一个事务中插入完整且通过 SVN 解析的快照，再更新 repositories.generation。同一命令保留一个 generation；提交前再次检查 enabled/generation 与用户写权限。通知丢失由下一次元数据复核弥补，不将无限期缓存当作授权。

业务连接只授予业务 DML 和读取已发布控制数据；迁移所有者用于 DDL；监控采样只读受限视图，遥测发布角色只写自己的实例记录与批次。服务无需 PostgreSQL 超级用户。M1 隔离数据库端口 16432、名称 pgvn_poc、目录 runtime/pgvn/pg-primary；不得复用旧 PgFS 或生产库进行故障测试。

DAV 创建映射与存储事务要共用一个数据库事务，或写可恢复的创建意图并验证不存在孤儿。跨节点命令按期望状态与版本 CAS；同一仓库/事务提交至多发布一个 revision。重试先查持久化回执。清理策略不凭年龄认定一个长事务已经失效。

数据库权限、CAS、配置发布失败、迁移重放/摘要不符、路由冲突和跨库映射均纳入相应里程碑的真实数据库测试，M0 仅固定设计。
