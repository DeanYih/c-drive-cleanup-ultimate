# C盘清理瘦身Skill

<img src="assets/icon.png" width="96" height="96" alt="icon">

> Windows C 盘空间治理：先量准，再动手。默认只读，删除必须你点头。

一个给 Agent 用的 C 盘清理技能包。它不猜、不估算、不为了报告好看去凑数——
每个数字都来自本机实测，量不到的宁可不写。

当前版本：**v1.3.0**

---

## 它解决什么问题

「C 盘红了」这句话背后有五种完全不同的情况：缓存堆出来的、几个大文件占的、
应用数据长在 C 盘的、系统级文件（休眠/分页/还原点/搜索索引）吃掉的、
以及**根本不是 C 盘的问题**（云盘占位符把 143 GB 的逻辑体积挂在你眼皮底下，
本地其实只占 536 字节）。

不做区分就动手，结果是清完 2 GB 发现没变化，或者更糟——删掉了不该删的东西。
这个技能先做的是**把这五种情况分开**。

## 能力

| 能力 | 说明 |
|---|---|
| 基线测量 | 磁盘总量/可用/增长率，逐盘对齐，不靠估算 |
| 只读扫描 | 白名单目录 + 应用缓存大户 + 大文件三档（1GB/500MB/100MB）+ 系统级目标 |
| 链接盲区报告 | junction / symlink / 云盘占位符四类分列，逻辑体积与本地实际占用分开算 |
| 风险分级 | RED / AMBER / GREEN / UNKNOWN，每条写明「删了会怎样」 |
| HTML 体检报告 | 单文件、零脚本、可带走、用户名自动打码为 `%USERPROFILE%` |
| 白名单护栏删除 | 默认进回收站（Shell API + `FOF_ALLOWUNDO`），每批 ≤10 项 |
| 跨盘迁移 | 源目录改名留底 + junction + `--rollback` 秒级还原 + `--audit` 台账巡检 |
| 微信专项 | 3.x / 4.x 布局自动识别，主库与附件分开对待，md5 去重出计划不直接删 |
| 回收站恢复 | `$I` 元数据解析，按原路径还原（回收站已清空则无能为力） |
| 定时巡检 | 生成 Task Scheduler XML，**只生成不注册**，默认只读扫描 |

## 快速上手

```bash
# 1) 一次只读扫描，出一份可带走的 HTML 报告（不删任何东西）
python -S scripts/build_report.py --out "%USERPROFILE%\Desktop\C盘体检报告.html"

# 2) 把报告给用户看，让他选 —— 不要替他决定
# 3) 只清「可以放心清理」那一档
python -S scripts/delete_safe.py --targets targets.json            # dry-run
python -S scripts/delete_safe.py --targets targets.json --confirm  # 确认后
```

迁移要长期解决时：

```bash
python -S scripts/migrate_with_junction.py --src "C:\path\to\cache" --dst "D:\cache" --check   # 预检
python -S scripts/migrate_with_junction.py --src "C:\path\to\cache" --dst "D:\cache"           # 执行
python -S scripts/migrate_with_junction.py --audit                                             # 台账巡检
python -S scripts/migrate_with_junction.py --rollback "C:\path\to\cache"                       # 回滚
```

## 安全边界

- **默认 dry-run。** 任何删除都要显式确认，且每批 ≤10 个目标。
- **默认走回收站。** 只有「纯缓存且用户明确同意彻底删」才用 `python -S` 真删。
- **结构性红线**：盘根、用户根、`C:\Windows`（除少数白名单子目录）、`Program Files`、
  `ProgramData`、个人目录、微信聊天主库——命中即拒，不走可配置规则。
- **绝不联网。** `scripts/` 下没有任何 `requests` / `urllib` / `socket` 调用，
  扫描产物只写本地。
- **不承诺释放多少 GB。** 清理不影响运行速度，卡顿请查启动项 / 内存 / 磁盘健康。

## 目录结构

```
SKILL.md                    技能主文件（安全红线、SOP、脚本清单）
assets/icon.png             图标
references/
  anti_patterns.md          清理类工具十种常见坑 + 本技能的反向做法
  app_cache_relocation.md   应用缓存重定向对照表
  migration.md              迁移手册（junction / WSL / Docker / 位置选项卡）
  pitfalls.md               41 条实战踩坑（含真机验收专章）
  risk_rules.json           风险登记规则库（RED/AMBER/GREEN，可被 --rules 覆盖）
  targets_catalog.md        30+ 类垃圾目录完整清单
  wechat_module.md          微信 4.x 专项详解
scripts/                    22 个脚本，全部零网络、只读优先
```

## 验证

两套测试，缺一不可：

| 测试 | 规模 | 证明什么 |
|---|---|---|
| `scripts/selftest.py` | 161 项合成断言 | 逻辑正确（构造目录树、边界条件、事故回归） |
| `scripts/field_test.py` | 29 项真机验收 | 真机有效（真实路径、真实回收站、真实计划任务） |

```bash
python -S scripts/selftest.py
python -S scripts/field_test.py                            # 26 项只读
python -S scripts/field_test.py --with-migrate --with-schtasks --with-clean-task   # 29 项含写操作
```

真机验收里包含：真实创建 junction 再回滚、真实注册计划任务并运行、删除→回收站→
恢复→sha256 比对闭环。跑了才发现的问题（路径拼接、目录 vs 文件、云盘占位符、
`isdir` 大小写不敏感）全在这层暴露——只靠合成用例是看不见的。

## 兼容性

- Windows 10 22H2 实机验证；Windows 11 未完整回归。
- Python 3.9+，纯标准库，无第三方依赖。
- 沙箱环境（删除被安全钩子拦截）有专门通道选型说明，见 `SKILL.md` 第 1 节。

## 许可

MIT。用得上的拿走，出了问题请先看 `references/pitfalls.md`。
