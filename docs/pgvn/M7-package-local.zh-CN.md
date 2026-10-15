# M7 离线包与本机部署验证

交付范围为当前机器上的原生 Windows x64 开发快照，M7 仍未完成。新机操作步骤见 [部署指南](../../deploy/windows/README.zh-CN.md)。源仓库中的 [首轮本机证据](evidence/m7-package-local.json) 和 [UTF-8 部署证据](evidence/m7-package-utf8.json) 记录各自的实际 ZIP、SHA-256、验收报告和失败边界；包验收记录也可作为 ZIP 的独立侧文件保存，避免将归档自身的摘要嵌入归档。

## 构建与依赖

```powershell
python scripts/pgvn/package.py --development
```

输出在 `runtime/pgvn/packages`。省略 `--development` 时，必须有同一冻结 Release 构建的完整 M6 PASS 且 milestone_complete=true，否则拒绝生成正式包。开发快照明确标记 `development-snapshot`，不自动宣告干净机器或 Polarion 应用验收通过。

依赖来自当前固定 SDK：原生服务/管理程序及 DLL、Apache 与 DAV/授权及常用代理/TLS 模块、SVN 客户端、PostgreSQL 客户端。Apache 启动程序为从锁定的 2.4.68 `server/main.c` 构建的 `pgvnhttpd.exe`，链接原 SDK 核心 DLL。包内四个无签名 PostgreSQL 客户端副本合入 UTF-8 清单，记录变换前后摘要并验证非资源节字节与程序代码/数据 RVA 不变；原 SDK 文件及签名的 Apache/SVN 程序不重写。具体机制与平台要求见 [UTF-8 记录](Windows-UTF8.zh-CN.md)。解析每个 x64 PE 的普通/延迟导入并追踪闭包，显式加入 APR crypto 等动态选择项；VC 运行库作为应用本地依赖，不把它当作 Windows 系统 DLL。随附供应商许可、来源与每文件摘要，运行不需要开发 SDK、Python、Node 或现有 Polarion。

包内包含数据库/全局配置模板、中文步骤、SQL 查阅副本及内嵌网页资源参考；实际网页仍从 exe 提供。密码、部署密钥、测试探针、旧 svnserve 和 Windows 系统 DLL 不入包。安装/运行账户文件分离，LocalService 只读业务和监控连接，管理员连接保存在受保护目录。

ZIP 文件按固定顺序和时间戳生成；相同输入输出相同摘要，已有内容不同则拒绝覆盖。校验通过能证明输入/输出完整性，操作系统、服务端数据库和证书配置仍需在目标环境验证。

## 本机验收入口

```powershell
python tests/pgvn/test_m7_package.py --archive runtime/pgvn/packages/实际包名.zip --supplementary-paths --write-client svn
```

验收器核对专用 16432 集群的 data_directory，使用两个唯一数据库和不同非超级用户所有者，解压至带空格、中文、emoji、é 和 NFD 组合字符的两个独立目录；仅登记本次唯一 LocalService 服务和回环 URL ACL，最后卸载。既有 Polarion、SVN 及已安装 PostgreSQL 服务不受其控制。

覆盖：Windows PowerShell 5.1、最小 PATH、完整摘要/x64 检查、安装幂等、服务就绪、内嵌页面、双协议同 UUID、真实 SVN 写/DAV 读、二进制修订属性及锁；脚本停服备份后恢复原服务，在另一目录和空数据库恢复并保留 ACL/密钥/密码文件，验证两种协议读及 DAV 继续提交、原库不变。监控令牌保持有效，监控不能读取凭据、PUBLIC 不能执行遥测发布。篡改包文件被拒绝，结束后程序文件与 ZIP 摘要不变。

这轮数据库连接使用已有隔离 fixture 的回环 trust 认证，不代表生产 SCRAM/TLS 配置已完成验收。全局 hook 文件按指南部署后再激活，外部程序依赖需额外清点。

## 已保留的失败与待办

- 修复 Windows PowerShell 5.1 从 PowerShell 7 继承模块路径时的系统模块自动加载，以及服务控制程序集未显式加载；原失败记录保留。
- 中文安装目录的旧包 DAV 503 已定位并修复：APR 将环境变量转换为 UTF-8，而原宿主进程代码页仍为 936，libpq 的文件打开据此失败。原始失败保留，新的进程 UTF-8 清单及专用宿主已通过上述扩展路径安装和完整备份恢复。
- 中文账号的 `svnmucc` 提交断连仍在，原版 svnserve + FSFS 对照 `m6-username-control-fdc46bd10f35` 同样失败。标准 `svn` checkout/add/commit 已通过中文账号实测，规范 UTF-8 作者属性由管理工具的原始 Base64 与标准客户端 XML 双重核对；这不声明已修复 `svnmucc`。
- 验收参数不隐式跳过客户端差异：默认仍使用 `svnmucc`；上面的扩展配置显式选择 `--write-client svn`。首轮英文配置 `--ascii-paths --ascii-user` 的原始 PASS 和各轮失败仍保留，不能与新包混用摘要。
- M6 的完整上游协议入口、故障域和代表性性能尚未完成。干净 Windows 安装、重启自动恢复、跨主机故障切换及隔离 Polarion 项目/LiveDoc/索引演练仍待独立环境。
