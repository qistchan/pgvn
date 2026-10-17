# M6 原版网络套件覆盖账本

更新：2026-10-17，自动属性/继承/外部引用组件。[最新逐函数快照](evidence/m6-network-coverage-client-properties.json)绑定固定归档 `53bd33126d6be42bdd2725f1ee92132e03c291aa4a3fcc910eccde10dad796f4`、62 套件和 1,984 个原版函数，当前构建 `e63c40be2a119368e6060d4e4637dfc2ee8936d1a8dc45106cfa1aa0e56bb997`。这是覆盖审计，不是 M6 完成结论。[上一版 288 函数快照](evidence/m6-network-coverage.json)保持原字节和哈希，旧组件仍可核对。

## 计数口径

当前构建 568 个函数在 SVN、HTTP、HTTPS 均取得 FSFS/PgVN 一致的原版结果；两后端三协议共 3120 PASS / 174 XFAIL / 114 SKIP，实际进入 3294 次。PASS、XFAIL、SKIP 分开记录，SKIP 不计实际进入；客户端提前结束的用例不证明存储 I/O。

另有 676 个函数仅有旧构建的已接纳结果，仍需最终构建复跑；740 个尚无已接纳引用。唯一函数按源文件和原编号去重。只接纳原编号、函数名、两侧结果、原始快照及报告/日志哈希相符的记录，定点重复和失败尝试不累加。本轮 13 对 XFAIL 诊断转储仅条目顺序不同，专项字典复核及最初严格文本比较的 FAIL 记录均见[组件说明](M6-network-client-properties.zh-CN.md#xfail-诊断复核)，不声称全部诊断逐字相同。

## 逐套件概览

“本构建结果”依次为 SVN/HTTP/HTTPS 函数数，包含原有 SKIP；其余两列按唯一函数计数。具体结果、装饰器和 run ID 见 JSON。

| 套件 | 静态函数 | 本构建结果 SVN/HTTP/HTTPS | 仅旧构建 | 无已接纳引用 |
| --- | --- | --- | --- | --- |
| authz | 35 | 0/0/0 | 35 | 0 |
| autoprop | 30 | 30/30/30 | 0 | 0 |
| basic | 71 | 0/0/0 | 71 | 0 |
| blame | 19 | 0/0/0 | 19 | 0 |
| cat | 9 | 0/0/0 | 9 | 0 |
| changelist | 18 | 18/18/18 | 0 | 0 |
| checkout | 14 | 0/0/0 | 13 | 1 |
| commit | 74 | 0/0/0 | 74 | 0 |
| copy | 118 | 0/0/0 | 118 | 0 |
| dav | 3 | 0/0/0 | 0 | 3 |
| depth | 50 | 50/50/50 | 0 | 0 |
| diff | 96 | 0/0/0 | 96 | 0 |
| entries | 3 | 3/3/3 | 0 | 0 |
| export | 31 | 0/0/0 | 31 | 0 |
| externals | 71 | 71/71/71 | 0 | 0 |
| getopt | 9 | 9/9/9 | 0 | 0 |
| history | 2 | 0/0/0 | 2 | 0 |
| import | 7 | 0/0/0 | 7 | 0 |
| info | 21 | 0/0/0 | 21 | 0 |
| input_validation | 25 | 25/25/25 | 0 | 0 |
| iprop_authz | 1 | 1/1/1 | 0 | 0 |
| iprop | 9 | 9/9/9 | 0 | 0 |
| lock | 63 | 0/0/0 | 2 | 61 |
| log | 46 | 0/0/0 | 46 | 0 |
| merge_authz | 4 | 0/0/0 | 0 | 4 |
| merge_automatic | 25 | 0/0/0 | 0 | 25 |
| merge_reintegrate | 22 | 0/0/0 | 0 | 22 |
| merge | 143 | 0/0/0 | 0 | 143 |
| merge_tree_conflict | 29 | 0/0/0 | 0 | 29 |
| mergeinfo | 13 | 13/13/13 | 0 | 0 |
| mod_authz_svn | 11 | 0/0/0 | 0 | 11 |
| mod_dav_svn | 8 | 0/0/0 | 0 | 8 |
| move | 15 | 15/15/15 | 0 | 0 |
| patch | 82 | 0/0/0 | 0 | 82 |
| pegrev_parse | 84 | 84/84/84 | 0 | 0 |
| prop | 46 | 0/0/0 | 46 | 0 |
| redirect | 7 | 0/0/0 | 0 | 7 |
| relocate | 7 | 7/7/7 | 0 | 0 |
| resolve | 8 | 8/8/8 | 0 | 0 |
| revert | 36 | 36/36/36 | 0 | 0 |
| schedule | 16 | 16/16/16 | 0 | 0 |
| shelf2 | 31 | 0/0/0 | 0 | 31 |
| shelf | 32 | 0/0/0 | 0 | 32 |
| special | 27 | 27/27/27 | 0 | 0 |
| stat | 45 | 45/45/45 | 0 | 0 |
| svnadmin | 74 | 0/0/0 | 0 | 74 |
| svnauthz | 14 | 0/0/0 | 0 | 14 |
| svndumpfilter | 10 | 0/0/0 | 0 | 10 |
| svnfsfs | 3 | 0/0/0 | 0 | 3 |
| svnlook | 13 | 0/0/0 | 0 | 13 |
| svnmover | 28 | 0/0/0 | 0 | 28 |
| svnmucc | 9 | 0/0/0 | 0 | 9 |
| svnrdump | 57 | 0/0/0 | 0 | 57 |
| svnsync_authz | 7 | 0/0/0 | 0 | 7 |
| svnsync | 31 | 0/0/0 | 0 | 31 |
| svnversion | 7 | 7/7/7 | 0 | 0 |
| switch | 39 | 39/39/39 | 0 | 0 |
| trans | 11 | 11/11/11 | 0 | 0 |
| tree_conflict | 26 | 26/26/26 | 0 | 0 |
| update | 86 | 0/0/0 | 86 | 0 |
| upgrade | 35 | 0/0/0 | 0 | 35 |
| wc | 18 | 18/18/18 | 0 | 0 |

## 历史与适用性边界

只读取组件明确的 final/runs 分支及本轮已完成队列。旧字段仅依据原始 URL、既有组件记录和实际别名核对补证，不重写旧报告。此前 3 个中断尝试保留且不计通过；checkout14 仍保留 stock PASS/native FAIL 的本地文件提供器边界，不转成 SKIP/PASS，也不套用到其他函数。

没有当前结果不等于不适用。其余物理格式/架构边界仍需逐项源码和实测审查，补对应 PgVN 语义检查；本账本没有生成新排除。原生 C、CLI、真实历史、备份、故障和性能组件分别验收。见[本轮组件](M6-network-client-properties.zh-CN.md)、[上一轮组件](M6-network-wc-state.zh-CN.md)和[总体进展](progress.zh-CN.md)。PGVN-601、M6、M7 保持未完成。
