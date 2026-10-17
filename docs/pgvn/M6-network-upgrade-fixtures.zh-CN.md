# M6 旧工作副本升级夹具与 Windows 搬移

日期：2026-10-17。原版 `upgrade_tests` 1–35 及 `authz_tests` 24（`upgrade_absent`）在 SVN、HTTP、HTTPS 完成 FSFS/PgVN 对照，**36 个唯一函数、216 个结果全部 PASS**。详细摘要见[结构化证据](evidence/m6-network-upgrade-fixtures.json)。PGVN-601、M6、M7 保持未完成。

## 原版结果

| 原版范围 | SVN | HTTP | HTTPS |
| --- | --- | --- | --- |
| upgrade 1–35 | 35 PASS (`621c41d6bce9`) | 35 PASS (`8dcba925b1af`) | 35 PASS (`23bd33c79411`) |
| authz 24 | 1 PASS (`c9fccf099b03`) | 1 PASS (`8b164d0cc7db`) | 1 PASS (`18b925e5f9ef`) |

每格为单个后端计数，两侧原编号、函数名和结果相同。原版函数、装饰器、test_list、客户端输出和断言未改，没有新增排除。升级对象是客户端旧工作副本元数据；这些结果不表示 PgVN 支持或需要升级 FSFS 的服务端磁盘格式。

## 三项夹具修复

1. 补齐原版 `get_current_format()` 直接读取的 `subversion/libsvn_wc/wc.h`。头文件来自同一锁定归档，30,467 字节，SHA-256 为 `517d77c89898b93bcb51257e66ed16cd26f160bca0d3d39ba886386f94e8154b`，工作副本格式为 31。原有 234 项输入不变；六个执行快照各自核对全部 235 项文件。
2. 计划和执行入口允许套件名中出现数字，以接受原版 `shelf2_tests`。路径仍由严格名称模式及固定目录约束。原版目录仍为 62 套件、1,984 个函数，未把合法套件名拒绝当成不适用。
3. 两后端对同一原版旧工作副本准备函数安装局部搬移适配器。原函数继续执行清理和解包；适配器只替换该模块调用期间的 `shutil.move`，不修改全局 `shutil`。同卷 `os.rename` 对 WinError 5、32、33 最多重试约 3 秒，避免转入无法删除只读 `.svn` 文件的复制/删除回退。

适配器限制隔离库前缀、后端、工作根、锁定归档、临时目录和目标工作副本；已有目标、路径越界及其他错误仍失败。搬移前后逐项检查文件摘要、模式和 Windows 属性；不通过 chmod 或改写元数据使原测试通过。原生历史仓库仍通过固定 FSFS 输入的标准 dump/load 准备，保留 UUID 和 youngest，原生仓库别名不生成本地目录。

## 原始失败与离线验证

首次 SVN 升级运行 `m6-network-7d505953863d` 为 FSFS 33 PASS/2 FAIL、PgVN 32 PASS/3 FAIL，五个失败均发生在旧工作副本准备的搬移阶段。初始只锁子文件的假设未重现目录搬移拒绝；后续真实目录 deny-delete 句柄可重现 rename 失败和复制/删除回退的 WinError 5，释放句柄后重试 rename 保留全部字节与属性。首次失败中占用句柄的进程未知，不据此归因于杀毒软件。

离线检查 `m6-network-upgrade-offline-0e9428b370b8` 的 **21 项**通过，包含真实 Windows 目录共享锁的释放/永久拒绝、5/32/33 错误重试、非法路径/库/后端、归档篡改、清理失败、未知错误及搬移后篡改。没有启动真实子进程、连接数据库或执行原版测试函数。开发中缺少 json 导入、过严的精确重试次数断言分别产生的两份 FAIL 已冻结；后者首轮没有捕获逐次事件，另一次未改代码的诊断和最终检查分别保留。现在核对每次尝试与错误账本，不假定操作系统不会额外暂时拒绝。

三协议事件审计确认 **228 次**搬移的归档、原用例、目标相对路径、目录指纹和条目数在两侧一致；实际记录 5 次 WinError 5 后重试成功。重试次数可以因操作系统状态不同而不同。失败日志及未执行的旧计划保持原身份，没有重启已执行计划或改写失败结果。

## 构建、清理与证据

本轮冻结清单 `5e3d4cc3f2f8491a27b6aaede96e1ec921b31f908ec4324574bc580ef323b1e9` 含 **155 项注册输入**，改动为五个测试文件；五个生产产物、三个网页资源、67 项运行依赖和 84 项 Apache 依赖与此前 `e63c40be…` 构建一致。构建身份为 `c036339` dirty，本次代码提交不回写该身份。本轮没有新运行浏览器或完整 M5，也没有重打运行包；既有记录仍绑定其原始清单。

六个隔离库所有者均非超级用户，监控令牌行数和残留客户端连接均为零。28 个内层、6 个外层 Job 在关闭前活动进程为零；只读 CIM 观察确认 runtime 进程及临时 Windows 服务为零，专用 PostgreSQL 集群、隔离库、角色和日志保留。HTTPS 两侧均核对证书链/主机名及拒绝对照。历史仓库别名全部不存在。

通过结果的原始输入、报告、日志及离线结果冻结于 `runtime/pgvn/baselines/m6-upgrade-fixture-preflight-18b925e5f9ef`，**484 个文件**，索引 SHA-256 `7c4083d6ba4350a1e4f53ca060e6fbb3a108d85467b384f4ae3dd253ab73f259`。审计脚本、逐次搬移签名、队列和诊断控制文件另冻结于 `runtime/pgvn/baselines/m6-upgrade-fixture-controls-18b925e5f9ef`，索引 `ec3a66b168429089c39564e3201bf61f5ffd7f1f6c2d0a4e5d0c7030d1a2ab01`。最初失败和两轮离线失败的独立索引见结构化证据，保持原字节。

## 继续推进

其余 45 个新计划准备完成，尚未执行，覆盖合并、patch、shelf/shelf2 和 svnmucc 的 377 个唯一函数，预计 2262 个两后端三协议结果。shelf 的原有实验功能谓词继续保留，默认 SKIP 不能当成后端实际进入。

[已有覆盖账本](M6-network-coverage.zh-CN.md)仍明确绑定其 `e63c40be…` 快照，不能将 568 个函数直接改记到新清单。继续剩余套件及适用性、最终构建复跑、管理维护、638 修订历史及备份恢复、提交确认与跨故障域 HA、代表性性能和监控预算，以及独立 Windows/Polarion 验收。

在已授权专用集群重现时，创建新 UUID 计划；已启动计划不复用：

```powershell
python -B tests/pgvn/test_m6_network_upgrade.py
$run = python -B tests/pgvn/test_m6_network.py --plan --protocol svn --suite upgrade_tests --cases 1:35 | ConvertFrom-Json
python -B tests/pgvn/test_m6_network.py --execute-plan $run.plan
```
