# M6 工作副本语义与仓库不存在错误

日期：2026-10-16。本组件完成固定上游 switch、depth、revert 全部 125 个原版函数的 SVN、HTTP、HTTPS 差分，并修复仓库不存在时的协议错误分类与 DAV XML 响应。结果见[结构化证据](evidence/m6-network-wc-workflows.json)。PGVN-601、M6、M7 继续推进。

## 原版套件与结果

| 原版套件 / 范围 | SVN | HTTP | HTTPS |
| --- | --- | --- | --- |
| switch 1–39 | 38 PASS / 1 XFAIL (`df981bd94377`) | 38 PASS / 1 XFAIL (`9a116f2e7e1d`) | 38 PASS / 1 XFAIL (`a0c047f65414`) |
| depth 1–50 | 48 PASS / 2 XFAIL (`e4637c749512`) | 48 PASS / 2 XFAIL (`1fc08719fc5a`) | 48 PASS / 2 XFAIL (`d830d66b3cf6`) |
| revert 1–36 | 33 PASS / 3 XFAIL (`23f8e4911480`) | 33 PASS / 3 XFAIL (`849c925f0da2`) | 33 PASS / 3 XFAIL (`07b22641703d`) |

每项均包含原版 FSFS 与 PgVN 两侧，合计 **750 个结果：714 PASS、36 个原有 XFAIL，750 次实际进入用例**。每侧、每协议实际进入 125 个唯一函数；没有新增排除、SKIP 或断言改写。定点 switch16 的六个结果用于修复确认，不重复计入 750。

原版 XFAIL 为 switch9、depth49/50、revert25/26/36。18 对完整诊断仅精确归一已记录的工作目录与监听地址后逐字一致；原始客户端输出、函数、装饰器和 test_list 均保留。完整 HTTPS 轮次验证私有 CA 证书链与主机名，并拒绝未知 CA 和错误主机名，没有修改机器信任库。所有原生仓库别名在入口前后均不存在本地目录。

## 原始失败与修复

原版 switch16 切换到不存在的仓库根时，FSFS 返回仓库无法打开的协议诊断。PgVN 的 SVN 入口虽然使用正确的 E210005，却返回另一段自定义文本；DAV 入口则将注册表缺失当作仓库内部路径缺失，客户端最终收到 E160013，无法满足原版错误语义。

DAV 请求在 URI 翻译阶段便可能退出，早于授权及仓库提供器。只修复后面的提供器映射或授权返回点不能覆盖这条路径；三个候选的原始 FAIL、模块及源码摘要均已保存。定位到固定上游 `util.c` 的提前返回后，修复覆盖 URI 翻译、源仓库授权与 COPY 目标仓库三个出口。

| 场景 | 当前行为 |
| --- | --- |
| SVN 的未知或已停用路由 | E210005；统一文本 `No repository found at the requested URL`。 |
| DAV 的未知或已停用仓库 | HTTP 404，使用固定上游 XML 错误序列化器返回 E180001；统一文本 `Could not find the requested SVN repository`。 |
| 已存在仓库内的缺失路径 | 保留原来的仓库内路径错误处理。 |
| 实际数据库故障 | 保留受控的 503 分类，不映射为仓库不存在。 |

未知与停用路由使用相同通用诊断；授权检查仍保持失败关闭。没有增加授权豁免或迁移数据库结构，控制 schema 仍为 6。SVN、DAV 回归分别核对两节点以及 HTTP v1/v2；新增 OPTIONS 响应的 XML 类型、错误码、文本与请求 ID 检查，以及 COPY 到缺失仓库时的 404 XML 和 HEAD 不变检查。

独立 HTTP 诊断另覆盖未知/停用仓库的 PROPFIND、POST、HEAD：六种错误后均在同一 TCP 连接继续成功执行有效仓库 OPTIONS，排除错误响应留下未读实体导致的帧错位。这些诊断不计作上游原版函数或性能基准。

## 冻结构建与完整回归

冻结清单 `e63c40be2a119368e6060d4e4637dfc2ee8936d1a8dc45106cfa1aa0e56bb997` 含 154 项输入，基于 `c6381272e20af5fc02bfed5dcb9a7dc859dba587` 的未提交修复构建。证据保留构建当时的基线与 dirty 状态，不改写成之后的提交身份。生产改动涉及 SVN 诊断、DAV 映射/序列化及固定上游适配；网页资源未变化。

同一清单下，真实浏览器 **15 项监控 + 6 项 DAV** 检查通过。桌面实例显示实际会话上限 64，独立对端显示 2；丢失实例和恢复后的新实例可区分。375×812 明暗布局、局部表格滚动及自动刷新后的两个详情展开状态和焦点均实际观察。数据库故障显示过期样本和未知节点，恢复后重新就绪；之后撤销令牌，数据与健康状态清空、输入为空且焦点返回令牌框，DOM 与辅助功能树一致。

首轮浏览器只完成部分检查，因多标签页的视口设置未作用到被测页而没有登记 PASS；记录和清理证据均保留。新夹具在唯一监控标签页应用视口并重载，实际核对 `innerWidth=375`、`innerHeight=812`；没有通过修改 CSS、DOM 或安全策略代替窄屏测试。实际 Windows 高对比度视觉仍待验收。

完整 M5 **`m5-0587e0a9e03b`** 十组通过，耗时 **578.641 秒**，内含 M4 **`m4-77ebedd6492f`** 八组以及 M0–M3 回归。一次启动漏带 SVNKit 环境变量的失败 `m5-9d02fcf1345a` 原样保留；确认自有进程、服务和三个关联库的有效令牌均为零后，以正确环境重新执行全门禁，未把局部通过视为全门禁结果。

![同一冻结构建的服务概览](images/m6-wc-workflows-overview.jpg)

![同一冻结构建的实际会话容量](images/m6-wc-workflows-capacity.jpg)

<p align="center">
  <img src="images/m6-wc-workflows-nodes.jpg" alt="375 像素暗色节点概览，区分在线和失联实例" width="300">
</p>

截图均为隔离测试数据，采样数值和套件执行耗时不代表生产性能。

## 清理与归档

最终九组关闭 42 个内层 Job、9 个外层 Job，关闭前活动进程均为零。三组定点另关闭 14/3 个 Job；首轮旧构建关闭 17/4 个 Job，原有失败不进入最终通过统计。16 个网络隔离库保留且所有者逐一核对，没有创建监控令牌。各轮数据库、角色和原始日志均保留。

两次浏览器夹具均已停止，两枚短期令牌均已撤销；门禁清理按测试 UUID、目录创建窗口和实际数据库所有者核对，22 个隔离库中共 7 枚测试令牌均已撤销。最终运行进程和临时测试服务均为零。范围仅限已授权的 `127.0.0.1:16432` 专用集群和本仓库 runtime，不修改已安装 PostgreSQL 或 Polarion 输入。

| 归档 | 索引 SHA-256 | 文件数 |
| --- | --- | --- |
| `m6-wc-workflows-full-07b22641703d` | `6a69344d42a963b78e774bbff1fa72e6b5651e6ada4d14b099055cecc3fe8406` | 443 |
| `m6-wc-workflows-target-63fa44a19d27` | `73e7a0d5d4606780b35b4c30972fcf959139db7af818bd619ebd33fbe2477437` | 323 |
| `m6-wc-workflows-browser-378953f87f86` | `0471a2c7b8a75dee20fb2c806f770d021ec41e6907afeebc2cf7f8a20ad4a2d9` | 16 |

归档位于 `runtime/pgvn/baselines`。首轮旧构建、三个修复候选及环境失败的证据分别保留，摘要与当前结果分开，详见结构化记录。部署使用匹配的完整 Windows 开发包，完整性、95 个 x64 镜像及清空 PATH 启动检查见[包旁记录](evidence/m7-package-wc-workflows.json)。包状态为 `development-snapshot`；干净机器、整机重启及 Polarion 应用验收不据此完成。

## 可复现入口

下列命令仅用于已获批的隔离集群，每次计划须使用新的数据库；不得再次执行已经启动过的计划。

```powershell
python scripts/pgvn/build.py --config config/build.local.json --configuration Release
python scripts/pgvn/build_upstream_network.py
python scripts/pgvn/build_upstream_helpers.py
python scripts/pgvn/build_upstream_http.py
$run = python tests/pgvn/test_m6_network.py --plan --protocol svn --suite switch_tests --cases 1:39 | ConvertFrom-Json
python tests/pgvn/test_m6_network.py --execute-plan $run.plan
# HTTP/HTTPS 分别生成全新计划；depth_tests 为 1:50，revert_tests 为 1:36。
```

完成同一二进制的真实浏览器检查并停止夹具后，运行完整门禁：

```powershell
$env:PGVN_TEST_JAVA_HOME = 'C:\Program Files\Eclipse Adoptium\jdk-21.0.11.10-hotspot'
$env:PGVN_TEST_SVNKIT_PLUGINS = 'C:\Polarion\polarion\plugins'
python tests/pgvn/run_gates.py --milestone M5
```

SDK 路径为本机验证环境的只读输入；其他开发机应使用自己的实际路径。部署目标不需要 Java 或 Python。

## 剩余范围

本组件不能替代全部 62 个原版套件、1,984 个函数的适用性台账与执行；较早组件仍绑定各自构建，没有与本轮合并为一次最终产品矩阵。继续其余套件、最终产品的早期套件复跑、维护语义、638 修订真实历史与 30 表备份恢复复核、确认提交账本、跨故障域主备与 fencing、代表性持续性能和监控预算。实际高对比度、独立 Windows 安装重启及隔离 Polarion 应用验收继续待完成。
