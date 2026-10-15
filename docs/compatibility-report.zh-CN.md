# PgFS Windows 原生兼容验证

PgFS 已能通过原生 svnserve 和 Apache mod_dav_svn 提供 SVN 服务。
当前验证针对 Windows x64、SVN 1.14.5、PostgreSQL 18.6；测试同时使用
未经修改的 Polarion SVN 客户端和安装中的 SVNKit 1.10.1。

| 验证 | 结果 |
| --- | --- |
| 自定义集成检查 | 33 项通过 |
| 官方原生 FS/仓库检查 | 94 项通过，4 项上游跳过，4 项存储或版本假设不适用 |
| 官方命令与协议回归 | 51 组适用完整套件；1891 次 PASS，69 次 SKIP，54 次上游预期失败 XFAIL；无非预期失败 |
| 合成 ALM 仓库 | 20 个历史版本全部与 FSFS 一致 |
| 真实 Polarion 历史 | r0–r637，共 638 个版本全部校验通过；全部变更路径与修订属性一致；5 个完整历史树快照一致 |
| 原始源码 | 1195 个 C、头文件和 Python 文件与固定版本归档一致 |

补充验证使用两个独立 svnserve 进程共享同一仓库，确认提交和可变修订属性双向
立即可见、锁跨前端生效、受限读取被拒绝。中文、NFC/NFD 名称及大小写不同的
目录也按同一事件序列与 FSFS 对照，保持原始名称。

回归次数包含同一用例在不同协议下的重复执行。跳过、预期失败和不适用
用例均保留在报告中，不计为通过。

验证涵盖提交/更新、复制/移动、历史、合并与 mergeinfo、二进制内容和属性、
锁及授权、hooks、dump/load、hotcopy、freeze、校验和数据库崩溃恢复。
本轮修复了跨进程写流互斥、删除路径的锁保留、verify 进度通知和锁错误说明，
并让全库校验复用已验证的不可变节点。新增检查覆盖 HEAD 已删除文件的历史损坏
与指定版本范围的校验行为，以及包含继承节点和暂存内容的事务根校验。
逐版本检查仅遍历新节点，全局检查覆盖历史数据，恢复校验仍覆盖完整 HEAD。

完整证据： [compatibility-evidence.json](../artifacts/tests/compatibility-evidence.json)。
运行方式及范围： [implementation-status.md](implementation-status.md)。
服务接入： [service-integration.md](service-integration.md)。

按用户授权，只读导出了真实 Polarion 仓库截至 r637 的完整历史，
导入项目内 `runtime/corpus` 隔离仓库和 `pgfs_poc` 测试数据库。
完整快照比对覆盖 r0, r1, r318, r636, r637，
包括实际文件流摘要及长度、目录结构、节点属性、创建/起源/复制元数据。
修订属性以固定导出为基准逐版本比对，全部变更路径也逐版本一致。
此处 5 个快照属于抽样；全部版本均执行了完整性校验，
不等于每个版本都做了全树逐节点比较。源仓库、Polarion 配置和服务未作修改。

已完成的是 Windows 原生 SVN 兼容与服务接入阶段。真实 Polarion 应用运行、
生产性能、复制与故障切换仍需独立验证。当前 hotcopy 限同一 PostgreSQL 数据库；
跨数据库迁移使用 SVN dump/load。当前报告不证明所有 SVN 版本及部署环境都兼容。
当前文件内容按 1 MiB 分块存储，尚无版本间增量压缩或去重；全库校验已在一次调用内
复用已验证的不可变节点，同时保留每次访问的修订约束检查。吞吐、延迟、缓存内存
和增量压缩仍属于后续性能评估的明确重点。

当前模块 SHA-256：`a06a79ba5efb9cf9bace026fdda1dfe820e3df13f23ae1f3f08403a2b121bb5c`。
