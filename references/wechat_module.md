# 微信专项清理子模块（微信/QQ 占 30-80GB 时启用）

为什么单独成模块：微信类通讯工具的目录结构、加密方式、清理边界与系统垃圾完全不同——
用同一套逻辑要么删错聊天库、要么只能干瞪眼看缓存涨。

## 红线（任何脚本都不得违反）

- `Msg/*.db`（3.x）与 `db_storage/*`（4.x）是加密聊天库，**永远不删**
  （删了微信启动后从手机全量重拉，体感像"记录没了"）
- 删除一律走回收站（`FOF_ALLOWUNDO`），绝不 `rm -rf`
- 备份真要删，必须先在微信 App"备份与恢复→将备份恢复到手机"流程验证过超集可还原
- 不动账号登录信息，不动 `config`/`login`/`ilink`/`net` 等系统子目录
- 删除任何被微信占用的文件前，**先退出微信**（否则全部被锁、报删不掉）

## 第一步：先认清是哪一种目录（否则后面全白做）

`wechat_analyze.py` 会先判断布局，认不出就**报错退出**，不输出"每个子目录都是账号"的假结果：

| 布局 | 特征 | 含义 |
|---|---|---|
| `wechat3-root` | `<root>/<wxid>/{Msg,FileStorage,BackupFiles}` | 微信 3.x 账号根 |
| `wechat4-root` | `<root>/<wxid>/{db_storage,msg/…}` 或根下有 `xwechat_files` | 微信 4.x 账号根 |
| `program-data` | `AppData\Roaming\Tencent\xwechat` 下有 `config/log/net/crashinfo` | **客户端程序数据**，不是聊天记录 |
| `unknown` | 以上都不像 | 立即报错，请用户确认路径 |

> 踩过的坑：Windows 的 `isdir` 不区分大小写，用 `isdir(p,"msg")` 判 4.x 会把 3.x 的
> `Msg/`（大写）一起命中，3.x 账号根被误判成 4.x，然后一个类别都统计不到。必须用
> `os.listdir()` 拿真实名比对。

## ② 程序数据里的纯缓存直清（最高性价比，实测 1.05GB）

```bash
python -S scripts/wechat_analyze.py --root "$APPDATA\Tencent\xwechat" --md
```

`program-data` 布局会按子目录出账并给判定：

| 判定 | 子目录 | 处理 |
|---|---|---|
| 可清 | `xplugin`（小程序/插件包）、`radium`（内置浏览器运行时）、`log`、`crashinfo`、`update`、`xplayer` | 退出微信后直接删，会按需重建 |
| 谨慎 | `net`、`net_1`、`net_2`、`confsdk` | 删了可能要重新登录 |
| 保留 | `config`、`login`、`ilink`、`uh`、`finderlive`、`All users` | 不动 |

这六类「可清」目录已进 `scan_safe_targets.py` 白名单与 `risk_rules.json` 的 GREEN，
所以可以直接走 `delete_safe.py`。实测本机 `xplugin` 652MB + `radium` 372MB + `log` 34MB
= **1.05GB**，占整个微信目录的 99%。

## ③ 账号根分类盘点（只读）

```bash
python -S scripts/wechat_analyze.py --root "$USERPROFILE\Documents\WeChat Files" --md   # 3.x
python -S scripts/wechat_analyze.py --root "D:\某处\xwechat_files" --md                 # 4.x
```

输出每个账号下各类别的字节数、文件数、**处置建议**（可清/谨慎/保留/禁止）和"动了会怎样"。
**绝对只读。** 受保护类别（`Msg` / `db_storage`）标「禁止」。

### 4.x 的真实结构（实测 129.87 GB / 52.4 万文件）

```
xwechat_files\
  wxid_7oo0moqwl71g22\            ← 只有 config/ Finderlive/（几乎空的壳）
  wxid_7oo0moqwl71g22_6f5f\       ← 真正的数据区
      msg\attach\    68.50 GB   493,887 文件   聊天里的图片/视频/表情
      msg\file\      46.46 GB    10,274 文件   收到的文件本体
      db_storage\     3.73 GB       174 文件   消息主库（禁止）
      msg\video\      2.87 GB     2,831 文件
      cache\          1.44 GB    14,292 文件
  all_users\  All Users\  Backup\  FinderLive\    ← 公共/功能目录，不算账号
```

三个必须知道的点（都是合成结构造不出来、只有真机能发现的）：

1. **账号是两级的**：`wxid_xxx` 和 `wxid_xxx_3f2f` 属于同一个微信号，数据全在带后缀那个目录里。
   不合并的话，报告里同一个微信会拆两行，其中一行显示「未发现已知子目录」，
   用户会以为那是残留空壳。（脚本已按 `_[0-9a-f]{4,8}$` 后缀合并。）
2. **`All Users` / `all_users` / `Backup` / `FinderLive` 不是账号**，是功能目录。
   混进账号表就是纯噪音。
3. **`msg/attach` 往往是最大头，而且它不能删**。本机实测 68.5 GB / 49 万个文件，
   占了整个微信的 55%。删了聊天里对应消息就是空白，且不保证能重新下载。
   **正确做法是微信内「设置→存储空间→聊天记录管理」按会话清，不是删目录。**

### 真实盘点结果长这样（本机 2026-10-20 实测）

```
合计 123.11 GB；其中明确可清 1.46 GB（缓存/临时/埋点），
聊天内容与主库 121.66 GB 不能直接删目录
公共/功能目录（不算账号）：FinderLive 72.5 MB, all_users 434.1 KB, ...
```

这就是"不吹牛"的样子：**不会告诉你"能清 100GB"**，因为那 121 GB 是聊天内容。
能真正安全清的只有 1.46 GB 缓存，其余只能迁移（把整个 `xwechat_files` 指到别的盘）
或在 App 内按会话删。

按结果分流：
- `BackupFiles` 占大头（>30GB）→ 走 ④
- `FileStorage/File`（3.x）或 `msg/file`（4.x）占大头且多 .xlsx/.pdf/.docx → 走 ⑤
- `MsgAttach` / `msg/attach` / `Cache` / `Video` 占大头 → 走 ⑥（代码处理不了，必须 App 内清）

## ④ 备份真子集识别（结构对比，不读内容）

```bash
python -S scripts/wechat_backup_inspect.py --root "<wxroot>\<账号>\BackupFiles"
```
- 解析每个 `android_<hex>` 备份目录的 `BAK_0_TEXT` + `BAK_N_MEDIA` 段大小
- 段大小签名识别真子集：A 包含 B 的全部 MEDIA 段且 TEXT 字节一致、A 段数严格更多 → B 是 A 的子集
- **真实子集可送回收站**，但强烈建议先在 App 内"恢复到手机"验证超集可还原后再删
- 备份是 AES 加密（密钥从账号派生），只能按段大小对比，不是内容对比
- 传错目录（不是 BackupFiles）会**直接报错**，不再输出一堆 `total_bytes: 0` 的空壳

## ⑤ 收发文件 md5 真去重

```bash
# 1) 生成计划（只读，带 HTML 审查报告）
python -S scripts/wechat_dedup_plan.py --root "<wxroot>" --cache file_dedup_cache.json \
    --out ./dedup_out --min-mb 1 --max-files 800
# 2) 浏览器打开 ./dedup_out/wechat_dedup_report.html，用户逐组核对（可按"文件名不一致"筛选）
# 3) 执行（默认 dry-run；--confirm 才真送回收站）
python -S scripts/wechat_dedup_recycle.py --plan ./dedup_out/file_dedup_plan.json --confirm
```
- **同时支持 3.x `FileStorage/File` 与 4.x `msg/file`**（旧版只找前者，
  在真实 4.x 数据上会一个文件都扫不到、输出"0 组重复"，看起来像没重复、其实是没找对地方）
- `--min-mb`（默认 0.05MB）跳过缩略图级小文件；`--max-files` 先小样本试跑再放开
- **md5 内容哈希**（非文件名匹配）→ 真去重；同组取最短 basename 保留（最像原始版）
- 缓存机制：`(size, mtime)` 命中跳过哈希，重复运行秒级完成
- 每批 ≤10 个文件走 `SHFileOperationW + FOF_ALLOWUNDO`，逐批校验
- 报告标"⚠ 文件名不一致"组（byte-identical 但名字暗示内容不同）→ 由用户在报告里决定
- **执行端独立复核**：计划文件即使被手工篡改，位于 `Msg/` 或计划 root 之外的条目
  会被逐条剔除并打印原因（实测拦下聊天主库与 `C:\Windows\System32\...` 两类篡改）

**真实数据实测（本机 129GB / 52 万文件）**：取 ≥1MB 的前 800 个文件，
识别出 **7 组重复 / 86.5 MB**，典型形态是：
- `xxx.pdf` 与 `xxx(1).pdf`（同一份文件被发了两次）
- `xxx.pptx` 与 `.wpp_backup\xxx.pptx.wppsnapshot.1789695046568`（WPS 自动备份快照，
  **这类可以放心删**）

> 注意：微信 4.x 有 `config\hardlink.dat`，说明它自己也在用硬链接合并相同文件。
> 体积统计和去重必须识别同 inode 的多路径（`dedup_planner` 已做；微信专项按 md5 分组本身安全）。

## ⑥ 微信 App 内清缓存（必须用户手动，代码无法可靠替代）

「朋友圈缓存图」vs「好友对话图」的映射在加密的消息库里，外部进程无法可靠区分。唯一正解：

> PC 微信 → 设置 → 通用 → 存储空间 → ① 清理缓存（缩略图/视频预览，安全可清，用时重建）
> ② 聊天记录管理 → 按占用排序，对最大几个群逐个勾选删除的媒体类型（文字默认保留）

经验排序：先删「视频」「文件」（通常可放心），再决定「图片」；1:1 私聊保守，工作群/营销群可激进。

## 收益参考（一次典型会话，实测基线）

| 子步骤 | 典型回收 | 风险 |
|---|---|---|
| ② 程序缓存直清 | **0.5-3GB**（本机实测 1.05GB） | 极低（会重建，退出微信后删） |
| ③ 盘点 | 0（只读） | 无 |
| ④ 备份真子集 | 5-30GB（视备份数） | 删错=丢历史消息；先验证超集可还原 |
| ⑤ md5 去重 | 0.5-3GB | 低（真去重 + 回收站可还原） |
| ⑥ App 内清 | 5-30GB（视活跃度） | 中（误删好友图片不可还原，必须用户点） |
| 合计 | 微信占用的 30-60% | — |

> ② 一定要先做：它是唯一"零风险、几秒钟、1GB 起步"的一步，也是最容易被忽略的一步
> （因为它在 `AppData\Roaming` 里，不在直观的"微信文件"目录下）。
