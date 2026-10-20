import os, sys, json, stat, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsutil_safe import dir_size, iter_files  # noqa: E402  统一实现：不跟随 junction/symlink

# Bulk-safe roots: deletable wholesale (these are caches/temp/logs, not live data)
SAFE_ROOTS = [
    r"C:\Windows\Temp",
    r"C:\Windows\Panther",
    r"C:\Windows\Logs",
    r"C:\Windows\SoftwareDistribution\Download",
    r"C:\Windows\System32\CodeCache",
    r"C:\ProgramData\Microsoft\Windows\WER",
    r"C:\ProgramData\Microsoft\Windows\WER\ReportQueue",
]

# Per-user safe subdirs (relative to each C:\Users\<name>)
USER_SUBDIRS = [
    r"AppData\Local\Temp",
    r"AppData\Local\Microsoft\Edge\User Data\Default\Cache",
    r"AppData\Local\Microsoft\Edge\User Data\Default\Code Cache",
    r"AppData\Local\Google\Chrome\User Data\Default\Cache",
    r"AppData\Local\Google\Chrome\User Data\Default\Code Cache",
    r"AppData\Local\pip\Cache",
    r"AppData\Local\npm-cache",
    r"AppData\Local\CrashDumps",
    r"AppData\Local\NVIDIA\DXCache",
    r"AppData\Local\NVIDIA\GLCache",
    r"AppData\Local\D3DSCache",
    # 微信 4.x 客户端程序数据里的纯缓存（会按需重建）。
    # 为什么必须列进来：实测这台机器 xwechat 有 1.05GB 明确可清
    # （xplugin 652MB + radium 372MB + log 34MB），但白名单里一个都没有 ——
    # 等于「分析得出可清、工具却清不了」，能力断层。
    # 注意：只列 xplugin/radium/log/crashinfo/update/xplayer 这些**程序缓存**，
    # 绝不列 net/login/config/ilink（那是登录态与配置，删了要重新登录/配对）。
    r"AppData\Roaming\Tencent\xwechat\xplugin",
    r"AppData\Roaming\Tencent\xwechat\radium",
    r"AppData\Roaming\Tencent\xwechat\log",
    r"AppData\Roaming\Tencent\xwechat\crashinfo",
    r"AppData\Roaming\Tencent\xwechat\update",
    r"AppData\Roaming\Tencent\xwechat\xplayer",
]


def collect():
    """执行一次完整白名单扫描，返回结果 dict（供 main 与 build_report 复用）。"""
    out = {"categories": {}, "programdata_junk": {"bytes": 0, "count": 0, "sample": []},
           "warnings": []}
    total = 0

    # 1) Bulk-safe roots
    for root in SAFE_ROOTS:
        if os.path.isdir(root):
            s = dir_size(root)
            if s > 0:
                out["categories"][root] = s
                total += s

    # 2) Per-user temp / edge cache / pip cache
    # 注意：os.path.join("C:", "Users") 会得到盘相对路径 "C:Users"（丢反斜杠），
    # 必须显式补上分隔符，否则扫描结果路径错乱、上层无法复用。
    #
    # 【实测踩坑】这里原来写的是 `"C:".rstrip("\\:")` —— rstrip 会把盘符冒号
    # 一起吃掉，得到 "C\Users"，isdir 恒为 False，于是**整段用户目录从未被扫描，
    # 而且一声不响**。实测漏报 TEMP 88MB + pip 794MB + npm 461MB + Edge 25MB
    # ≈ 1.4GB，报告只显示 0.17GB，用户以为 C 盘很干净。
    # 所以：只剥尾部反斜杠（保留冒号），并且路径不可用时必须显式记 warning。
    sysdrive = (os.environ.get("SystemDrive") or "C:").rstrip("\\/")
    users = sysdrive + "\\Users"
    if not os.path.isdir(users):
        out["warnings"].append(
            "无法定位用户目录（%s 不存在）：用户级缓存（TEMP/pip/npm/浏览器）本次"
            "**未扫描**，下面的数字偏低，不要当成全部可清理量。" % users)
    if os.path.isdir(users):
        for u in os.listdir(users):
            base = os.path.join(users, u)
            if not os.path.isdir(base):
                continue
            for sub in USER_SUBDIRS:
                p = os.path.join(base, sub)
                if os.path.isdir(p):
                    s = dir_size(p)
                    if s > 0:
                        out["categories"][p] = s
                        total += s
            # 浏览器多 Profile 动态发现：Edge/Chrome 的缓存目录名为
            # user data\<Profile 名>\cache|code cache，Profile 名随用户而变
            # （Default / Profile 1 / Profile 2 ...）。硬编码 Default 会漏报，
            # 导致报告低估可释放量（删除白名单是支持任意 profile 的）。
            for browser in (r"AppData\Local\Microsoft\Edge\User Data",
                            r"AppData\Local\Google\Chrome\User Data"):
                ubase = os.path.join(base, browser)
                if not os.path.isdir(ubase):
                    continue
                try:
                    profiles = os.listdir(ubase)
                except OSError:
                    continue
                for prof in profiles:
                    if not prof.startswith(("Default", "Profile")):
                        continue
                    for leaf in ("Cache", "Code Cache"):
                        pp = os.path.join(ubase, prof, leaf)
                        if os.path.isdir(pp):
                            s = dir_size(pp)
                            if s > 0:
                                key = pp
                                if key not in out["categories"]:
                                    out["categories"][key] = s
                                    total += s

    # 3) ProgramData junk: .dmp/.tmp/.log excluding QQPCMgr protected path
    #    用 iter_files（不跟随 junction/symlink）：os.walk 会跟进链接，
    #    把链接指向的外部文件误算进可释放量。
    pd = r"C:\ProgramData"
    junk_ext = (".dmp", ".tmp")
    cap = 0
    if os.path.isdir(pd):
        for fp in iter_files(pd):
            if "qqpcmgr" in fp.lower():   # never touch QQ PCMgr protected data
                continue
            low = fp.lower()
            if not (low.endswith(junk_ext) or low.endswith(".log")):
                continue
            try:
                s = os.lstat(fp).st_size
            except OSError:
                continue
            if s <= 0:
                continue
            out["programdata_junk"]["bytes"] += s
            out["programdata_junk"]["count"] += 1
            total += s
            if cap < 50:
                out["programdata_junk"]["sample"].append(fp)
                cap += 1

    out["total_bytes"] = total
    out["total_gb"] = round(total / 1024 / 1024 / 1024, 2)
    out["note"] = ("Above is filesystem junk only. For WinSxS run "
                   "Dism.exe /Online /Cleanup-Image /AnalyzeComponentStore; "
                   "for VSS run vssadmin list shadowstorage. "
                   "QQPCMgr *.db is kernel-protected and excluded.")
    return out


def main():
    ap = argparse.ArgumentParser(description="白名单安全垃圾分类扫描（只读）")
    ap.add_argument("--out", help="把 JSON 写入文件（计划任务里必须用这个，"
                                  "因为 Task Scheduler 不走 shell，'>' 重定向不生效）")
    args = ap.parse_args()

    out = collect()
    text = json.dumps(out, ensure_ascii=False)
    if args.out:
        d = os.path.dirname(os.path.abspath(args.out))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)


if __name__ == "__main__":
    main()
