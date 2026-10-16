# M6 原版授权套件：三协议本机验证

日期：2026-10-16。原版 `authz_tests` 1–35 在 SVN、HTTP、HTTPS 与原版 FSFS 对照一致。PGVN-601、M6 和 M7 仍未完成；完整记录见[授权组件证据](evidence/m6-network-authz.json)。

## 可复现入口

```powershell
python scripts/pgvn/build.py --config config/build.local.json --configuration Release
python scripts/pgvn/build_upstream_network.py
python scripts/pgvn/build_upstream_helpers.py
python scripts/pgvn/build_upstream_http.py
python tests/pgvn/test_m6_network_policy.py
python tests/pgvn/test_m6_network.py --plan --protocol svn --suite authz_tests --cases 1:35
python tests/pgvn/test_m6_network.py --execute-plan <新计划路径>
# HTTP、HTTPS 分别生成新计划，将 --protocol 改为 http、https，再执行对应计划。
```

仅在专用 `127.0.0.1:16432` 集群和仓库 runtime 中执行，每轮新建 `pgvn_m6_net_<12位十六进制>` 库及非超级用户所有者。计划只能执行一次；完整范围与输入锁见[网络入口指南](M6-upstream-network.zh-CN.md)。

## 结果与适用性

| 冻结运行 | 传输 | PgVN 与原版 FSFS 各侧结果 |
| --- | --- | --- |
| `m6-network-1417d1baf195` | SVN | 31 PASS、3 原有 XFAIL、1 原有 SKIP |
| `m6-network-f878f09e4f61` | HTTP | 31 PASS、2 原有 XFAIL、2 原有 SKIP |
| `m6-network-e3399f07ba00` | HTTPS | 31 PASS、2 原有 XFAIL、2 原有 SKIP |

每侧均保留 35 个结果。SVN 的 XFAIL 为 13、14、25，SKIP 为 28；HTTP/HTTPS 的 XFAIL 为 14、25，SKIP 为 13、26。原函数、断言、装饰器及选择列表未改，没有新增排除。三协议实际进入 200 次原用例（含两侧与原有 XFAIL，不含 SKIP）；修复点 11/24/28 的额外 HTTP 确认不重复计入覆盖。HTTPS 继续验证私有 CA、主机名及拒绝未知 CA/错误主机名。

## 明确的夹具边界

- 将每库 `svnserve.conf`、authz 和 groups 文件放在全局测试目录，通过版本化配置发布生效；svnserve 访问档位仅作用于 SVN 传输。
- 仅将原夹具的精确仓库名映射为原生 catalog 名，大小写错误的限定规则、全局规则及注释保持原样。
- 管理工具先校验策略；仅在明确的 authz 解析拒绝后，由该隔离库的非超级用户所有者写入无效文件状态，让真实服务解析并报错。写入前核对库名前缀、计划、真实集群目录、所有者、冻结构建、仓库 ID 与代数；有效策略仍走生产管理 API。
- 原用例 24 的旧 FSFS 归档在全局输入目录解包，用原版 `svnadmin dump` 与原生 `svnadmin load --force-uuid` 导入，复核 UUID/HEAD；原来的工作副本升级、重定位及更新断言照常执行。
- 原用例 28 使用未改动的上游 `mod_dontdothat.c`，分别构建 FSFS 与 PgVN 私有测试模块；原生版本仅采用已有 DAV 符号命名空间。FSFS 配置额外 parent 挂载，原生以受保护的第二条 catalog 路由映射同一仓库 ID。模块不进入部署包。

所有声明过的原生仓库别名均检查不存在，涵盖未经过普通 build 的旧历史用例。11 项离线保护检查通过，未建立真实数据库连接。最终四轮（含三项修复确认）共 16 个测试 Job，清理后各 Job 活跃进程为 0；隔离库及所有者保留。授权组件执行不创建监控令牌或 Windows 服务。

## 生产修复与证据

原版用例 11 发现生产错误分类差异：无效 authz 配置曾返回 HTTP 503，现按 `SVN_ERR_AUTHZ_INVALID_CONFIG` 类型返回 403；未找到仓库仍为 404，其他后端故障保留 503。此次仅 `mod_authz_pgvn.so` 产品变化，其他四个生产二进制及网页资源未变。早期夹具失败和原始 503 FAIL 均保留。

冻结清单为 `612a8df7a539a427294e49a6b089c5577e082a56dc04d5be2a9234a2af5329ce`；327 文件基线索引为 `d4b495f7a5057249b3dbd8b16997a353408fb62e78bf1c4fd88cd2ecc27b1b02`。浏览器与全 M5 的本轮结果以同一证据文件记录为准；网络指南中较早轮次的“待回归”描述保留为当时记录。

本组件不完成 PGVN-601、M6 或 M7。剩余套件/适用性、全部维护语义、真实历史与备份在最终构建上的复核、跨故障域与确认账本、代表性性能和独立环境验收继续推进。Windows hook 的 `cmd_alternative` 仍未由原生 Python hook 桥执行。

## 本轮完整回归

新浏览器记录 `m4-browser-authz-b92e824399c5` 的 15 项和 `m5-browser-authz-b92e824399c5` 的 6 项通过，已绑定本轮五个生产产品和网页资源。包含分页/搜索/详情、键盘和窄屏、多 DAV 实例、失联/恢复、数据库样本过期/未知及撤销令牌后清空数据与焦点恢复。令牌只在内存传递，用后撤销，夹具已停止；截图通过 CUA 实际查看，本轮不声称导出新的图片文件。

完整 M5 `m5-7e3dbdd48f38` 的 10 组通过，耗时 594.734 秒；其中 M4 `m4-0041b28dd2df` 的 8 组及其内含的 M0–M3 七组也全部通过。真实数据库故障仍返回 503，并在恢复后返回 200，与无效 authz 的 403 分类分别验证。临时测试服务及 URL ACL 按门禁清理，数据库保留供诊断。

以上是本机候选的固定构建回归，未将短时监控测量替代 M6 的代表性长窗口性能，也未宣告干净 Windows 或 Polarion 应用验收完成。
