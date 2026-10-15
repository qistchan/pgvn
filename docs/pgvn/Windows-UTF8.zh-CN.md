# Windows 进程 UTF-8 与中文部署路径

本轮修复中文安装目录下 DAV 无法连接数据库的问题，新增专用 Apache 宿主 `pgvnhttpd.exe`。开发快照的中文、emoji、é 与 NFD 组合字符目录、中文账号配合标准 `svn` 的安装、提交和备份恢复已通过本机检查。M6/M7 整体仍在进行，具体归档与报告分别见 [编码组件证据](evidence/m6-windows-utf8.json) 和 [部署证据](evidence/m7-package-utf8.json)。

## 原因与实现

隔离 C 探针记录到进程代码页为 936 时，APR `apr_app_initialize` 会将 CRT 环境变量转换为 UTF-8；libpq 读取 `PGSERVICEFILE` 后通过普通文件 API 打开路径，代码页不一致导致中文服务文件不可读。英文路径对照通过。进程清单启用 UTF-8 后，初始化前后 `GetACP()` 均为 65001，中文与补充平面字符路径的文件打开和真实数据库连接均成功。

实现使用 Windows 的 `activeCodePage=UTF-8` 进程机制，不调整系统区域设置。目标系统必须支持这一机制（Windows 10 1903 起）；自有服务、管理和 Apache 宿主在入口核对实际 `GetACP()`，不满足时明确退出。先运行 `Test-PgVNPackage.ps1` 核对当前目标机器的结果，平台生命周期或整个服务器版本认证不由这项组件测试推导。

- `pgvnserve`、`pgvnadmin` 和自有测试程序在链接时嵌入清单，版本 JSON 增加实际 `process_code_page`。
- `pgvnhttpd` 使用锁定的 Apache 2.4.68 `server/main.c` 加一个自有 Windows 宽字符入口，链接匹配 SDK 的 `libhttpd`、APR 与 APR-util。工作进程、服务安装和测试入口统一调用它；原签名 `httpd.exe` 仅供未修改的上游对照，不进入部署包。
- `src/pgvn/httpd/source-lock.json` 锁定五个官方源码/许可输入的大小和 SHA-256。`fetch_httpd.py` 显式下载并核对；构建只校验本地文件，不隐式联网。构建和包记录来源、源摘要及许可。
- 包内 `psql`、`pg_dump`、`pg_restore`、`pg_isready` 使用无签名原文件的私有副本合入清单。资源变换器拒绝签名文件，保留原清单的权限/兼容字段，并核对非资源节字节、标志、代码与数据 RVA、导入表；只允许 Windows 资源写入器移动可丢弃的重定位/调试节，重定位目录按精确偏移校验。SDK 源文件不修改，包记录前后摘要及资源写入器身份。

## 已测范围与限制

原生探针覆盖 APR 初始化前后、真实 libpq 连接、Unicode 服务文件和输出路径；签名 SDK 拒绝变换且摘要不变；无签名副本的两次独立输出相同、二次应用幂等，导入依赖不变。非超级用户管理角色可将 schema dump 写入带 emoji 的路径。

包验收使用 Windows PowerShell 5.1、最小 PATH 和真实 LocalService，覆盖双协议同 UUID、二进制内容和修订属性、锁、只读令牌、异目录/异所有者恢复、恢复后继续 DAV 提交、原数据库 HEAD 不变。中文作者同时比较管理工具返回的规范 UTF-8 原始字节与标准 `svn propget --xml` 文本；普通终端 `propget --strict` 的文本编码转换不能作为原始存储字节的基准。所有原始失败均保留，包括修正前这一测试基准选择错误。

固定 SDK 的 `svnmucc` 中文账号提交断连仍在原版 svnserve + FSFS 对照复现。本轮显式使用 `--write-client svn` 验证标准工作副本提交，不把它记录为 svnmucc 修复。SVNKit 1.10.1 中文路由修订属性的已知差异继续按 M5 指南处理。

当前构建的实际浏览器复验完成明暗概览、搜索/分页/详情、DAV 方法与错误 ID、刷新焦点、暂停、节点/数据库故障恢复及撤销清空；但文档所述 viewport API 设置后实际宽度仍为 1280，两项窄屏检查记为待补，报告为 PARTIAL。网页三个资源与已提交改版版本逐字节一致；旧窄屏证据仍只归属原冻结构建，不改写为新构建 PASS。完整 M4/M5 浏览器门禁不因这轮局部复验而改为通过。

本轮服务端单项回归结果另列在组件证据中；不能将其合称完整 M6 PASS。上游协议入口、故障账本、跨故障域、代表性性能、干净机器/整机重启和 Polarion 应用演练仍待后续验收。

## 依据

- [Microsoft：进程 UTF-8 代码页](https://learn.microsoft.com/en-us/windows/apps/design/globalizing/use-utf8-code-page)
- [Apache APR Windows 初始化源码](https://github.com/apache/apr/blob/1.7.x/misc/win32/start.c)
- [PostgreSQL libpq 连接与服务文件处理](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/interfaces/libpq/fe-connect.c)
- [Apache HTTP Server 2.4.68 主入口](https://github.com/apache/httpd/blob/2.4.68/server/main.c)
