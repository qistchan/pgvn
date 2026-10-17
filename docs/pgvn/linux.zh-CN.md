# PgVN Linux 开发与部署

首轮目标为 Ubuntu 24.04 LTS、x86_64、systemd。Linux 与 Windows 使用同一套仓库核心、SQL、协议补丁、网页和版本描述。当前包仍为 `development-snapshot`。提供真实 Ubuntu 内核的 QEMU VM 验收，覆盖 systemd、开机启动、崩溃恢复、系统重启和兼容包升级/回退；容器集成测试另行覆盖协议与恢复。Windows 同版本回归按本次约定暂缓。原 M6/M7 的既有失败状态继续保留。

## 构建与依赖

产品包含 `bin/pgvnserve`、`bin/pgvnadmin`、专用 Hook supervisor、PostgreSQL 备份工具，以及匹配的 Apache 宿主和两个 PgVN 模块。固定来源为 SVN 1.14.5、APR 1.7.6、APR-util 1.6.3、libpq/客户端工具 18.6、Apache 2.4.68。源码归档和宿主锁见 `scripts/pgvn/linux-dependencies.json`、`linux-httpd-dependencies.json` 及 `src/pgvn/httpd/source-lock.json`。

Ubuntu 构建工具、OpenSSL、libevent、PCRE2 等系统依赖由构建容器提供；精确已安装包和 ELF 依赖写入每次构建清单。使用仓库的 `linux-builder.Dockerfile`、`linux-product-builder.Dockerfile`，配置组织批准的网络代理与 CA。构建离线阶段只使用已校验归档，不关闭 TLS 校验。

```bash
python3 scripts/pgvn/prepare_linux_sdk.py --output build/pgvn/linux-sdk
python3 scripts/pgvn/prepare_linux_httpd.py --sdk build/pgvn/linux-sdk --output build/pgvn/linux-httpd-sdk
python3 scripts/pgvn/prepare_linux_tools.py --sdk build/pgvn/linux-sdk --output build/pgvn/linux-tools-sdk
python3 scripts/pgvn/build.py --target linux --components all --configuration Release \
  --linux-sdk build/pgvn/linux-sdk --linux-httpd-sdk build/pgvn/linux-httpd-sdk \
  --linux-tools-sdk build/pgvn/linux-tools-sdk
python3 tests/pgvn/run_linux_product.py --product build/pgvn/linux/release-product --sdk build/pgvn/linux-sdk
python3 scripts/pgvn/package_linux.py --product build/pgvn/linux/release-product
```

命令在构建容器中运行；集成测试宿主需要访问 Docker socket。`--sanitize` 为完整自有 C 实现启用 ASan/UBSan。源码发生变化后须重新构建，不复用先前清单或验收。SDK 等第三方共享库保持独立构建，不应将自有代码的 sanitizer 覆盖解释为全部依赖已插桩。正常包要求已提交构建输入；`--allow-dirty` 只用于显式标记的开发测试包。

运行库通过相对 RPATH 加载。不要全局设置 `LD_LIBRARY_PATH` 指向 PgVN 的 lib 目录，否则系统 SVN 等客户端可能混入另一套 APR/SVN ABI。Linux 包展开后可迁移安装路径；模块必须使用包内匹配的 Apache 宿主。

## 安装和初始化

运行依赖可用 `ldd bin/pgvnserve` 和 `ldd apache/bin/httpd` 检查。Ubuntu 24.04 常见运行依赖包括 `libevent-2.1-7t64`、`libssl3t64`、`libpcre2-8-0`、`libuuid1`、`libexpat1`、`libcrypt1`、`zlib1g`、Python 3 和 systemd。完整依赖以包内构建清单为准。

先校验 tar.gz 附带的 SHA-256，再解包。安装脚本再次检查所有文件、构建身份及迁移摘要；摘要用于完整性检查，包来源信任由交付渠道保证。

```bash
sudo useradd --system --user-group --home-dir /var/lib/pgvn --shell /usr/sbin/nologin pgvn
sudo python3 PACKAGE/deploy/linux/pgvnctl.py install --package PACKAGE
sudo python3 /opt/pgvn/current/deploy/linux/pgvnctl.py init
```

安装操作可重复执行；`init` 已有配置时保持原文件。布局为 `/opt/pgvn/<version>-<digest>`、`current` 和 `previous` 链接，配置位于 `/etc/pgvn`，状态位于 `/var/lib/pgvn`，原生服务日志使用 journald，Apache 错误日志位于 `/var/log/pgvn/httpd-error.log`。`--root DIR` 可演练目录安装，不调用宿主 systemd。安装不会自动启动服务或执行数据库迁移。

将模板 `pg_service.conf.example` 复制到 `/etc/pgvn/pg_service.conf`，配置 owner、service 和 monitor 三个独立 PostgreSQL 登录角色。先由数据库管理员建立数据库及非超级用户角色，给 owner 数据库 CREATE 权限。使用 `verify-full` 和受信任 CA；密码保存在服务账号拥有的 `0600` `/etc/pgvn/pgpass`，不要放入命令行或普通日志。服务文件可用 root:pgvn、0640。`environment` 提供 `PGSERVICEFILE` 和 `PGPASSFILE` 路径。

密钥由 `init` 创建，目录 `0700`、文件 `0600`、属主为服务 UID。服务配置可为 root:pgvn、0640。Hook 根目录和代码可由 root 拥有，禁止组/其他用户写入和路径中的符号链接。密钥不放入 Git 或普通诊断包。

```bash
export PGSERVICEFILE=/etc/pgvn/pg_service.conf PGPASSFILE=/etc/pgvn/pgpass
PGVN_DSN=service=pgvn-owner /opt/pgvn/current/bin/pgvnadmin db init
PGVN_DSN=service=pgvn-owner /opt/pgvn/current/bin/pgvnadmin db grant \
  --service-role pgvn_service --monitor-role pgvn_monitor
sudo -u pgvn --preserve-env=PGSERVICEFILE,PGPASSFILE \
  /opt/pgvn/current/bin/pgvnserve --check-config --config /etc/pgvn/pgvn.ini
sudo systemctl daemon-reload
sudo systemctl enable --now pgvnserve.service
```

管理 CLI 的 `check`/`diagnose` 如需管理权限，应使用 owner 连接的单独受保护配置；运行时配置使用受限 service 角色。所有账号的角色权限均以 `pgvnadmin db grant` 和现有 SQL 契约为准。`PGVN_DSN` 仅在没有 `--config` 时使用，传入配置后由配置的 connection 字段决定连接。

## Apache 与协议

按 `httpd.conf.example` 设置 `/etc/pgvn/httpd.conf`，为 Basic 认证建立受保护的 `http-users`。HTTP 用户认证和 PgVN 路径授权分别生效；原生 SVN 的 bypass 不改变 DAV 授权。认证域须与仓库 settings 的 identity_domain 一致。

仓库 route 包含挂载前缀，例如 `/svn/repository`；两个协议分别使用 `svn://127.0.0.1:3690/svn/repository` 和 `https://host:8443/svn/repository`。HTTP 模板默认仅监听回环。配置匹配主机 SAN 的证书和私钥后，使用 `httpd-tls.conf.example`；客户端验证 CA，不跳过证书检查。

```bash
sudo -u pgvn /opt/pgvn/current/apache/bin/httpd -t -f /etc/pgvn/httpd.conf
sudo systemctl enable --now pgvnhttpd.service
sudo systemctl reload pgvnhttpd.service
```

监控默认监听 `http://127.0.0.1:18033/`。`/health/live`、`/health/ready` 区分进程存活与业务就绪；PG 暂不可用时保留监控并有界重试，不能将缓存命中当作数据库就绪。监控 token 的作用域、有效期、撤销、Origin 和网页资源与 Windows 共用。远程访问通过受控代理或隧道，保持 Host/Origin 与适配器约定一致。

## Hook、停止与缓存

版本化 Hook 使用逻辑名，例如 `{"pre-commit":{"version":1,"name":"check-policy","arguments":["中文 参数"],"timeout_seconds":10}}`，每个节点在 `[pgvn-hooks]` 映射 `check-policy = check-policy`。节点缺少必需映射或可执行文件时拒绝提供仓库。Linux 不直接执行遗留 `.exe` 声明。

不经 shell 拼接命令。每次调用由独立 Linux subreaper supervisor 执行，验证已打开的代码及路径，限制 JSON、输出保留和超时；输出超限后继续排空，调用方关闭/被终止也触发回收。supervisor 收养并清理双重 fork、setsid 的后代。Hook 仅接收明确允许的环境及连接配置引用，不继承原始密码。应保留 `/proc` 的正常进程视图；systemd cgroup 进一步约束整个服务进程树。

SIGTERM 停止接收新连接，取消 Hook，为在途协议请求提供 10 秒排空期，再取消 I/O 和数据库交换。SQL 客户端也有 deadline，失败连接关闭，禁止不明提交结果的自动重放；提交回执按现有机制消歧。systemd 在 30 秒最终边界清理 cgroup。

`cache-mode = off|local`，默认 off；Redis 当前不启用。local 模式仅缓存已发布、非事务根可达、不超过 1 MiB 的完整文件内容；每次仍向 PG 获取当前节点及内容元数据，再按仓库 ID、内容 ID、MD5/SHA1、长度和格式号定位。缓存使用独立堆内存，校验 SHA-256 和内容校验和，损坏时回源。上限由 `cache-local-mib` 指定（1–256，默认16），最多1024项，LRU 淘汰；命中数据复制到请求 pool。大文件继续有界分块传输。监控 metrics 提供 hit/miss/eviction/error/bytes。

缓存命名空间属于当前 context 实例，进程退出后全部销毁。恢复、克隆或切换数据库必须停止所有节点后重新启动，生成新的运行实例和空缓存；不保留跨进程缓存，不使用公开 SVN UUID 隔离环境。配置/认证代次、HEAD、锁、revprops、提交与权限判定始终读取 PostgreSQL。

## 备份、恢复、升级与回退

完整逻辑备份使用包内 PostgreSQL 18.6 工具，包含全数据库以及指定状态目录中的配置、密钥及 CA。先停止所有共享该 PG 的 SVN/Apache 节点，稳定密钥轮换；`--nodes-stopped` 声明此操作前提。备份目录仅属主可读写，单独加密保存和传输。

```bash
python3 /opt/pgvn/current/deploy/linux/pgvnctl.py backup --service pgvn-owner \
  --state /etc/pgvn --destination /backup/new-backup --nodes-stopped
python3 /opt/pgvn/current/deploy/linux/pgvnctl.py restore --service pgvn-restore \
  --backup /backup/new-backup --state-destination /recovery/new-state --nodes-stopped
```

恢复目标必须为新建空数据库，不执行 drop/clean。使用与备份 schema 摘要匹配的包，单事务恢复、校验迁移，单独恢复到新状态目录。检查仓库 verify、历史及内容摘要，重新 grant 目标 service/monitor 角色，将状态文件装入目标目录并修正 UID、权限与配置路径后，才重启所有节点。角色和数据库级配置由 PG 管理员预建；恢复不复制 Windows PG 数据目录。密钥字节格式兼容固定跨平台向量，真实 Windows→Linux→Windows 运行验证仍需 Windows 环境。

升级前保留匹配包、逻辑备份和配置，停止所有节点：

```bash
sudo python3 NEW_PACKAGE/deploy/linux/pgvnctl.py upgrade --package NEW_PACKAGE --nodes-stopped
# 由 owner 显式执行 db migrate，再以目标角色校验后启动。
sudo python3 /opt/pgvn/current/deploy/linux/pgvnctl.py rollback \
  --config /etc/pgvn/owner.ini --nodes-stopped
```

升级不会隐式运行迁移或启动服务。回退先用 previous 包校验当前数据库 schema/迁移摘要，失败则不切换；不可逆迁移需要新空库恢复匹配备份，不承诺原地降级。保存配置和密钥时不要覆盖仍需用于解密的旧 key。

诊断可用 `pgvnctl.py diagnose --config OWNER_CONFIG`、`journalctl -u pgvnserve -u pgvnhttpd` 及包内清单；脚本只导出原生脱敏诊断，不输出整个环境或配置文件。真实主机验收应执行启停、崩溃重启、PG 晚启动、端口占用、开机启动及重启恢复，并保存与该包摘要对应的证据。

## 可重复的 Linux 验收

完整产品集成覆盖原生 SVN、HTTP/HTTPS、SVNKit、浏览器监控、Hook、取消与排空，以及全库备份恢复。恢复后重新授权角色，验证原密钥实际认证、二进制内容、控制面数据摘要、锁令牌及新缓存实例。`run_linux_product.py --performance` 另测两个原生节点在并发 1/8/32 下关闭/启用本地缓存的三轮数据。缓存默认关闭；Redis 未采用，不能宣称已经过 Redis 性能验证。

实际 systemd 验收使用独立 Ubuntu 24.04 rootfs 和发行版内核，不依赖 KVM，也不为 VM 配置网卡：

```bash
docker build -t pgvn-linux-vm-tools:ubuntu24 -f tests/pgvn/linux-vm-tools.Dockerfile .
python3 tests/pgvn/run_linux_vm.py --package RELEASE_PACKAGE \
  --previous-package DEBUG_PACKAGE --hook-artifacts PRODUCT_TEST_ARTIFACTS/hooks
```

两包须来自同一干净提交且 schema 相同，Debug 包用于兼容包切换演练；不代表旧 schema 的逆向迁移。测试记录包摘要、镜像 ID、内核、两次 boot ID、systemd 状态、Hook 后代回收、停止时长和离线 `e2fsck`，成功后删除临时磁盘。VM 使用 TCG，仅提供功能证据。部署到目标机器仍应按实际用户、PG、证书和网络配置执行现场验收。
