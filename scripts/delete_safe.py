import os, sys, json, shutil, argparse, stat

# ---- Allowlist (safe to delete). Everything else is rejected. ----
SAFE_DIR_PREFIXES = [
    r"c:\windows\temp",
    r"c:\windows\panther",
    r"c:\windows\logs",
    r"c:\windows\softwaredistribution\download",
    r"c:\windows\system32\codecache",
    r"c:\programdata\microsoft\windows\wer",
]
JUNK_EXT = (".dmp", ".tmp", ".log")

# Windows reparse point (junction / symlink / mount point) 标记位。
# os.path.islink() 对 junction 返回 False（只对 symlink 为真），必须用 lstat 的属性位判断。
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def norm(p):
    return os.path.abspath(p).lower().replace("/", "\\")


def is_reparse_point(path):
    """True if path itself is a junction / symlink / mount point.

    安全性关键：白名单目录（如 %TEMP%）下可能存在指向个人目录的 junction，
    若把它当普通目录 rmtree，取决于 Python 版本/平台行为可能穿透删除目标内容。
    必须显式识别并只删除链接本身。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def find_reparse_points(root):
    """列出 root 子树内所有 reparse point（不跟随它们）。用于删除前预警。"""
    found = []
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            if is_reparse_point(e.path):
                found.append(e.path)
            elif e.is_dir(follow_symlinks=False):
                stack.append(e.path)
    return found


def is_allowed(path):
    n = norm(path)
    # whole-dir safe roots
    for pre in SAFE_DIR_PREFIXES:
        if n == pre or n.startswith(pre + "\\"):
            return True
    # per-user safe subdirs：路径形如 c:\users\<用户名>\appdata\local\temp
    # 注意：norm() 产出单反斜杠；必须按段解析出用户名，再匹配其后的安全子路径
    if n.startswith("c:\\users\\"):
        rest = n[len("c:\\users\\"):]          # 例如 qiu\appdata\local\temp\foo.tmp
        parts = rest.split("\\", 1)
        if len(parts) == 2 and parts[0]:       # parts[0]=用户名, parts[1]=其下相对路径
            rel = parts[1]
            # 固定白名单子目录（整目录可删，均可自动重建）
            safe_subs = [
                "appdata\\local\\temp",
                "appdata\\local\\pip\\cache",
                "appdata\\local\\npm-cache",
                "appdata\\local\\crashdumps",
                "appdata\\local\\nvidia\\dxcache",
                "appdata\\local\\nvidia\\glcache",
                "appdata\\local\\d3dscache",
            ]
            if rel in safe_subs or any(rel.startswith(s + "\\") for s in safe_subs):
                return True
            # 浏览器缓存：只允许 user data\<任意Profile>\cache 与 \code cache，
            # 其余（登录态/书签/扩展等）不碰
            for browser in ("appdata\\local\\microsoft\\edge\\user data\\",
                            "appdata\\local\\google\\chrome\\user data\\"):
                if rel.startswith(browser):
                    tail = rel[len(browser):]          # <Profile>\cache\...
                    seg = tail.split("\\", 2)
                    if len(seg) >= 2 and seg[0] and seg[1] in ("cache", "code cache"):
                        return True
    # ProgramData junk files only (never QQPCMgr, never whole dirs)
    if n.startswith("c:\\programdata\\") and "qqpcmgr" not in n:
        if os.path.isfile(path) and n.endswith(JUNK_EXT):
            return True
    return False


def schedule_reboot_delete(path):
    """Try to queue a delete-at-reboot via PendingFileRenameOperations."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager",
            0, winreg.KEY_READ | winreg.KEY_WRITE)
        try:
            cur, _ = winreg.QueryValueEx(key, "PendingFileRenameOperations")
            if isinstance(cur, str):
                cur = [cur]
        except FileNotFoundError:
            cur = []
        src = "\\??\\" + os.path.abspath(path)
        cur = list(cur) + [src, ""]
        winreg.SetValueEx(key, "PendingFileRenameOperations", 0,
                          winreg.REG_MULTI_SZ, cur)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


def unlink_reparse_point(path):
    """只删除 junction/symlink 本身，绝不递归其指向的内容。

    junction 是目录，必须用 RemoveDirectoryW（os.rmdir 在其上可用）；
    symlink/文件用 os.unlink。
    """
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        os.rmdir(path)          # 目录型链接(junction)：只摘链接
    else:
        os.unlink(path)         # 文件型链接
    return True


def delete_one(path, confirm):
    if not is_allowed(path):
        return (path, "SKIPPED", "not in allowlist")
    if not os.path.exists(path) and not os.path.islink(path):
        return (path, "SKIPPED", "not found")
    # --- 护栏1：目标是 reparse point -> 只删链接本身 ---
    if is_reparse_point(path):
        if not confirm:
            return (path, "DRYRUN", "reparse point (junction/symlink) - would unlink link only, NOT its target")
        try:
            unlink_reparse_point(path)
            return (path, "UNLINKED_LINK", "removed junction/symlink itself; target untouched")
        except OSError as e:
            return (path, "LOCKED", "unlink link failed: %s" % e)
    # --- 护栏2：普通目录但子树内含 reparse point -> 需人工介入，不盲删 ---
    if os.path.isdir(path):
        links = find_reparse_points(path)
        if links:
            return (path, "SKIPPED",
                    "contains %d reparse point(s); refusing blind recursive delete - "
                    "review manually: %s" % (len(links), "; ".join(links[:5])))
    if not confirm:
        return (path, "DRYRUN", "would delete")
    try:
        if os.path.isfile(path):
            os.remove(path)
        else:
            shutil.rmtree(path)
        return (path, "DELETED", "")
    except (PermissionError, OSError) as e:
        # locked -> try queue reboot delete
        if schedule_reboot_delete(path):
            return (path, "QUEUED_REBOOT", str(e))
        return (path, "LOCKED", str(e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True, help="JSON file with {\"paths\":[...]}")
    ap.add_argument("--confirm", action="store_true", help="actually delete (default dry-run)")
    args = ap.parse_args()

    with open(args.targets, "r", encoding="utf-8") as f:
        data = json.load(f)
    paths = data.get("paths", [])

    results = []
    for p in paths:
        results.append(delete_one(p, args.confirm))

    deleted = sum(1 for r in results if r[1] == "DELETED")
    unlinked = sum(1 for r in results if r[1] == "UNLINKED_LINK")
    queued = sum(1 for r in results if r[1] == "QUEUED_REBOOT")
    skipped = sum(1 for r in results if r[1] in ("SKIPPED", "LOCKED"))
    print(json.dumps({
        "confirm": args.confirm,
        "total": len(results),
        "deleted": deleted,
        "unlinked_links": unlinked,
        "queued_reboot": queued,
        "skipped_or_locked": skipped,
        "details": [{"path": r[0], "status": r[1], "info": r[2]} for r in results],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
