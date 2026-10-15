# SVN 兼容与服务接入完成审计

本审计对应目标“实现 PgFS，先推进到 SVN 兼容完成，支持接入 SVN 服务”，
对应原 PoC 计划的 P0–P2。固定环境为 Windows x64、Subversion 1.14.5、
APR 1.7.6、APR-util 1.6.3、PostgreSQL 18.6。

当前模块：`a06a79ba5efb9cf9bace026fdda1dfe820e3df13f23ae1f3f08403a2b121bb5c`。
完整的命令输出、精确用例编号、跳过原因、源码摘要和构建摘要由
[证据汇总](../artifacts/tests/compatibility-evidence.json) 校验。

| 目标与计划中的验收项 | 直接证据 | 结果 |
| --- | --- | --- |
| Windows 原生 FS 模块可加载；r0、UUID、格式及重启后读取 | `p0-results.json`；构建清单；原生 FS/仓库套件 | 通过 |
| 文件、目录、流、内容校验、二进制/空属性、历史不可变 | `p1-results.json`；原生 FS 套件；官方 prop/cat/checkout/depth/update/switch/export 等套件 | 通过 |
| 暂存事务可重开、撤销、校验失败回滚；原子提交与无丢失并发发布 | `p1-results.json`；原生 FS 事务与 merging_commit 检查；`faults-results.json` | 通过 |
| 同文件/属性、删除与修改、并发双删除及目录属性合并 | 原生 FS merging_commit、unordered_txn_dirprops、dir_prop_merge；官方 commit/update/merge 套件 | 通过 |
| 同路径重建、嵌套/历史复制、移动、复制源删除、谱系与 copyfrom | `p2-results.json`；原生 FS branch/closest-copy/history；合成仓库全部 20 个版本的完整快照 | 通过 |
| 大小写不同路径、中文、NFC/NFD 原始名称和空目录 | `p1-results.json` 中路径名称检查，PgFS 与 FSFS 执行同一事件序列并逐名称核对 | 通过 |
| 锁 token、所有者、抢占、强制解锁、递归删除与真正过期 | `p2-results.json`；上游 lock-helper 创建有期限锁；过期后其他用户提交；官方 lock 的 file/svn/http 套件 | 通过 |
| pre-commit 拒绝、post-commit 报错保留已提交事实、revprop hooks | `services-results.json`；官方 commit 测试 38 在 file/HTTP 两种协议通过 | 通过 |
| 路径授权在读取、枚举、历史、合并与同步中生效 | 两种协议的 authz/merge_authz/svnsync_authz/iprop_authz；mod_authz_svn；原生客户端受限读取检查 | 通过 |
| 提交后第二个独立前端读取；可变修订属性和锁跨前端一致 | `services-results.json` 的双 svnserve 检查；`dual-service-fixture.json` | 通过 |
| 发布版本与已完成暂存通过数据库崩溃恢复；未完成流不发布 | `recovery-results.json`；写流进程终止后接管；`faults-results.json` 的连接与 COMMIT 响应故障 | 通过 |
| dump/load/verify、UUID/r0、复制历史、双向迁移 | `admin-results.json`；FSFS→PG→FSFS；合成仓库逐版本对照；官方 svnadmin/svnrdump/svnsync/svndumpfilter | 通过 |
| pack、freeze、recover、完整和增量 hotcopy、独立命名空间 | `admin-results.json`；原生仓库套件；真实历史全量 verify | 通过 |
| 正常协议客户端可读写；Polarion 安装中的 SVNKit 可用 | 原版 SVN 1.14.5 客户端与 SVNKit 1.10.1，分别通过 svnserve 与 Apache mod_dav_svn | 通过 |
| 真实 Polarion 历史只读导出、隔离导入及语义对照 | r0–r637 共 638 个版本全量校验；全部变更路径和修订属性一致；5 个完整快照一致 | 通过 |
| 每个 FS 接口有实现、标准不支持行为或加载器委托 | 构建清单列出 77 个已绑定错误返回接口及 1 个加载器模拟接口；无服务路径指向生成占位回调 | 通过 |

可复现的最终证据为 33 项集成检查、94 项官方原生检查、
51 组官方命令/协议套件中的 1891 次 PASS。
每种协议的重复执行保留为独立执行记录。上游原有 SKIP、XFAIL 均逐项列出，
不作为 PASS；不存在 FAIL 或 XPASS。1195 个
上游 C、头文件和 Python 文件与固定归档逐字节一致，官方断言未作修改。

## 存储与版本范围

原生 FS 测试 40 依赖 FSFS/BDB 的格式数字，65/67 操作其他后端的物理
rep-cache/表示文件；原生 repos 测试 21 假定后端存在于 SVN 1.4 之前。
svnadmin 测试 43 直接破坏 FSFS 的 `db/revs/0/1`。这五项存储/版本假设
均有源码依据，不能套用到 PostgreSQL 存储；PgFS 自己的格式、内容损坏检测
和恢复另有直接检查。具体原因保留在证据 JSON 的 exclusions 中。

hotcopy 当前覆盖同一 PostgreSQL 数据库中的独立仓库命名空间；跨数据库
迁移使用 dump/load。后端原生 ABI 固定在 SVN 1.14.5，其他服务器版本需要
重新构建和验证。BDB 日志及其他后端的物理存储控制返回标准不支持行为。

真实库的全量完整性校验覆盖全部修订；与原仓库的完整树逐节点比对覆盖
r0, r1, r318, r636, r637，属于明确列出的抽样。
全部修订属性以固定导出为基准，全部变更路径逐修订比对。原 Polarion
仓库和服务配置未作修改。

## 后续 PoC 阶段

[P3–P5 原计划](plans/2026-10-14-postgresql-fs-poc.md) 中的真实 Polarion 应用
工单/LiveDoc/附件/基线/索引重建，代表性性能与容量评估，以及同步副本、
隔离旧主和故障接管仍需继续实施。本审计完成的是当前用户指定的 SVN
兼容与服务接入阶段，不是整个产品化 PoC。
