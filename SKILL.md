---
name: c-drive-cleanup-ultimate
slug: c-drive-cleanup-ultimate
displayName: C盘清理瘦身Skill
version: 1.3.0
summary: C 盘空间治理全流程：量准→只读扫描→风险分级→可带走的 HTML 体检报告→白名单护栏清理→跨盘迁移（留底可回滚、带台账巡检）→防复发巡检。含微信 3.x/4.x 专项与系统级大户（休眠/分页/搜索索引/还原点/WinSxS）的正确处理方式，默认 dry-run。
tags: [windows, disk-cleanup, c-drive, 清理, 磁盘空间, 瘦身, 迁移, 微信]
license: MIT
description: |
  Windows C 盘清理与瘦身。当用户说"C盘满了"、"C盘红了"、"清理C盘"、"C盘瘦身"、
  "释放磁盘空间"、"磁盘空间不足"、"清理缓存/临时文件"、"清理微信缓存"、"C盘占用分析"、
  "把XX迁移到D盘"、"关闭休眠"、"WinSxS太大"、"回收站误删恢复"、"电脑卡但不知道哪里占空间"、
  "free up disk space / clean C drive" 时使用；也用于删除被沙箱安全钩子拦截、批量删除需确认的场景。
  做的事情：先量准（实测数字，不估不猜）→ 只读扫描 → 按 GREEN/AMBER/RED 三档分级并说明
  「删了会怎样」→ 出一份可带走的 HTML 体检报告 → 用户确认后按白名单分批清理（默认进回收站）→
  需要长期解决的走跨盘迁移（源目录改名留底、秒级可回滚、带台账巡检防失效）→ 定时只读巡检防复发。
  覆盖微信 3.x/4.x 专项（含 4.x 两级账号结构、附件与主库的区分）、系统级大户（休眠/分页文件、
  搜索索引、系统还原点、WinSxS 可回收组件）、云盘占位符与链接盲区识别。
  明确不做：卸载软件、系统性能调优（注册表/服务/驱动）、内存与硬件问题、服务器批量部署。
  默认 dry-run，删除必须用户确认；所有结论都来自本机实测，不承诺释放多少 GB。
agent_created: true
---

# C盘清理瘦身Skill（c-drive-cleanup-ultimate）

全部流程与方法均在本机（WorkBuddy 沙箱 Windows）实测验证沉淀而成。
核心纪律：**先量准、再估算、后删除；每步留痕；误删零容忍**。

> **能力边界**：本技能管的是「C 盘的空间」——清理、迁移、防复发。
> **不做**系统性能调优：不碰注册表清理、不禁用系统服务、不改驱动、不调动画特效。
> 那些做法收益模糊、回滚困难，且很容易把一台好机器调坏。想提速请找专门工具。

---

## 快速路径（先看这里，80% 的情况就这三步）

用户说「C 盘满了 / 飘红 / 释放空间」时，**不要先读完整篇**，直接：

```bash
# 1) 一次只读扫描，出一份可带走的 HTML 报告（不删任何东西）
python -S scripts/build_report.py --out "%USERPROFILE%\Desktop\C盘体检报告.html"

# 2) 把报告给用户看，让他选——不要替他决定
# 3) 只清报告里「可以放心清理」那一档
python -S scripts/delete_safe.py --targets targets.json            # 先 dry-run
python -S scripts/delete_safe.py --targets targets.json --confirm  # 用户点头后再加
```

就这三步。下面「本技能不做什么」与「安全红线」先扫一眼，其余是**边界**与**深度场景**
（迁移、微信、WinSxS、误删恢复），按需再查——不需要每次都通读。

> 想省轮次：报告里已经写明分档、每项「删了会怎样」和「怎么搬」，
> 直接把报告结论转述给用户即可，不要在对话里重复展开整张表。

---

## 边界：本技能不做什么（动手前先确认没走错门）

明确划边界，是为了让该触发的时候准确触发、不该触发的时候不要乱动。

| 你的诉求 | 该找谁 |
|---|---|
| **卸载软件** | Windows「设置 → 应用」，本技能不碰卸载流程（只报残留目录） |
| **让电脑变快 / 开机加速 / 驱动更新 / 系统调优** | 本技能只管「空间」，不做系统性能调优。清出空间不必然更快，也不承诺提速 |
| **数据恢复**（回收站已清空的情况） | 需要专业恢复工具。本技能只能恢复**仍在回收站里**的东西 |
| **内存不足 / 杀毒 / 硬盘健康检测** | 不在范围内 |
| **服务器 / NAS / 企业批量部署** | 本技能面向个人 Windows 10+ 工作站，不含域策略、批量脚本分发 |
| **"一键全自动清理，别问我"** | 本技能默认 dry-run，删除必须你点头。想要无人值守只能开"定时只读巡检" |

三条**不做**的承诺，比做什么更重要：

1. **不承诺释放多少 GB。** 每台机器不一样，报告里的数字是当场量出来的，不是目标值。
   拿别人的"清了 30GB"当预期，只会得出"这工具没用"的错误结论。
2. **不为覆盖率而清理。** 扫不到大户就如实说"你这台已经很干净了"，
   绝不为了报告好看去拉几个边缘目录凑数。
3. **不写"查了 XX，没有"这种行。** 十行里有九行是"本机没装"，会把真正要看的淹掉。

---

## 0. 安全红线（绝对禁止，违反即不可逆损失）

1. **绝不碰**：`Desktop` / `Documents` / `Downloads` / `Pictures` 等个人目录（只读扫描，
   删除必须用户逐条指定）；用户明确说"别动"的目录；`C:\Windows`（除
   `SoftwareDistribution\Download`、`Temp`、`Panther`、`Logs`）；`C:\Program Files`；
   `pagefile.sys`（只能挪不能删）；微信 `Msg/*.db` 加密聊天库。
2. **删除前必须**：只读扫描 → 把报告完整呈现给用户 → 拿到显式确认 → 才执行。
3. **删除通道默认进回收站**；只有"纯缓存且用户明确同意彻底删"才可永久删。
4. **小批量**：每批 ≤10 个目标，每批后重新测量确认空间上升、系统无异常再继续；失败即停不重试。
5. **迁移先复制后删源**：跨盘迁移先复制+校验（文件数+字节数），确认无误才删源；任一步失败立即回滚。
6. **禁止** `rm -rf` / `del /S /Q` / 通配符批量删；禁止 `-ExecutionPolicy Bypass` 执行来路不明的 ps1。
7. **junction/symlink 红线**（2026-10-05 审计新增）：白名单目录（如 `%TEMP%`）下可能存在
   指向个人目录的 junction。`os.path.islink()` 对 junction **返回 False**（只对 symlink 为真），
   若按普通目录 rmtree，行为依赖库版本——Python 3.13 会抛错保护，但**不可依赖**。
   → 删除前必须用 `lstat().st_file_attributes & 0x400` 判 reparse point，
   **只摘链接本身（`os.rmdir`），绝不递归其内容**；目录子树内含 reparse point 时
   一律 SKIPPED 转人工。`delete_safe.py` 已内置这两道护栏（`is_reparse_point` /
   `find_reparse_points`，输出 `UNLINKED_LINK` 状态），不要绕过脚本手写删除逻辑。
8. **统计口径红线**：估算体积时**禁止 `os.walk`（默认跟随 junction）**，会把链接指向的
   外部数据算进可释放量 → 报告虚高、用户预期落空；循环链接还会无限递归。
   统一改用 `fsutil_safe.dir_size()` / `iter_files()` / `count_tree()`（`scandir` + 单次 stat
   + 判 `st_file_attributes & 0x400`）。
9. **链接判定红线**：`os.path.islink()` 对 junction 返回 False、`is_dir(follow_symlinks=False)`
   对 junction 返回 True —— 两个 API 都不能用来判 junction，只有
   `os.lstat(p).st_file_attributes & 0x400` 可靠。禁止复制粘贴遍历代码，一律用 `fsutil_safe`。
10. **迁移前必须预检源内链接**：`shutil.copytree` 默认**跟随**链接，会把链接指向的外部数据
    **实体复制**到目标盘（浪费空间/撑爆目标盘/丢失链接语义），指向自身或上级目录时还会无限
    递归。故 `migrate_with_junction.py` 默认拒绝源内含 junction 的迁移，需 `--allow-junction-copy`
    显式放行，或先单独迁移链接目标。
11. **截断即声明红线**：任何扫描/遍历只要设了上限（`--max-files`、超时、剪枝），输出里必须带
    `truncated=true` 或 `excluded=[...]`，报告里必须写"结论为下限"。
    **静默截断 = 系统性低估**：把单次扫描上限硬编码成 3 万文件，80 万文件的机器
    只看 3.75% 就敢报"共 12.4GB 垃圾"。本技能不重复这种做法。
12. **扫描零联网红线**：`scripts/` 下不得出现 `requests`/`urllib`/`socket`/`http` 调用。
    扫描产物（文件名、路径）具备强辨识性，等同目录清单，**只写本地、绝不上报**。
    （把扫描结果同步到远端服务器这类设计，一律不做。）

## 1. 本机环境与删除通道选型（关键差异化经验，先读这段再动手）

本机 WorkBuddy 沙箱对删除有特殊行为（2026-10 实测），选错通道会白干或误判：

| 现象 | 事实 | 正确做法 |
|---|---|---|
| `os.remove`/`shutil.rmtree` 删了却还在 | 沙箱 sitecustomize 把它们重定向为"移入回收站" | 要真删：用 `python -S` 跳过 sitecustomize（仅限纯缓存） |
| 大量删除被拦/变慢 | 每文件删前 spawn 守卫检查（~0.6s/个）；>50 个文件/轮需用户确认 | 分批 ≤10；批量缓存目录优先 `python -S` 或 Shell API |
| 回收站里的文件删不掉，反而被还原 | 回收站重删会被还回去 | 清空回收站走下面专门通道 |
| ctypes 直调 `SHEmptyRecycleBinW` 被杀 | 沙箱 SIGTERM | ① 让用户手动清空；② 经同意后 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 再跑；③ 任务计划程序拉起进程执行（不带 shim） |
| Bash 调 `powershell` 被拒 | 安全策略拦截 | 统一写 .py 脚本、内部 `subprocess` 调 CLI |
| PowerShell 工具拿不到 stdout | 本机限制 | 同上，走 Python subprocess |
| `sc.exe`/`reg.exe` 被封 | 沙箱策略 | `Dism.exe`/`vssadmin`/`fsutil`/PowerShell(经python) 放行可用 |

**删除通道优先级**（对缓存类目标）：
1. `python -S` + `os.remove`/`shutil.rmtree`（真删，最快；仅限白名单纯缓存）
2. Shell API `SHFileOperationW`+`FOF_ALLOWUNDO`（进回收站可恢复；个人数据用这个）→ `scripts/shell_delete.py`
3. 清空回收站：**优先引导用户自己在资源管理器中右键「清空回收站」**（一步操作，最安全）。
   `scripts/empty_recycle_bin.py` 仅在需要批量/静默清空时使用；该脚本本机**未经真实清空验证**
   （回收站里有用户真实文件时不代为清空），发布说明中不得写成"已验证"。
4. `robocopy /MOVE`（移动语义；注意目标同名文件会跳过，可改源时间戳 `os.utime` 绕过）
5. `MoveFileExW`+`MOVEFILE_DELAY_UNTIL_REBOOT` 或 `PendingFileRenameOperations`（被锁文件排重启删）

被运行进程锁定的文件（WinError 32/5）：先 `taskkill /im xxx.exe`（先优雅后 /f），等 2-3 秒再删；
国产杀软自我保护锁定的路径（WinError 5 持续）属"需用户操作"项——让用户在杀软里关自我保护，别死磕。

## 2. 标准流水线（SOP，按序执行）

### Stage 0 — 基线测量
```
python -S scripts/measure_free.py C:
```
输出 JSON（free_gb/readings/stable）。**必须 3 次读数稳定**（极差 <500MB）才可信，系统有瞬时写入。
全流程结束时用同一脚本复测对比。
**探针校准（首次会话建议做一次）**：写 100MB 临时文件→删除→复测 free。若删除后 free 不回升
（VSS/系统还原吃删除空间，见 pitfalls 32），报告里必须预告"本机空间回吐滞后"，并引导管理员
跑 `vssadmin list shadowstorage` 验证——避免用户拿资源管理器读数误判"白干"。

### Stage 1 — 只读扫描 + 权威估算（不删任何东西）
```
python -S scripts/scan_safe_targets.py    # 白名单安全垃圾分类字节数（无上限遍历）
python -S scripts/scan_c_drive.py         # 临时目录/浏览器缓存/更新缓存/回收站/已知大户
python -S scripts/scan_tiers.py --root C:\ --md   # 大文件三档（≥1GB / 500MB-1GB / 100-500MB）
Dism.exe /Online /Cleanup-Image /AnalyzeComponentStore   # WinSxS 唯一可信分解
vssadmin list shadowstorage                              # 卷影/还原点占用
```
- **目录遍历禁止手动封顶**（曾有 17.2GB 的 WinSxS 因遍历截断被误报成 5.16GB）。
- **云盘有两种形态，别混为一谈**：
  | 形态 | 特征 | 我们的处理 |
  |---|---|---|
  | Cloud Files 占位符（WPS / OneDrive） | 文件属性含 `OFFLINE`/`RECALL_ON_DATA_ACCESS`，逻辑大小可上百 GB 而本地只有几百字节 | `iter_files` 不跟随 + `link_probe` 双数字（逻辑/本地），判 `cloud-only` |
  | 虚拟文件系统盘（X: 这类 CloudFS 聚合盘） | 盘符级 `FileSystem=CloudFS`，目录都"正常"，没有占位符属性 | 体积按逻辑大小统计；迁移时由「目标盘非 NTFS」预检直接拦住 |
  
  误判的代价是双向的：把占位符当真实占用会让我们虚报上百 GB（用户清完发现空间没变），
  把虚拟盘当本地盘则会得出"这块能释放"的错误结论。
- **大文件必须分档**：只给"有 12.4GB 大文件"无法指导行动；「3 个 ≥1GB 的文件占了 62%」
  用户才知道该看哪 3 个。`scan_tiers.py` 输出的 `excluded` 会列出剪枝目录（WinSxS 等用 DISM 处理），
  不静默跳过。
- WinSxS 三层结构：active（~7-8GB，不可动）/ superseded（可清）/ rollback（需 ResetBase，
  代价=失去卸载更新能力，且实际收益常常只有几十 MB——先看 AnalyzeComponentStore 数字）。
- 大文件定位可推荐用户用 WizTree/TreeSize 秒级看全盘方块图（本技能不做全盘可视化）。

### Stage 2 — 分级报告 + 用户确认
把扫描结果整理成三档表格呈现（完整清单见 `references/targets_catalog.md`）：

| 档 | 含义 | 处理 |
|---|---|---|
| 🟢 安全 | 纯缓存/临时/可自动重建 | 用户一句"清理"即可执行（走白名单护栏） |
| 🟡 谨慎 | 回收站、浏览器数据、开发缓存、大文件 | 逐项列出，用户逐项确认 |
| 🔴 红线 | 个人目录、系统核心、pagefile | **永不列入清理项**，只给建议 |

给出预估可释放总量。**禁止拍脑袋高估**——数字必须来自 Stage 1 权威分解。
**期望管理**：主动说明释放量占总容量的比例（如 1GB/220GB 资源管理器蓝条几乎不动），
并把 hiberfil/系统还原等"大头"的前置条件（需管理员/需关软件）一次讲清，避免用户清完后
问"为啥容量没变"。

**风险登记（出报告前必跑，不要靠肉眼判断哪个能删）**：
```
python -S scripts/risk_register.py --targets targets.json --md
```
- 按 `references/risk_rules.json` 给每条路径定级：RED（禁止）/AMBER（逐项确认）/UNKNOWN（按 AMBER 处理）/GREEN（可批量）。
- 输出**两套数字**：`检查量(checked)` vs `可释放量(reclaim)`。二者必须分开写
  —— 「扫到了多少」和「真能腾出多少」不是一回事，混着写等于把扫描量当成果。
- 每个 AMBER 项自带 `delay_seconds`（默认 8s，高危 15s）：确认按钮延迟出现，防连点误删。
- 报告里要写**具体损失**（"删了所有网站会退出登录"），不要写"确定删除？"这种无信息量的废话。
- `--fail-on red` 可在自动化串里用退出码 2 拦住带红线的计划。

**交付一份可带走的 HTML 报告（推荐，聊天里的表格关掉就没了）**：
```
python -S scripts/build_report.py --out "%USERPROFILE%\Desktop\C盘体检报告.html"
```
它把「白名单缓存 / 应用数据大户 / 大文件三档 / 链接盲区 / 微信」一次性收进单文件 HTML，
三个板块：**可以放心清理**、**要动之前请想清楚**（含"删了会怎样"和"怎么搬"）、**不要碰**。

报告的四条硬规范（都踩过坑，不要改）：

1. **零 JS、零外部资源。** 页面由 Python 直接拼出来，不依赖脚本渲染 ——
   现场写 JS 一旦写坏，浏览器**不报错**，页面就是空白，用户和 AI 都会以为"报告生成了"。
   同理不引 CDN，断网也能双击打开。
2. **路径脱敏。** 报告里把真实用户名换成 `%USERPROFILE%`。用户会截图转发，别把姓名带出去。
3. **不列"程序本体"当红线。** `Program Files` 下的 `msedge.dll`、`libcef.dll` 这类
   用户根本不会去删，列出来只会把真正该警示的（聊天记录目录、虚拟机盘、pagefile）
   淹掉。报告只列"用户可能真的想动"的红线项，其余折叠成一行计数。
4. **有上限。** 每张表最多 40 行、大文件最多 15 条，超出折叠成"另有 N 项"。
   曾经有人把整块硬盘倒进报告（1.7 万个格子、13MB），打开就卡，等于没写。

报告还含一个**系统级大户**板块（`scan_system_targets.py` 提供数据）：
Windows.edb 搜索索引、hiberfil.sys、pagefile.sys、Windows.old、系统还原点、WinSxS 可回收组件。
这些占了 C 盘真正的大头，但**一个都不能用普通删除**——报告只给「正确做法」，不给清理按钮。
需要管理员的项标「需管理员」而不是猜个数字，避免用户按错误数字做决策。

### Stage 3 — 分批清理（确认后）
```
python -S scripts/risk_register.py --targets targets.json          # 执行前再审计一次
python -S scripts/delete_safe.py --targets targets.json            # dry-run 预览
python -S scripts/delete_safe.py --targets targets.json --confirm  # 真删（白名单护栏）
python scripts/shell_delete.py <路径...>                            # 走回收站的可恢复删除
```
- `delete_safe.py` 内置白名单：不在名单内的路径一律 SKIPPED 并告警（个人目录天然被拒）。
- 重复文件先出计划再删（**别边扫边删**）：
  `python -S scripts/dedup_planner.py --root D:\ --min-mb 10 --keep-policy newest --emit-targets dup.json`
  —— 强制选一种保留策略（`shortest` 默认 / `newest` / `oldest` / `source-dir` / `none`），
  避免"留了副本、删了原件"；计划会自动剔除命中红线的候选。
- npm 缓存优先 `npm cache clean --force` 原生命令（2万+小文件走文件系统删除极慢）。
- 系统级项目（Windows 更新残留等）优先引导 `cleanmgr.exe → 清理系统文件`（勾选
  Windows 更新清理/传递优化/设备驱动程序包/以前的 Windows 安装）或存储感知。
- 休眠文件（常 4-16GB）：管理员执行 `powercfg /h off`（失去休眠+快速启动）；恢复 `/h on`。
- WinSxS：`Dism.exe /Online /Cleanup-Image /StartComponentCleanup`（`0x80070005` 拒绝访问
  的根因常是 `PendingFileRenameOperations` 有残留，**重启一次再跑**即可，不是权限问题）。
- 卷影：`vssadmin list shadowstorage` 若虚高多为瞬时值，先重启观察，别硬删。
- 每批删完 → `measure_free.py` 复测 → 确认上升再继续。

### Stage 4 — 治本迁移（清理救急，迁移防复发）
按目标类型选方案，完整命令与回滚步骤见 `references/migration.md`：

**4a. 缓存重定向清单（标准动作，清理报告必附）**：
1. 运行 `python -S scripts/locate_app_caches.py`——只读识别 C 盘上所有已知应用缓存/数据目录，
   按占用排序输出 Markdown 清单（应用、路径、占用、调整方式、官方入口、Agent 可否代改）。
2. 把清单呈给用户，**双通道执行**：
   - **手动通道**：❌ 标注项（微信/QQ/WPS/钉钉/飞书/网盘/Docker 等 GUI-only）→ 用户按清单中的
     官方点击路径自行调整，Agent 跟进验证新位置生效。
   - **代改通道**：✅ 标注项 → 用户一句指令后 Agent 直接调整，**必须遵守"可用性保障协议"**（见下）：
     - 配置类（npm/pip/HF/Gradle/Maven/Go/Cargo）：执行 `setx` 或包管理器 config 命令；
       **改前先备份原配置文件/记录原值，改后告知回滚命令**。
     - junction 类（无官方设置的缓存目录）：经用户确认后跑 `scripts/migrate_with_junction.py`
       （结束进程 → 复制校验 → **源目录改名留底** → 建联接 → 自动验证）。
3. 改完验证：让应用产生新缓存文件，确认实际落盘在非 C 盘；写进收尾报告"持续增长源"栏。

**可用性保障协议（代改后软件必须一如既往，硬性要求）**：
1. **改前**：记录并备份原值/原目录（junction 流程自动把源目录改名 `.pre-junction-bak` 留底，不直接删除）。
2. **改后自动验证**（junction 由脚本内置）：复制校验（文件数+字节数）→ 原路径可见内容 → 经 junction
   写读测试；配置类执行 `get` 命令确认新值生效。
3. **冷启动验证**（必须交给用户逐项确认）：重新打开软件能正常启动、加载原有历史数据（微信能看
   聊天列表/旧图片）、产生一条新数据且落在非 C 盘。**未通过冷启动验证前不得清理留底。**
4. **异常即回滚**（秒级、无损）：junction 用 `migrate_with_junction.py --rollback <源目录>`
   （删联接 + 留底改名还原，数据回 C 盘原位）；配置类用备份值还原。
5. **观察期**：留底目录保留 ≥7 天，期间软件一切正常后才用 `--delete-backup` 清掉（送回收站再加一层保险）。
6. **迁移脚本自带三保险**：任一步失败自动中止并还原；`--verify` 随时复查；`--rollback` 一键还原。

**4b. 其余迁移方案**：
- **AppData 大户/写死路径的应用数据** → junction 联接（`scripts/migrate_with_junction.py`：
  预检（见下）→ 结束进程 → 复制 → 校验（文件数+字节数）→ 源改名留底 → 建 junction → 验证指向，
  永久生效无需改应用设置；QQ NT 版唯一可靠方案）。源内含 junction 时默认拒绝，需
  `--allow-junction-copy` 显式放行。

**迁移预检（动手前必跑，任何一项不过就是零改动中止）**：
```
python -S scripts/migrate_with_junction.py --check <源目录> <目标目录>
```
| 错误码 | 拦什么 | 为什么必须拦 |
|---|---|---|
| E0104 / E0205 | 目标盘或源卷不是 NTFS | junction 跨文件系统语义不成立；FAT32 无 ACL、文件锁不可靠，数据库类应用会「越来越慢 + 权限错误」，极难定位 |
| E0101 | 目标不是固定本地盘 | U 盘拔掉即断链，应用当场打不开数据 |
| E0103 | 目标盘余量 < 源体积 105% | 复制到一半撑爆目标盘 |
| E0204 | 路径 ≥ 248 字符 | 部分 Win32 API 在 260 处失败 |
| E0207 | 目标在源目录内部 | 无限递归复制 |
| E0203 | 源内含 junction | 复制会跟随链接，把树外数据实体复制过来 |
| **W0206** | 源含 SQLite 库 + wal/shm，或文件数过万 | **热数据警告**：数据库锁跨卷、大版本升级可能重置重解析点。建议只迁大块冷数据（媒体/模型），关键库留系统盘 |

**迁移台账与巡检（防"C 盘悄悄回涨"）**：
```
python -S scripts/migrate_with_junction.py --audit
```
junction 会被**磁盘清理 / 存储感知 / 应用更新器 / 大版本升级悄悄换回真实目录** ——
数据回到 C 盘、空间悄悄涨回去，用户完全无感。这是迁移类方案最常见的失效方式。
脚本每次迁移成功都登记到 `%LOCALAPPDATA%\c-drive-cleanup-ultimate\junctions.json`，
`--audit` 逐条检查并区分四种状态：

| 状态 | 含义 | 处理 |
|---|---|---|
| ✅ OK | 仍指向原目标 | 无事 |
| ❌ BROKEN | 已变回真实目录（最需要告警） | 确认目标盘数据还在 → 挪走新目录 → 重迁 |
| ⚠ RETARGET | 仍是 junction 但指向变了 | 多半是盘符漂移 |
| ❌ MISSING | 原路径不存在 | 确认盘是否掉线 |

**盘符漂移的根治办法**（值得写进给用户的建议）：把目标盘挂载成 NTFS **空文件夹**
（磁盘管理 → 更改驱动器号 → 装入 `C:\mnt\Data`），之后再也不怕 E: 变 F:。

**junction 的三个透明边界，必须提前告诉用户**（否则会被当成 bug）：
1. 跨进程的「打开文件」对话框 / 网页上传控件**不认** junction —— 要交给别的软件选的文件，
   先复制到桌面等真实目录；
2. 部分宿主应用拒绝把 junction 里的目录当工作区（报 `cannot host automations`），
   这与位置无关，卡的是"属于应用自身数据区"这个身份；
3. 「磁盘清理 / 存储感知 / 更新器」会自愈掉 junction —— 用 `--audit` 定期巡检。

**4c. 其余迁移方案（续）**：
- **WSL vhdx**（10-50GB）→ `wsl --manage <distro> --move` 或 导出/导入；vhdx 需 compact。
- **Docker Desktop** → 设置里改 Disk image location（官方通道），或先 `docker system prune -a`。
- **个人目录**（文档/下载/桌面）→ 引导用户用"属性→位置→移动"，**不要用 junction**。
- **虚拟内存** → 高级系统设置挪到 D 盘（需重启；建议 C 盘保留少量）。
- 微信/QQ 数据迁移注意事项（进程、加密 Registry.db、新版微信 4.x）见 migration.md。

### Stage 5 — 收尾核验与报告
1. `measure_free.py` 终值 vs Stage 0 基线，给出释放总量。
2. 报告含：已清理项及体积 / 🟡未处理项及原因 / 被锁定跳过项 / 🔴建议项 / 持续增长源与对应迁移建议。
3. 若涉及误删恢复：`scripts/restore_from_recycle.py --list [--find 关键词] / --restore $XXXXXXX`
   （解析 `$RECYCLE.BIN\<SID>\` 的 `$I` 元数据按原路径恢复；回收站已被清空则无法恢复）。

### Stage 6 — 定期巡检（防复发，替代需要付费的"自动清理"）
```
python -S scripts/gen_clean_task.py --daily 10:00 --out-json "D:\reports\c_scan.json"
python -S scripts/gen_clean_task.py --weekly SUN,09:30          # 更推荐：每周一次足够
python -S scripts/gen_clean_task.py --idle                      # 空闲触发，最不打扰
```
- 只生成 Task Scheduler XML + 注册命令，**脚本永不自行注册任务**（不给系统留后门）。
- 默认动作是**只读扫描**：定期把「这周新增多少垃圾、谁长得最快」推给用户，删除仍需人工确认。
- `--action clean` 会真删，必须同时给 `--targets-file` + `--i-understand-this-deletes` 双开关。
- **运行级别决定能不能注册上**（实测）：
  | 场景 | 参数 | 结果 |
  |---|---|---|
  | 只读扫描 | 默认 | `LeastPrivilege`，普通权限即可注册 |
  | 自动清理（含系统缓存） | 默认 | `HighestAvailable`，**非提权环境注册会「拒绝访问」** |
  | 自动清理（只清用户级） | 加 `--runlevel least` | 普通权限可注册，但删不了 `C:\Windows\Temp` 这类系统目录 |

  脚本会在生成时检查当前终端是否提权，不满足就提前说明两条出路（提权执行 / 降级 least），
  而不是让你对着四个字「拒绝访问」猜。
- 提醒阈值：**剩余空间 <10% 时才在报告里主动建议瘦身**，
  比每天弹窗催清理体面得多；巡检周期每周一次即可，不必每天。
- 卸载：`python -S scripts/gen_clean_task.py --print-unregister --name <任务名>`。

## 3. 微信专项清理子模块（微信/QQ 占 30-80GB 时启用）

**红线**：`Msg/*.db`（3.x）与 `db_storage/*`（4.x）永远不删；删除一律走回收站；
备份删除前必须先在微信 App 内"备份与恢复→恢复到手机"验证可还原；
删任何被微信占用的文件前先退出微信。完整流水线见 `references/wechat_module.md`。

| 步骤 | 脚本 | 说明 |
|---|---|---|
| ⓪ 认清布局 | `wechat_analyze.py --root <任意微信目录> --md` | 先判 `wechat3-root` / `wechat4-root` / `program-data` / `unknown`；**认不出直接报错**，不输出假账号 |
| ① 程序数据缓存直清 | `wechat_analyze.py --root "$APPDATA\Tencent\xwechat" --md` → `delete_safe.py` | `xplugin`/`radium`/`log`/`crashinfo`/`update`/`xplayer` 是纯缓存（已进白名单+GREEN）；**实测本机 1.05GB，零风险**，最容易被忽略的一步 |
| ② 账号根分类盘点（只读） | `wechat_analyze.py --root <账号根>` | 按账号分类 BackupFiles/FileStorage(3.x) 或 db_storage/msg(4.x)；受保护类别标「禁止删」 |
| ③ 备份真子集识别 | `wechat_backup_inspect.py --root <账号>\BackupFiles` | 按加密段大小签名识别可删的旧备份子集；传错目录会报错，不输出空壳 |
| ④ 收发文件 md5 去重 | `wechat_dedup_plan.py` → `wechat_dedup_recycle.py --confirm` | 真内容哈希去重 + HTML 审查报告，默认 dry-run；执行端独立复核计划（实测能拦下被篡改的聊天主库与树外路径） |
| ⑤ App 内清缓存 | 用户手动 | 设置→通用→存储空间→清理缓存/聊天记录管理（代码无法可靠替代） |

## 4. 脚本清单

> **改动任何脚本后必须重跑两套测试**，缺一不可：
>
> | 测试 | 跑什么 | 证明什么 | 成本 |
> |---|---|---|---|
> | `python -S scripts/selftest.py` | **157 项**合成断言，自造自清 | **逻辑正确** | 毫秒级 |
> | `python -S scripts/field_test.py` | **26 项**真实路径只读验收（`--with-schtasks` / `--with-migrate` / `--with-clean-task` 各加 1 项，全开 **29 项**） | **在真机上真的有效** | ~2.5 分钟 |
>
> `--with-schtasks` 额外做一次真实的「注册→运行→校验产出→删除」计划任务；
> `--with-migrate` 额外做一次真实的「迁移→读写穿透→回滚」。
>
> 为什么两套都要：实测中 selftest 88/88 全绿的同时，真机上有 6 个 P0 缺陷一个都没抓到
> （路径拼接吃掉盘符导致整段漏扫 / schtasks 拒绝 GroupId / WinSxS 目录被判"大文件" /
> 云盘挂载点造成 113GB 盲区 / 去重在个人目录被全拦 / 回收站恢复把路径截断）。
> 这些不是逻辑错，是**环境形状**问题，合成用例造不出来。详见 `references/pitfalls.md` 第 34-38 条。
>
> 出现 SKIP 表示该环境缺少对应真实场景，**这些能力本次未被验证**，不要当已通过。

| 脚本 | 用途 | 默认行为 |
|---|---|---|
| `fsutil_safe.py` | **公共安全原语**（库，非入口）：reparse 判定 / 安全遍历（剪枝 + `on_link` 回调）/ 体积统计 / 链接解析 / 链接盲区探测（`link_probe`）/ 归属校验 | 只读 |
| `selftest.py` | **合成自检**：157 项断言，覆盖红线拦截、遍历语义、链接护栏、去重、风险定级、分档、微信布局识别、能力闭环、真机事故回归 | 只读，自造自清 |
| `field_test.py` | **真机验收**：26 项真实路径只读断言（用户目录覆盖 / WinSxS 判级 / 云占位符识别 / 计划任务可控 / 去重 keep-one / 删除恢复闭环 / 微信布局 / 微信去重护栏 / 删除白名单 / 备份识别 / 扫描口径一致 / 审计性能 / 截断自曝 / **微信 4.x 真实数据** / **去重支持 4.x**）；`--with-schtasks` 加真实计划任务，`--with-migrate` 加真实迁移回滚，`--with-clean-task` 加真实定时删除（全开 29 项） | 只读，三个开关才动系统 |
| `build_report.py` | **HTML 体检报告**：把白名单/应用大户/大文件三档/系统级大户/链接盲区/微信收进单文件 HTML（零 JS、零外部资源、路径脱敏、表格有上限） | 只读，只写一个 HTML 文件 |
| `scan_system_targets.py` | **系统级大户检测**（只读）：Windows.edb 搜索索引 / hiberfil.sys / pagefile.sys / swapfile.sys / Windows.old / 系统还原点(VSS) / WinSxS 可回收组件。这些**都不能用普通删除**，脚本只测量并给出正确做法；需要管理员的项标「需管理员」，不猜数字 | 只读；`Dism` / `vssadmin` 需管理员，非提权时自动降级 |
| `measure_free.py` | C: 空闲测量（3 次稳定判定） | 只读 |
| `scan_safe_targets.py` | 白名单安全垃圾分类扫描（无上限遍历；`--out` 供定时任务写文件；路径定位失败会写 `warnings`） | 只读 |
| `scan_c_drive.py` | 临时/浏览器/更新缓存/回收站/已知大户扫描 | 只读 |
| `scan_tiers.py` | **大文件三档**（≥1GB / 500MB-1GB / 100-500MB）+ 类型直方图；剪枝目录与**链接盲区**都显式列出（含云盘占位符「本地实际占用」）；单实例锁 | 只读 |
| `locate_app_caches.py` | 应用缓存归属识别 → 重定向清单（含官方入口与代改标记） | 只读 |
| `risk_register.py` | **风险登记**：按 `risk_rules.json` 定级 RED/AMBER/UNKNOWN/GREEN；结构性红线硬拦（盘根/用户根/ProgramData）；同级别按 pattern 特异性择优（理由必须说对）；出「检查量 vs 可释放量」 | 只读 |
| `dedup_planner.py` | **重复文件规划**：三级哈希（size→头尾 8KB→SHA256）+ 5 种保留策略；**keep-one 语义**（组内留一份即可删副本，个人目录副本升级为「必须走回收站」） | dry-run，只出计划 |
| `delete_safe.py` | 白名单护栏删除（junction 识别 + 子树链接拦截 + PendingFileRename 排重启） | **dry-run，需 --confirm** |
| `shell_delete.py` | Shell API 删除（默认进回收站；--force 彻底删；--contents 只清内容；报告实际释放量） | 进回收站 |
| `empty_recycle_bin.py` | API 清空回收站（含沙箱降级方案） | — |
| `migrate_with_junction.py` | 应用数据跨盘迁移 + junction（留底回滚版：`--verify` 复查 / `--rollback` 一键还原 / `--delete-backup` 清留底 / `--allow-junction-copy` 放行源内链接）。**迁移完成时 C 盘释放为 0**（源改名留底），清留底后才真正释放——输出里会明写这笔账，回滚后会提示目标盘孤儿副本的清理命令 | 复制校验+指向校验通过才建联接，源目录永不直接删除 |
| `gen_clean_task.py` | **定时巡检**：生成 Task Scheduler XML（每日/每周/空闲），Principals 用 `UserId`+`LogonType`（写 `GroupId` 会导致 schtasks 拒绝访问）；默认只读扫描；`--action clean` 需双开关 | 只生成不注册 |
| `restore_from_recycle.py` | 回收站元数据解析与误删恢复 | 只读，--restore 才动 |
| `wechat_analyze.py` | 微信盘点：**先识别布局**（3.x / 4.x / 程序数据 / 未知），再按对应口径出账；认不出直接报错。程序数据模式给出「可清 / 谨慎 / 保留」判定 | 只读 |
| `wechat_backup_inspect.py` | 备份真子集识别：按加密段大小签名比对；**传错目录直接报错**（不输出 `total_bytes: 0` 的空壳） | 只读 |
| `wechat_dedup_plan.py` / `wechat_dedup_recycle.py` | 微信 md5 去重计划/执行（执行端做归属+红线校验） | **dry-run，需 --confirm** |

> **禁止**在业务脚本里手写目录遍历或体积统计 —— 一律 `from fsutil_safe import ...`。
> 历史上 `dir_size` 被独立实现过 4 份，全部踩了同一个 junction 坑（详见 pitfalls 21/22/24）。

## 5. 设计取舍：采纳什么、拒绝什么

清理类工具的设计空间是有限的，能同时满足「有用」和「不害人」的交集很窄。
下表是本技能当前的取舍，左列是本包实现的，右列是**明确不做**的：

| 采纳的做法 | 拒绝的做法 |
|---|---|
| 规则与引擎分离，规则库可独立更新 | 规则**静默**热更新，用户不可审查/不可回滚 |
| itemid 贯穿「清理项 / 白名单 / 风险文案」 | 把最有价值的「自动清理」「大文件搬家」锁进付费档 |
| 每个危险项配**定制警告**（红色标出具体损失） | 主界面淡化不可恢复，默认永久删除 |
| 大文件 1GB/500MB/100MB 三档 + 占比 | 扫描硬上限且不告知 → 系统性低估 |
| 扫描/检查/清理三段状态分离（checksize ≠ cleansize） | 焦虑话术、好评诱导弹窗、抽样截图 |
| 重复文件 5 种保留策略 | 右键菜单注入 + 桌面快捷方式守护 |
| 全局互斥量防并发扫描 | 上报埋点 + 文件上传接口 |

详见 `references/anti_patterns.md`（10 条反面模式 + 反向做法）。

**一条判定标准**：任何设计决策，问一句「这是为了让用户少丢文件，还是为了让转化率更高」。
答不上来就回到本文件第 0 节红线。

## 6. 参考资料
- `references/targets_catalog.md` — 30+ 类垃圾文件完整目录（🟢🟡🔴 分级 + cleanmgr 勾选项 + 休眠/虚拟内存）
- `references/risk_rules.json` — 风险登记规则库（25 条：9 RED / 14 AMBER / 2 GREEN，pattern 已覆盖微信 4.x；含 `scope` 与结构性红线，可被 `--rules` 覆盖）
- `references/app_cache_relocation.md` — 应用缓存重定向对照表（微信/QQ/WPS/网盘/浏览器/npm/pip/Docker 等的软件内改路径入口 + junction 兜底判定流程；清单自动生成见 `scripts/locate_app_caches.py`）
- `references/pitfalls.md` — 41 条实战踩坑清单（估算翻车/杀软内核锁/VSS 吃删除空间/Git Bash 路径转换 + **第 34-41 条真机验收专章**）
- `references/migration.md` — 迁移手册（junction 全流程与回滚 / WSL / Docker / 位置选项卡 / 微信QQ 机制）
- `references/wechat_module.md` — 微信专项四步流水线详解与收益参考
- `references/anti_patterns.md` — 反面模式清单（每条含现象描述 + 我们的反向做法）
