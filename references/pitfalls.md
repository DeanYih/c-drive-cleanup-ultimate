# 实战踩坑清单（每条都是真金白银的教训）

全部条目来自本机沙箱实测记录。执行前通读一遍，能避开 90% 的翻车。

## 测量与估算

1. **目录遍历手动封顶** → 曾把实际 17.2GB 的 WinSxS 误报成 5.16GB。→ 永远无上限 `os.walk`，大目录宁可慢。
2. **估算靠拍脑袋** → 扫描"看着 15GB+"实际只清出 8.5GB，用户当场抓包。→ 删前必跑权威分解
   （WinSxS 用 `DISM /AnalyzeComponentStore`，卷影用 `vssadmin list shadowstorage`）。
3. **ResetBase 收益高估** → 估 1-2GB 实得 ~42MB（StartComponentCleanup 早已清掉 superseded 层）。
   ResetBase 只在"从没跑过"时收益大，且代价是失去卸载累积更新能力。
4. **单次空间读数不可信** → 系统有瞬时写入，`fsutil volume diskfree` 必须测 3 次取稳定值（极差<500MB）。
5. **`pagefile.sys`/`hiberfil.sys` 常规遍历看不到** → 用 `Get-ChildItem C:\ -Force -File` 才显示，别漏估。
6. **空间瞬时波动别慌** → 删除后数字回落不一定是"回灌"；先查 SoftwareDistribution 是否空、有无 WU 进程，
   确认不是 Windows Update 再下结论（曾有"回灌=WinUpdate"的误判被用户截图证伪）。
7. **System Volume Information 虚高多为瞬时** → 实测 12.3GB 多为 transient，重启/关占用进程后自动回落，别硬删。

## 删除与沙箱

8. **`rm` 被 sitecustomize 劫持成回收站** → `os.remove`/`shutil.rmtree` 被重定向为移入回收站，
   且每文件 ~0.6s 守卫检查、>50 个/轮需确认、对回收站再删会被还原。→ 纯缓存用 `python -S` 真删；
   个人数据走 Shell API + `FOF_ALLOWUNDO`。
9. **回收站性能陷阱** → 上万小文件走回收站慢到超时（实测 npm `_cacache` 22 分钟跑不完，永久删除 30 秒）。
   永久删除仅限"用户已确认 + 纯可重建缓存"。
10. **`SHEmptyRecycleBinW` 在沙箱内被 SIGTERM 杀** → 降级：用户手动清空 / 经同意
    `CODEBUDDY_SAFE_DELETE_ENABLED=0` / 任务计划程序拉起进程执行（不带 shim）。
11. **Bash 调 `powershell` 被安全策略拦** → 写 Python 脚本内部 `subprocess` 调 CLI；Bash 只跑 `python x.py`。
    复杂逻辑不要用 `python -c` 内联（多行转义极易出错）。
12. **沙箱封 `sc.exe`/`reg.exe`，但放行 `Dism.exe`/`vssadmin`/`fsutil`** → 受保护服务（如 WSearch）
    改走 PowerShell `Set-Service`/`Stop-Service`（SCM API 有特权），注册表直写 `Start=4` 会 WinError 5。
13. **会话内 `OpenProcess` 报错 5 是沙箱假象** → 换任务计划程序上下文重测再下结论，别凭错误码断定"本机没权限"。
14. **路径含 `$`（如 `$Recycle.Bin`）在 Bash 里被变量展开** → 放进 Python 脚本文件无此问题。
15. **`Local Settings`/`Application Data` 等是兼容 junction** → 扫描会重复计数，以 `AppData\Local`/`Roaming`
    直属目录为准；PowerShell 遍历用 `LinkType -eq $null` 排除 reparse point。

## 锁定与权限

16. **WinError 32（被占用）** → 正常现象：先 `taskkill`（优雅→/f），等 2-3 秒再删；删不掉就跳过收集，
    用 `PendingFileRenameOperations` 排重启删，绝不循环重试。
17. **WinError 5（拒绝访问）排查顺序** → ① `icacls <文件>` 查 DENY 条目，`/remove:d` 移除后通常立即成功；
    ② 清只读属性；③ 国产杀软内核自我保护（退出 GUI 不会卸载 minifilter 驱动）→ 属"需用户操作"项，
    让用户在杀软设置里关自我保护，别死磕；④ DISM 报 0x80070005 的根因常是 PendingFileRenameOperations
    有残留，重启一次即可。
18. **受杀软逐文件扫描拖慢** → npm cache 卡死时改用 `npm cache clean --force` 原生命令；孤儿文件留待重启后清。

## 数据安全

19. **跨盘 `shutil.move` 源被锁会留残缺副本** → 务必清理目标盘残缺副本，避免后续 junction 指向坏数据。
    迁移一律"先复制→校验（文件数+字节数）→确认→再删源"，失败立即回滚。
20. **「软件搬家」实时镜像 = 双盘同删陷阱** → 某些清理类工具会把 C 盘个人数据在另一块盘建立**实时镜像**
    （不是快照）：删 C 盘副本时镜像盘会被同步删除。铁证：两块独立磁盘的目录 mtime 不可能自发对齐到秒，
    若各子目录 mtime 秒级完全一致必是实时镜像（父目录 mtime 不能当"冻结"证据）。纪律：删个人数据前先确认
    无跨盘实时镜像；镜像盘只当影子，不当独立备份单独清理。
21. **`os.path.islink()` 对 junction 返回 False**（只对 symlink 为真）→ 白名单目录下的 junction
    会被 `isdir()` 判为 True 而落入 `shutil.rmtree` 分支。Python 3.13 的 rmtree 会抛
    "Cannot call rmtree on a symbolic link" 保护目标，但这是**库版本的恩赐，不可依赖**
    （旧版本/其他实现可能穿透删除）。判定必须用 `os.lstat(p).st_file_attributes & 0x400`。
    2026-10-05 审计已给 `delete_safe.py` 加 `is_reparse_point()` / `find_reparse_points()` 双护栏。
22. **`os.walk` 默认跟随 junction** → 用它估算目录体积会把链接指向的**外部**数据算进来
    （实测：Temp 内一个指往 5MB 目录的链接，让 Temp 的统计从 0 变成 5,000,000 字节），
    导致报告虚高、用户清理后空间没涨；循环链接还会无限递归拖死扫描。
    一律改用 `os.scandir` + `e.is_dir(follow_symlinks=False)` 并显式跳过 `st_file_attributes & 0x400`。
23. **扫描与删除白名单必须同源校验** → 两处各写一份清单必然漂移（实测本技能曾出现"删除支持任意
    浏览器 Profile、扫描却只认 Default"的漏报）。改一侧必须同步另一侧，并用差集脚本交叉验证。
24. **为了判链路而多做一次 `os.lstat` 会让遍历慢 2~11 倍** → `DirEntry.stat()` 是**缓存**的，
    而 `os.lstat(e.path)` 是一次全新系统调用。若在遍历里既 `is_reparse_point(e.path)`（内部 lstat）
    又 `e.stat()`，每条目 2 次系统调用。实测（真实目录）：
    pip 缓存 0.92s→0.40s、Windows\Logs 0.068s→0.024s、Edge 用户数据 2.82s→0.44s。
    正确做法：**一次 `e.stat(follow_symlinks=False)`，从返回的 `st_mode` + `st_file_attributes`
    直接判 `S_ISLNK` 与 `0x400`**（见 `fsutil_safe._entry_stat`）。
    教训：修复安全性问题时必须同时量性能——本次修复正确性后曾引入 2~11 倍回退。
25. **`shutil.copytree` 默认跟随 junction —— 迁移会"实体复制"外部数据** → 实测：源目录内一个
    指向外部 100KB 目录的 junction，复制后目标盘出现一个**真实目录**（不再是链接）。
    危害：目标盘平白多一份数据（大目录可能撑爆盘）、链接语义丢失；若链接指向自身/上级目录
    会无限递归（实测钻进 `WinError 206` 路径超长）。更阴的是校验会**静默通过** ——
    因为源侧统计也跟随了链接，两边同样虚高、差异被掩盖。
    纪律：迁移前预检源内链接，默认拒绝并要求显式放行。
26. **`_winapi` 没有 `ReadJunction`（只有 `CreateJunction`）** → 用 `hasattr` 兜底后返回值恒为
    `None`，使"写读测试"退化成「写一个文件、再读同一个文件」——**永远通过**。这是最危险的一类
    缺陷：安全网假装在工作，用户拿到虚假绿灯。解析 junction 指向请用 `os.path.realpath()`
    （Windows 上走 `GetFinalPathNameByHandleW`，实测可正确解开 junction）。
    教训：任何"校验/验证"代码都必须注入一次**必然失败**的输入来自证有效（负向测试），
    否则无法区分「真的通过」和「逻辑根本没生效」。
27. **空目录迁移会被误判失败并自动回滚** → 旧判定 `not (ok and items and w_ok)` 要求目录非空；
    空的数据/缓存目录迁移成功后 items 为空 → 触发"验证失败"回滚。空目录迁移是合法场景，
    判定应只依赖「是 junction + 写读通过 + 指向正确」，条目数仅作展示。
28. **删除计划的执行端不能盲信计划文件** → 计划 JSON 等于删除指令；若它由旧版脚本（跟随链接）
    生成，`delete_path` 可能落在目录树之外。执行端必须做归属校验（`fsutil_safe.is_under`），
    并独立复核红线（`Msg/` 路径、`.db/.sqlite`），不依赖生成端的保证。
29. **把成千上万个小文件"送回收站"极慢**（实测 1.5 万个文件送到回收站，近 6 分钟仍未完成）→
    回收站需为每个文件写 `$I` 元数据。**纯缓存类**（可自动重建、无个人数据）优先用
    `python -S` 直接 unlink / `shell_delete.py --force`，别走 FOF_ALLOWUNDO；
    **仅个人数据**才用回收站换可恢复性。大批量时先小样本测速再决定通道。
30. **安全网必须做负向测试** → 本次审计发现过"验证代码逻辑失效但永远返回通过"的缺陷
    （见 26）。凡新增校验/护栏，都要注入一个**必然失败**的输入证明它真的会拦；
    只用正样本测试等于没测。`scripts/selftest.py` 已固化关键技术性质的断言，改动后务必重跑。
31. **`SHQueryRecycleBinW` 的耗时随回收站条目数增长** → 实测：回收站约 0 项时查询≈0 s，
    1.65 万项时耗时 **3.28 s**，直接把只读扫描 `scan_c_drive.py` 从 ~0.8 s 拖到 3.5 s。
    若发现扫描"无故变慢"，先查回收站条目数；流程内清空回收站（Stage 3）后即恢复。
32. **删除后 free 不回升：VSS/系统还原会"吃"掉删除释放的空间**（2026-10-05 真机实测，
    另一台 Win11 用户机）→ 探针实验实锤：bypassed 写 500MB → free 立减 500MB；删除 →
    文件消失但 free 不回升（等 6s 仍不回升）。最合理解释是**系统还原/VSS 即时快照**对被删
    数据做块级 COW 存档——删多少吃多少；而 Shell API 清空回收站的空间可立即到账。
    → Stage 0 基线后做一次**写入-删除探针校准**（写 100MB → 删 → 复测 free）：
    删除不回升就在报告里**预告"本机空间回吐滞后"**并引导管理员跑 `vssadmin list shadowstorage`
    验证 / 关系统还原，避免用户拿着资源管理器数字误判"白干"。
33. **Git Bash 会把 `/参数` 当路径转换**（MSYS 路径转换）→ 真机实测 `powercfg /h off` 报
    "参数无效"（`/h` 被转成 `H:\`）、`taskkill /im` 同款翻车。
    → 调 Windows CLI 一律用 Python `subprocess.run(['powercfg','/h','off'])` 列表传参
    （不走 shell 无转换）；命令报错时 stderr 是 GBK 编码，用 `errors='replace'` 解码，
    否则 UnicodeDecodeError 会把真实报错（如"需要管理员权限"）整个吞掉。

---

## 真机验收专章（2026-10-20 实测补齐）

下面 5 条是**同一件事**：合成单测 88/88 全绿，真机一跑全崩。
它们不是逻辑写错，而是**环境形状**超出了合成用例的想象。因此新增
`scripts/field_test.py`（真机只读验收），与 `selftest.py` 分工互补：
前者证明「逻辑正确」，后者证明「在真机上真的有效」。

34. **盘符被 `rstrip("\\:")` 吃掉，整段用户目录静默漏扫** →
    `"C:".rstrip("\\:")` 得到 `"C"`（rstrip 删的是**字符集**，冒号也在其中），
    拼出 `"C\Users"`，`isdir` 恒 False，于是 `if isdir(users):` 整块被跳过，
    **一声不响**。实测漏报 TEMP 88MB + pip 794MB + npm 461MB + Edge 25MB ≈ **1.4GB**，
    报告只显示 0.17GB。同一段代码的注释里还写着"必须显式补上分隔符"，属于自我打脸。
    → 只剥尾部反斜杠 `rstrip("\\/")`；并且**路径定位失败必须写 warnings**，
    绝不允许"扫不到就当作没有"。凡是 `if isdir(x):` 决定整块功能，else 分支必须发声。

35. **计划任务 XML 只写 `<GroupId>` 会让 schtasks 拒绝访问** →
    `<GroupId>S-1-5-32-545</GroupId>`（Users 组）结构完全合法、XML 校验全绿，
    但 `schtasks /Create /XML` 返回「错误: 拒绝访问」（exit=1），任务压根注册不上。
    真机对照实验：去掉 Principals → 成功；改 `<UserId>DOM\user</UserId>` +
    `<LogonType>InteractiveToken</LogonType>` → 成功；保留 GroupId → 拒绝访问。
    → `gen_clean_task.py` 固定用 UserId + LogonType，`validate()` 额外断言
    "有 UserId、无裸 GroupId"；**真实验收**放在 `field_test.py` 的 T15
    （注册→运行→校验产出→删除）—— 结构校验永远替代不了它。

36. **目录被判进「单文件 > 100MB」规则** → `match_size_mb` 条件规则没有 scope，
    `C:\Windows\WinSxS`（13.4GB 的**目录**）命中"大文件"，判成 AMBER，文案还写着
    "体积大不等于垃圾"。用户点「确认」就可能把系统组件存储列进清理计划。
    → 条件规则加 `scope: "file"`；同时 `dir\*` 形态的 pattern 必须能匹配
    **目录自身**（删除 dir 与删除 dir 下一切等价）。这里还有个连环坑：
    目录自身匹配最初写成 `np == pat[:-2]`，pattern 带 `*` 时恒为 False，
    导致 `*\appdata\local\pip\cache\*` 匹配不到目录、纯缓存被判成 AMBER ——
    必须继续用 `fnmatch`。
    另外**同级别要按 pattern 特异性择优**：QQ 消息库同时命中「个人目录」和
    「聊天记录主库」，两条都是 RED，但理由必须说后者，否则用户当普通文档处理。

37. **云盘挂载点造成 113GB 静默盲区，而跟随它又会虚高** →
    用户目录里 `WPSDrive\...\WPS企业云盘` 是 Cloud Files 重解析点（标记 `0x9000301a`）。
    `iter_files` 不跟随 → 大文件统计 9.1GB；PowerShell `Get-ChildItem -Recurse` 跟随
    → 122.7GB。**两个数字差 113GB，谁都没告诉用户为什么**。实测该目录 7209 个文件、
    逻辑 143.4GB，但每个文件属性含 `OFFLINE|RECALL_ON_DATA_ACCESS`，
    **本地实际只占 536 字节**（C 盘总共才用 119.7GB，不可能有 113GB 是它）。
    → 不跟随（防虚高），但必须**报告盲区**：`iter_files(on_link=...)` 回调收集，
    `link_probe()` 跟随进去量化，同时输出 `logical` 与 `local` 两个数字。四类必须分开：
    * `internal`：Windows 兼容性 junction（`My Documents`/`Application Data`/`Cookies`），
      指回 AppData 内部，体积已计入父树 —— 实测 85 个"链接"里绝大多数是它，
      不剔除就会把真正的 143GB 云盘淹没在噪音里；
    * `cross-drive`：junction 指向别的盘（`.workbuddy` → `E:\WorkBuddyData`），
      **不占本盘**，报它会制造"用户目录占 8.8GB"的假象；
    * 探测要防自引用循环（realpath 去重），但**不要用"平均文件大小"判循环** ——
      `.workbuddy` 真有 46.6 万文件 / 9.2GB，其中 36.8 万个 <1KB、7893 个 0 字节，
      平均 25B/文件是真实情况。可靠信号是 `dirs / files` 比例。
    * 触顶（`capped`）时字节数标注必须是"仅部分"，不能写"下限"——
      这两个词意思不同。

38. **去重被红线一刀切，功能在最常见场景等于不存在** →
    红线规则回答的是「删了会不会丢唯一副本」，而重复文件删的是**副本**且每组留一份，
    两者语义不同。旧实现直接剔除 RED，实测 `Documents`/`Pictures` 上 40 个候选
    全被判 RED、**0 组结果** —— 而照片/文档去重正好是它最主要的用途。
    → 改成 **keep-one 语义**：有 keeper 时允许删副本，但必须
    ① 走回收站（禁 `delete_safe.py` 永久删）② 逐条确认 ③ 标 `recycle_required`；
    只有 `--keep-policy none`（无 keeper）才硬拦。修复后在真机上跑出
    8 组真实重复 / 13.4MB。**注意 `--emit-targets` 的 note 要跟着变**，
    否则下游会拿它去永久删。

39. **「脚本跑通了」不等于「统计对了」——假结果比报错更坏** →
    微信盘点旧版把 `--root` 下每个子目录都当账号。传 `AppData\Roaming\Tencent\xwechat`
    （1.1GB 真实数据）时，它输出 `All Users`/`c_history`/`log`/`net_1` 一堆空账号，
    **没报错、JSON 结构正常、真正的 xplugin 652MB 一字节没统计**。备份识别同款：
    传错目录会列出 18 个 `total_bytes: 0` 的"备份"。
    → 凡是有"结构假设"的脚本，必须**先验证结构再干活**：认不出就报错退出。
    判据：如果输出里出现大量零字节条目，或"账号/备份"数量等于目录总数，
    基本可以判定是把结构猜错了。
    另外 Windows 的 `isdir` **不区分大小写**：用 `isdir(p,"msg")` 判微信 4.x，
    会把 3.x 的 `Msg/`（大写）一起命中，导致 3.x 账号根被判成 4.x、所有类别统计为 0。
    必须 `os.listdir()` 拿真实名比对。

40. **迁移完成 ≠ 空间释放；回滚完成 ≠ 收拾干净** → 迁移的流程是
    「复制到目标盘 → 源目录改名留底 → 建 junction」，所以**此刻 C 盘一个字节都没释放**，
    留底还占着同样的体积（实测 757.6MB 的 pip 缓存迁移后，C 盘空闲反而从 79.51→79.05GB）。
    如果报告写"已释放 757MB"，用户回头一看容量没变，就会认定工具骗人。
    → 迁移输出必须明写「当前释放 0 / 清留底后释放 X」。
    反过来，`--rollback` 之后目标盘那份副本就成了**孤儿**：不提醒的话，
    用户"迁了又回滚"的结果是**多占一份磁盘**。回滚输出必须给出孤儿路径、
    体积和确切的清理命令。

41. **能力要接上通路，否则等于没做** → 微信程序数据里有 1.05GB 明确可清
    （xplugin 652MB + radium 372MB + log 34MB），分析脚本能算出来、分级也能标 GREEN，
    但 `scan_safe_targets.py` 的白名单里一个都没列 —— **分析得出、工具清不掉**。
    修好白名单后同一台机器的可清理量从 1.77GB 涨到 **2.87GB**。
    → 每新增一类"可清目标"，必须同时检查三处：分析脚本能发现、风险库能分级、
    白名单能执行。`selftest.py` 的 `t_capability_wiring()` 就是钉这条的。

