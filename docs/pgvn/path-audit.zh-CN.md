# 固定 SVN 1.14.5 路径与私有 ABI 审计

输入源码只读，构建副本位于 build/pgvn。源树、头文件与二进制摘要由 `scripts/pgvn/build.py` 写入构建清单；不会将修改过的库覆盖回 SVN、Apache 或 Polarion 安装目录。

| 入口 | 当前依赖 | 适配路线与验证 |
| --- | --- | --- |
| PgFS pgfs_create/open/config_files | pgfs.conf、本地目录标识 | M1 抽取按 ID 创建/绑定；旧插件保留文件适配器并回归 |
| PgFS hotcopy/delete | 读取源/目标配置、目录删除 | M1/M2 新增 ID 管理入口，复用 SQL 树复制，不将逻辑标识传入 svn_io |
| FS loader svn_fs_create/open/type | format、db/fs-type、动态提供者 | M1 直接构造 FS 并绑定 PgFS；专用进程静态链接固定源码，不走路径识别 |
| libsvn_repos create_svn_repos_t/get_repos/open | 本地 format、locks、db、hooks、conf | M1 在固定源码仓库层增加按 ID 构造器；完整初始化能力表、格式与配置，禁止伪造可访问目录 |
| libsvn_repos hooks.c | 每库脚本名与仓库路径参数 | M1 定义事件分派；M3/M5 验证 pre/post、撤权和应答丢失 |
| libsvn_repos authz.c | 文件或 URL 载入规则 | 使用 svn_repos_authz_parse2 的 svn_stream_t 接口，从同一版本数据库快照解析 |
| svnserve/serve.c find_repos | 磁盘逐级定位、svnserve.conf/passwd/authz | M3 改为路由及配置入口，保留命令处理与编辑器逻辑 |
| svnserve 的 libsvn_ra_svn 私有调用 | 现有 DLL 仅导出 svn_ra_svn_init | M3 从固定源码静态构建 libsvn_ra_svn，不假定私有入口可以导入 |
| mod_dav_svn repos.c | SVNPath/SVNParentPath、本地打开 | M5 独立 provider pgvn 与按 ID 入口，保留协议 namespace 和 SVN 特殊 URI |
| mod_dav_svn activity.c | dav/activities.d、虚拟事务文件 | M5 使用数据库复合键及状态机，跨工作进程逐请求路由验收 |
| mod_authz_svn | 对 DAV 原模块私有路径解析函数的依赖 | M5 配套 mod_authz_pgvn 通过公共核心解析同一仓库身份 |
| svnadmin/svnlook 命令入口 | 本地 repo 路径和格式 | M2 用 pgvnadmin 命令面，调用公共核心及上游流接口 |

现有 DLL 实测：`svn_fs_new` 可导入，仓库私有构造器未导出。即使部分函数可导入，也不能借此绕过仓库元数据初始化；路径敏感的 FS loader、libsvn_repos 与 hooks 按固定源码适配。所有改过的私有库必须静态链接或独立命名，避免向同进程原模块暴露同名不兼容实现。

构建已覆盖两个已知消费 ABI 差异：PE 导入库根据可执行节区分函数和 DATA；svn_ctype_table 运行时探测为数组时，生成消费头适配。外部 vendor/SDK 不变。Windows Apache SDK 的 os.h/APR 提供平台定义，MinGW 额外请求的 ap_config_auto.h 用明确的空消费 shim 补足。最终兼容性由模块实际加载/请求及后续协议回归证明，不能仅凭链接通过判断。

M0 不注册部分 DAV/authz provider，只导出独立模块结构，使用显式 bootstrap handler 验证加载、请求和进程退出后的卸载。默认路径不会伪装成可用的仓库服务。
