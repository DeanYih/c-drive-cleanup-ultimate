# -*- coding: utf-8 -*-
"""系统级大户检测（只读）：那些「占了很大空间，但绝不能用普通删除处理」的东西。

为什么单独成脚本
----------------
普通目录扫描能覆盖缓存和大文件，但 C 盘真正的空间大头往往是这几类：
  * Windows.edb        搜索索引，几百 MB 到几 GB，被 WSearch 服务锁着
  * hiberfil.sys       休眠文件，通常等于内存大小（16G 内存就是 16G 文件）
  * pagefile.sys       虚拟内存，同样 GB 级
  * Windows.old        系统升级残留，几 GB 到几十 GB
  * 系统还原点         由 VSS 管理，可能几十 GB
  * WinSxS 可回收组件  13GB 的目录里，有多少是能安全回收的（只有 DISM 说得准）

这些**一个都不能用 delete_safe 处理**：删 pagefile 蓝屏、手删 WinSxS 毁系统、
删 Windows.edb 前不停服务会失败还可能损坏索引。所以本脚本只做三件事：
量准、说清正确做法、把可以自动化的那部分标出来。绝不代删。

需要管理员的项（vssadmin / DISM / 搜索服务状态）在普通权限下会失败，
此时输出 `denied` 并说明「需要管理员才能测」，不猜数字。

用法:
    python -S scripts/scan_system_targets.py
    python -S scripts/scan_system_targets.py --json out.json
    python -S scripts/scan_system_targets.py --md
"""
import argparse
import ctypes
import json
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SYSTEM_DRIVE = os.environ.get("SystemDrive", "C:") + "\\"


def hsize(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%d %s" % (n, unit) if unit == "B" else "%.2f %s" % (n, unit)
        n /= 1024.0


def fsize(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def probe_exists(path):
    """不读大小、只判存在。系统文件的读取受限但属性查询通常还能过。"""
    try:
        # GetFileAttributesW 返回 0xFFFFFFFF 表示不存在；能拿到属性说明"有这个东西"
        r = ctypes.windll.kernel32.GetFileAttributesW(ctypes.c_wchar_p(path))
        return r != 0xFFFFFFFF
    except Exception:
        return os.path.exists(path)


def run(cmd, timeout=120):
    """跑外部命令，返回 (rc, 合并输出)。不抛异常，失败由调用方判断。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="mbcs", errors="replace", timeout=timeout,
                           shell=False)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def svc_state(name):
    rc, out = run(["sc", "query", name], timeout=30)
    if rc != 0:
        return None
    m = re.search(r"STATE\s*:\s*(\d+)", out)
    if not m:
        return None
    return {"1": "stopped", "2": "start_pending", "3": "stop_pending",
            "4": "running"}.get(m.group(1), "unknown")


def check_search_index():
    """Windows.edb：搜索索引。被 WSearch 服务锁定，必须停服务才能动。"""
    p = os.path.join(SYSTEM_DRIVE, "ProgramData", "Microsoft", "Search", "Data",
                     "Applications", "Windows", "Windows.edb")
    sz = fsize(p)
    st = svc_state("WSearch")
    if sz is None:
        if not probe_exists(p):
            return {"name": "Windows 搜索索引 (Windows.edb)", "path": p, "bytes": None,
                    "verdict": "未启用",
                    "how": "本机没有 Windows.edb —— 搜索索引未启用或索引位置被改过，无需处理",
                    "note": ""}
        return {"name": "Windows 搜索索引 (Windows.edb)", "path": p, "bytes": None,
                "service": st, "verdict": "需管理员",
                "how": "停服务 → 移走/删除 → 重启服务自动重建（索引会变小）。"
                       "管理员 PowerShell：net stop WSearch && del \"%s\" && net start WSearch" % p,
                "note": "文件存在但普通权限读不到大小；服务状态: %s" % (st or "未知")}
    return {
        "name": "Windows 搜索索引 (Windows.edb)",
        "path": p, "bytes": sz, "service": st,
        "verdict": "需停服务",
        "how": ("停服务 → 移走/删除 → 重启服务自动重建（索引会变小）。"
                "管理员 PowerShell：net stop WSearch && del \"%s\" && net start WSearch"
                % p),
        "note": "重启后会重建索引，前几天搜索会稍慢；能在「索引选项」里先缩小索引范围更好。",
    }


def check_paging_files():
    out = []
    for name, verdict, how in (
        ("hiberfil.sys", "可关闭", "管理员执行 powercfg /h off（彻底关闭休眠，立即释放等额空间）；"
                              "powercfg /h on 可恢复"),
        ("pagefile.sys", "禁止删", "不能删。要缩就到「系统属性→高级→性能→虚拟内存」调整，"
                             "建议 C 盘保留少量或交给系统管理"),
        ("swapfile.sys", "禁止删", "随虚拟内存一起由系统管理，不要单独处理"),
    ):
        p = os.path.join(SYSTEM_DRIVE, name)
        sz = fsize(p)
        if sz is None:
            # 读不到大小 ≠ 不存在。pagefile/hiberfil 默认受保护，
            # 静默跳过会让用户以为"本机没有休眠文件"——那是错的。
            if probe_exists(p):
                out.append({"name": name, "path": p, "bytes": None,
                            "verdict": "存在（需管理员读取大小）",
                            "how": how,
                            "note": "文件存在但普通权限拿不到大小，别按 0 处理"})
            continue
        out.append({"name": name, "path": p, "bytes": sz,
                    "verdict": verdict, "how": how,
                    "note": "被内核占用，普通删除必然失败" if verdict == "禁止删" else ""})
    return out


def check_windows_old():
    p = os.path.join(SYSTEM_DRIVE, "Windows.old")
    if not os.path.isdir(p):
        return None
    total = 0
    for dp, dn, fn in os.walk(p):
        for f in fn:
            try:
                total += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return {
        "name": "Windows.old（系统升级残留）",
        "path": p, "bytes": total, "verdict": "可用官方工具清",
        "how": "设置 → 系统 → 存储 → 临时文件 → 勾选「以前的 Windows 安装」；"
               "或用「磁盘清理」→ 清理系统文件。手动删会因权限失败",
        "note": "删掉后无法回退到上一个 Windows 版本，确认新系统稳定再清。",
    }


def check_restore_points():
    """系统还原点：VSS 管理，需要管理员。"""
    rc, out = run(["vssadmin", "list", "shadowstorage"], timeout=60)
    if rc != 0 or "拒绝访问" in out or "Access is denied" in out.lower():
        return {"name": "系统还原点（VSS）", "bytes": None, "verdict": "需管理员",
                "how": "管理员 PowerShell 执行 vssadmin list shadowstorage 查看占用；"
                       "清理用「系统属性→系统保护→配置→删除」全部还原点",
                "note": "普通权限读不到，这里不猜数字"}
    best = 0
    for m in re.finditer(r"最大[\s\S]{0,80}?([\d.,]+)\s*(GB|MB|TB)", out):
        val = float(m.group(1).replace(",", ""))
        unit = m.group(2)
        best = max(best, val * {"KB": 1 / 1024.0, "MB": 1 / 1024.0,
                                "GB": 1.0, "TB": 1024.0}[unit] * 1024 ** 3)
    # 直接抓"已用空间"
    used = 0
    for m in re.finditer(r"已用[\s\S]{0,40}?([\d.,]+)\s*(GB|MB|TB)", out):
        val = float(m.group(1).replace(",", ""))
        unit = m.group(2)
        used = max(used, val * {"MB": 1 / 1024.0, "GB": 1.0, "TB": 1024.0}[unit] * 1024 ** 3)
    return {"name": "系统还原点（VSS）", "bytes": int(used) if used else None,
            "verdict": "需在系统保护里清",
            "how": "系统属性 → 系统保护 → 选中 C: → 配置 → 「删除」全部还原点，"
                   "并把最大使用量调到 5% 左右",
            "note": "还原点是系统回滚的最后手段，清理前想清楚"}


def check_dism_store():
    """WinSxS 组件存储：只有 DISM 能说清「13GB 里有多少能安全回收」。"""
    rc, out = run(["Dism.exe", "/Online", "/Cleanup-Image", "/AnalyzeComponentStore"],
                  timeout=900)
    if rc != 0 or "拒绝访问" in out:
        return {"name": "WinSxS 组件存储", "bytes": None, "verdict": "需管理员",
                "how": "管理员执行 Dism /Online /Cleanup-Image /AnalyzeComponentStore "
                       "查看「可回收的程序包」；清理用 /StartComponentCleanup",
                "note": "普通权限读不到组件存储详情。目录总体积不等于可回收量——"
                        "WinSxS 里大量文件是硬链接到系统其它位置的，直接看目录会严重虚高。"}
    reclaim = None
    actual = None
    for pat in (r"可回收的程序包\s*[:：]\s*([\d.,]+)\s*(GB|MB)",
                r"Reclaimable Packages\s*[:：]\s*([\d.,]+)\s*(GB|MB)"):
        m = re.search(pat, out)
        if m:
            v = float(m.group(1).replace(",", ""))
            reclaim = v * (1024 ** 3 if m.group(2).upper() == "GB" else 1024 ** 2)
            break
    for pat in (r"组件存储实际大小\s*[:：]\s*([\d.,]+)\s*(GB|MB)",
                r"Actual Size of Component Store\s*[:：]\s*([\d.,]+)\s*(GB|MB)"):
        m = re.search(pat, out)
        if m:
            v = float(m.group(1).replace(",", ""))
            actual = v * (1024 ** 3 if m.group(2).upper() == "GB" else 1024 ** 2)
            break
    return {"name": "WinSxS 组件存储", "bytes": int(actual) if actual else None,
            "reclaimable": int(reclaim) if reclaim else None,
            "verdict": "用官方工具清",
            "how": "管理员执行 Dism /Online /Cleanup-Image /StartComponentCleanup"
                   "（安全，只清被取代的旧组件）；"
                   "加 /ResetBase 会更彻底，但之后已装更新无法卸载，不建议默认用",
            "note": "不要手动删 WinSxS 里任何东西，会损坏系统组件"}


def collect():
    items = []
    si = check_search_index()
    if si:
        items.append(si)
    items.extend(check_paging_files())
    wo = check_windows_old()
    if wo:
        items.append(wo)
    items.append(check_restore_points())
    items.append(check_dism_store())
    known = [i for i in items if i.get("bytes")]
    return {
        "items": items,
        "known_bytes": sum(i["bytes"] for i in known),
        "note": ("这些都是「占了空间但不能用普通删除」的项。本脚本只测量与说明，"
                 "不做任何修改。需要管理员的项在普通权限下会标「需管理员」，不猜数字。"),
    }


def to_md(d):
    L = ["### 系统级大户（占了空间，但不能用普通删除）", ""]
    L.append("| 项目 | 体积 | 处理方式 | 注意 |")
    L.append("|---|---|---|---|")
    for i in d["items"]:
        size = hsize(i["bytes"]) if i.get("bytes") else "需管理员"
        if i.get("reclaimable"):
            size += "（可回收 %s）" % hsize(i["reclaimable"])
        L.append("| %s<br><code>%s</code> | %s | %s | %s |"
                 % (i["name"], i.get("path", "-"), size,
                    i.get("how", ""), i.get("note", "")))
    L.append("")
    L.append("- 已知体积合计 **%s**（未含需管理员才能测的项）" % hsize(d["known_bytes"]))
    L.append("- %s" % d["note"])
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="系统级大户检测（只读）")
    ap.add_argument("--json", help="输出 JSON 到文件")
    ap.add_argument("--md", action="store_true", help="Markdown 输出")
    args = ap.parse_args()
    d = collect()
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        print("已写入 %s" % args.json)
    elif args.md:
        print(to_md(d))
    else:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
