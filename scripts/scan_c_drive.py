# -*- coding: utf-8 -*-
"""C盘可清理项只读扫描（不删除任何文件）
用法: python scan_c_drive.py [--deep]
--deep 时额外遍历 Program Files / AppData 大户目录（耗时数分钟）
"""
import os
import sys
import ctypes
import string

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsutil_safe import dir_size  # noqa: E402  统一实现：不跟随 junction/symlink

sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def fmt(size):
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024:.1f} KB"


def disk_space():
    print("=== 磁盘空间 ===")
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            free = ctypes.c_ulonglong(0)
            total = ctypes.c_ulonglong(0)
            if ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                    ctypes.c_wchar_p(root), None, ctypes.byref(total), ctypes.byref(free)):
                print(f"  {root} 总 {total.value / 1024**3:.1f} GB / 可用 {free.value / 1024**3:.1f} GB")


def user_paths():
    user = os.path.expanduser('~')
    local = os.path.join(user, 'AppData', 'Local')
    roaming = os.path.join(user, 'AppData', 'Roaming')
    docs = os.path.join(user, 'Documents')
    return user, local, roaming, docs


def scan_common():
    user, local, roaming, docs = user_paths()
    targets = [
        # 不用 %TEMP%：Agent 会话的 TEMP 环境变量可能指向系统目录（C:\Windows\TEMP），
        # 会把"用户临时目录"错标成系统目录。用户临时目录只认 profile 下的 Local\Temp。
        ("用户临时目录", os.path.join(local, 'Temp')),
        ("系统临时目录", r'C:\Windows\Temp'),
        ("Windows更新缓存", r'C:\Windows\SoftwareDistribution\Download'),
        ("旧版微信数据", os.path.join(docs, 'WeChat Files')),
        ("新版微信数据(xwechat)", os.path.join(roaming, 'Tencent', 'xwechat')),
        ("微信小程序商店缓存", os.path.join(roaming, 'Tencent', 'WeChatAppStore')),
        ("QQ数据", os.path.join(docs, 'Tencent Files')),
        ("应用宝安卓引擎", r'C:\Program Files\Tencent\Androws'),
    ]
    # 浏览器缓存：各 Profile 的 Cache/Code Cache/GPUCache
    for browser, base in [
        ("Chrome", os.path.join(local, 'Google', 'Chrome', 'User Data')),
        ("Edge", os.path.join(local, 'Microsoft', 'Edge', 'User Data')),
    ]:
        if os.path.isdir(base):
            for profile in os.listdir(base):
                if not profile.startswith(('Default', 'Profile')):
                    continue
                for cache in ('Cache', 'Code Cache', 'GPUCache'):
                    p = os.path.join(base, profile, cache)
                    if os.path.isdir(p):
                        targets.append((f"{browser} {profile} {cache}", p))

    print("\n=== 可清理项扫描（只读） ===")
    for label, path in targets:
        if os.path.exists(path):
            print(f"  {label}: {fmt(dir_size(path))}\n      {path}")
        else:
            print(f"  {label}: 不存在")


def scan_system_files():
    print("\n=== 系统文件 ===")
    for name in ('hiberfil.sys', 'pagefile.sys', 'swapfile.sys'):
        p = os.path.join('C:\\', name)
        try:
            print(f"  {name}: {fmt(os.path.getsize(p))}")
        except OSError:
            print(f"  {name}: 不存在")


def scan_deep():
    """大户目录钻取"""
    _, local, roaming, _ = user_paths()
    print("\n=== AppData\\Local 大户（前20） ===")
    entries = []
    if os.path.isdir(local):
        for e in os.scandir(local):
            if e.is_dir(follow_symlinks=False):
                entries.append((e.name, dir_size(e.path)))
    for name, size in sorted(entries, key=lambda x: -x[1])[:20]:
        if size > 100 * 1024 ** 2:
            print(f"  {name}: {fmt(size)}")
    print("\n=== AppData\\Roaming 大户（前20） ===")
    entries = []
    if os.path.isdir(roaming):
        for e in os.scandir(roaming):
            if e.is_dir(follow_symlinks=False):
                entries.append((e.name, dir_size(e.path)))
    for name, size in sorted(entries, key=lambda x: -x[1])[:20]:
        if size > 50 * 1024 ** 2:
            print(f"  {name}: {fmt(size)}")


def scan_recycle_bin():
    print("\n=== 回收站 ===")
    from ctypes import wintypes

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD),
                    ('i64Size', ctypes.c_longlong),
                    ('i64NumItems', ctypes.c_longlong)]
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    r = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    if r == 0:
        print(f"  共 {info.i64NumItems} 项 / {fmt(info.i64Size)}")
    else:
        print("  查询失败")


if __name__ == '__main__':
    disk_space()
    scan_common()
    scan_system_files()
    scan_recycle_bin()
    if '--deep' in sys.argv:
        scan_deep()
    print("\n[扫描完成，未修改任何文件]")
