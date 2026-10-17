# Linux 开发交付与验证（2026-10-17）

Ubuntu 24.04 LTS / x86_64 的 Linux 原生开发快照已交付。验证源码提交为 `011bea1c5249ad49e2a5eac3f0311b21055d7fa3`，产品版本 `0.1.0-dev`、catalog 8 / FS 4。完整 Release、Debug 和 ASan/UBSan 产品来自同一干净提交；此后的文档提交不改变包内 Git 身份。结构化清单见 [Linux 产品证据](evidence/linux-product-delivery.json)，操作方式见 [Linux 部署指南](linux.zh-CN.md)。

## 已交付及验证

| 计划项 | Linux 结果 |
| --- | --- |
| PX-L01–03 | 公共平台接口、GCC 顶层完整构建、固定 APR/APU/SVN/libpq/httpd 来源、ELF 与相对 RPATH、实际版本/ABI 和安装包身份检查 |
| PX-L04 | OpenSSL 凭据、保护密钥、固定向量、权限/轮换组件；恢复后用原密钥实际 SVN 认证 |
| PX-L05 | 版本化声明/节点映射、独立 supervisor、无 shell、受控环境与 FD、有界输入输出、超时取消及逃逸后代回收 |
| PX-L06 | 共享监控 API/网页、令牌撤销/Origin、请求限制、64 慢请求/排队并发/中断恢复及实际 Chromium 桌面/移动流程 |
| PX-L07–08 | 幂等安装/初始化、配置/诊断、systemd 非 root 运行、PG 晚启动、SIGKILL 后恢复、真实内核重启、自启动和停止排空 |
| PX-L09 | 匹配的 Apache 宿主/模块、随包配置、实际 HTTP/受信 HTTPS、SVNKit、双节点提交读取、graceful reload |
| PX-L10 | PG18.6 全库逻辑备份/恢复、密钥字节与实际认证、控制面/凭据/锁/回执/DAV 表摘要、历史 verify、二进制内容、新缓存实例、空目标保护 |

Release 集成 53 项、完整 sanitizer 集成 52 项通过；两轮均包含 58 项原生 Hook 断言与独立 JSON/参数/环境/FD 检查、缓存所有权/损坏/淘汰/并发验证、9 项实际浏览器流程。缓存探针的 8222 项断言中 8192 项是逐字节比较，不作为独立功能数。SDK 第三方共享库未插桩；Apache 未插桩宿主的全局池关闭 leak 检测，原生可执行文件保持默认 leak 检测。

真实 VM 使用发行版 Ubuntu rootfs、`Linux localhost 6.8.0-147-generic #147-Ubuntu SMP PREEMPT_DYNAMIC Thu Sep 24 11:33:16 UTC 2026 x86_64 x86_64 x86_64 GNU/Linux`，QEMU TCG、无网卡，systemd 确认运行于 VM 而非容器。34 项通过，包括不同 boot ID 的两次启动、同源码 Debug→Release→Debug→Release 兼容包升级/回退、密钥/HEAD/原生与 DAV 内容保留、Hook 后代回收及重启后自启动。停止两服务耗时 0.537 秒，离线 `e2fsck -f -n` 返回 0。此演练不包含 schema 降级。

VM 预检发现并修复：私有 umask 下包目录不可遍历、Apache 模板错误加载已静态内嵌的 event MPM、journald socket 不能通过 `/proc/self/fd/2` 重新打开、Hook 测试 PID 文件过早可见。最终包按修复后的模板在干净 VM 重新通过。失败预检保留原状态与诊断，未改写为成功。

## 性能与缓存决策

直接 FS API 的独立三轮实验来自较早干净提交 `4f822ad795de8b9ed17fc370f6b892dd375f577c`，保留原身份：8 MiB 内容列字节由 16,777,278 降至 8,388,639，查询仍为 15；256 项目录查询由 260 降至 4。并发 1/8/32 的每轮 P95 中位数，hex→binary 为 203.607→52.936、543.338→134.051、2355.039→538.025 ms；per-node→batch 为 28.659→2.785、186.717→10.502、798.604→88.448 ms。这里的字节是 bytea 列数据，排除 TCP；这些数字不代表协议或应用整体加速。

最终 Release 对两个原生节点执行 512 KiB 同内容读取，off/local 各三轮，顺序交替，两组 PG 对象均预热。336 次计时读取逐次校验 SHA-256，零错误；完整组数据含 P50/P95/P99、吞吐、客户端/服务 CPU、SQL 与缓存计数。各轮 P95 中位数如下：

| 并发 | off P95 (ms) | local P95 (ms) |
| --- | --- | --- |
| 1 | 28.133 | 32.081 |
| 8 | 63.703 | 82.888 |
| 32 | 252.692 | 266.511 |

该负载的 SQL/次仅从 51 降至 50，local 的延迟没有稳定收益，**默认保持 off**；保留有界、可关闭的 local 供具体部署验证。Redis 未采用，未实现或执行 Redis PoC，不声称有其加速/故障测试数据。这是共享云主机、本地 PG、暖对象、每次新建 SVN CLI 会话的小样本实验，未测远端 PG、HTTP/Polarion 性能或 PG CPU/I/O 归因，不能据此完成 P1 的双平台/代表性负载准入。

## 交付及剩余边界

Release 包为 `PgVN-0.1.0-dev-linux-x64-73d256a5ffc8b3b3.tar.gz`，SHA-256 `bb1d21fcd1ee3665587f0c7f863f917993764b0a0cc21528f86b4e589a004f2c`。原始报告、命令、浏览器图片和包保存在本次工作区 `artifacts/pgvn/tests/linux-product-331e45bdbb33`、`artifacts/pgvn/tests/linux-product-fc891e90290b`、`artifacts/pgvn/tests/linux-vm-36f1ee6a9caf`；Git 保存源码、复现入口与摘要清单，不包含测试密钥或数据库。

按用户要求暂缓 Windows W1/W2，因此 PX-L04/PX-L10 中真实 Windows→Linux→Windows 互导、混合 OS 节点及 Windows 同提交编译/GUI/服务回归仍待补验。Linux 移植开发及本平台验收不替代原 M6/M7、PGVN-601/602、三协议严格差分与 shelf 已知 FAIL；真实 Polarion 业务与跨主机 HA/代表性性能仍按原计划推进。包继续标识 development-snapshot。
