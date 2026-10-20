# -*- coding: utf-8 -*-
"""大文件分档扫描 —— 回答「空间到底被谁占了」，而不是只给一个总数。

为什么分档
----------
「C 盘有 12.4GB 大文件」无法指导行动；「3 个 ≥1GB 的文件占了大文件总量的 62%」
就能让用户立刻知道该看哪 3 个。所以这里用三档模型（1GB / 500MB / 100MB），
每档同时给件数、字节数与占比。

截断与剪枝（见 references/anti_patterns.md 第 7 条）
---------------------------------------------------
* 硬上限 + 静默跳过是最常见的**系统性低估**来源：扫了 3.75% 却报一个总数；
* 本脚本默认**无上限**；一旦因为 `--max-files` 截断，输出里强制带
  `truncated=true` + `note`，并把结论标明为「下限」。
* 剪枝掉的目录（WinSxS 等）必须列在 `excluded` 里 —— 静默跳过等于骗人。

用法
----
    # C 盘三档概览（默认 ≥100MB 才记账）
    python -S scripts/scan_tiers.py --root C:\\

    # 只看某个用户目录，出 Markdown 报告
    python -S scripts/scan_tiers.py --root "C:\\Users\\X" --md --top 20

    # 完整遍历（不剪枝）—— 很慢，慎用
    python -S scripts/scan_tiers.py --root C:\\ --no-skip
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsutil_safe import (  # noqa: E402  不跟随 junction/symlink + 支持剪枝
    iter_files, link_probe, resolve_link_target, is_under)

# 默认剪枝：这些目录要么用专用工具（DISM/磁盘清理）处理，要么无权读，
# 硬遍历只会拖慢扫描且容易得出误导性结论。
DEFAULT_SKIP = [
    r"C:\Windows\WinSxS",
    r"C:\Windows\System32\DriverStore",
    r"C:\Windows\servicing",
    r"C:\System Volume Information",
    r"C:\$Recycle.Bin",
    r"C:\$WinREAgent",
]
DEFAULT_TIERS = [1024, 500, 100]   # MB，从大到小

LOCK_NAME = "ccu_scan_tiers.lock"
LOCK_STALE_SEC = 7200


# ---------------------------------------------------------------- 单实例锁
def acquire_lock(force=False):
    """跨进程互斥，防止两个扫描同时跑把磁盘 IO 打满。

    用文件锁实现，不碰内核命名对象（避免沙箱/权限问题）。
    崩溃残留的锁超过 2 小时自动失效。
    """
    path = os.path.join(os.environ.get("TEMP", "."), LOCK_NAME)
    if os.path.exists(path):
        try:
            age = time.time() - os.path.getmtime(path)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                pid = (f.read() or "?").strip()
        except OSError:
            age, pid = 0, "?"
        if age < LOCK_STALE_SEC and not force:
            return None, "另一个扫描进行中（pid=%s，锁 %d 秒前创建）。用 --force-lock 忽略。" % (pid, int(age))
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return path, None
    except OSError as e:
        return None, "无法创建锁文件: %s" % e


def release_lock(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------- 扫描
def fmt(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0


def scan(roots, min_mb=100, tiers=None, max_files=0, skip=None, progress=True):
    tiers = sorted(tiers or DEFAULT_TIERS, reverse=True)
    min_bytes = min_mb * 1024 * 1024
    skip = [] if skip is None else skip

    found = []          # (size, path)
    scanned = 0
    tree_bytes = 0      # 所有扫到的文件字节（含小文件），用于算占比分母
    truncated = False
    links = []          # 未跟随的链接/占位符 / stat 失败的目录（盲区）
    t0 = time.time()

    def on_link(p, why):
        links.append({"path": p, "reason": why})

    for root in roots:
        if not os.path.isdir(root):
            continue
        for fp in iter_files(root, skip_dirs=skip, on_link=on_link):
            scanned += 1
            try:
                sz = os.lstat(fp).st_size
            except OSError:
                continue
            tree_bytes += sz
            if sz >= min_bytes:
                found.append((sz, fp))
            if progress and scanned % 50000 == 0:
                sys.stderr.write("\r  已扫描 %d 个文件，命中 %d 个大文件 ..." % (scanned, len(found)))
                sys.stderr.flush()
            if max_files and scanned >= max_files:
                truncated = True
                break
        if truncated:
            break
    if progress and scanned:
        sys.stderr.write("\r" + " " * 60 + "\r")

    # 去重（同一个链接可能被多个 root 撞上）
    seen = set()
    uniq = []
    for lk in links:
        k = os.path.normcase(lk["path"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(lk)

    return {
        "found": found, "scanned": scanned, "tree_bytes": tree_bytes,
        "truncated": truncated, "elapsed": round(time.time() - t0, 1),
        "links": uniq,
    }


def bucketize(found, tiers, tree_bytes):
    big_bytes = sum(s for s, _ in found)
    out = []
    for t in tiers:
        lo = t * 1024 * 1024
        items = [(s, p) for s, p in found if s >= lo]
        b = sum(s for s, _ in items)
        out.append({
            "label": ">=%dMB" % t,
            "min_mb": t,
            "count": len(items),
            "bytes": b,
            "pct_of_big": round(b * 100.0 / big_bytes, 1) if big_bytes else 0.0,
            "pct_of_tree": round(b * 100.0 / tree_bytes, 1) if tree_bytes else 0.0,
        })
    # 互斥档位：1GB 以上、500MB-1GB、100-500MB
    exclusive = []
    for i, t in enumerate(tiers):
        lo = t * 1024 * 1024
        hi = tiers[i - 1] * 1024 * 1024 if i > 0 else None
        items = [(s, p) for s, p in found if s >= lo and (hi is None or s < hi)]
        b = sum(s for s, _ in items)
        label = ">=%dMB" % t if hi is None else "%d-%dMB" % (t, tiers[i - 1])
        exclusive.append({"label": label, "count": len(items), "bytes": b})
    return exclusive, big_bytes


def verdict(pr):
    """给一个盲区下判断：是云盘占位（不必清）还是本地真实数据（要单独处理）。"""
    logical, local, ph = pr["logical"], pr["local"], pr["placeholders"]
    if ph and logical and local <= logical * 0.05:
        return "cloud-only"
    if local:
        return "local-data"
    return "empty"


VERDICT_TEXT = {
    "cloud-only": "云盘占位符：逻辑大小很大但**本地几乎不占**，不是清理目标",
    "local-data": "本地真实数据：确实占盘，需单独处理（工具清理或迁移）",
    "empty": "空/无法读取",
    "cross-drive": "跨盘链接：数据实际在**另一个盘**，不占本盘空间",
    "internal": "指向扫描范围内的兼容性链接（体积已计入父目录，不是盲区）",
}


def _drive(p):
    return (os.path.splitdrive(p)[0] or "").lower()


def probe_links(links, limit=20, roots=None):
    """对未跟随的链接逐个量化盲区体积。

    必须先剔除「指向扫描范围内的链接」：Windows 用户目录里有一堆兼容性
    junction（`Application Data` / `My Documents` / `Cookies` / `「开始」菜单` …），
    它们指回 AppData 内部，体积早就被父树统计过了。实测它们占掉 85 个「盲区」
    里的绝大多数 —— 不剔除就等于用噪音把真正的 143GB 云盘挂载点淹掉。
    """
    kept = []
    for lk in links:
        ent = {"path": lk["path"], "reason": lk["reason"]}
        if lk["reason"] == "reparse":
            tgt = resolve_link_target(lk["path"])
            ent["target"] = tgt
            if tgt and roots and any(is_under(tgt, rt) for rt in roots):
                ent["verdict"] = "internal"
                kept.append(ent)
                continue
            # 指向另一个盘的链接：数据根本不在本盘，统计它只会制造假象
            # （实测 .workbuddy 是 junction -> E:\WorkBuddyData\.workbuddy，8.8GB 在 E 盘）
            if tgt and roots and any(_drive(tgt) and _drive(tgt) != _drive(rt) for rt in roots):
                ent["verdict"] = "cross-drive"
                kept.append(ent)
                continue
        kept.append(ent)

    # 真盲区优先探测，且按体积从大到小排；内部链接/跨盘链接不浪费遍历
    skip_kind = ("internal", "cross-drive")
    real = [b for b in kept if b.get("verdict") not in skip_kind]
    for ent in real[:limit]:
        if ent["reason"] == "reparse" and os.path.isdir(ent["path"]):
            pr = link_probe(ent["path"])
            ent.update({
                "files": pr["files"],
                "logical_bytes": pr["logical"],
                "local_bytes": pr["local"],
                "cloud_placeholders": pr["placeholders"],
                "capped": pr["depth_capped"] or pr["file_capped"],
                "verdict": verdict(pr),
            })
    real.sort(key=lambda b: (-(b.get("local_bytes") or 0), -(b.get("logical_bytes") or 0)))
    others = [b for b in kept if b.get("verdict") in skip_kind]
    return real + others, len(others)


def ext_histogram(found):
    agg = defaultdict(lambda: [0, 0])
    for s, p in found:
        ext = os.path.splitext(p)[1].lower() or "(无扩展名)"
        agg[ext][0] += 1
        agg[ext][1] += s
    rows = [{"ext": k, "count": v[0], "bytes": v[1]} for k, v in agg.items()]
    rows.sort(key=lambda r: -r["bytes"])
    return rows[:15]


def main():
    ap = argparse.ArgumentParser(description="大文件三档扫描（只读，零联网）")
    ap.add_argument("--root", action="append", help="扫描根目录，可重复；默认 C:\\")
    ap.add_argument("--min-mb", type=int, default=100, help="最小关注体积，默认 100MB")
    ap.add_argument("--tiers", default=",".join(str(t) for t in DEFAULT_TIERS),
                    help="分档阈值(MB,逗号分隔)，默认 1024,500,100")
    ap.add_argument("--top", type=int, default=15, help="列出前 N 个大文件")
    ap.add_argument("--max-files", type=int, default=0, help="扫描文件数上限（0=不限）")
    ap.add_argument("--no-skip", action="store_true", help="不剪枝（完整遍历，很慢）")
    ap.add_argument("--md", action="store_true", help="Markdown 报告（默认 JSON）")
    ap.add_argument("--out", help="写入文件")
    ap.add_argument("--force-lock", action="store_true", help="忽略已有锁")
    ap.add_argument("--quiet", action="store_true", help="不显示进度")
    ap.add_argument("--link-limit", type=int, default=20,
                    help="最多探测多少个未跟随的链接（默认 20；云盘挂载点遍历较慢）")
    ap.add_argument("--no-link-probe", action="store_true",
                    help="只列出未跟随的链接，不进入统计体积（最快）")
    args = ap.parse_args()

    roots = args.root or ["C:\\"]
    tiers = [int(x) for x in args.tiers.split(",") if x.strip()]
    skip = [] if args.no_skip else DEFAULT_SKIP

    lock, err = acquire_lock(args.force_lock)
    if err:
        print(json.dumps({"error": err}, ensure_ascii=False))
        return 1
    try:
        r = scan(roots, args.min_mb, tiers, args.max_files, skip, not args.quiet)
    finally:
        release_lock(lock)

    found = r["found"]
    exclusive, big_bytes = bucketize(found, sorted(tiers, reverse=True), r["tree_bytes"])
    top = sorted(found, key=lambda x: -x[0])[:args.top]

    # 只报告与本次 root 有交集的剪枝目录：扫 D 盘时列出 C:\Windows\WinSxS 只会让人困惑
    def _overlap(a, b):
        a, b = os.path.normcase(os.path.abspath(a)), os.path.normcase(os.path.abspath(b))
        return a.startswith(b) or b.startswith(a)

    shown_skip = [s for s in skip if any(_overlap(s, rt) for rt in roots)]

    # 盲区：遍历时刻意没有跟随的链接/占位符。**必须报告**，否则「只有 9GB
    # 大文件」这种结论是骗人的（实测该用户真值 122GB，差额全在云盘挂载点里）。
    if args.no_link_probe:
        blindspots, internal_n = [], 0
    else:
        blindspots, internal_n = probe_links(r["links"], args.link_limit, roots)
    real_spots = [b for b in blindspots
                  if b.get("verdict") not in ("internal", "cross-drive")]
    known_local = sum(b.get("local_bytes", 0) for b in real_spots)
    cloud_only = [b for b in real_spots if b.get("verdict") == "cloud-only"]

    res = {
        "roots": roots,
        "scanned_files": r["scanned"],
        "elapsed_sec": r["elapsed"],
        "truncated": r["truncated"],
        "max_files": args.max_files,
        "min_mb": args.min_mb,
        "tiers": exclusive,
        "big_files": {"count": len(found), "bytes": big_bytes,
                      "pct_of_scanned": round(big_bytes * 100.0 / r["tree_bytes"], 1) if r["tree_bytes"] else 0.0},
        "scanned_bytes": r["tree_bytes"],
        "excluded": shown_skip,
        "top": [{"path": p, "bytes": s} for s, p in top],
        "by_ext": ext_histogram(found),
        "link_total": len(r["links"]),
        "link_internal": internal_n,
        "link_probed": len(real_spots),
        "blindspots": blindspots,
        "blindspot_local_bytes": known_local,
    }
    if r["truncated"]:
        res["note"] = ("扫描因 --max-files=%d 截断：以上数字是**下限**，"
                       "真实可释放量只会更大。" % args.max_files)
    if shown_skip:
        res["note_excluded"] = "以下目录未遍历（用 DISM/磁盘清理 处理）：%s" % "; ".join(shown_skip)
    if r["links"]:
        txt = ("另有 %d 个链接/挂载点未跟随（盲区）。不跟随是刻意的：跟随会把"
               "树外数据和云盘逻辑大小算进来，导致体积虚高。" % len(r["links"]))
        if cloud_only:
            txt += ("其中 %d 个是云盘占位符（本地几乎不占空间），别把它们当成可清理量。"
                    % len(cloud_only))
        if known_local:
            txt += "已探明这些盲区的**本地实际占用**合计 %s。" % fmt(known_local)
        res["note_blindspot"] = txt

    if args.md:
        L = ["### 大文件分档（≥%dMB，扫描 %d 个文件，用时 %.1fs）" % (args.min_mb, r["scanned"], r["elapsed"]), ""]
        if r["truncated"]:
            L.append("> **扫描已截断**（上限 %d 个文件），以下数字是下限，真实值只会更大。" % args.max_files)
            L.append("")
        L.append("| 档位 | 文件数 | 体积 | 占大文件比例 |")
        L.append("|---|---|---|---|")
        for t in exclusive:
            L.append("| %s | %d | %s | %.1f%% |" % (t["label"], t["count"], fmt(t["bytes"]),
                                                   (t["bytes"] * 100.0 / big_bytes) if big_bytes else 0))
        L.append("")
        L.append("- 大文件合计 **%s**，占已扫描内容 %.1f%%" % (
            fmt(big_bytes), res["big_files"]["pct_of_scanned"]))
        if shown_skip:
            L.append("- 未遍历（不静默跳过，明确列出）：%s" % "、".join("`%s`" % s for s in shown_skip))
        L.append("")
        if res["link_total"]:
            L.append("#### 盲区：未跟随的链接 / 挂载点（真盲区 %d 个）" % len(real_spots))
            L.append("")
            L.append("> 这些路径**刻意没有跟随** —— 跟随会把树外数据、云盘逻辑大小算进来，"
                     "让数字虚高。但漏掉不说就是骗人，所以单列在这里。")
            L.append("")
            if real_spots:
                L.append("| 路径 | 文件数 | 逻辑大小 | 本地实际占用 | 性质 |")
                L.append("|---|---|---|---|---|")
                for b in real_spots:
                    if b.get("files") is None:
                        L.append("| `%s` | — | — | — | 无法读取（%s） |" % (b["path"], b["reason"]))
                        continue
                    L.append("| `%s` | %d | %s | %s | %s |" % (
                        b["path"], b["files"], fmt(b["logical_bytes"]),
                        fmt(b["local_bytes"]) + ("（仅部分）" if b.get("capped") else ""),
                        VERDICT_TEXT.get(b.get("verdict"), "未知")))
                L.append("")
                L.append("- 盲区本地实际占用合计 **%s**" % fmt(res["blindspot_local_bytes"]))
            else:
                L.append("- 没有真盲区（全部是指向扫描范围内的兼容性链接）。")
            if res["link_total"] > len(real_spots):
                L.append("- 另有 %d 个链接已排除，不算盲区：指向扫描范围内的兼容性链接"
                         "（`My Documents` / `Application Data` 这类，体积已计入父目录）、"
                         "或跨盘链接（数据在另一个盘，不占本盘）。"
                         % (res["link_total"] - len(real_spots)))
            L.append("")
        if top:
            L.append("#### 最大的 %d 个文件" % len(top))
            L.append("")
            L.append("| 体积 | 路径 |")
            L.append("|---|---|")
            for s, p in top:
                L.append("| %s | `%s` |" % (fmt(s), p))
            L.append("")
        if res["by_ext"]:
            L.append("#### 按类型")
            L.append("")
            L.append("| 类型 | 数量 | 体积 |")
            L.append("|---|---|---|")
            for row in res["by_ext"][:8]:
                L.append("| %s | %d | %s |" % (row["ext"], row["count"], fmt(row["bytes"])))
        text = "\n".join(L)
    else:
        text = json.dumps(res, ensure_ascii=False, indent=1)

    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
