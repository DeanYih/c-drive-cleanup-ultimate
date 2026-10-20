#!/usr/bin/env python3
"""WeChat BackupFiles inspector (read-only).

Usage:
    python -S scripts/wechat_backup_inspect.py --root <path-to-Account>/BackupFiles

WeChat on Windows keeps each account's backups under
<root>/<account>/BackupFiles/android_<hex>.  Each backup contains:
  - BAK_0_TEXT            : single text archive (conversations, KB-MB scale)
  - BAK_0_MEDIA .. BAK_N_MEDIA : ~2 GB media segments
  - Backup.db             : encrypted metadata DB

The script walks each backup directory and reports:
  - TEXT bytes (BAK_0_TEXT size)
  - MEDIA bytes per segment (BAK_N_MEDIA size)
  - TOTAL bytes
  - which backups are byte-identical subsets (same TEXT + same MEDIA segment
    sizes) -> a true subset can be safely deleted once the user confirms the
    superset is intact.

Output is JSON.  The script never deletes anything.  Use the JSON to decide
which backup(s) to send to the recycle bin.

Note: BAK_0_TEXT and the BAK_N_MEDIA files are AES-encrypted with per-backup
keys derived from the WeChat account; you cannot read the contents from a
script.  Compare by segment size only.
"""
import os, sys, json, argparse, re
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsutil_safe import iter_files  # noqa: E402  统一实现：不跟随 junction/symlink


SEG_RE = re.compile(r"^BAK_(\d+)_(TEXT|MEDIA)$", re.I)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", required=True,
                   help="BackupFiles directory (must contain android_* subdirs)")
    args = p.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit("ERROR: root not found: " + root)

    # 先确认这**真的**是 BackupFiles：真备份下必然有 BAK_0_TEXT / BAK_N_MEDIA。
    # 不加这道探测的话，随便传个目录（例如整个 Documents）都会输出一堆
    # "total_bytes: 0" 的假备份，看起来成功、实际上什么也没识别到。
    try:
        subdirs = [n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))]
    except OSError as e:
        sys.exit("ERROR: 无法读取 %s: %s" % (root, e))
    looks_like = 0
    for name in subdirs:
        try:
            if any(SEG_RE.match(e) for e in os.listdir(os.path.join(root, name))):
                looks_like += 1
        except OSError:
            pass
    if looks_like == 0:
        print(json.dumps({
            "error": "这不是 BackupFiles 目录，已停止（不输出空壳假结果）",
            "root": root,
            "scanned_subdirs": len(subdirs),
            "detail": "没有任何子目录里存在 BAK_<n>_TEXT / BAK_<n>_MEDIA 文件",
            "hint": ("微信备份在 <账号>\\BackupFiles\\android_xxxxxxxx。"
                     "先用 wechat_analyze.py 定位账号根，再指向其中的 BackupFiles。"),
        }, ensure_ascii=False, indent=2))
        return 1

    out = {}
    signatures = defaultdict(list)

    for name in sorted(os.listdir(root)):
        bp = os.path.join(root, name)
        if not os.path.isdir(bp):
            continue
        text = 0
        media = []
        try:
            entries = sorted(os.listdir(bp))
        except OSError as e:
            out[name] = {"error": "listdir failed: " + str(e)}
            continue
        for ent in entries:
            m = SEG_RE.match(ent)
            if not m:
                continue
            idx = int(m.group(1))
            kind = m.group(2).upper()
            full = os.path.join(bp, ent)
            try:
                if os.path.isfile(full):
                    sz = os.path.getsize(full)
                else:
                    sz = 0
                    # 用 iter_files（不跟随 junction/symlink）代替 os.walk
                    for fp in iter_files(full):
                        try:
                            sz += os.lstat(fp).st_size
                        except OSError:
                            pass
            except OSError:
                sz = -1
            if kind == "TEXT":
                text = sz
            else:
                media.append((idx, sz))
        media.sort()
        total = text + sum(s for _, s in media if s >= 0)
        sig = (text, tuple(s for _, s in media))
        signatures[sig].append(name)
        out[name] = {
            "text_bytes": text,
            "media_segments": [{"index": i, "bytes": s} for i, s in media],
            "total_bytes": total,
            "segment_count": len(media),
        }

    # True subset detection: B is a subset of A if TEXT bytes match and every
    # B segment index exists in A with identical size, and A has strictly
    # more segments than B.
    subsets = {}
    backups = list(out.keys())
    for a in backups:
        for b in backups:
            if a == b:
                continue
            ao = out[a]; bo = out[b]
            if ao.get("text_bytes") != bo.get("text_bytes"):
                continue
            if ao.get("segment_count", 0) <= bo.get("segment_count", 0):
                continue
            a_idx = {m["index"]: m["bytes"] for m in ao.get("media_segments", [])}
            b_idx = {m["index"]: m["bytes"] for m in bo.get("media_segments", [])}
            if all(a_idx.get(i) == s for i, s in b_idx.items()):
                subsets.setdefault(b, []).append(a)

    identical = [sorted(v) for _, v in signatures.items() if len(v) > 1]

    report = {
        "root": root,
        "backups": out,
        "subsets": subsets,
        "identical_groups": identical,
        "decision": {
            "deletable_subsets": sorted(subsets.keys()),
            "supersets_to_keep": sorted(set(b for v in subsets.values() for b in v)),
            "warning": ("Backup files are AES-encrypted; structural "
                        "comparison only. Confirm with the WeChat app's "
                        "'restore backup to phone' feature before deleting "
                        "any backup."),
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()