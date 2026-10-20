#!/usr/bin/env python3
"""WeChat directory analyzer (read-only)。

用法:
    python -S scripts/wechat_analyze.py --root "<微信数据根>" [--top 20] [--md]

它先判断 --root 是哪一种布局，再按对应口径统计 —— 不做"把每个子目录当账号"的猜测：

  wechat3-root   微信 3.x 账号根（`WeChat Files\\<wxid>\\{Msg,FileStorage,BackupFiles}`）
  wechat4-root   微信 4.x 账号根（`xwechat_files\\<wxid>\\{db_storage,msg,cache}`）
  program-data   客户端程序数据目录（`AppData\\Roaming\\Tencent\\xwechat`,
                 里面是 log/xplugin/radium/net 等，**不是聊天记录**）
  unknown        认不出来 -> 直接报错退出，**不输出空账号糊弄人**

为什么必须做这个区分（实测事故）
--------------------------------
旧版把 root 下每个子目录都当账号，于是传 `AppData\\Roaming\\Tencent\\xwechat`
（1.1GB 真实数据）时，输出里出现 `All Users`/`c_history`/`log`/`net_1`
一堆空账号，真正的 652MB `xplugin` + 367MB `radium` 一个字节都没统计到。
脚本不报错、JSON 结构也"正常"，属于最坏的一种错：**看起来成功的假结果**。

绝不删除任何东西。
"""
import os
import re
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsutil_safe import iter_files, count_tree  # noqa: E402  统一实现：不跟随 junction/symlink

# ---- 微信 3.x 账号下的类别 ----
CATEGORIES_3 = [
    "BackupFiles", "FileStorage/File", "FileStorage/MsgAttach", "FileStorage/Cache",
    "FileStorage/Video", "FileStorage/Sns", "Msg", "Log", "All Users", "Applet", "WMPF",
]
# ---- 微信 4.x 账号下的类别 ----
CATEGORIES_4 = [
    "db_storage", "msg/attach", "msg/file", "msg/video", "msg/thumb",
    "cache", "temp", "backup", "config", "emoticon",
]
PROTECT = {"Msg", "db_storage"}

# 每个类别「能不能动、动了会怎样」—— 这是真实数据跑出来后补的：
# 第一版只打了「禁止删」标记，于是 68.5GB 的 msg/attach 那一行没有任何处置建议，
# 用户看到 68GB 根本不知道该不该动。分类不做处置建议 = 只报了个数字。
CATEGORY_VERDICT = {
    "db_storage": ("禁止", "消息主库（加密）。删了聊天记录全部消失，没有云端可恢复"),
    "msg":        ("禁止", "消息主库（加密），永不删除"),
    "msg/attach": ("谨慎", "聊天里的图片/视频/表情本体。删了对应消息里就是空白，可能不可再下载"),
    "msg/file":   ("谨慎", "聊天里收到的文件本体。这是用户要的文件，不是缓存"),
    "msg/video":  ("谨慎", "聊天视频本体"),
    "msg/thumb":  ("可清", "缩略图缓存，会按需重建"),
    "cache":      ("可清", "本地缓存，会按需重建"),
    "temp":       ("可清", "临时文件"),
    "apm_record": ("可清", "性能埋点记录，无业务价值"),
    "config":     ("保留", "登录态与本地配置，删了要重新登录"),
    "emoticon":   ("谨慎", "自定义表情包，删了要重新收藏"),
    "backup":     ("谨慎", "备份数据，动之前先在 App 内验证可还原"),
    "resource":   ("保留", "运行资源（表情/字体/皮肤）"),
    "business":   ("保留", "业务数据"),
    "FileStorage/File":      ("谨慎", "聊天里收到的文件本体"),
    "FileStorage/MsgAttach": ("谨慎", "聊天里的图片/视频/表情本体"),
    "FileStorage/Cache":     ("可清", "缩略图缓存，会重建"),
    "FileStorage/Video":     ("谨慎", "聊天视频本体"),
    "FileStorage/Sns":       ("谨慎", "朋友圈附件"),
    "BackupFiles":           ("谨慎", "备份归档，删之前先验证超集可还原"),
    "Log":                   ("可清", "运行日志"),
}

# 这些不是「账号」，是微信的功能/公共目录，混进账号表会让报告变噪音
NON_ACCOUNT = {"all users", "all_users", "backup", "backupfiles", "finderlive",
               "xwechat_filesall users"}

# ---- 程序数据目录里各子目录的性质（微信 4.x，AppData\Roaming\Tencent\xwechat）----
PROGRAM_DIRS = {
    "log":        ("可清", "运行日志，删了只影响排查问题"),
    "crashinfo":  ("可清", "崩溃转储文件"),
    "c_history":  ("可清", "命令/会话历史缓存"),
    "xplugin":    ("可清", "小程序与插件包，按需重新下载"),
    "radium":     ("可清", "内置浏览器/插件运行时缓存，会重建"),
    "xplayer":    ("可清", "内置播放器缓存"),
    "update":     ("可清", "更新包下载缓存"),
    "net":        ("谨慎", "网络模块数据，删掉可能要重新登录"),
    "net_1":      ("谨慎", "网络模块数据（分片）"),
    "net_2":      ("谨慎", "网络模块数据（分片）"),
    "confsdk":    ("谨慎", "配置 SDK 数据"),
    "ilink":      ("保留", "设备互联凭证，删了要重新配对"),
    "login":      ("保留", "登录态"),
    "config":     ("保留", "本地配置"),
    "uh":         ("保留", "用户习惯数据"),
    "finderlive": ("保留", "视频号/直播数据"),
    "all users":  ("保留", "公共数据"),
}
_PROG_MARKERS = ("config", "log", "net", "crashinfo")


def detect_layout(root):
    """返回 (layout, detail)。认不出来就 'unknown'，绝不硬猜。"""
    try:
        entries = [n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))]
    except OSError as e:
        return "unknown", f"无法读取目录: {e}"
    low = {n.lower() for n in entries}

    if "xwechat_files" in low:
        return "wechat4-root", "根下直接是 xwechat_files/"
    if "wechat files" in low:
        return "wechat3-root", "根下直接是 WeChat Files/"

    # 逐账号判断时必须**看真实大小写**：Windows 的 isdir 不区分大小写，
    # 用 `isdir(p,"msg")` 去测 4.x 会把 3.x 的 `Msg`（大写）也命中，
    # 于是 3.x 账号根被误判成 4.x，然后一个类别都统计不到（实测踩过）。
    # 4.x 用 `db_storage` 或小写 `msg/`，3.x 用大写 `Msg/` 或 `FileStorage/`。
    for n in entries:
        p = os.path.join(root, n)
        try:
            real = set(os.listdir(p))
        except OSError:
            continue
        if "db_storage" in real:
            return "wechat4-root", f"账号目录 {n} 含 db_storage（4.x）"
        if "msg" in real and "Msg" not in real:
            return "wechat4-root", f"账号目录 {n} 含小写 msg/（4.x）"
        if "FileStorage" in real or "Msg" in real or "BackupFiles" in real:
            return "wechat3-root", f"账号目录 {n} 含 FileStorage/Msg（3.x）"

    hits = [k for k in _PROG_MARKERS if k in low]
    if len(hits) >= 2:
        return "program-data", "命中程序目录标记: %s" % ", ".join(hits)
    return "unknown", "既不像账号根也不像程序数据目录（子目录: %s）" % ", ".join(sorted(entries)[:8])


def _walk(d):
    t = c = 0
    for fp in iter_files(d):
        try:
            t += os.lstat(fp).st_size
            c += 1
        except OSError:
            pass
    return t, c


def analyze_program_data(root, top):
    """程序数据目录：按子目录出账，并给出可清性判断。"""
    rows = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        t, c = _walk(p)
        verdict, why = PROGRAM_DIRS.get(name.lower(), ("待判断", "未登记的子目录，先别动"))
        rows.append({"name": name, "bytes": t, "files": c, "verdict": verdict, "why": why})
    rows.sort(key=lambda r: -r["bytes"])
    reclaim = sum(r["bytes"] for r in rows if r["verdict"] == "可清")
    return {
        "layout": "program-data",
        "root": root,
        "detected_because": "命中程序目录标记（log/net/config/crashinfo）",
        "dirs": rows[:max(top, 30)],
        "total_bytes": sum(r["bytes"] for r in rows),
        "reclaimable_bytes": reclaim,
        "note": ("这是微信**客户端程序数据**目录，不是聊天记录根。"
                 "`可清` 项可安全删除（会按需重建）；`保留` 项删了要重新登录/配对。"
                 "真正的聊天记录在 文档\\xwechat_files 或 文档\\WeChat Files，"
                 "请对那个目录再跑一次本脚本。"),
    }


def account_key(name):
    """把微信 4.x 的两级账号目录合并成同一个账号。

    真实结构（实测 F:\\缓存\\xwechat_files）：
        wxid_7oo0moqwl71g22/          只有 config/ Finderlive/
        wxid_7oo0moqwl71g22_6f5f/     真正的数据区：db_storage/ msg/ cache/ temp/
    不合并的话，报告里同一个微信会拆成两行，其中一行显示「未发现已知子目录」，
    用户会以为那个是残留空壳。
    """
    base = re.sub(r"_[0-9a-f]{4,8}$", "", name)
    return base or name


def analyze_accounts(root, layout, top):
    cats = CATEGORIES_4 if layout == "wechat4-root" else CATEGORIES_3
    accounts = {}       # 合并后的账号 -> 类别聚合
    aliases = {}        # 合并后的账号 -> 实际目录名列表
    extras = {}         # 非账号的功能目录（All Users / Backup / FinderLive）

    for name in sorted(os.listdir(root)):
        ap = os.path.join(root, name)
        if not os.path.isdir(ap):
            continue
        found = {}
        for cat in cats:
            cp = os.path.join(ap, *cat.split("/"))
            if os.path.isdir(cp):
                t, c = _walk(cp)
                found[cat] = {"bytes": t, "files": c,
                              "protected": cat in PROTECT or cat.split("/")[0] in PROTECT}
        if name.lower() in NON_ACCOUNT:
            t, c = _walk(ap)
            extras[name] = {"bytes": t, "files": c}
            continue
        key = account_key(name) if layout == "wechat4-root" else name
        aliases.setdefault(key, []).append(name)
        slot = accounts.setdefault(key, {})
        for cat, d in found.items():
            if cat in slot:
                slot[cat]["bytes"] += d["bytes"]
                slot[cat]["files"] += d["files"]
            else:
                slot[cat] = dict(d)

    top_files = []
    if top > 0:
        big = []
        for name in os.listdir(root):
            ap = os.path.join(root, name)
            if not os.path.isdir(ap):
                continue
            for sub in (("msg", "file"), ("msg", "attach"), ("FileStorage", "File")):
                d = os.path.join(ap, *sub)
                if not os.path.isdir(d):
                    continue
                for fp in iter_files(d):
                    try:
                        big.append((os.lstat(fp).st_size, fp))
                    except OSError:
                        pass
        big.sort(reverse=True)
        top_files = [{"bytes": b, "path": p} for b, p in big[:top]]

    empty = [k for k, v in accounts.items() if not v]
    return {
        "layout": layout,
        "root": root,
        "accounts": accounts,
        "account_dirs": aliases,
        "extras": extras,
        "accounts_without_known_dirs": empty,
        "top_files": top_files,
        "warning": ("db_storage / Msg 是聊天记录主库，删了不可恢复，永不删除。"
                    "msg/attach、msg/file 是聊天内容本体——删了聊天里就空白，"
                    "优先用微信「设置→存储空间」在 App 内按会话清，而不是删目录。"
                    "BackupFiles 可能含冗余副本，用 wechat_backup_inspect.py 再看。"),
    }


def fmt_size(n):
    """自适应单位：小目录不要显示成 0.0 MB（那会让用户以为没东西）。"""
    if n >= 1024 ** 3:
        return "%.2f GB" % (n / 1024.0 ** 3)
    if n >= 1024 ** 2:
        return "%.1f MB" % (n / 1024.0 ** 2)
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % n


def to_md(res):
    L = ["### 微信目录盘点（布局：%s）" % res["layout"], ""]
    if res["layout"] == "program-data":
        L.append("> %s" % res["note"])
        L.append("")
        L.append("| 子目录 | 体积 | 文件数 | 判定 | 说明 |")
        L.append("|---|---|---|---|---|")
        for r in res["dirs"]:
            L.append("| `%s` | %s | %d | %s | %s |"
                     % (r["name"], fmt_size(r["bytes"]), r["files"], r["verdict"], r["why"]))
        L.append("")
        L.append("- 合计 **%s**，其中明确可清 **%s**"
                 % (fmt_size(res["total_bytes"]), fmt_size(res["reclaimable_bytes"])))
        return "\n".join(L)

    L.append("| 账号 | 类别 | 体积 | 文件数 | 处置 | 动了会怎样 |")
    L.append("|---|---|---|---|---|---|")
    total = 0
    reclaim = kept = 0
    for acct, cats in res["accounts"].items():
        if not cats:
            L.append("| `%s` | （未发现已知子目录） | — | — | — | — |" % acct)
            continue
        for cat, d in sorted(cats.items(), key=lambda kv: -kv[1]["bytes"]):
            verdict, why = CATEGORY_VERDICT.get(cat, ("待判断", "未登记的类别，先别动"))
            if not d["protected"] and verdict == "禁止":
                verdict = "禁止"
            L.append("| `%s` | %s | %s | %d | %s | %s |"
                     % (acct, cat, fmt_size(d["bytes"]), d["files"], verdict, why))
            total += d["bytes"]
            if verdict == "可清":
                reclaim += d["bytes"]
            elif verdict in ("谨慎", "保留", "禁止"):
                kept += d["bytes"]
    L.append("")
    L.append("- 合计 **%s**；其中明确可清 **%s**（缓存/临时/埋点），"
             "聊天内容与主库 %s 不能直接删目录"
             % (fmt_size(total), fmt_size(reclaim), fmt_size(kept)))
    if res.get("extras"):
        ex = ", ".join("%s %s" % (k, fmt_size(v["bytes"]))
                       for k, v in sorted(res["extras"].items(),
                                          key=lambda kv: -kv[1]["bytes"]))
        L.append("- 公共/功能目录（不算账号）：%s" % ex)
    L.append("- %s" % res["warning"])
    if res["accounts_without_known_dirs"]:
        L.append("- 注意: %d 个账号下未发现任何已知子目录（可能是已清空或只有配置）"
                 % len(res["accounts_without_known_dirs"]))
    return "\n".join(L)


def main():
    p = argparse.ArgumentParser(description="微信目录盘点（只读）",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", required=True, help="微信数据根目录")
    p.add_argument("--top", type=int, default=20, help="列出前 N 个大文件（0=关闭）")
    p.add_argument("--md", action="store_true", help="Markdown 输出（默认 JSON）")
    args = p.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit("ERROR: root 不存在: " + root)

    layout, why = detect_layout(root)
    if layout == "unknown":
        print(json.dumps({
            "error": "无法识别的微信目录结构，已停止（不输出猜出来的假数据）",
            "root": root,
            "detail": why,
            "hint": ("微信 3.x 账号根形如 文档\\WeChat Files；4.x 形如 文档\\xwechat_files；"
                     "程序数据在 AppData\\Roaming\\Tencent\\xwechat。"),
        }, ensure_ascii=False, indent=2))
        return 1

    if layout == "program-data":
        res = analyze_program_data(root, args.top)
        res["detected_because"] = why
    else:
        res = analyze_accounts(root, layout, args.top)
        res["detected_because"] = why

    print(to_md(res) if args.md else json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
