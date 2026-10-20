# -*- coding: utf-8 -*-
"""技能自检 —— 验证安全护栏与遍历语义是否仍然正确。

用法:
    python -S scripts/selftest.py            # 全部检查
    python -S scripts/selftest.py --quick    # 跳过需要建 junction 的用例

为什么需要它
------------
本技能的核心价值是「不会误删、不会虚报」。这类性质靠人工复看代码无法保证
（历史上多个缺陷都是"看起来对但静默失效"）。自检把关键性质固化成可重复执行的断言，
每次改动脚本后跑一遍即可确认没有回退。

设计约束:
  * 只在自己的临时目录内造数据，绝不触碰真实用户数据
  * 造的文件数极少（<20），避免触发沙箱批量删除守卫
  * 需要 junction 的用例在无权限时自动跳过并标记 SKIP
"""
import os
import sys
import shutil
import stat
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsutil_safe as F
from delete_safe import is_allowed, is_reparse_point, find_reparse_points
from wechat_dedup_recycle import violation
import risk_register as RR
import scan_tiers as ST
import dedup_planner as DP
import gen_clean_task as GT
import restore_from_recycle as RC

PASS, FAIL, SKIP = [], [], []


def chk(cond, label):
    if cond:
        PASS.append(label)
        print("  PASS  " + label)
    else:
        FAIL.append(label)
        print("  FAIL  " + label)


def skip(label, why):
    SKIP.append(label)
    print("  SKIP  %s  (%s)" % (label, why))


def rm(p):
    """安全清理自己造的测试目录（先摘链接，绝不递归链接目标）。"""
    if not os.path.exists(p) and not os.path.islink(p):
        return
    if os.path.isdir(p) and F.is_reparse_point(p):
        try:
            os.rmdir(p)
        except OSError:
            pass
        return
    shutil.rmtree(p, ignore_errors=True)


def make_junction(target, link):
    """建目录联接；不可用则返回 False。"""
    try:
        import _winapi
        _winapi.CreateJunction(target, link)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 红线拦截
def t_redlines():
    print("\n[1] 安全红线（必须全部拦截）")
    for p in [
        r"C:\Users\X\Desktop\a.docx",
        r"C:\Users\X\Documents\a.db",
        r"C:\Users\X\Downloads\b.exe",
        r"C:\Users\X\Pictures\c.jpg",
        r"C:\Windows\System32\config\SAM",
        r"C:\Program Files\App\a.exe",
        r"C:\pagefile.sys",
        r"C:\hiberfil.sys",
        r"C:\Users\X\AppData\Local\Microsoft\Edge\User Data\Default\Login Data",
        r"C:\Users\X\AppData\Local\Microsoft\Edge\User Data\Default\Bookmarks",
        r"C:\Users\X\.ssh\id_rsa",
        r"C:\ProgramData\QQPCMgr\a.dmp",
        r"E:\elsewhere\x.tmp",
        r"C:\Users\X\AppData\Local\Temp\..\..\..\Desktop\x",
    ]:
        chk(is_allowed(p) is False, "拦截 " + p)


def t_allowlist():
    print("\n[2] 白名单（必须放行）")
    for p in [
        r"C:\Windows\Temp",
        r"C:\Windows\Panther",
        r"C:\Windows\Logs",
        r"C:\Windows\SoftwareDistribution\Download",
        r"C:\Windows\System32\CodeCache",
        r"C:\ProgramData\Microsoft\Windows\WER",
        r"C:\Users\X\AppData\Local\Temp",
        r"C:\Users\X\AppData\Local\Temp\a.tmp",
        r"C:\Users\X\AppData\Local\pip\Cache",
        r"C:\Users\X\AppData\Local\npm-cache",
        r"C:\Users\X\AppData\Local\CrashDumps",
        r"C:\Users\X\AppData\Local\NVIDIA\DXCache",
        r"C:\Users\X\AppData\Local\Google\Chrome\User Data\Profile 3\Code Cache\js",
        r"C:\Users\X\AppData\Local\Temp\B.TMP",
    ]:
        chk(is_allowed(p) is True, "放行 " + p)


# ------------------------------------------------------------ 遍历语义
def t_traversal(base):
    print("\n[3] 遍历语义（不跟随链接 / 归属校验）")
    ext = os.path.join(base, "ext")
    src = os.path.join(base, "src")
    os.makedirs(ext)
    os.makedirs(src)
    for i in range(10):
        open(os.path.join(ext, "big%02d.bin" % i), "w").write("E" * 5000)   # 50KB 外部
    open(os.path.join(src, "own.txt"), "w").write("o" * 1000)
    os.makedirs(os.path.join(src, "sub"))
    open(os.path.join(src, "sub", "n.txt"), "w").write("n" * 2000)

    chk(F.dir_size(src) == 3000, "dir_size 统计自有内容=3000 (得 %d)" % F.dir_size(src))
    chk(F.count_tree(src) == (2, 3000), "count_tree=(2,3000) (得 %s)" % (F.count_tree(src),))

    link = os.path.join(src, "lnk")
    if not make_junction(ext, link):
        skip("junction 相关用例", "无法创建 junction（权限或平台限制）")
    else:
        chk(is_reparse_point(link) is True, "junction 被识别为 reparse point")
        chk(is_reparse_point(src) is False, "普通目录不误判")
        chk(len(find_reparse_points(src)) == 1, "find_reparse_points 找到 1 个链接")
        # 核心性质：有链接也不能把外部数据算进来
        chk(F.dir_size(src) == 3000, "[关键] 有 junction 时 dir_size 仍=3000 (得 %d)" % F.dir_size(src))
        chk(F.count_tree(src) == (2, 3000), "[关键] 有 junction 时 count_tree 仍不虚高")
        names = sorted(os.path.basename(p) for p in F.iter_files(src))
        chk(names == ["n.txt", "own.txt"], "[关键] iter_files 不跟随链接 (得 %s)" % names)
        tgt = F.resolve_link_target(link)
        chk(tgt and os.path.normcase(os.path.normpath(tgt)) == os.path.normcase(os.path.normpath(ext)),
            "resolve_link_target 解出正确目标")
        chk(F.resolve_link_target(src) is None, "非链接返回 None")

    # 各脚本此前各自手写的实现，必须与公共原语一致
    try:
        import scan_safe_targets as SS
        import scan_c_drive as SC
        from shell_delete import dir_size as SD
        vals = {F.dir_size(src), SS.dir_size(src), SC.dir_size(src), SD(src)}
        chk(len(vals) == 1, "[关键] 各处 dir_size 实现结果一致 (%s)" % vals)
    except ImportError as e:
        skip("跨脚本一致性", str(e))

    # 归属校验
    chk(F.is_under(r"C:\root\a\b.txt", r"C:\root") is True, "is_under 树内放行")
    chk(F.is_under(r"C:\rootx\a.txt", r"C:\root") is False, "is_under 拒绝同前缀目录")
    chk(F.is_under(r"C:\other\a.txt", r"C:\root") is False, "is_under 拒绝树外")


# --------------------------------------------------- 删除护栏（reparse）
def t_delete_guard(base):
    print("\n[4] 删除护栏（链接只摘本身、子树含链接拒绝盲删）")
    target = os.path.join(base, "keepme")
    os.makedirs(target)
    open(os.path.join(target, "important.txt"), "w").write("KEEP")
    link = os.path.join(base, "evil")
    if not make_junction(target, link):
        skip("删除护栏用例", "无法创建 junction")
        return
    from delete_safe import delete_one
    st_link = delete_one(link, False)[1]
    chk(st_link == "DRYRUN", "dry-run 下识别为链接 (%s)" % st_link)
    st_link = delete_one(link, True)[1]
    chk(st_link == "UNLINKED_LINK", "确认后只摘链接本身 (%s)" % st_link)
    chk(os.path.isfile(os.path.join(target, "important.txt")), "[关键] 链接目标未被破坏")
    chk(not os.path.exists(link), "链接已被移除")

    outer = os.path.join(base, "outer")
    os.makedirs(os.path.join(outer, "sub"))
    open(os.path.join(outer, "sub", "x.txt"), "w").write("x")
    if make_junction(target, os.path.join(outer, "sub", "inner")):
        st = delete_one(outer, True)[1]
        chk(st == "SKIPPED", "子树含链接时拒绝盲删 (%s)" % st)
        chk(os.path.isdir(outer), "拒绝后目录保留")


# --------------------------------------------------- 微信去重执行端校验
def t_wechat_guard():
    print("\n[5] 微信去重执行端的归属与红线校验")
    root = r"C:\wechat\WeChat Files"
    ok_cases = [r"C:\wechat\WeChat Files\wxid\FileStorage\File\a.pdf"]
    bad_cases = [
        (r"D:\elsewhere\a.pdf", "越界"),
        (r"C:\wechat\WeChat Files\wxid\Msg\a.db", "Msg 红线"),
        (r"C:\wechat\WeChat Files\wxid\Msg\attach\x.db", "Msg 红线"),
        (r"C:\wechat\WeChat Files\wxid\FileStorage\File\x.db", ".db 红线"),
    ]
    for p in ok_cases:
        chk(violation(p, root) is None, "放行 " + p[-40:])
    for p, why in bad_cases:
        chk(violation(p, root) is not None, "拒绝(%s) %s" % (why, p[-40:]))
    chk(violation(r"C:\x\a.pdf", None) is not None, "plan 缺 root 时拒绝执行")


# ---------------------------------------------------------------- 主流程
def t_risk():
    """风险定级：这条链错了，报告就会把危险项写成"可清理"。"""
    print("\n[6] 风险登记（定级 / 批量纪律 / 两套数字）")
    rules = RR.load_rules()
    cases = [
        (r"C:\Users\X\Desktop\a.docx", "RED", "桌面"),
        (r"C:\Users\X\Documents\a.txt", "RED", "文档"),
        (r"C:\Users\X\.ssh\id_rsa", "RED", "私钥"),
        (r"C:\Windows\System32\config\SAM", "RED", "系统核心"),
        (r"C:\Users\X\AppData\Local\Tencent\WeChat\Msg\msg.db", "RED", "聊天库"),
        (r"C:\Windows\Temp\a.tmp", "GREEN", "Windows Temp"),
        (r"C:\Users\X\AppData\Local\Temp\a.tmp", "GREEN", "用户 Temp"),
        (r"C:\Users\X\AppData\Local\Google\Chrome\User Data\Default\Cache\f1", "GREEN", "浏览器 Cache"),
        (r"C:\Users\X\AppData\Local\SomeApp\settings.json", "AMBER", "应用数据"),
        (r"D:\zzz\unregistered.bin", "UNKNOWN", "未登记"),
    ]
    for p, want, why in cases:
        got = RR.classify(p, rules)["level"]
        chk(got == want, "定级 %-8s -> %-7s (得 %s) %s" % (why, want, got, p[-38:]))

    # RED 必须 block，且不带延迟（block 项不需要"再等等"）
    red = RR.classify(r"C:\Users\X\Desktop\a.docx", rules)
    chk(red["block"] is True, "RED 项 block=True")
    # AMBER 必须有延迟（防连点误删）
    amber = RR.classify(r"C:\Users\X\AppData\Local\SomeApp\settings.json", rules)
    chk(amber["delay_seconds"] >= 5, "AMBER 项有确认延迟 (%ds)" % amber["delay_seconds"])

    # 批量纪律
    paths = [r"C:\Users\X\Desktop\f%d.txt" % i for i in range(11)]
    res = RR.audit(paths, rules, max_batch=10)
    chk(res["batch_ok"] is False, "超过 10 项时 batch_ok=False")
    chk(res["safe_to_execute"] is False, "含红线时 safe_to_execute=False")
    chk(res["checked_bytes"] >= 0 and "reclaim_bytes" in res, "同时给出检查量与可释放量两套数字")


def t_field_regressions(base):
    """真机实测发现的缺陷，逐条钉死。

    每一条都对应一次**真实事故**：下面这些检查全部是在 field_test.py 于真机
    跑出失败之后补的，而当时本文件是 88/88 全绿。合成用例给不了真机的形状 ——
    「路径恰好写错」「目录不是文件」「云盘挂载点藏在用户目录」都不是代码逻辑
    问题，而是环境事实问题。所以这一节必须存在，否则同类问题还会再犯。
    """
    print("\n[10] 真机实测回归（每项对应一次真实事故）")
    rules = RR.load_rules()

    # A1 目录名本身要命中 `dir\*` 规则（字面量路径）
    c = RR.classify(r"C:\Windows\WinSxS", rules)
    chk(c["level"] == "RED" and c["block"],
        "[事故] WinSxS 目录判 RED/block（曾被判 AMBER『单文件>100MB』）")

    # A2 带通配符的 `dir\*` 也要匹配目录自身（曾用 np == pat[:-2]，带 * 时恒 False）
    c = RR.classify(r"C:\Users\X\AppData\Local\pip\Cache", rules)
    chk(c["level"] == "GREEN",
        "[事故] pip Cache 目录判 GREEN（曾因通配符匹配失效被判 AMBER）")
    c = RR.classify(r"C:\Users\X\AppData\Local\Temp", rules)
    chk(c["level"] == "GREEN", "[事故] 用户 TEMP 目录判 GREEN")

    # A3 结构性红线（容器不是垃圾）
    for p, why in (("C:\\", "盘根"), (r"C:\Users\X", "用户配置单元根"),
                   (r"C:\ProgramData", "ProgramData 根")):
        c = RR.classify(p, rules)
        chk(c["level"] == "RED" and c["rule_id"] == "STRUCT" and c["block"],
            "[事故] %s 判 RED/STRUCT" % why)

    # A4 条件规则（size/age）不得作用于目录
    c = RR.classify(r"C:\Windows", rules)
    chk(c["category"] != "large-file", "[事故] 目录不命中 large-file 规则")

    # A5 理由必须精确：聊天库不能被「个人目录」抢答
    c = RR.classify(r"C:\Users\X\Documents\Tencent Files\123\Msg3.0.db", rules)
    chk(c["category"] == "message-db",
        "[事故] 聊天库理由 = message-db（曾显示『个人目录』，用户会当普通文档）")
    c = RR.classify(r"C:\Users\X\Documents\xwechat_files\wxid\db_storage\a.db", rules)
    chk(c["level"] == "RED", "微信 4.x xwechat_files 消息库判 RED")

    # A6 项目数据保护（记忆库/技能/密钥）
    c = RR.classify(r"C:\Users\X\.workbuddy", rules)
    chk(c["level"] == "RED", "[事故] .workbuddy 判 RED（曾为 AMBER，可能被真删）")

    # A7 红线项不计体积（否则对 C:\ 递归会卡死；实测 2 分钟无响应）
    t0 = time.time()
    c = RR.classify("C:\\", rules)
    cost = time.time() - t0
    chk(cost < 3, "[事故] 盘根定级 %.2fs（应秒级，红线项不统计体积）" % cost)
    chk(c.get("bytes_known") is False, "[事故] 红线项 bytes_known=False（体积未统计要标明）")

    # A8 遍历必须报告未跟随的链接（静默跳过 = 骗人）
    d = os.path.join(base, "linkreport")
    inner = os.path.join(d, "inner")
    os.makedirs(inner, exist_ok=True)
    with open(os.path.join(inner, "a.bin"), "wb") as f:
        f.write(b"x" * 16)
    outer = os.path.join(d, "outer")
    os.makedirs(outer, exist_ok=True)
    link = os.path.join(outer, "linked")
    if make_junction(inner, link):
        seen = []
        got = list(F.iter_files(outer, on_link=lambda p, why: seen.append((p, why))))
        chk(seen and seen[0][1] == "reparse",
            "[事故] iter_files 通过 on_link 报告未跟随的链接")
        chk(all("linked" not in g for g in got), "被报告的链接仍然不被跟进（不虚高）")
    else:
        skip("链接报告用例", "无法创建 junction")


def t_tiers(base):
    """分档统计：档位边界错会导致"该看的文件没出现在榜单里"。"""
    print("\n[7] 大文件分档（边界 / 占比 / 截断标注）")
    d = os.path.join(base, "tiers")
    os.makedirs(d, exist_ok=True)
    spec = {"big3m.bin": 3 << 20, "mid15m.bin": 3 << 19, "small.bin": 512 << 10}
    for name, size in spec.items():
        with open(os.path.join(d, name), "wb") as f:
            f.truncate(size)

    r = ST.scan([d], min_mb=0, tiers=[2, 1], max_files=0, skip=[], progress=False)
    excl, big = ST.bucketize(r["found"], [2, 1], r["tree_bytes"])
    chk(r["truncated"] is False, "未设上限时不标截断")
    chk(len(r["found"]) == 3, "3 个文件全部入账 (得 %d)" % len(r["found"]))
    chk(excl[0]["count"] == 1 and excl[0]["label"] == ">=2MB", ">=2MB 档 1 个 (得 %s)" % excl[0])
    chk(excl[1]["count"] == 1, "1-2MB 档 1 个 (得 %s)" % excl[1])
    chk(abs(big - (3 << 20) - (3 << 19) - (512 << 10)) < 1024, "大文件合计字节准确")

    # 截断必须被标注出来（红线 11）
    r2 = ST.scan([d], min_mb=0, tiers=[2, 1], max_files=1, skip=[], progress=False)
    chk(r2["truncated"] is True, "设了 --max-files 时 truncated=True")

    # 剪枝：skip 生效且不进入子树
    sub = os.path.join(d, "skipme")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "inner.bin"), "wb") as f:
        f.truncate(5 << 20)
    r3 = ST.scan([d], min_mb=0, tiers=[2], max_files=0, skip=[sub], progress=False)
    names = [os.path.basename(p) for _, p in r3["found"]]
    chk("inner.bin" not in names, "skip_dirs 剪枝生效")


def t_dedup(base):
    """保留策略：选错策略会"留副本、删原件"。"""
    print("\n[8] 重复文件保留策略（shortest / oldest / newest / 红线剔除）")
    d = os.path.join(base, "dup")
    os.makedirs(os.path.join(d, "a", "deep"), exist_ok=True)
    os.makedirs(os.path.join(d, "archive"), exist_ok=True)
    payload = b"Q" * (64 << 10)
    p_short = os.path.join(d, "a", "x.bin")
    p_deep = os.path.join(d, "a", "deep", "x.bin")
    p_arch = os.path.join(d, "archive", "x_copy.bin")
    for p, mtime in ((p_short, 3000), (p_deep, 1000), (p_arch, 2000)):
        with open(p, "wb") as f:
            f.write(payload)
        os.utime(p, (mtime, mtime))
    with open(os.path.join(d, "unique.bin"), "wb") as f:
        f.write(b"U" * (64 << 10))

    r = DP.plan([d], min_mb=0, policy="shortest", quiet=True)
    chk(r["group_count"] == 1, "1 组重复 (得 %d)" % r["group_count"])
    chk(r["groups"] and r["groups"][0]["keep"] == p_short, "shortest 保留最短路径")
    chk(r["groups"] and len(r["groups"][0]["delete"]) == 2, "同组其余 2 份进入待删")
    chk(abs(r["wasted_bytes"] - 128 * 1024) < 1, "可释放 = 2 份大小 (得 %d)" % r["wasted_bytes"])

    chk(DP.plan([d], 0, "oldest", None, 0, True)["groups"][0]["keep"] == p_deep, "oldest 保留最早修改")
    chk(DP.plan([d], 0, "newest", None, 0, True)["groups"][0]["keep"] == p_short, "newest 保留最新修改")

    # 红线 + keep-one 语义：个人目录里的重复**副本**可以删（每组仍留一份），
    # 但必须升级为「走回收站 + 逐条确认」。只有整组没有保留副本时才硬拦。
    # 旧实现一刀切剔除，导致 Documents/Pictures 上去重永远 0 组 —— 功能等于不存在。
    res = {"groups": [{"size": 1, "hash": "h", "count": 2,
                       "keep": r"C:\Users\X\Pictures\keep.jpg",
                       "delete": [r"C:\Users\X\Desktop\dup.jpg"], "wasted_bytes": 1}],
           "group_count": 1, "wasted_bytes": 1}
    out = DP.annotate_with_risk(res)
    chk(out["group_count"] == 1, "[事故] 个人目录里的重复副本不再被整组剔除（keep-one）")
    chk(out.get("recycle_required_count") == 1, "副本升级为 recycle_required")
    chk(out["groups"][0].get("recycle_required") is True, "组级标记 recycle_required=True")
    chk(not out.get("red_line_removed"), "有保留副本时不算红线事故")

    # 例外：keep-policy none（整组全删）没有保留副本 -> 必须硬拦
    res2 = {"groups": [{"size": 1, "hash": "h", "count": 1, "keep": None,
                        "delete": [r"C:\Users\X\Desktop\only.jpg"], "wasted_bytes": 1}],
            "group_count": 1, "wasted_bytes": 1}
    out2 = DP.annotate_with_risk(res2)
    chk(out2["group_count"] == 0 and out2.get("red_line_removed"),
        "[事故] 没有保留副本时仍硬拦红线（宁可不省空间）")


def t_restore_meta(base):
    """回收站 $I 元数据解析：偏移算错会让"恢复"把文件搬到一个不存在的前缀下。

    实测事故：按「header(24)+pathlen(4)+12 字节 unknown+path」解析，
    真机导出的 $I 其实没有那 12 字节，于是 `E:\\Workbuddyarea\\...`
    被还原成 `kbuddyarea\\...` —— 界面打印「✅ 已恢复」，文件却不在原处。
    """
    print("\n[11] 回收站元数据解析（偏移 / 兜底 / 防呆）")
    import struct
    d = os.path.join(base, "ifile")
    os.makedirs(d, exist_ok=True)
    target = r"E:\Workbuddyarea\tmp\del_test\a.txt"
    body = target.encode("utf-16-le")
    head = struct.pack("<QQQ", 2, 800, 132000000000000000)

    p1 = os.path.join(d, "$IAAAAAA.txt")
    with open(p1, "wb") as f:
        f.write(head + struct.pack("<I", len(target)) + body)
    m1 = RC.parse_i_file(p1)
    chk(m1 is not None and m1["path"] == target,
        "[事故] 真机布局(24+4+path)解析正确 (得 %r)" % (m1 and m1["path"]))

    # 带 12 字节 unknown 的变体（流传的另一种布局）：允许解析失败，
    # 但**绝不允许**产出一个截断的错误路径 —— 那才是真丢数据。
    p2 = os.path.join(d, "$IBBBBBB.txt")
    with open(p2, "wb") as f:
        f.write(head + struct.pack("<I", len(target)) + b"\x01\x00\x00\x00" * 3 + body)
    m2 = RC.parse_i_file(p2)
    chk(m2 is None or m2["path"] == target,
        "[事故] 变体布局不产生截断路径 (得 %r)" % (m2 and m2["path"]))

    # 完全无效的文件必须返回 None（不能猜）
    p3 = os.path.join(d, "$ICCCCCC.txt")
    with open(p3, "wb") as f:
        f.write(b"\x00" * 64)
    chk(RC.parse_i_file(p3) is None, "垃圾数据返回 None（不猜路径）")


def t_wechat_layout(base):
    """微信布局识别：认错布局会输出假账号，认不出必须报错。

    实测事故：把 `AppData\\Roaming\\Tencent\\xwechat`（1.1GB 真实数据）当账号根，
    输出一堆 `log`/`net`/`All Users` 空账号，真正的 xplugin 652MB 一字节没统计。
    另一个坑：4.x 的 `msg/`（小写）判定会把 3.x 的 `Msg/`（大写）一起命中，
    因为 Windows 的 isdir 不区分大小写 —— 必须用 listdir 拿真实名比对。
    """
    print("\n[12] 微信目录布局识别（3.x / 4.x / 程序数据 / 未知）")
    import wechat_analyze as WA

    r3 = os.path.join(base, "wx3", "wxid_a", "FileStorage", "File")
    os.makedirs(r3, exist_ok=True)
    os.makedirs(os.path.join(base, "wx3", "wxid_a", "Msg"), exist_ok=True)
    with open(os.path.join(r3, "a.pdf"), "wb") as f:
        f.write(b"x" * 100)
    root3 = os.path.join(base, "wx3")
    chk(WA.detect_layout(root3)[0] == "wechat3-root",
        "[事故] 含大写 Msg/FileStorage 的 3.x 判 wechat3-root（曾被 4.x 的 msg 抢判）")

    r4 = os.path.join(base, "wx4", "wxid_b", "db_storage")
    os.makedirs(r4, exist_ok=True)
    os.makedirs(os.path.join(base, "wx4", "wxid_b", "msg", "attach"), exist_ok=True)
    chk(WA.detect_layout(os.path.join(base, "wx4"))[0] == "wechat4-root",
        "4.x（db_storage）判 wechat4-root")

    pd = os.path.join(base, "wxpd")
    for n in ("config", "log", "net", "crashinfo"):
        os.makedirs(os.path.join(pd, n), exist_ok=True)
    chk(WA.detect_layout(pd)[0] == "program-data",
        "[事故] 程序数据目录判 program-data（不再输出空账号）")

    un = os.path.join(base, "wxun")
    os.makedirs(os.path.join(un, "randomdir"), exist_ok=True)
    chk(WA.detect_layout(un)[0] == "unknown", "未知结构判 unknown（不硬猜）")

    # 3.x 分类要真的统计到字节，且 Msg 标为受保护
    res = WA.analyze_accounts(root3, "wechat3-root", 0)
    acct = res["accounts"].get("wxid_a") or {}
    chk("FileStorage/File" in acct and acct["FileStorage/File"]["files"] == 1,
        "3.x 账号能统计到 FileStorage/File")
    chk(acct.get("Msg", {}).get("protected") is True, "3.x Msg 标记为禁止删")

    # 4.x 分类
    res4 = WA.analyze_accounts(os.path.join(base, "wx4"), "wechat4-root", 0)
    a4 = res4["accounts"].get("wxid_b") or {}
    chk("db_storage" in a4 and a4["db_storage"]["protected"] is True,
        "4.x db_storage 标记为禁止删")
    chk("msg/attach" in a4, "4.x msg/attach 被识别")


def t_capability_wiring():
    """能力必须接上通路：分析出可清的东西，白名单/分级也要能执行。

    实测断层：微信 xwechat 有 1.05GB 明确可清，但 scan_safe_targets 白名单里
    一个都没列 —— 分析得出、工具清不了，等于白分析。
    """
    print("\n[13] 能力闭环（能发现 -> 能分级 -> 能执行）")
    import scan_safe_targets as SST
    for sub in ("xwechat\\xplugin", "xwechat\\radium", "xwechat\\log"):
        chk(any(sub in s for s in SST.USER_SUBDIRS),
            "[事故] 白名单覆盖 %s（否则清不掉）" % sub)
    for sub in ("xwechat\\net", "xwechat\\login", "xwechat\\config"):
        chk(not any(sub in s for s in SST.USER_SUBDIRS),
            "白名单**不**含 %s（登录态/配置，删了要重登）" % sub)

    rules = RR.load_rules()
    c = RR.classify(r"C:\Users\X\AppData\Roaming\Tencent\xwechat\xplugin", rules)
    chk(c["level"] == "GREEN", "[事故] 微信程序缓存判 GREEN（曾因未登记判 AMBER）")
    c = RR.classify(r"C:\Users\X\AppData\Roaming\Tencent\xwechat\net", rules)
    chk(c["level"] == "AMBER", "微信 net/ 判 AMBER（需谨慎）")
    c = RR.classify(r"C:\Users\X\AppData\Roaming\Tencent\xwechat\db_storage\x.db", rules)
    chk(c["level"] == "RED", "微信 db_storage 判 RED")


def t_report(base):
    """HTML 报告：必须零 JS（写坏 JS 会白屏）、必须脱敏、必须三档齐全。

    为什么强调零 JS：让模型现场写 JS 渲染报告，写坏一次浏览器**不报错**，
    页面就是白的，用户和模型都发现不了。所以改成服务端拼 HTML，脚本只填空，
    最坏也只是样式朴素。
    """
    print("\n[14] HTML 体检报告（零 JS / 脱敏 / 三档齐全）")
    import build_report as BR

    up = os.environ.get("USERPROFILE") or r"C:\Users\X"
    tmp_path = os.path.join(up, "AppData", "Local", "Temp")
    d = {
        "generated_at": "2026-01-01 10:00", "drive": "C:",
        "disk": {"total": 100 << 30, "free": 30 << 30, "used": 70 << 30, "pct": 70.0},
        "items": [],
        "by_level": {
            "GREEN": [{"path": tmp_path, "bytes": 1234, "level": "GREEN", "rule": "R0030",
                       "warning": "纯缓存", "advice": "", "delay": 0}],
            "AMBER": [{"path": os.path.join(up, "AppData", "Roaming", "Tencent", "xwechat"),
                       "bytes": 5 << 20, "level": "AMBER", "rule": "R0024",
                       "warning": "可能含聊天记录", "advice": "", "delay": 15, "app": "微信"}],
            "RED": [{"path": os.path.join(up, ".workbuddy"), "bytes": 9 << 30,
                     "level": "RED", "rule": "R0009", "warning": "项目数据",
                     "advice": "", "delay": 0}],
            "UNKNOWN": [],
        },
        "totals": {"GREEN": 1234, "AMBER": 5 << 20, "RED": 9 << 30, "UNKNOWN": 0},
        "tiers": [{"label": ">=1024MB", "count": 1, "bytes": 2 << 30}],
        "big": {"count": 1, "bytes": 2 << 30, "pct": 10.0},
        "scanned_files": 10000,
        "top": [{"path": os.path.join(up, "big.bin"), "bytes": 2 << 30}],
        "blindspots": [{"path": r"C:\Users\X\WPSDrive\cloud", "logical_bytes": 100 << 30,
                        "local_bytes": 512, "verdict": "cloud-only"}],
        "link_total": 3, "excluded": [], "rules_version": "1.2", "warnings": [],
        "wechat": None,
        "system": {"items": [
            {"name": "hiberfil.sys", "path": r"C:\hiberfil.sys", "bytes": 8 << 30,
             "verdict": "可关闭", "how": "powercfg /h off", "note": "等于内存大小"},
            {"name": "WinSxS 组件存储", "path": "-", "bytes": None,
             "verdict": "需管理员", "how": "Dism /AnalyzeComponentStore", "note": ""},
        ], "known_bytes": 8 << 30, "note": "只测量，不删除"},
    }
    h = BR.render(d)
    chk(h.startswith("<!DOCTYPE html>"), "输出是完整 HTML")
    chk("<script" not in h.lower(), "[关键] 报告零 JS（写坏 JS 会白屏）")
    chk("http://" not in h and "https://" not in h, "[关键] 报告不引用任何外部资源（离线可看）")
    for sec in ("可以放心清理", "要动之前请想清楚", "不要碰", "大文件", "照着做"):
        chk(sec in h, "含板块「%s」" % sec)
    chk("%USERPROFILE%" in h, "[事故] 路径里的用户名已脱敏（防截图泄露）")
    chk(up.lower() not in h.lower(), "[事故] 报告里不出现真实用户目录全路径")
    chk("1.2 KB" in h or "1234" in h, "体积格式化正确")
    chk("9.0 GB" in h, "红线体积正确")
    chk("云端占位" in h, "盲区按 cloud-only 说明")
    chk("系统级大户" in h, "含系统级大户板块")
    chk("powercfg /h off" in h, "系统项给出正确做法")
    chk("需管理员" in h, "[事故] 读不到的项标「需管理员」而不是猜 0")
    chk("清空回收站" in h, "[事故] 提醒「进回收站不等于空间已释放」")
    # 空数据也不能崩（用户机器可能非常干净）
    for k in ("GREEN", "AMBER", "RED", "UNKNOWN"):
        d["by_level"][k] = []
        d["totals"][k] = 0
    d["top"] = []
    d["blindspots"] = []
    h2 = BR.render(d)
    chk("<h1>" in h2, "空数据也能渲染（不抛异常）")


def t_wechat4_structure(base):
    """微信 4.x 真实结构：两级账号合并、功能目录不当账号、类别有处置建议。

    这些都是拿真机数据（129.87GB / 52.4 万文件）跑出来的差异，合成结构原本没有
    「wxid_x + wxid_x_3f2f」这种两级形态。
    """
    print("\n[15] 微信 4.x 真实结构（两级账号 / 功能目录 / 处置建议）")
    import wechat_analyze as WA

    root = os.path.join(base, "wx4real")
    # 两级账号：壳目录 + 数据目录
    os.makedirs(os.path.join(root, "wxid_aaa", "config"), exist_ok=True)
    data = os.path.join(root, "wxid_aaa_3f2f")
    for sub, n in (("db_storage", 3), ("msg/attach", 5), ("msg/file", 2), ("cache", 4)):
        d = os.path.join(data, *sub.split("/"))
        os.makedirs(d, exist_ok=True)
        for i in range(n):
            with open(os.path.join(d, "f%d.bin" % i), "wb") as f:
                f.write(b"x" * 100)
    os.makedirs(os.path.join(root, "All Users", "config"), exist_ok=True)
    os.makedirs(os.path.join(root, "Backup"), exist_ok=True)

    chk(WA.detect_layout(root)[0] == "wechat4-root", "两级账号根判 wechat4-root")
    res = WA.analyze_accounts(root, "wechat4-root", 0)
    accs = res["accounts"]
    chk("wxid_aaa" in accs, "[事故] 两级账号合并为 wxid_aaa")
    chk("wxid_aaa_3f2f" not in accs, "[事故] 带后缀的数据目录不再单独成账号")
    chk(all(k.lower() not in ("all users", "all_users", "backup") for k in accs),
        "[事故] All Users / Backup 不当账号")
    chk("All Users" in res["extras"] or "all_users" in res["extras"],
        "功能目录归到 extras")
    merged = accs.get("wxid_aaa", {})
    chk("db_storage" in merged and "msg/attach" in merged,
        "合并后能同时看到主库与附件")
    chk(merged.get("db_storage", {}).get("protected") is True, "db_storage 标为受保护")

    md = WA.to_md(res)
    for word in ("处置", "动了会怎样", "合计"):
        chk(word in md, "报告含「%s」" % word)
    chk("谨慎" in md or "禁止" in md, "[事故] 类别带处置建议（不能只报数字）")

    # 去重必须认得 4.x 的 msg/file
    import wechat_dedup_plan as WDP
    chk("msg" in open(WDP.__file__, encoding="utf-8").read()
        and "file" in open(WDP.__file__, encoding="utf-8").read(),
        "[事故] 去重扫描路径含 msg/file")

    # gen_clean_task 的运行级别开关
    import argparse
    import gen_clean_task as GT
    a = argparse.Namespace(action="clean", daily="23:00", weekly=None, idle=False,
                           out_json=None, targets_file=r"D:\t.json",
                           runlevel="least", user=None)
    x = GT.build_xml(a)
    chk("<RunLevel>LeastPrivilege</RunLevel>" in x,
        "[事故] clean + --runlevel least 可免提权注册")
    a2 = argparse.Namespace(action="clean", daily="23:00", weekly=None, idle=False,
                            out_json=None, targets_file=r"D:\t.json",
                            runlevel="auto", user=None)
    chk("<RunLevel>HighestAvailable</RunLevel>" in GT.build_xml(a2),
        "clean 默认 HighestAvailable")


def t_task():
    """计划任务：注册不上的 XML 等于没做自动化。"""
    print("\n[9] 定时巡检 XML（结构 / 无 shell 重定向 / 双开关）")
    import argparse
    ns = argparse.Namespace(action="scan", daily="10:00", weekly=None, idle=False,
                           out_json=r"D:\reports\x.json", targets_file=None)
    xml = GT.build_xml(ns)
    chk(GT.validate(xml) == [], "scan 任务 XML 结构合法 (%s)" % GT.validate(xml))
    chk(" > \"" not in xml, "[关键] 不使用 shell 重定向（Task Scheduler 不认）")
    chk("--out" in xml, "改用脚本自带 --out 写报告")
    # [事故] 只写 <GroupId>S-1-5-32-545</GroupId> 的 XML 结构完全合法，
    # 但 schtasks /Create /XML 直接报「拒绝访问」，任务根本注册不上。
    # 合成单测只看结构，永远发现不了；这里把 Principals 的写法钉死。
    chk("<UserId>" in xml, "[事故] Principals 含 UserId（否则 schtasks 拒绝访问）")
    chk("<GroupId>" not in xml, "[事故] Principals 不含裸 GroupId")
    chk("<LogonType>InteractiveToken</LogonType>" in xml, "Principals 含 LogonType")

    ns2 = argparse.Namespace(action="clean", daily=None, weekly="SUN,09:30", idle=False,
                            out_json=None, targets_file=r"D:\reports\targets.json")
    xml2 = GT.build_xml(ns2)
    chk(GT.validate(xml2) == [], "clean 任务 XML 结构合法")
    chk("--confirm" in xml2, "clean 动作带 --confirm")


def main():
    base = os.path.join(tempfile.gettempdir(), "ccu_selftest")
    rm(base)
    os.makedirs(base)
    try:
        t_redlines()
        t_allowlist()
        t_traversal(base)
        t_delete_guard(base)
        t_wechat_guard()
        t_risk()
        t_field_regressions(base)
        t_tiers(base)
        t_dedup(base)
        t_wechat_layout(base)
        t_capability_wiring()
        t_report(base)
        t_wechat4_structure(base)
        t_restore_meta(base)
        t_task()
    finally:
        rm(base)
        left = os.path.exists(base)
        print("\n清理测试目录: %s" % ("失败，请手动检查 " + base if left else "完成"))

    total = len(PASS) + len(FAIL)
    print("\n" + "=" * 56)
    print("通过 %d / %d    跳过 %d    失败 %d" % (len(PASS), total, len(SKIP), len(FAIL)))
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("   - " + f)
    print("结论:", "全部通过" if not FAIL else "存在失败项，禁止投入实战使用")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
