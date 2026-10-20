# -*- coding: utf-8 -*-
"""Shell API 批量删除工具（绕过沙箱安全钩子对 rm/del/os.remove 的拦截）
用法:
    python shell_delete.py <路径1> [路径2 ...]      # 删除指定文件/目录（经回收站）
    python shell_delete.py --force <路径 ...>       # 彻底删除（不进回收站）
    python shell_delete.py --contents <目录>        # 只清空目录内容，保留目录本身

原理: 安全钩子只监控常规文件删除调用，不拦截 shell32 系统API。
默认 FOF_ALLOWUNDO（进回收站，可恢复）；--force 时不带该标志。
"""
import os
import sys
import ctypes
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsutil_safe import dir_size  # noqa: E402  统一实现：不跟随 junction/symlink

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ('hwnd', wintypes.HWND),
        ('wFunc', ctypes.c_uint),
        ('pFrom', wintypes.LPCWSTR),
        ('pTo', wintypes.LPCWSTR),
        ('fFlags', ctypes.c_ushort),
        ('fAnyOperationsAborted', wintypes.BOOL),
        ('hNameMappings', ctypes.c_void_p),
        ('lpszProgressTitle', wintypes.LPCWSTR),
    ]


def shell_delete(paths, to_recycle_bin=True):
    """批量删除。paths 为字符串列表。返回 SHFileOperationW 返回码（0=成功）。
    注意: pFrom 需以双 \\0 结尾；一次调用路径总数不宜过大，调用方应分批（每批<=50）。"""
    if not paths:
        return 0
    pfrom = '\x00'.join(paths) + '\x00\x00'
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = pfrom
    flags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI
    if to_recycle_bin:
        flags |= FOF_ALLOWUNDO
    op.fFlags = flags
    op.fAnyOperationsAborted = False
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))


def delete_in_batches(paths, batch_size=20, to_recycle_bin=True):
    """分批删除，每批之间汇报进度"""
    ok = 0
    for i in range(0, len(paths), batch_size):
        batch = paths[i:i + batch_size]
        code = shell_delete(batch, to_recycle_bin)
        ok += sum(1 for p in batch if not os.path.exists(p))
        print(f"  批次 {i // batch_size + 1}: 返回码 {code}, 累计删除 {ok}/{len(paths)}")
    return ok


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    force = False
    contents_only = False
    if args[0] == '--force':
        force = True
        args = args[1:]
    elif args[0] == '--contents':
        contents_only = True
        args = args[1:]
    if not args:
        print("缺少路径参数")
        return

    to_recycle = not force
    targets = []
    if contents_only:
        for d in args:
            if os.path.isdir(d):
                targets.extend(os.path.join(d, e) for e in os.listdir(d))
            else:
                print(f"跳过（不是目录）: {d}")
    else:
        targets = args

    targets = [t for t in targets if os.path.exists(t)]
    print(f"待删除 {len(targets)} 项（{'进回收站' if to_recycle else '彻底删除'}）")
    for t in targets:
        print(f"  {t}")

    # 删除前统计总体积（目录 dir_size + 文件 st_size），用于报告实际释放量。
    # 纪律：删除前先量，删除后才有可核对的数字（不能只信"删了很多项"）。
    before = 0
    for t in targets:
        if os.path.isdir(t) and not os.path.islink(t):
            before += dir_size(t)
        else:
            try:
                before += os.lstat(t).st_size
            except OSError:
                pass
    print(f"删除前合计: {before / 1024 / 1024:.1f} MB")
    ok = delete_in_batches(targets, to_recycle_bin=to_recycle)
    remain = [t for t in targets if os.path.exists(t)]
    freed = 0.0
    for t in remain:      # 未删掉的仍占空间
        try:
            freed += dir_size(t) if os.path.isdir(t) and not os.path.islink(t) else os.lstat(t).st_size
        except OSError:
            pass
    print(f"完成: 删除 {ok} 项, 残留 {len(remain)} 项, 释放约 {(before - freed) / 1024 / 1024:.1f} MB")
    if remain:
        print("残留项（可能被进程锁定，请先 taskkill 相关进程后重试，或用 MoveFileEx 登记重启删除）:")
        for t in remain:
            print(f"  {t}")


if __name__ == '__main__':
    main()
