# -*- coding: utf-8 -*-
"""应用数据跨盘迁移 + junction 永久生效（留底回滚版）
用法:
  预检:   python migrate_with_junction.py --check <源目录> <目标目录>   # 只判定，不动手
  迁移:   python migrate_with_junction.py <源目录> <目标目录> [--process 进程名1,进程名2]
          [--allow-junction-copy]   # 源内含 junction 时需显式开启（默认拒绝）
          [--force-non-ntfs]        # 目标盘是 FAT32/exFAT 时放行（只适合媒体等冷数据）
  验证:   python migrate_with_junction.py --verify <源目录>
  巡检:   python migrate_with_junction.py --audit     # 检查台账里所有 junction 是否还活着
  回滚:   python migrate_with_junction.py --rollback <源目录>
  清底:   python migrate_with_junction.py --delete-backup <源目录>   # 用户确认应用正常后才执行

迁移流程（安全优先，源目录永不直接删除）:
  0. 预检（任何一项不过就中止，零改动）：
     源/目标文件系统必须 NTFS（junction 无法跨文件系统语义正确工作）、
     目标必须是固定本地盘（U盘/网络盘拒绝）、目标盘余量 ≥ 源体积 105%、
     路径 < 248 字符、目标不能在源内部、源内不能含 junction（默认）、
     源含 SQLite 库 + wal/shm 或文件数过万时给"热数据"警告
  1. 结束占用进程（--process 可选）
  2. 复制源目录全部内容到目标目录（失败自动清理残留副本）
  3. 校验: 文件数 + 总字节数一致（不一致立即中止，源目录原样保留）
  4. 源目录【改名留底】为 <源目录>.pre-junction-bak（同盘原子操作，不删任何数据）
  5. _winapi.CreateJunction(目标, 源) 创建目录链接
  6. 自动验证: reparse 标记 + 原路径可见内容 + 经 junction 写读测试 + 指向确为目标
  7. 登记迁移台账（%LOCALAPPDATA%\\c-drive-cleanup-ultimate\\junctions.json）
  8. 输出【空间账】+【冷启动验证清单】+ 回滚命令

为什么必须预检（每一条都是真实事故换来的）:
  - 目标盘是 FAT32/exFAT：junction 能建上，但 FAT32 无 ACL、文件锁不可靠，
    SQLite 并发直接崩，表现为"越来越慢 + 权限错误"，极难定位；
  - 目标盘余量不足：复制到一半撑爆，源目录虽保住，但目标盘被写满；
  - 目标是 U 盘/移动硬盘：拔盘即断链，应用当场打不开数据；
  - 源含 SQLite 库：junction 让数据库锁跨卷，且大版本升级可能重置重解析点。

为什么需要台帐 + --audit（headline 失效原因）:
  junction 会被磁盘清理 / 存储感知 / 应用更新器 / 大版本升级**悄悄换回真实目录**，
  数据回到 C 盘、空间悄悄回涨，用户完全无感。只有台账能回答
  「我迁过什么、现在还有几个活着」。

保证"软件再次打开一如既往"的关键纪律:
  - 任何一步失败 → 中止，源目录原样，零影响
  - 迁移完成但应用异常 → --rollback 一条命令恢复原状（删联接 + 改名还原，秒级）
  - 冷启动验证通过 + 观察期无异常 → 才清理留底

关于源目录内的 junction（重要）:
  shutil.copytree 默认会**跟随**链接，把链接指向的外部数据实体复制过去。
  这会造成：目标盘多出一份真副本（浪费空间、可能撑爆）、丢失链接语义；
  若链接指向自身或上级目录还会无限递归。故默认拒绝迁移，需 --allow-junction-copy 显式确认。
"""
import os
import sys
import json
import time
import shutil
import ctypes
import subprocess
import _winapi  # Windows 内置模块：CreateJunction / 建 NTFS 目录联接

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsutil_safe import (  # noqa: E402  统一实现：不跟随 junction/symlink
    count_tree,
    find_reparse_points,
    is_reparse_point,
    resolve_link_target,
)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
TEST_FILE = "__junction_write_test.tmp"


def shell_delete(path, to_recycle_bin=True):
    from ctypes import wintypes as _wt

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", _wt.HWND), ("wFunc", ctypes.c_uint),
            ("pFrom", _wt.LPCWSTR), ("pTo", _wt.LPCWSTR),
            ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", _wt.BOOL),
            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", _wt.LPCWSTR),
        ]

    op = SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = path + "\x00\x00"
    flags = 0x0004 | 0x0010 | 0x0400  # SILENT | NOCONFIRMATION | NOERRORUI
    if to_recycle_bin:
        flags |= 0x0040  # ALLOWUNDO
    op.fFlags = flags
    op.fAnyOperationsAborted = False
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))


def kill_processes(names):
    for name in names:
        subprocess.run(["taskkill", "/im", name], capture_output=True)
    time.sleep(3)
    for name in names:
        subprocess.run(["taskkill", "/f", "/im", name], capture_output=True)
    time.sleep(2)


def tree_stats(path):
    """(文件数, 字节数) —— 不跟随 junction/symlink（统一实现见 fsutil_safe）。

    必须不跟随：os.walk / 裸 iterate 会把链接指向的外部数据一起算进来，
    于是「源」和「目标」两侧同样虚高，校验差异被掩盖 -> 静默通过。
    """
    return count_tree(path)


def is_junction(path):
    return is_reparse_point(path)


def resolve_target(junction_path):
    """读取 junction 指向的真实路径。

    注意：CPython 的 _winapi **没有** ReadJunction（只有 CreateJunction），
    旧版代码用 hasattr 兜底后 target 恒为 None，使写读测试退化成
    「写一个文件再读同一个文件」，永远通过 —— 假绿灯。
    这里改用 os.path.realpath（Windows 上走 GetFinalPathNameByHandleW）。
    """
    return resolve_link_target(junction_path)


def write_read_test(junction_path, target):
    """经原路径(即 junction)写入测试文件，从目标盘路径读回，再清理。"""
    p = os.path.join(junction_path, TEST_FILE)
    data = b"junction-ok-%d" % time.time_ns()
    try:
        with open(p, "wb") as f:
            f.write(data)
        tp = os.path.join(target, TEST_FILE) if target else p
        with open(tp, "rb") as f:
            ok = f.read() == data
        return ok
    except OSError:
        return False
    finally:
        for cand in {p, os.path.join(target, TEST_FILE) if target else p}:
            try:
                if os.path.exists(cand):
                    os.remove(cand)
            except OSError:
                # 沙箱可能把它送进回收站，不影响判定
                pass


def remove_junction(path):
    """RemoveDirectoryW 只删除 junction 链接本身，不碰目标数据。"""
    ok = ctypes.windll.kernel32.RemoveDirectoryW(ctypes.c_wchar_p(path))
    return bool(ok) and not os.path.exists(path)


def cmd_rollback(src):
    bak = src.rstrip("\\/") + ".pre-junction-bak"
    if not is_junction(src):
        print(f"❌ {src} 不是 junction，拒绝回滚（避免误操作真实目录）")
        return 1
    if not os.path.isdir(bak):
        print(f"❌ 留底目录不存在: {bak}，无法回滚。junction 指向的目标数据仍在，"
              f"如需保留新位置数据请勿手动删目标盘目录。")
        return 1
    # 回滚前先记下目标盘副本规模：回滚后它就成了"孤儿"，不提醒就会变成新的垃圾
    target = resolve_target(src)
    tf = ts = 0
    if target and os.path.isdir(target):
        tf, ts = tree_stats(target)
    if not remove_junction(src):
        print(f"❌ junction 删除失败: {src}（可能被进程占用，先关闭相关软件再试）")
        return 1
    os.rename(bak, src)
    print(f"✅ 回滚完成: 已删除 junction 并把留底还原为原目录 {src}（数据回到 C 盘原位）")
    if target and ts:
        print("====== 别忘了这一步 ======")
        print(f"目标盘上的迁移副本已成孤儿: {target}")
        print(f"  占用 {tf} 文件 / {ts / 1024**2:.1f} MB —— 不清掉的话，"
              f"你等于白搬一趟还多占了一份磁盘。")
        print(f"  确认回滚后软件一切正常，再删除它（先看一眼再接 delete_safe）：")
        print(f'    python -S "{os.path.join(os.path.dirname(os.path.abspath(__file__)), "shell_delete.py")}" "{target}"')
    return 0


def cmd_delete_backup(src):
    bak = src.rstrip("\\/") + ".pre-junction-bak"
    if not is_junction(src):
        print(f"❌ {src} 当前不是 junction，留底清理无意义，拒绝执行")
        return 1
    if not os.path.isdir(bak):
        print(f"留底目录不存在（可能已清理过）: {bak}")
        return 0
    code = shell_delete(bak, to_recycle_bin=True)  # 留底进回收站，再多一层保险
    print(("✅ 留底已送入回收站: " + bak) if not os.path.exists(bak)
          else f"❌ 清理未完成（码 {code}），可稍后重试或手动处理")
    return 0


def cmd_verify(src):
    src = os.path.normpath(os.path.abspath(src))
    if not is_junction(src):
        print(f"❌ {src} 不是 junction")
        return 1
    target = resolve_target(src)
    try:
        n = len(os.listdir(src))
    except OSError as e:
        print(f"❌ 无法读取 {src}: {e}")
        return 1
    print(f"junction: {src}")
    if target:
        drive = os.path.splitdrive(target)[0] or "?"
        print(f"  指向   : {target}   (位于 {drive} 盘)")
        # 真正有效的写读测试：经原路径(junction)写入，从【解析出的目标盘路径】读回
        w_ok = write_read_test(src, target)
    else:
        # 解析不出指向时验证强度会降级为「原路径可读写」，无法证明指向何处 —— 必须讲清楚
        print("  指向   : ⚠ 无法解析 junction 指向（验证强度降级为「原路径可读写」）")
        w_ok = write_read_test(src, None)
    print(f"  可见项 : {n}")
    print(f"  写读测试: {'✅ 通过' + ('（经原路径写入已落在 ' + (target or '') + '）' if target else '（仅证明原路径可读写）') if w_ok else '❌ 失败'}")
    if target and (os.path.splitdrive(target)[0] or "").upper() == (os.path.splitdrive(src)[0] or "").upper():
        print("  ⚠ 提示   : 指向与源在同一盘符，迁移未达到跨盘目的，请复核目标路径")
    bak = src.rstrip("\\/") + ".pre-junction-bak"
    print(f"  留底   : {'仍在 ' + bak + '（确认软件正常后可 --delete-backup）' if os.path.isdir(bak) else '已清理'}")
    if os.path.isdir(bak):
        bf, bs = tree_stats(bak)
        print(f"  空间账 : C 盘**尚未释放**，留底仍占 {bs / 1024**2:.1f} MB；"
              f"清留底后才释放同额（留底是回滚的唯一依据）")
    return 0 if w_ok else 1


# ---------------------------------------------------------------- 迁移预检
# 为什么要这么多前置闸门：迁移失败最坏的结果不是报错，而是「看起来成功」——
# junction 建上了、应用照样崩、或者过两天被磁盘清理悄悄换回真实目录、
# C 盘又涨回去。所以凡是能提前静态判断的，一律在动手前拦掉并给错误码。
DRIVE_FIXED = 3
MIN_FREE_RATIO = 1.05      # 目标盘至少留 5% 余量
MAX_PATH_LEN = 248         # 保守值：部分 Win32 API 在 260 处失败，去掉文件名余量

ERR = {
    "E0101": "目标不是固定本地盘（U盘/移动硬盘/网络盘）",
    "E0102": "目标路径已存在且非空",
    "E0103": "目标盘可用空间不足（需源体积的 105%）",
    "E0104": "目标盘文件系统不是 NTFS —— junction 目标必须是 NTFS",
    "E0201": "源路径不存在或不是目录",
    "E0202": "源路径已经是 junction（像已迁移过，用 --verify 查看）",
    "E0203": "源目录内含 junction/symlink（需 --allow-junction-copy 显式放行）",
    "E0204": "路径长度达到 248 字符（部分 API 会失败）",
    "E0205": "源卷文件系统不是 NTFS —— 无法在原位置创建 junction",
    "E0207": "目标路径位于源目录内部（会无限递归复制）",
}
WARN = {
    "W0206": "源目录含会话类热数据（SQLite 库/海量小文件）：junction 会让 SQLite "
             "锁与并发控制跑在另一个卷上，且大版本升级可能重置重解析点。"
             "建议只迁大块冷数据（媒体/模型权重），关键库留在系统盘。",
    "W0105": "目标盘不是 NTFS，已按 --force-non-ntfs 放行：FAT32/exFAT 无 ACL、"
             "文件锁不可靠，只适合放媒体/模型这类整块冷数据。",
}


def volume_fs_and_type(path):
    """返回 (文件系统名, 驱动器类型)。DRIVE_FIXED=3。"""
    root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    dtype = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
    buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), None, 0, None, None, None, buf, 261)
    return (buf.value if ok else "?"), dtype


def free_bytes(path):
    import ctypes as _c
    root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    free = _c.c_ulonglong(0)
    total = _c.c_ulonglong(0)
    _c.windll.kernel32.GetDiskFreeSpaceExW(
        _c.c_wchar_p(root), None, _c.byref(total), _c.byref(free))
    return free.value


def scan_stats(path):
    """一次遍历同时拿到 (文件数, 字节数, sqlite 库数, 有 wal/shm 的库数)。"""
    from fsutil_safe import iter_files
    n = b = db = wal = 0
    names = set()
    for fp in iter_files(path):
        try:
            b += os.lstat(fp).st_size
        except OSError:
            pass
        n += 1
        low = fp.lower()
        if low.endswith((".db", ".sqlite", ".sqlite3")):
            db += 1
            names.add(os.path.basename(low))
        elif low.endswith(("-wal", "-shm")):
            wal += 1
    return n, b, db, wal


def preflight(src, dst, allow_junction_copy=False, force_non_ntfs=False):
    """返回 (errors, warnings, stats)。errors 非空就必须中止。"""
    errs, warns = [], []
    src = os.path.normpath(os.path.abspath(src))
    dst = os.path.normpath(os.path.abspath(dst))

    if not os.path.isdir(src):
        errs.append(("E0201", src))
        return errs, warns, None
    if is_junction(src):
        errs.append(("E0202", src))
        return errs, warns, None
    for p in (src, dst):
        if len(p) >= MAX_PATH_LEN:
            errs.append(("E0204", "%d 字符: %s" % (len(p), p)))
    if os.path.normcase(dst).startswith(os.path.normcase(src) + os.sep):
        errs.append(("E0207", dst))

    sfs, _ = volume_fs_and_type(src)
    if sfs.upper() != "NTFS":
        errs.append(("E0205", "%s 位于 %s" % (src, sfs)))
    dfs, dtype = volume_fs_and_type(dst)
    if dtype != DRIVE_FIXED:
        errs.append(("E0101", "%s（驱动器类型 %d，需 3=固定盘）" % (dst, dtype)))
    if dfs.upper() != "NTFS":
        if force_non_ntfs:
            warns.append(("W0105", "%s 位于 %s" % (dst, dfs)))
        else:
            errs.append(("E0104", "%s 位于 %s" % (dst, dfs)))

    if os.path.exists(dst):
        try:
            if os.listdir(dst):
                errs.append(("E0102", dst))
        except OSError:
            pass

    n, b, db, wal = scan_stats(src)
    stats = {"files": n, "bytes": b, "db": db, "wal": wal}
    need = int(b * MIN_FREE_RATIO)
    fb = free_bytes(dst)
    if fb < need:
        errs.append(("E0103", "需要 %.2f GB（含 5%% 余量），可用 %.2f GB"
                     % (need / 1024.0 ** 3, fb / 1024.0 ** 3)))

    links = find_reparse_points(src, limit=5)
    if links and not allow_junction_copy:
        errs.append(("E0203", "; ".join(links[:3])))

    # 热数据：有 SQLite 库且伴随 -wal/-shm，或文件数过万（小文件+数据库都是 junction 敏感型）
    if (db and wal) or n > 50000:
        warns.append(("W0206", "文件 %d 个、SQLite 库 %d 个、wal/shm %d 个" % (n, db, wal)))
    return errs, warns, stats


def print_preflight(errs, warns):
    for code, detail in errs:
        print("  ❌ [%s] %s —— %s" % (code, ERR.get(code, ""), detail))
    for code, detail in warns:
        print("  ⚠  [%s] %s\n      实测: %s" % (code, WARN.get(code, ""), detail))
    return 1 if errs else 0


def migrate(src, dst, processes, allow_junction_copy=False, force_non_ntfs=False):
    # 规范化：消灭 shell 传参可能带来的 //、混合斜杠等问题（junction 目标必须是干净绝对路径）
    src = os.path.normpath(os.path.abspath(src))
    dst = os.path.normpath(os.path.abspath(dst))

    # 统一预检：文件系统 / 固定盘 / 空间余量 / 路径长度 / 源内链接 / 热数据
    errs, warns, stats = preflight(src, dst, allow_junction_copy, force_non_ntfs)
    if errs:
        print("❌ 迁移预检未通过（未做任何改动）：")
        print_preflight(errs, warns)
        return 1
    if warns:
        print("⚠ 迁移预检警告：")
        print_preflight([], warns)

    bak = src.rstrip("\\/") + ".pre-junction-bak"
    if os.path.exists(bak):
        print(f"留底已存在，请先处理（--rollback 或手动）: {bak}")
        return 1

    links = find_reparse_points(src)
    if links and allow_junction_copy:
        print(f"⚠ --allow-junction-copy 已启用：{len(links)} 个链接将被实体复制（不再是链接）")

    if processes:
        print(f"结束进程: {processes}")
        kill_processes(processes)

    sf, ss = stats["files"], stats["bytes"]
    print(f"源: {sf} 文件 / {ss / 1024**2:.1f} MB"
          f"（NTFS 预检通过，目标盘余量 %.1f GB）" % (free_bytes(dst) / 1024.0 ** 3))

    print("复制中（跨盘，大目录耗时较长）...")
    try:
        shutil.copytree(src, dst)
    except Exception as e:
        # 复制失败（路径超长/空间不足/权限）必须捕获：否则崩溃并留下残缺目标，
        # 下次重跑还会因「目标已存在」被拒。
        print(f"❌ 复制失败: {type(e).__name__}: {e}")
        print("   正在清理目标盘残留副本...")
        shutil.rmtree(dst, ignore_errors=True)
        print(f"   残留已清理: {not os.path.exists(dst)}    源目录未做任何改动。")
        return 1

    df, ds = tree_stats(dst)
    print(f"目标: {df} 文件 / {ds / 1024**2:.1f} MB")
    if (df, ds) != (sf, ss):
        print("❌ 校验不一致！源目录原样保留、未做任何改动，请人工核查目标盘副本后删除重试")
        return 1
    print("✅ 复制校验一致（文件数 + 字节数）")

    # 关键差异：源目录改名留底，绝不直接删除
    try:
        os.rename(src, bak)
    except OSError as e:
        print(f"❌ 源目录改名失败（可能仍被进程锁定）: {e}")
        print("   未做任何破坏性改动；请关闭相关软件后重跑。")
        return 1

    try:
        _winapi.CreateJunction(dst, src)
    except Exception as e:
        print(f"❌ junction 创建失败: {e}，正在自动回滚...")
        os.rename(bak, src)
        print("✅ 已还原：源目录恢复原位（数据在 C 盘原位 + 目标盘多一份副本，可自行删）")
        return 1

    # 验证：① 原路径可达且有内容；② 经原路径写读落在目标盘；③ 指向确实是 dst
    ok = os.path.isdir(src)
    items = os.listdir(src) if ok else []
    w_ok = write_read_test(src, dst)
    resolved = resolve_target(src)
    points_right = bool(resolved) and (
        os.path.normcase(os.path.normpath(resolved)) == os.path.normcase(os.path.normpath(dst)))
    # 注意：items 允许为 0（空目录迁移是合法的，旧版把它当失败会误回滚整个成功的迁移）
    if not (ok and w_ok and points_right):
        print("❌ 迁移后验证失败，正在自动回滚...")
        if not points_right:
            print(f"   指向校验失败: 解析得 {resolved!r}，期望 {dst!r}")
        remove_junction(src)
        os.rename(bak, src)
        print("✅ 已还原：源目录恢复原位")
        return 1

    # 登记台账：以后 --audit 才能发现"junction 被悄悄换回真实目录"这种静默失效
    try:
        ledger_upsert(src, dst, sf, ss)
    except OSError as e:
        print("   ⚠ 台账写入失败（不影响迁移结果）: %s" % e)

    print(f"✅ 迁移完成: {src} -> {dst}")
    print(f"   经原路径可见 {len(items)} 项，写读测试通过")
    print()
    # 必须讲清的一笔账：留底还在源盘，所以**此刻 C 盘一个字节都没释放**。
    # 不写这句，用户会以为迁移完空间就到账了，回头发现"容量没变"认为工具骗人。
    print("====== 空间账（重要）======")
    print(f"当前 C 盘释放: 0 字节 —— 源目录被改名为留底 {bak}，仍在 C 盘占 {ss / 1024**2:.1f} MB")
    print(f"清留底后才真正释放: {ss / 1024**2:.1f} MB（目标盘已多占同等体积，请确认目标盘余量）")
    print("为什么这么设计：留底是回滚的唯一依据。留底一删，回滚就没了。")
    print()
    print("====== 冷启动验证清单（交给用户，逐项确认）======")
    print("1. 重新打开该软件，确认能正常启动、正常加载原有数据")
    print("   （微信能加载聊天列表/打开旧图片；QQ 能进会话；编辑器能打开历史项目等）")
    print("2. 产生一条新数据（发个消息/存个文件/下载个包），确认运行如常")
    print("3. 确认新数据落在目标盘:", dst)
    print()
    print("====== 回滚与清理 ======")
    print(f"回滚（软件有任何异常就执行）: python {os.path.abspath(__file__)} --rollback \"{src}\"")
    print(f"验证: python {os.path.abspath(__file__)} --verify \"{src}\"")
    print(f"留底: {bak}  ← 建议保留 ≥7 天，软件一切正常后再执行:")
    print(f"       python {os.path.abspath(__file__)} --delete-backup \"{src}\"")
    return 0


# ---------------------------------------------------------------- 迁移台账与巡检
# 为什么需要台账：junction 会被「悄悄干掉」——磁盘清理 / 存储感知 / 应用更新器
# / 大版本升级都可能把重解析点换成真实目录，于是数据回到 C 盘、C 盘空间悄悄回涨，
# 而用户完全不知道发生过什么。没有台账就没人能回答「我这台机器迁过什么、
# 现在还有几个活着」。实测这是迁移类方案的头号失效原因。
def ledger_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "c-drive-cleanup-ultimate")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "junctions.json")


def ledger_load():
    p = ledger_path()
    if not os.path.exists(p):
        return {"version": 1, "items": []}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"version": 1, "items": []}


def ledger_save(d):
    p = ledger_path()
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def ledger_upsert(src, dst, files, nbytes):
    d = ledger_load()
    key = os.path.normcase(os.path.normpath(src))
    d["items"] = [i for i in d["items"]
                  if os.path.normcase(os.path.normpath(i.get("src", ""))) != key]
    d["items"].append({
        "src": os.path.normpath(src), "dst": os.path.normpath(dst),
        "at": time.strftime("%Y-%m-%d %H:%M"), "files": files, "bytes": nbytes,
    })
    ledger_save(d)


def ledger_remove(src):
    d = ledger_load()
    key = os.path.normcase(os.path.normpath(src))
    before = len(d["items"])
    d["items"] = [i for i in d["items"]
                  if os.path.normcase(os.path.normpath(i.get("src", ""))) != key]
    if len(d["items"]) != before:
        ledger_save(d)


def cmd_audit():
    """巡检所有登记过的 junction：还活着吗？有没有已经悄悄失效？

    四种状态必须分开报，因为处理方式完全不同：
      OK        仍是指向原目标的 junction
      BROKEN    已经被换成真实目录 —— 数据回来了，C 盘被悄悄吃掉（最需要告警）
      RETARGET  还是 junction 但指向变了（盘符漂移 / 手动改过）
      MISSING   原路径不存在
    """
    d = ledger_load()
    items = d.get("items", [])
    if not items:
        print("台账为空：本机没有通过本技能迁移过的目录。")
        print("台账位置: %s" % ledger_path())
        return 0

    print("迁移台账巡检  %s" % time.strftime("%Y-%m-%d %H:%M"))
    print("台账: %s" % ledger_path())
    print("-" * 72)
    bad = 0
    for it in items:
        src, dst = it.get("src", ""), it.get("dst", "")
        bak = src.rstrip("\\/") + ".pre-junction-bak"
        if not os.path.exists(src):
            state, extra = "MISSING", "原路径不存在"
        elif not is_junction(src):
            state = "BROKEN"
            extra = "已不是 junction（多半被磁盘清理/更新器/系统升级换回真实目录）"
            bad += 1
        else:
            cur = resolve_target(src)
            if cur and os.path.normcase(os.path.normpath(cur)) != os.path.normcase(os.path.normpath(dst)):
                state, extra = "RETARGET", "现在指向 %s" % cur
                bad += 1
            else:
                state, extra = "OK", "指向 %s" % dst
        mark = {"OK": "✅", "BROKEN": "❌", "RETARGET": "⚠ ", "MISSING": "❌"}[state]
        print("%s %-8s %s" % (mark, state, src))
        print("          %s" % extra)
        if it.get("files"):
            print("          迁移时: %d 文件 / %.1f MB（%s）"
                  % (it["files"], it.get("bytes", 0) / 1024.0 ** 2, it.get("at", "?")))
        if os.path.isdir(bak):
            print("          留底仍在: %s（确认正常后 --delete-backup）" % bak)
    print("-" * 72)
    if bad:
        print("发现 %d 个异常。BROKEN 的处理办法：" % bad)
        print("  1) 先确认目标盘数据是否还在: 看上面的 dst 路径")
        print("  2) 数据在 → 把 C 盘这个新目录挪走，再重新跑一次迁移")
        print("  3) 数据不在 → 别动，先排查目标盘是否掉线/盘符变了")
    else:
        print("全部正常（%d 个 junction 均存活）" % len(items))
    return 1 if bad else 0


def cmd_check(src, dst):
    """只跑预检，不做任何改动。"""
    errs, warns, stats = preflight(src, dst)
    print("预检: %s -> %s" % (os.path.abspath(src), os.path.abspath(dst)))
    if stats:
        print("  源: %d 文件 / %.1f MB" % (stats["files"], stats["bytes"] / 1024.0 ** 2))
        print("  目标盘余量: %.1f GB（需 %.1f GB，含 5%% 余量）"
              % (free_bytes(dst) / 1024.0 ** 3,
                 stats["bytes"] * MIN_FREE_RATIO / 1024.0 ** 3))
    if not errs and not warns:
        print("  ✅ 预检通过，可以迁移")
        return 0
    print_preflight(errs, warns)
    print("  %s" % ("❌ 存在阻断项，不要迁移" if errs else "⚠ 有警告，请评估后再决定"))
    return 1 if errs else 0


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    if args[0] == "--audit":
        return cmd_audit()
    if args[0] == "--check":
        rest = [a for a in args[1:] if not a.startswith("--")]
        if len(rest) != 2:
            print("--check 需要 <源目录> <目标目录>")
            return 1
        return cmd_check(rest[0], rest[1])
    mode = None
    if args[0] in ("--verify", "--rollback", "--delete-backup"):
        mode = args[0]
        args = args[1:]
    if mode:
        if len(args) != 1:
            print(f"{mode} 需要且只需要一个源目录参数")
            return 1
        if mode == "--rollback":
            rc = cmd_rollback(args[0])
            if rc == 0:
                ledger_remove(args[0])
            return rc
        return {"--verify": cmd_verify, "--rollback": cmd_rollback,
                "--delete-backup": cmd_delete_backup}[mode](args[0])

    processes = []
    allow_jc = False
    force_non_ntfs = False
    if "--allow-junction-copy" in args:
        allow_jc = True
        args = [a for a in args if a != "--allow-junction-copy"]
    if "--force-non-ntfs" in args:
        force_non_ntfs = True
        args = [a for a in args if a != "--force-non-ntfs"]
    if "--process" in args:
        i = args.index("--process")
        processes = [p.strip() for p in args[i + 1].split(",") if p.strip()]
        args = args[:i] + args[i + 2:]
    if len(args) != 2:
        print(__doc__)
        return 1
    return migrate(args[0], args[1], processes, allow_jc, force_non_ntfs)


if __name__ == "__main__":
    sys.exit(main())
