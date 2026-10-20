#!/usr/bin/env python3
"""WeChat dedup executor - sends duplicates to the Windows Recycle Bin.

Usage:
    # dry-run: show what would be sent
    python -S scripts/wechat_dedup_recycle.py --plan file_dedup_plan.json

    # actually send (recoverable via the Recycle Bin)
    python -S scripts/wechat_dedup_recycle.py --plan file_dedup_plan.json --confirm

The plan is produced by scripts/wechat_dedup_plan.py.  Files are processed
in batches of 10 (configurable via --batch N).  After each batch the script
re-checks os.path.exists on every path so a stuck/locked file is reported
instead of silently skipped.

Recycle Bin semantics:
  - FOF_ALLOWUNDO: files end up in the Recycle Bin (recoverable from the
    Windows desktop), they are NOT permanently deleted.
  - Bypass sitecustomize hooks by running with `python -S`.
  - Never use `rm -rf` / `del /S /Q` / `os.unlink` here - we want
    recoverability, not irreversibility.
"""
import os, sys, json, argparse, ctypes
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsutil_safe import is_under  # noqa: E402

# 红线：微信加密聊天库永不可删（技能安全红线第 1 条）
PROTECTED_MARKERS = ("\\msg\\", "\\msg\\attach\\")
PROTECTED_EXT = (".db", ".sqlite", ".db-wal", ".db-shm")


def violation(path, root):
    """返回拒绝理由字符串；None 表示放行。

    纵深防御：计划文件是被删除动作的「命令」，不能盲信 ——
    若计划是旧版本或用 os.walk 生成的（会跟随 junction），
    可能混入目录树之外、或 Msg/*.db 这类红线文件。
    """
    if not root:
        return "plan 缺少 root 字段，无法校验归属（拒绝执行）"
    if not is_under(path, root):
        return "不在计划 root 之下（疑似链接跟随导致的越界）"
    low = path.lower()
    if any(m in low for m in PROTECTED_MARKERS):
        return "位于 Msg/ 加密聊天库路径下（红线）"
    if low.endswith(PROTECTED_EXT):
        return "是 .db/.sqlite 数据库文件（红线）"
    return None


FO_DELETE = 0x0003
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT = 0x0004
FOF_NOERRORUI = 0x0400


class SHFILEOPSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def send_to_recycle(paths):
    pfrom = "\0".join(paths) + "\0\0"
    op = SHFILEOPSTRUCT()
    op.wFunc = FO_DELETE
    op.pFrom = pfrom
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc, bool(op.fAnyOperationsAborted)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", required=True, help="file_dedup_plan.json produced by wechat_dedup_plan.py")
    p.add_argument("--batch", type=int, default=10, help="files per SHFileOperation batch")
    p.add_argument("--confirm", action="store_true",
                   help="actually send files to Recycle Bin (default: dry-run)")
    args = p.parse_args()

    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)

    todos = plan.get("to_delete", [])
    if not todos:
        sys.exit("plan has no to_delete entries")

    # --- 归属与红线校验（先全部过滤，再进入删除流程） ---
    root = plan.get("root")
    rejected, clean = [], []
    for t in todos:
        why = violation(t.get("delete_path", ""), root)
        if why:
            rejected.append((t.get("delete_path", ""), why))
        else:
            clean.append(t)
    if rejected:
        print("!! 计划中有 %d 项未通过安全校验，已剔除（不执行）:" % len(rejected))
        for p, why in rejected[:10]:
            print("   %s  <- %s" % (p, why))
        if len(rejected) > 10:
            print("   ... 另有 %d 项" % (len(rejected) - 10))
        print("   提示：若计划由旧版脚本生成，请用当前 wechat_dedup_plan.py 重新生成。")
    todos = clean
    if not todos:
        sys.exit("安全校验后无可执行项，已中止（未删除任何文件）")

    BATCH = max(1, args.batch)
    print("Plan summary:")
    print("  root            : %s" % root)
    print("  files to delete : %d" % len(todos))
    print("  bytes reclaimable: %d (%.2f GB)" % (plan.get("to_delete_bytes", 0),
                                                 plan.get("to_delete_bytes", 0) / 1e9))
    print("  batch size      : %d" % BATCH)
    print("  mode            : %s" % ("RECYCLE (recoverable)" if args.confirm else "DRY-RUN (no changes)"))

    if not args.confirm:
        print("\nDRY-RUN: re-run with --confirm to actually send these files to the Recycle Bin.")
        for d in todos[:5]:
            print("  would recycle :", d["delete_path"])
        if len(todos) > 5:
            print("  ... and %d more" % (len(todos) - 5))
        return

    ok = 0
    failed = []
    for i in range(0, len(todos), BATCH):
        batch = todos[i:i + BATCH]
        paths = [b["delete_path"] for b in batch]
        _rc, _aborted = send_to_recycle(paths)
        for p in paths:
                if not os.path.exists(p):
                    ok += 1
                else:
                    failed.append(p)
        print("  batch %02d -> removed %d / %d" % (i // BATCH + 1, ok, len(todos)))

    print("\n=== DONE ===")
    print("removed=%d  failed=%d" % (ok, len(failed)))
    if failed:
        print("FAILED (likely locked by WeChat - try again after quitting WeChat):")
        for f in failed[:20]:
            print("   ", f)
        if len(failed) > 20:
            print("   ... and %d more" % (len(failed) - 20))


if __name__ == "__main__":
    main()