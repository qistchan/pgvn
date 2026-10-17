# PgVN Linux 部署

默认目标是 Ubuntu 24.04 LTS x86_64。本目录提供初始化、配置检查、systemd 单元、监控启动、逻辑备份和 Apache 示例。它不表示干净主机上的开机自启、崩溃拉起或 Apache 模块装载已经验收。

## 路径

| 用途 | 默认路径 |
| --- | --- |
| 程序 | `/opt/pgvn/<version>` |
| 配置 | `/etc/pgvn` |
| 密钥与运行状态 | `/var/lib/pgvn` |
| 日志 | `/var/log/pgvn` |

仓库历史仍在 PostgreSQL。`/var/lib/pgvn/cache-epoch` 在恢复后必须换成新值，服务启动前要能读到它。

## 命令

```bash
sudo deploy/linux/pgvn-init.sh
sudo deploy/linux/pgvn-check.sh
sudo deploy/linux/pgvn-service.sh install
```

`pgvn.service` 以 `User=pgvn` 运行，`SIGTERM` 后由 `TimeoutStopSec=20` 限时结束。监控进程收到该信号后停止接受连接，并在 `drain_ms` 内等已开始的请求结束。

监控只监听 `127.0.0.1`。令牌文件每行是 `sha256` 十六进制和剩余秒数，算法与 Windows 监控相同：对 `pgvn_` 加 64 位十六进制秘密做 SHA-256。没有数据库采样时，`/api/monitor/v1/` 和 `/metrics` 在认证通过后返回 `database_unavailable`。

## 尚未在本机完成的部分

`pgvnserve`、两个 Apache 模块和完整监控采样仍依赖带 Windows 头文件的共享核心，以及固定的 Subversion 1.14.5 SDK。`pgvn-apache-check.sh` 在模块不存在时只检查示例，不会把未装载的配置报成通过。`PGVN_STRICT_SDK=1` 时，Linux 构建会因为缺少 1.14.5 头文件失败。
