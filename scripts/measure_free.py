# -*- coding: utf-8 -*-
"""C 盘空闲空间测量（3 次读数取稳定值）
用法: python measure_free.py [盘符，默认 C:]

主通道：Win32 API GetDiskFreeSpaceExW（无需管理员权限，沙箱内可用）。
备用通道：fsutil volume diskfree（需管理员；本机沙箱内会"拒绝访问"，失败自动降级）。
输出 JSON: {drive, free_bytes, free_gb, readings_bytes, stable, method}
"""
import sys
import re
import json
import ctypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def diskfree_api(drive):
    """GetDiskFreeSpaceExW：返回用户可用字节数，失败返回 None。无需管理员。"""
    root = drive.rstrip("\\/:") + ":\\"
    free = ctypes.c_longlong(0)
    total = ctypes.c_longlong(0)
    total_free = ctypes.c_longlong(0)
    ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p(root),
        ctypes.byref(free),        # 用户可用
        ctypes.byref(total),       # 总量
        ctypes.byref(total_free))  # 含保留的可用
    return free.value if ok else None


def diskfree_fsutil(drive):
    """fsutil 备用通道：输出为本地化中文，需管理员权限。"""
    import subprocess
    d = drive.rstrip("\\").rstrip(":") + ":"
    r = subprocess.run(["fsutil", "volume", "diskfree", d], capture_output=True)
    txt = r.stdout.decode("gbk", "ignore") + r.stderr.decode("gbk", "ignore")
    for label in ("总可用字节数", "TotalFreeBytes", "Total free bytes"):
        m = re.search(label + r"\s*[:：]\s*([\d,]+)", txt)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


def measure(drive):
    for fn, method in ((diskfree_api, "GetDiskFreeSpaceExW"), (diskfree_fsutil, "fsutil")):
        readings = []
        for _ in range(3):
            f = fn(drive)
            if f:
                readings.append(f)
        if readings:
            return readings, method
    return [], "none"


def main():
    drive = sys.argv[1] if len(sys.argv) > 1 else "C:"
    readings, method = measure(drive)
    if not readings:
        print(json.dumps({"error": "all measurement channels failed"}))
        return
    avg = sum(readings) // len(readings)
    print(json.dumps({
        "drive": drive,
        "free_bytes": avg,
        "free_gb": round(avg / 1024 / 1024 / 1024, 2),
        "readings_bytes": readings,
        "stable": (max(readings) - min(readings)) < 500 * 1024 * 1024,
        "method": method,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
