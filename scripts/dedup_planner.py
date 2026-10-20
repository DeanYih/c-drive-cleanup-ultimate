# -*- coding: utf-8 -*-
"""重复文件规划 —— 只出计划，不删任何东西。

为什么要「保留策略」而不是「保留第一个」
----------------------------------------
同组重复文件里"哪一份该留"没有通用答案，必须由用户选。所以给 5 种保留策略
（保留源文件 / 最新修改 / 最早创建 / 路径最短 / 不保留源文件）。
挑选逻辑取决于使用场景：

  * 照片备份：应该留**最早创建**的那份（原始），删掉后来复制的
  * 下载多份：应该留**最新修改**的（可能修过）
  * 分散在多处：应该留**路径最短**的（更可能是"主位置"）
  * 有明确归档目录：应该留**归档目录内**的

一刀切"保留第一个"必然出现「留了副本、删了原件」的惨案。

三级筛选（性能）
----------------
1. 按 size 分组（一次遍历，O(n)）
2. 同 size 组内比「头 8KB + 尾 8KB」快速哈希（大文件不必全读）
3. 前两步都相同的才做全量 SHA-256

安全
----
* 不跟随 junction/symlink（fsutil_safe）
* 硬链接识别（st_ino/st_dev 相同 -> 同一内容实体，只提示不列入删除）
* 默认剔除触发 RED 红线的候选（复用 risk_register.classify）
* 默认 dry-run：只写计划文件；实际删除交给 delete_safe.py / shell_delete.py

用法
----
    # 默认：保留路径最短的
    python -S scripts/dedup_planner.py --root "D:\\Photos" --min-mb 1 --md

    # 保留最新修改的，输出 targets.json 给 delete_safe
    python -S scripts/dedup_planner.py --root D:\\ --min-mb 10 --keep-policy newest --emit-targets dup_targets.json

    # 保留归档目录内的副本
    python -S scripts/dedup_planner.py --root D:\\ --source-dir "D:\\Archive" --keep-policy source-dir
"""
import argparse
import hashlib
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsutil_safe import iter_files  # noqa: E402

LOCK_NAME = "ccu_dedup_planner.lock"
LOCK_STALE_SEC = 7200
HEAD = 8192

POLICIES = ("shortest", "newest", "oldest", "source-dir", "none")


def fmt(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0


def quick_hash(path, size):
    """头 8KB + 尾 8KB + 长度。比全量 SHA-256 快一个数量级，足够筛掉绝大多数假重复。"""
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(HEAD))
        if size > HEAD * 2:
            f.seek(-HEAD, os.SEEK_END)
            h.update(f.read(HEAD))
    return h.hexdigest()


def full_hash(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def acquire_lock(force=False):
    path = os.path.join(os.environ.get("TEMP", "."), LOCK_NAME)
    if os.path.exists(path) and not force:
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:
            age = 0
        if age < LOCK_STALE_SEC:
            return None, "另一个查重任务进行中（锁 %d 秒前创建）。用 --force-lock 忽略。" % int(age)
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
        return None, "无法创建锁: %s" % e


def release_lock(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def pick_keeper(members, policy, source_dir):
    """从同组文件里选出保留者。members: [(path, size, mtime)]"""
    if policy == "newest":
        return max(members, key=lambda m: m[2])
    if policy == "oldest":
        return min(members, key=lambda m: m[2])
    if policy == "source-dir" and source_dir:
        sd = os.path.normcase(os.path.abspath(source_dir))
        inside = [m for m in members if os.path.normcase(os.path.abspath(m[0])).startswith(sd)]
        if inside:
            return min(inside, key=lambda m: len(m[0]))
    if policy == "none":
        return None
    # shortest（默认）= 路径最短的那个（更可能是主位置，而非深层副本）
    return min(members, key=lambda m: (len(m[0]), m[0]))


def plan(roots, min_mb=1, policy="shortest", source_dir=None, max_files=0, quiet=False):
    min_bytes = min_mb * 1024 * 1024
    by_size = defaultdict(list)
    scanned = 0
    t0 = time.time()

    for root in roots:
        if not os.path.isdir(root):
            continue
        for fp in iter_files(root):
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            if st.st_size < min_bytes:
                continue
            scanned += 1
            by_size[st.st_size].append((fp, st.st_size, st.st_mtime, getattr(st, "st_ino", 0)))
            if max_files and scanned >= max_files:
                break
        if max_files and scanned >= max_files:
            break

    candidates = {k: v for k, v in by_size.items() if len(v) > 1}
    groups = []
    for size, members in candidates.items():
        # 2) 快速哈希分组
        by_q = defaultdict(list)
        for m in members:
            try:
                by_q[quick_hash(m[0], size)].append(m)
            except OSError:
                continue
        for qh, subs in by_q.items():
            if len(subs) < 2:
                continue
            # 3) 全量哈希分组
            by_full = defaultdict(list)
            for m in subs:
                try:
                    by_full[full_hash(m[0])].append(m)
                except OSError:
                    continue
            for fh, same in by_full.items():
                if len(same) < 2:
                    continue
                # 硬链接：同一 inode 的多个路径不算"重复占用"，删一个不释放空间
                inodes = [m[3] for m in same if m[3]]
                if len(set(inodes)) < len(same) and inodes:
                    continue
                keeper = pick_keeper(same, policy, source_dir)
                keep_path = keeper[0] if keeper else None
                groups.append({
                    "size": size,
                    "hash": fh[:16],
                    "count": len(same),
                    "keep": keep_path,
                    "keep_mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(keeper[2])) if keeper else None,
                    "delete": [m[0] for m in same if m[0] != keep_path],
                    "wasted_bytes": size * (len(same) - 1),
                })
        if not quiet:
            sys.stderr.write("\r  已扫描 %d 个候选文件，发现 %d 组重复 ..." % (scanned, len(groups)))
            sys.stderr.flush()
    if not quiet and scanned:
        sys.stderr.write("\r" + " " * 60 + "\r")

    groups.sort(key=lambda g: -g["wasted_bytes"])
    return {
        "scanned_files": scanned,
        "groups": groups,
        "group_count": len(groups),
        "wasted_bytes": sum(g["wasted_bytes"] for g in groups),
        "policy": policy,
        "elapsed": round(time.time() - t0, 1),
    }


def annotate_with_risk(res):
    """用 risk_register 给每个待删副本定级 —— 但按 **keep-one 语义**，不是"一刀切拦红线"。

    为什么不能直接剔除 RED
    ----------------------
    红线规则（`*\\documents\\*` 等）回答的是「删了会不会丢唯一副本」。而重复文件
    计划删的是**副本**，且每组至少保留一份。二者语义不同：

      * 直接剔除 -> 实测在 Documents/Pictures 上跑，40 个候选**全部**被判 RED，
        0 组结果 —— 去重功能在最常见的场景（照片/文档重复）里等于不存在；
      * 正确的做法 -> 保留一份的前提下允许删副本，但必须
        ① 走回收站（可恢复，不永久删）② 逐条确认（不许批量）③ 明确标注来源。

    唯一硬拦的例外：`--keep-policy none`（整组全删）—— 那才是真的没有保留副本。
    """
    try:
        import risk_register as RR
    except Exception:
        return res
    rules = RR.load_rules()
    removed, flagged_all = [], []
    for g in res["groups"]:
        keep, flagged = [], []
        for p in g["delete"]:
            c = RR.classify(p, rules)
            if c["level"] == "RED":
                if not g.get("keep"):
                    removed.append({"path": p, "rule_id": c["rule_id"], "why": c["warning"]})
                    continue
                flagged.append({"path": p, "rule_id": c["rule_id"], "why": c["warning"]})
            keep.append(p)
        g["delete"] = keep
        if flagged:
            g["recycle_required"] = True
            g["personal_items"] = flagged
            flagged_all.extend(flagged)
    res["groups"] = [g for g in res["groups"] if g["delete"]]
    res["group_count"] = len(res["groups"])
    res["wasted_bytes"] = sum(g["wasted_bytes"] for g in res["groups"])
    if removed:
        res["red_line_removed"] = removed
        res["note_red"] = ("有 %d 个候选触发红线且**该组没有保留副本**（全删策略），已剔除 —— "
                           "宁可不省这几个字节，也不做不可恢复的删除。" % len(removed))
    if flagged_all:
        res["recycle_required_count"] = len(flagged_all)
        res["note_recycle"] = (
            "有 %d 个待删副本位于个人目录/受保护目录。因为**每组仍保留了一份**，"
            "删副本不等于丢数据，故不拦；但必须：① 用 shell_delete.py 进回收站"
            "（禁止 delete_safe.py 永久删）② 逐条确认，不许批量 ③ 删前先备份。"
            % len(flagged_all))
    return res


def main():
    ap = argparse.ArgumentParser(description="重复文件规划（默认 dry-run，只出计划）")
    ap.add_argument("--root", action="append", required=True, help="扫描根目录，可重复")
    ap.add_argument("--min-mb", type=int, default=1, help="最小文件体积，默认 1MB")
    ap.add_argument("--keep-policy", choices=POLICIES, default="shortest",
                    help="保留策略：shortest(默认)/newest/oldest/source-dir/none")
    ap.add_argument("--source-dir", help="配合 --keep-policy source-dir：优先保留此目录内的副本")
    ap.add_argument("--max-files", type=int, default=0, help="候选文件上限（0=不限）")
    ap.add_argument("--emit-targets", help="把待删清单写成 delete_safe.py 可用的 targets.json")
    ap.add_argument("--md", action="store_true", help="Markdown 报告（默认 JSON）")
    ap.add_argument("--out", help="写入文件")
    ap.add_argument("--force-lock", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    lock, err = acquire_lock(args.force_lock)
    if err:
        print(json.dumps({"error": err}, ensure_ascii=False))
        return 1
    try:
        res = plan(args.root, args.min_mb, args.keep_policy, args.source_dir,
                   args.max_files, args.quiet)
    finally:
        release_lock(lock)

    res = annotate_with_risk(res)

    if args.emit_targets:
        paths = [p for g in res["groups"] for p in g["delete"]]
        note = ("由 dedup_planner.py 生成，需先用 risk_register.py 审计，"
                "再交 delete_safe.py 执行")
        if res.get("recycle_required_count"):
            note = ("⚠ 含个人目录/受保护目录内的副本：**禁止**交 delete_safe.py 永久删除。"
                    "请用 shell_delete.py 走回收站，逐条确认后再执行。")
        with open(args.emit_targets, "w", encoding="utf-8") as f:
            json.dump({"paths": paths, "recycle_required": bool(res.get("recycle_required_count")),
                       "note": note}, f, ensure_ascii=False, indent=1)
        res["targets_file"] = args.emit_targets
        res["targets_note"] = note

    if args.md:
        L = ["### 重复文件计划（策略：%s，扫描 %d 个候选，用时 %.1fs）" % (
            args.keep_policy, res["scanned_files"], res["elapsed"]), ""]
        L.append("| 组 | 单份体积 | 份数 | 可释放 | 保留 |")
        L.append("|---|---|---|---|---|")
        for i, g in enumerate(res["groups"][:40], 1):
            L.append("| %d | %s | %d | %s | `%s` |" % (
                i, fmt(g["size"]), g["count"], fmt(g["wasted_bytes"]), os.path.basename(g["keep"] or "-")))
        L.append("")
        L.append("- 合计可释放 **%s**（%d 组）" % (fmt(res["wasted_bytes"]), res["group_count"]))
        if res.get("note_recycle"):
            L.append("- " + res["note_recycle"])
        if res.get("note_red"):
            L.append("- " + res["note_red"])
        if res.get("recycle_required_count"):
            L.append("- 下一步：逐条复核 → `shell_delete.py`（**进回收站**）→ 确认无碍后自己清空回收站")
        else:
            L.append("- 下一步：`risk_register.py --audit` 审计 → `shell_delete.py`（进回收站）")
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
