# M6 原生上游 FS 与仓库入口

本组件完成固定 Subversion 1.14.5 C 测试的无目录适配与对照，不能据此宣布 M6 完成。实际原始报告与构建身份见 [证据摘要](evidence/m6-native.json)。上游网络协议完整入口、故障账本、性能和应用演练继续按总计划推进。

## 运行

在已配置固定 SDK 的管理员开发终端执行：

```powershell
python scripts/pgvn/build.py --config config/build.local.json --configuration Release
python scripts/pgvn/build_native_tests.py
python tests/pgvn/test_m6_native.py --applicable
```

测试入口先核对专用 `127.0.0.1:16432` 的实际数据目录，创建独立数据库及非超级用户所有者。PgVN 程序以受限的管理角色进行 FS/仓库语义测试；它不是网络身份授权验收的替代。测试数据库使用唯一 `pgvn_m6_native_` 前缀，C 夹具也检查数据库名与运行根目录守卫。

不带 `--applicable` 时运行全部原始用例，目录模式等边界会保留为 FAIL；不会替换断言或将 FAIL 改写成 SKIP。可用 `--suite fs|repos --backend pg|fsfs|directory-fsfs --case N` 定位单项；自定义选择和完整适用矩阵不能混用。`both` 为默认，覆盖 PgVN、原 SDK FSFS，以及链接本轮修复核心的 FSFS 目录对象仓库组。

## 适配与身份

上游 `fs-test.c`、`repos-test.c` 逐字节保持不变，源摘要分别为 `e97c73aea6626d3d4c18eac373eaf5389fab04198b5edb8626c9c67915410c6f`、`40e945d3228d791cfdebd3977dd1b8914d21f51f84f008b9ba32ec30ba76e292`。测试代码以优化方式编译但保留 C 断言，链接同一 Release 公共核心。

`svn_test_fs.c` 的生成副本仅重命名四个创建函数的定义，原始文件不修改；原辅助函数中的创建调用仍绑定到新的 PgVN 夹具。仅靠预处理宏同时重命名定义和引用会让 `create_blame_repository` 绕回旧路径创建，首轮发现并修正，失败记录保留。原生夹具将测试别名映射到数据库仓库，重开调用按已知别名或 `pgvn:` ID 打开；不创建本地壳目录，也不模拟磁盘配置或 hook 生效。

创建/重开账本记录 ID、别名及实际代码页。运行器检查所有别名都未成为本地目录，并扫描遗漏的仓库壳标记。构建与运行前后核对公共核心归档、产品、测试程序、上游源、夹具及私有 DLL。原版 FSFS 对照链接原 SDK 库；目录模式对照使用同一修复核心及原创建夹具，分开报告。

## 结果与边界

| 对象 | PASS | 上游 SKIP | 架构排除 |
| --- | ---: | ---: | ---: |
| PgVN FS | 62 | 3 | 5 |
| PgVN 仓库 | 25 | 1 | 6 |
| 原版 FSFS FS + 仓库 | 101 | 1 | 0 |
| 同核心 FSFS 目录模式仓库 | 31 | 1 | 0 |

PgVN 的四个上游跳过为 FS 44（FSFS 升级）、51（模块列举）、57（FSFS 配置）和仓库 31（可选 authz 性能）。11 个架构排除在 `native_applicability.py` 中按函数名、编号及固定源核对：

- FS 38：先断言本地 FS 目录存在，再用传统路径删除；PgVN 清理由管理 API 验证。
- FS 40：将所有非 FSFS/FSX 后端当成 BDB，断言旧格式 3/SVN 1.5；独立探针检查真实 PgVN 存储格式 1。
- FS 65、67：直接锁定/改写 SQLite rep-cache 或 FSFS 表示文件；PostgreSQL 不使用这些物理结构。
- FS 70：最后一段要求 `NULL` FS 通过全局 loader 发现 pg；PgVN 直接按 ID 绑定。独立探针检查对象 ioctl 和无全局模块时的真实错误。
- 仓库 8、9、22：通过本地 `file://` 目录发现配置、authz/groups 或 config pool；后续网络测试仍需覆盖对应场景。
- 仓库 19：断言本地仓库目录存在后删除；PgVN 使用排空及 purge。
- 仓库 21：要求创建 pre-1.4 物理仓库格式；独立探针检查现代仓库格式 5 与 mergeinfo 能力。
- 仓库 24：通过路径 getter 写磁盘 pre-commit hook；PgVN 使用版本化数据库 hook。独立探针保留旧锁令牌访问上下文及实际提交检查，不把原用例记为通过。

首轮未筛选运行保留 12 FAIL，包含一个夹具问题与旧 hook 路径 getter 崩溃。修复后文件修订历史原用例恢复通过；旧磁盘 hook 原用例返回路径错误，不再崩溃。生产补丁使 16 个配置、锁和 hook 路径 getter 对按 ID 对象返回 NULL，调用者须使用 PgVN 的配置和 hook 接口；普通目录对象保持原行为。

独立探针逐项调用 16 个 getter，检查逻辑身份、格式、能力和 ioctl，再用旧锁令牌 API 完成 r1 提交。测试私有 DLL 的可逆篡改在建库/启动前被拒绝，随后逐字节恢复；原 SDK 不改写。服务端功能回归的实际范围与结果单独列在证据中；浏览器复验、完整 M6 和部署包均不能从这些原生结果推导。
