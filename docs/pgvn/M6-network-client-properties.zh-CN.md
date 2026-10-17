# M6 自动属性、继承与外部引用

日期：2026-10-17。固定上游 10 个完整套件、280 个原版函数在 SVN、HTTP、HTTPS 夹具下完成 FSFS/PgVN 对照。原始报告摘要见[结构化证据](evidence/m6-network-client-properties.json)。本组件不宣告 PGVN-601、M6 或 M7 完成。

## 原版结果

| 套件 / 范围 | SVN | HTTP | HTTPS |
| --- | --- | --- | --- |
| autoprop 1–30 | 30 PASS (`cd32f84c3d5d`) | 30 PASS (`00311b6d0a3f`) | 30 PASS (`34b6ee34815e`) |
| getopt 1–9 | 9 PASS (`a815074e21a0`) | 9 PASS (`280ea2bc74ff`) | 9 PASS (`d0f92eb6c4ad`) |
| input_validation 1–25 | 25 PASS (`565a02f77584`) | 25 PASS (`533b8d039746`) | 25 PASS (`35ea50b28cae`) |
| pegrev_parse 1–84 | 68 PASS / 16 XFAIL (`a56a6fa7be7a`) | 68 PASS / 16 XFAIL (`17375029382f`) | 68 PASS / 16 XFAIL (`7b0a83227cef`) |
| trans 1–11 | 9 PASS / 2 XFAIL (`09a9b799c731`) | 9 PASS / 2 XFAIL (`9a64fcdb6d12`) | 9 PASS / 2 XFAIL (`378fd80900aa`) |
| special 1–27 | 19 PASS / 1 XFAIL / 7 SKIP (`4bb1ff0fbdef`) | 19 PASS / 1 XFAIL / 7 SKIP (`8e672113bef0`) | 19 PASS / 1 XFAIL / 7 SKIP (`a09a62e782eb`) |
| mergeinfo 1–13 | 13 PASS (`144590fd8900`) | 13 PASS (`a86d3c4423fa`) | 13 PASS (`ee632902f30d`) |
| iprop 1–9 | 9 PASS (`1638a394e4b5`) | 9 PASS (`19829c750147`) | 9 PASS (`5438f345a32d`) |
| iprop_authz 1–1 | 1 PASS (`11c96c8443d9`) | 1 PASS (`f4c74e09f9f7`) | 1 PASS (`8bb79a385a9c`) |
| externals 1–71 | 69 PASS / 2 XFAIL (`aa81c6c63762`) | 69 PASS / 2 XFAIL (`33d8822340b1`) | 69 PASS / 2 XFAIL (`076a5095f6ba`) |

每格展示一个后端的计数，两侧原编号、函数名和结果一致。两后端三协议共 **1,680 个结果：1512 PASS / 126 XFAIL / 42 SKIP**；实际进入 **1638 次**，含原有 XFAIL、不含 SKIP。原函数、装饰器、test_list、客户端输出及预期断言均未改写，没有新增排除。

## XFAIL 诊断复核

63 对原有 XFAIL 中，50 对完整诊断仅归一各自工作目录和精确监听地址后文本相同；另 13 对在 pegrev_parse 的 `ACTUAL STATUS TREE` 字典转储中出现条目顺序差异。最初严格文本比较结果为 FAIL，原比较器、失败记录和原始日志均保留，不改写为逐字相同。

| 协议 | 原运行 ID | 仅字典条目顺序不同的原编号 |
| --- | --- | --- |
| SVN | `m6-network-a56a6fa7be7a` | 81–84 |
| HTTP | `m6-network-17375029382f` | 80–84 |
| HTTPS | `m6-network-7b0a83227cef` | 80–83 |

固定上游 `State.__eq__` 比较归一后的路径字典，`dump_tree_script__crawler` 按子节点遍历顺序打印。专项复核只对上述 13 对诊断中唯一的状态树字典按路径核对：路径唯一、全部原始 Item 行（含状态、修订和移动信息）相同，块外错误、异常、堆栈及 XFAIL 标记必须逐字相同；没有排序任意命令输出，也没有改变原断言。全部 30 次运行两侧共 180 项源码文件核对确认所审阅打印/比较逻辑与执行快照一致。78 个反例验证状态变更、路径丢失、重复键、块外差异或其他转储标题均被拒绝。规则、逐对哈希和源码摘录见[结构化证据](evidence/m6-network-client-properties.json)的 `xfail_diagnostic_review`。

协议列指运行夹具。getopt、部分输入校验和 peg revision 用例可在客户端结束；这些原版结果不证明对应请求访问了 PostgreSQL，不能替代服务端、存储、故障或性能验收。原有 POSIX/Windows 条件和 Wimp/XFAIL 保留，SKIP 不计实际进入。

## 夹具与验证范围

使用固定原版 golden 文件和真实工作副本；保留裸 `@` 文件名、工作目录、换行转换与特殊文件表示。继承属性及 externals 通过真实提交、历史 URL、跨仓库路由和已有受限复制/策略发布入口执行。没有生成原生仓库别名对应的本地 repo、conf 或 activities 目录，也没有模拟客户端输出。

HTTPS 对两后端均验证证书链与主机名，并拒绝未知 CA 和错误主机名；信任只写入私有客户端配置。140 个内层、30 个外层 Job 关闭前活动进程为零；30 个隔离库所有者均为非超级用户，监控令牌行数及残留客户端连接为零。库与角色保留；只读 CIM 确认 runtime 进程和临时测试服务为零。

本轮未修改生产代码或测试适配器，154 项注册输入、5 个生产产物及运行依赖仍匹配 `e63c40be2a119368e6060d4e4637dfc2ee8936d1a8dc45106cfa1aa0e56bb997`；构建身份仍为原始 `c638127` dirty 构建。明确复用同构建浏览器 15+6 项和完整 M5 `m5-0587e0a9e03b` 十组的原始验收，未声称新运行。详见[上一组件的同构建说明](M6-network-wc-state.zh-CN.md#同一冻结构建的既有验收)。

既有开发包 `fa6fd390be872efa` 的运行时与本轮完全相同，其 95 个 x64 镜像、完整性和 PowerShell 5.1 精简 PATH 启动已验收，见[原包记录](evidence/m7-package-wc-state.json)。本轮未重建运行包，包内文档早于本组件；没有新增干净机器、重启或 Polarion 验收结论。

## 覆盖与剩余范围

新[逐函数快照](evidence/m6-network-coverage-client-properties.json)核对 62 套件、1,984 个函数和 118 份已接纳运行。当前构建三协议结果从 288 增至 **568 个函数、23 个完整套件**；另有 **676** 个函数仅有旧构建结果，**740** 个尚无已接纳引用。原有 SKIP、XFAIL 和客户端局部验证范围分别保留。

[汇总页面](M6-network-coverage.zh-CN.md)滚动更新，但[上一版 JSON](evidence/m6-network-coverage.json)及其哈希保持不变。此前 3 份中断尝试仍保留且不计通过；checkout14 的 file:///SUBST 原生边界仍单列，不转成 SKIP/PASS。

继续剩余完整套件/适用性、旧构建结果复跑、维护语义、638 修订真实历史和 30 表备份恢复复核、提交确认与跨故障域主备/fencing、代表性持续性能及监控预算。Windows 高对比度、独立机器安装重启与隔离 Polarion 应用验收仍待完成。功能套件耗时不作为性能基准。

## 可复现入口

仅用于获批的 `127.0.0.1:16432` 专用集群和本仓库 runtime。每次创建新计划，已经启动的计划不重复执行。

```powershell
$run = python tests/pgvn/test_m6_network.py --plan --protocol svn --suite autoprop_tests --cases 1:30 | ConvertFrom-Json
python tests/pgvn/test_m6_network.py --execute-plan $run.plan
```

原始输入、报告和日志冻结于 `runtime/pgvn/baselines/m6-client-properties-full-076a5095f6ba`，索引 `e1d818c89f8aa3f36d13ce86438a2a0db50212cecbb734cefff242a43764eb10`，共 863 个文件。
