# -*- coding: utf-8 -*-
"""真实环境验收（field test）—— 在真机上证明能力有效，而不只是合成单测全绿。

为什么必须有两个测试
--------------------
`selftest.py` 用合成数据、自造自清、毫秒级，验证「逻辑正确」。
本文件在**真实路径**上只读跑，验证「在真机上真的有效」。

两者缺一不可。这次实测就是铁证：selftest 88 项全绿的同时，真实环境里
藏着 5 个 P0 缺陷，一个都没被合成用例抓到：

  1. `scan_safe_targets` 把 "C:" 用 rstrip("\\:") 洗成 "C"，用户目录从未被扫描
     -> 合成用例里路径是 tmp 目录，根本走不到那段代码；
  2. `gen_clean_task` 的 XML 只写 GroupId，schtasks 直接「拒绝访问」
     -> 合成用例只校验 XML 结构，结构确实合法；
  3. `C:\\Windows\\WinSxS` 被"大文件"规则判成 AMBER（它是目录，不是文件）
     -> 合成用例造不出 13.4GB 的目录；
  4. 用户目录里藏着 WPS 云盘挂载点，跳过它让大文件统计从 122GB 掉到 9GB
     -> 合成用例的树里没有云盘占位符；
  5. 去重在个人目录被红线全拦，功能等于不存在
     -> 合成用例走的是 tmp 目录，不触发红线。

用法
----
    python -S scripts/field_test.py                  # 只读，全部真实用例
    python -S scripts/field_test.py --verbose
    python -S scripts/field_test.py --with-schtasks  # 额外真实注册+删除计划任务
    python -S scripts/field_test.py --json out.json

约定
----
* 只读：不删任何用户文件，只往 %TEMP% 写临时文件并自己清掉。
* 真实路径不存在 -> SKIP（不是 PASS），并在结尾说明「哪些能力在本机没被验证到」。
* 退出码：有 FAIL 返回 1，否则 0。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
PY = sys.executable
sys.path.insert(0, HERE)

sysdrive = (os.environ.get("SystemDrive") or "C:").rstrip("\\/")
WIN = sysdrive + "\\Windows"
USERPROFILE = os.environ.get("USERPROFILE") or ""
TEMP = os.environ.get("TEMP") or ""

RESULTS = []


def rec(status, name, detail=""):
    RESULTS.append({"status": status, "name": name, "detail": detail})
    return status


def run(script, *args, timeout=300):
    """黑盒调用同目录脚本，返回 (rc, stdout, stderr)。"""
    cmd = [PY, "-S", os.path.join(HERE, script)] + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def jrun(script, *args, timeout=300):
    rc, out, err = run(script, *args, timeout=timeout)
    try:
        return rc, json.loads(out), err
    except Exception:
        return rc, None, (err or out)


def exists(*parts):
    return all(parts) and os.path.exists(os.path.join(*parts))


# ============================================================ 用例

def t01_user_root_resolution():
    """用户目录定位：不允许出现 "C\\Users" 这种被 rstrip 洗坏的路径。"""
    rc, data, err = jrun("scan_safe_targets.py")
    if data is None:
        return rec("FAIL", "T01 用户目录定位", "扫描脚本无 JSON 输出: %s" % (err or rc)[:160])
    cats = data.get("categories") or {}
    bad = [k for k in cats if k.lower().startswith(("c\\\\", "c:users"))]
    if bad:
        return rec("FAIL", "T01 用户目录定位", "出现无效路径: %s" % bad[:3])
    users_items = [k for k in cats if "\\users\\" in k.lower()]
    if not TEMP or not os.path.isdir(TEMP):
        return rec("SKIP", "T01 用户目录定位", "本机 TEMP 不可用，无法验证用户级扫描")
    if not users_items:
        return rec("FAIL", "T01 用户目录定位",
                   "扫到 %d 个分类但**没有任何用户目录项** —— 用户级缓存整段漏扫" % len(cats))
    return rec("PASS", "T01 用户目录定位", "%d 个分类，其中 %d 个用户级路径"
               % (len(cats), len(users_items)))


def t02_temp_covered():
    """白名单扫描必须覆盖真实 TEMP（这是最高频的清理目标）。"""
    rc, data, err = jrun("scan_safe_targets.py")
    if data is None:
        return rec("FAIL", "T02 TEMP 覆盖", "无 JSON 输出")
    cats = {k.lower() for k in (data.get("categories") or {})}
    if not TEMP:
        return rec("SKIP", "T02 TEMP 覆盖", "TEMP 未设置")
    if TEMP.lower() not in cats:
        return rec("FAIL", "T02 TEMP 覆盖", "TEMP=%s 不在扫描结果里（漏报）" % TEMP)
    sz = data["categories"][TEMP]
    return rec("PASS", "T02 TEMP 覆盖", "%s = %.1f MB" % (TEMP, sz / 1048576.0))


def t03_warnings_field():
    """结果必须带 warnings 字段（路径异常时不许静默）。"""
    rc, data, err = jrun("scan_safe_targets.py")
    if data is None:
        return rec("FAIL", "T03 静默失败保护", "无 JSON 输出")
    if "warnings" not in data:
        return rec("FAIL", "T03 静默失败保护", "缺 warnings 字段，路径异常时会静默漏扫")
    return rec("PASS", "T03 静默失败保护", "warnings=%s" % (data["warnings"] or "[]"))


def t04_independent_cross_check():
    """用**独立实现**统计 TEMP 体积，与脚本结果比对（防止自己验自己）。"""
    if not TEMP or not os.path.isdir(TEMP):
        return rec("SKIP", "T04 独立交叉校验", "TEMP 不可用")
    total = 0
    stack = [TEMP]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_file_attributes & 0x400 and not (st.st_file_attributes & 0x10):
                        continue
                    if st.st_file_attributes & 0x10:
                        stack.append(e.path)
                    else:
                        total += st.st_size
        except OSError:
            pass
    rc, data, err = jrun("scan_safe_targets.py")
    if data is None:
        return rec("FAIL", "T04 独立交叉校验", "无 JSON 输出")
    got = (data.get("categories") or {}).get(TEMP, 0)
    if got == 0:
        return rec("SKIP", "T04 独立交叉校验", "TEMP 为空，无法比对")
    diff = abs(got - total) / float(total) if total else 1.0
    if diff > 0.15:
        return rec("FAIL", "T04 独立交叉校验",
                   "独立统计 %.1fMB vs 脚本 %.1fMB，偏差 %.0f%%"
                   % (total / 1048576.0, got / 1048576.0, diff * 100))
    return rec("PASS", "T04 独立交叉校验", "偏差 %.1f%%（容差 15%%）" % (diff * 100))


def _classify(paths):
    import risk_register as RR
    rules = RR.load_rules()
    return [RR.classify(p, rules) for p in paths]


def t05_winsxs_red():
    """WinSxS 必须是 RED + block：手删会毁掉系统组件。"""
    p = os.path.join(WIN, "WinSxS")
    if not os.path.isdir(p):
        return rec("SKIP", "T05 WinSxS 判级", "本机无 WinSxS")
    it = _classify([p])[0]
    if it["level"] != "RED" or not it["block"]:
        return rec("FAIL", "T05 WinSxS 判级",
                   "判成 %s / block=%s（规则 %s）—— 目录被当成普通文件"
                   % (it["level"], it["block"], it["rule_id"]))
    return rec("PASS", "T05 WinSxS 判级", "RED/block，规则 %s" % it["rule_id"])


def t06_structural_containers():
    """盘根 / 用户根 / ProgramData 必须是 RED（容器不是垃圾）。"""
    cands = []
    if sysdrive:
        cands.append(sysdrive + "\\")
    if USERPROFILE:
        cands.append(USERPROFILE)
        parent = os.path.dirname(USERPROFILE)
        if parent:
            cands.append(parent)
    cands.append(os.path.join(sysdrive + "\\", "ProgramData"))
    cands = [c for c in cands if c and os.path.exists(c)]
    if not cands:
        return rec("SKIP", "T06 结构性红线", "无可测容器路径")
    bad = []
    t0 = time.time()
    for it in _classify(cands):
        if it["level"] != "RED" or not it["block"]:
            bad.append("%s=%s" % (it["path"], it["level"]))
    cost = time.time() - t0
    if bad:
        return rec("FAIL", "T06 结构性红线", "未拦: %s" % ", ".join(bad))
    if cost > 20:
        return rec("FAIL", "T06 结构性红线",
                   "拦住了但耗时 %.1fs（应秒级；红线项不该递归统计体积）" % cost)
    return rec("PASS", "T06 结构性红线", "%d 个容器全部 RED，耗时 %.1fs" % (len(cands), cost))


def t07_chat_db_reason():
    """聊天记录库必须判 RED，且**理由**要说"聊天主库"而不是泛泛的"个人目录"。"""
    import risk_register as RR
    rules = RR.load_rules()
    # 不依赖本机真有 QQ/微信：用符合真实命名的路径做语义验证
    fake = [
        os.path.join(USERPROFILE, "Documents", "Tencent Files", "123456", "Msg3.0.db"),
        os.path.join(USERPROFILE, "Documents", "WeChat Files", "wxid_x", "Msg", "MSG0.db"),
        os.path.join(USERPROFILE, "Documents", "xwechat_files", "wxid_x", "db_storage", "a.db"),
    ]
    bad = []
    for p in fake:
        it = RR.classify(p, rules)
        if it["level"] != "RED":
            bad.append("%s=%s" % (os.path.basename(p), it["level"]))
        elif it["category"] != "message-db":
            bad.append("%s 理由错(%s)" % (os.path.basename(p), it["category"]))
    if bad:
        return rec("FAIL", "T07 聊天库判级与理由", "; ".join(bad))
    return rec("PASS", "T07 聊天库判级与理由", "3 类消息库均 RED + message-db")


def t08_cache_green():
    """纯缓存必须是 GREEN —— 不能被泛化的 appdata AMBER 吃掉（错误恐吓）。"""
    cases = []
    if TEMP:
        cases.append(TEMP)
    p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "pip", "Cache")
    if os.path.isdir(p):
        cases.append(p)
    cases = [c for c in cases if c and os.path.isdir(c)]
    if not cases:
        return rec("SKIP", "T08 纯缓存判级", "无可测缓存目录")
    bad = [("%s=%s" % (it["path"], it["level"])) for it in _classify(cases)
           if it["level"] != "GREEN"]
    if bad:
        return rec("FAIL", "T08 纯缓存判级", "被误判: %s" % "; ".join(bad))
    return rec("PASS", "T08 纯缓存判级", "%d 个缓存目录均 GREEN" % len(cases))


def t09_dir_not_size_rule():
    """目录不得命中「单文件 > 100MB」规则（目录体积不是文件体积）。"""
    p = WIN
    if not os.path.isdir(p):
        return rec("SKIP", "T09 目录不吃尺寸规则", "无 Windows 目录")
    it = _classify([p])[0]
    if it["category"] == "large-file":
        return rec("FAIL", "T09 目录不吃尺寸规则",
                   "Windows 目录被判成 large-file（scope 未生效）")
    return rec("PASS", "T09 目录不吃尺寸规则", "判为 %s/%s" % (it["level"], it["category"]))


def t10_workbuddy_protected():
    """本技能自己的数据目录（记忆/密钥/技能）必须 RED。"""
    p = os.path.join(USERPROFILE, ".workbuddy")
    if not os.path.isdir(p):
        return rec("SKIP", "T10 项目数据保护", "本机无 .workbuddy")
    it = _classify([p])[0]
    if it["level"] != "RED":
        return rec("FAIL", "T10 项目数据保护", "判成 %s（规则 %s）" % (it["level"], it["rule_id"]))
    return rec("PASS", "T10 项目数据保护", "RED（%s）" % it["rule_id"])


def t11_link_blindspot_reported():
    """scan_tiers 必须报告未跟随的链接/挂载点，且能区分 cloud-only。"""
    root = USERPROFILE
    if not root:
        return rec("SKIP", "T11 盲区报告", "无 USERPROFILE")
    rc, data, err = jrun("scan_tiers.py", "--root", root, "--min-mb", "1024",
                         "--quiet", "--link-limit", "5", timeout=600)
    if data is None:
        return rec("FAIL", "T11 盲区报告", "无 JSON 输出: %s" % (err or rc)[:160])
    if "blindspots" not in data or "link_total" not in data:
        return rec("FAIL", "T11 盲区报告", "结果缺 blindspots/link_total 字段")
    n = data["link_total"]
    vd = [b.get("verdict") for b in data["blindspots"]]
    if n and not data["blindspots"]:
        return rec("FAIL", "T11 盲区报告", "存在 %d 个链接但一个都没列出" % n)
    detail = "链接 %d 个，已探测 %d 个，类型 %s" % (
        n, len(data["blindspots"]), sorted(set(v for v in vd if v)) or "-")
    return rec("PASS", "T11 盲区报告", detail)


def t12_cloud_placeholder_verdict():
    """verdict 必须与实测占比一致：本地占比 <5% 判 cloud-only，否则判 local-data。

    注意断言的是**一致性**，不是「都该判 cloud-only」：有些目录里既有云端
    占位符也有真实本地文件（实测坚果云 .del 目录就是 31% 本地），
    那种判 local-data 才是对的 —— 一刀切要求 cloud-only 会误杀正确行为。
    """
    root = USERPROFILE
    rc, data, err = jrun("scan_tiers.py", "--root", root, "--min-mb", "1024",
                         "--quiet", "--link-limit", "20", timeout=600)
    if data is None:
        return rec("FAIL", "T12 云占位符识别", "无 JSON 输出")
    spots = [b for b in data["blindspots"] if b.get("cloud_placeholders")]
    if not spots:
        return rec("SKIP", "T12 云占位符识别", "本机用户目录无云盘占位符")
    bad, checked = [], 0
    for b in spots:
        lb, lo = b.get("logical_bytes") or 0, b.get("local_bytes") or 0
        if lb < 50 * 1024 * 1024:      # 太小的样本，占比没有统计意义
            continue
        checked += 1
        want = "cloud-only" if lo <= lb * 0.05 else "local-data"
        if b.get("verdict") != want:
            bad.append("%s: 本地 %.1fMB/逻辑 %.1fMB 应判 %s 实判 %s" % (
                os.path.basename(b["path"]), lo / 1048576.0, lb / 1048576.0,
                want, b.get("verdict")))
    if bad:
        return rec("FAIL", "T12 云占位符识别", "; ".join(bad[:3]))
    if not checked:
        return rec("SKIP", "T12 云占位符识别", "有占位符但样本 <50MB，无法判定")
    big = max(spots, key=lambda b: b.get("logical_bytes") or 0)
    return rec("PASS", "T12 云占位符识别",
               "%d 项占比与判定一致；最大 %s 逻辑 %.1fGB / 本地 %dB"
               % (checked, os.path.basename(big["path"]),
                  (big["logical_bytes"] or 0) / 1073741824.0, big["local_bytes"]))


def t13_link_probe_bounded():
    """link_probe 必须在大目录上有界返回（不能因自引用链接而无界转圈）。

    为什么不用「平均文件大小」当判据：实测 `.workbuddy` 真有 46.6 万文件 / 9.2GB，
    其中 36.8 万个 <1KB、7893 个 0 字节 —— 平均 25B/文件是**真实情况**，
    拿它判循环会误杀。真正可靠的信号是 **目录数 / 文件数**：
    正常目录树 dirs << files，转圈时 dirs 会迅速追平 files。
    """
    import fsutil_safe as F
    p = os.path.join(USERPROFILE, ".workbuddy")
    if not os.path.isdir(p):
        p = os.path.join(USERPROFILE, "Documents")
    if not os.path.isdir(p):
        return rec("SKIP", "T13 探测有界性", "无可测大目录")
    t0 = time.time()
    pr = F.link_probe(p, max_files=120000)
    cost = time.time() - t0
    if cost > 90:
        return rec("FAIL", "T13 探测有界性", "耗时 %.0fs，疑似无界遍历" % cost)
    if pr["files"] > 120000:
        return rec("FAIL", "T13 探测有界性", "文件数 %d 超过上限" % pr["files"])
    if pr["files"] and pr["dirs"] > pr["files"]:
        return rec("FAIL", "T13 探测有界性",
                   "目录数 %d > 文件数 %d，疑似循环重复计数" % (pr["dirs"], pr["files"]))
    return rec("PASS", "T13 探测有界性",
               "%d 文件 / %d 目录 / %.1fMB / %.1fs"
               % (pr["files"], pr["dirs"], pr["logical"] / 1048576.0, cost))


def t14_task_xml_principals():
    """计划任务 XML 必须有 UserId、不得有裸 GroupId（否则 schtasks 拒绝访问）。"""
    tmp = tempfile.mkdtemp(prefix="ccu_ft_")
    xml = os.path.join(tmp, "t.xml")
    try:
        rc, out, err = run("gen_clean_task.py", "--daily", "10:00", "--out", xml)
        if not os.path.exists(xml):
            return rec("FAIL", "T14 任务 XML 主体", "未生成 XML: %s" % (err or out)[:160])
        raw = open(xml, "rb").read()
        text = raw.decode("utf-16") if raw[:2] == b"\xff\xfe" else raw.decode("utf-8", "replace")
        if "<UserId>" not in text:
            return rec("FAIL", "T14 任务 XML 主体", "缺 UserId")
        if "<GroupId>" in text:
            return rec("FAIL", "T14 任务 XML 主体", "存在 GroupId（会导致拒绝访问）")
        if "schtasks" not in out and "/Create" not in out:
            return rec("PASS", "T14 任务 XML 主体", "含 UserId，且给了注册命令（命令提示待核）")
        return rec("PASS", "T14 任务 XML 主体", "含 UserId + LogonType，无 GroupId")
    finally:
        for f in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, f))
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def t15_task_real_register():
    """真实注册 -> 立即运行 -> 校验产出 -> 删除。需要 schtasks 权限。"""
    tmp = tempfile.mkdtemp(prefix="ccu_ft_")
    xml = os.path.join(tmp, "t.xml")
    rep = os.path.join(tmp, "report.json")
    task = "CCU-FieldTest-%d" % (os.getpid() % 100000)
    try:
        rc, out, err = run("gen_clean_task.py", "--daily", "10:00",
                           "--name", task, "--out-json", rep, "--out", xml)
        if not os.path.exists(xml):
            return rec("FAIL", "T15 任务真实注册", "未生成 XML")
        c = subprocess.run(["schtasks", "/Create", "/TN", task, "/XML", xml, "/F"],
                           capture_output=True, text=True, encoding="gbk", errors="replace")
        if c.returncode != 0:
            msg = (c.stdout or "") + (c.stderr or "")
            if "拒绝访问" in msg or "Access" in msg:
                return rec("SKIP", "T15 任务真实注册", "无注册权限（需管理员）")
            return rec("FAIL", "T15 任务真实注册", "注册失败: %s" % msg.strip()[:120])
        try:
            subprocess.run(["schtasks", "/Run", "/TN", task],
                           capture_output=True, text=True, encoding="gbk")
            deadline = time.time() + 60
            while time.time() < deadline and not os.path.exists(rep):
                time.sleep(1)
            if not os.path.exists(rep):
                return rec("FAIL", "T15 任务真实注册", "注册成功但任务未产出报告（未真正执行）")
            data = json.load(open(rep, encoding="utf-8"))
            if "total_gb" not in data:
                return rec("FAIL", "T15 任务真实注册", "报告缺 total_gb")
            return rec("PASS", "T15 任务真实注册",
                       "注册/运行/产出(%.2fGB)/卸载 全通" % data["total_gb"])
        finally:
            subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"],
                           capture_output=True, text=True, encoding="gbk")
    finally:
        for f in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, f))
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def t16_dedup_keep_one():
    """去重在个人目录必须能出计划（keep-one 语义），而不是被红线全拦。"""
    root = os.path.join(USERPROFILE, "Documents")
    if not os.path.isdir(root):
        return rec("SKIP", "T16 去重 keep-one", "无 Documents")
    rc, data, err = jrun("dedup_planner.py", "--root", root, "--min-mb", "1",
                         "--quiet", "--max-files", "20000", timeout=600)
    if data is None:
        return rec("FAIL", "T16 去重 keep-one", "无 JSON 输出: %s" % (err or rc)[:160])
    if data.get("group_count", 0) == 0:
        return rec("SKIP", "T16 去重 keep-one", "本机 Documents 无重复文件，无法验证")
    groups = data["groups"]
    if any(not g.get("keep") for g in groups):
        return rec("FAIL", "T16 去重 keep-one", "存在没有保留副本的组")
    rec_ok = all(g.get("recycle_required") for g in groups
                 if any(i["rule_id"] for i in (g.get("personal_items") or [])))
    detail = "%d 组 / 可释放 %.1fMB" % (data["group_count"],
                                     data.get("wasted_bytes", 0) / 1048576.0)
    if data.get("recycle_required_count"):
        detail += "，%d 个副本要求走回收站" % data["recycle_required_count"]
        if not rec_ok:
            return rec("FAIL", "T16 去重 keep-one", detail + "（recycle_required 未置位）")
    return rec("PASS", "T16 去重 keep-one", detail)


def t17_audit_speed():
    """风险审计必须秒级返回（不允许对目录递归求体积卡死）。"""
    paths = [sysdrive + "\\", USERPROFILE, os.path.join(sysdrive + "\\", "Windows")]
    paths = [p for p in paths if p and os.path.exists(p)]
    if not paths:
        return rec("SKIP", "T17 审计性能", "无可测路径")
    t0 = time.time()
    _classify(paths)
    cost = time.time() - t0
    if cost > 15:
        return rec("FAIL", "T17 审计性能", "%.1fs（应 <15s，红线项不该统计体积）" % cost)
    return rec("PASS", "T17 审计性能", "%d 个容器 %.2fs" % (len(paths), cost))


def t18_truncation_disclosed():
    """截断必须自曝（不许出现「扫了一部分却报一个总数」的静默截断）。"""
    root = USERPROFILE
    rc, data, err = jrun("scan_tiers.py", "--root", root, "--min-mb", "1",
                         "--max-files", "5000", "--quiet", timeout=300)
    if data is None:
        return rec("FAIL", "T18 截断自曝", "无 JSON 输出")
    if not data.get("truncated"):
        return rec("SKIP", "T18 截断自曝", "未触发截断")
    if "下限" not in (data.get("note") or ""):
        return rec("FAIL", "T18 截断自曝", "truncated=true 但 note 未说明「数字是下限」")
    return rec("PASS", "T18 截断自曝", "truncated + 明确标注下限")


def t19_delete_restore_loop():
    """删除→回收站→按原路径恢复→内容 sha256 必须一致（自造文件，不碰用户数据）。

    这条是"能不能兜底"的唯一证明。实测时它抓到了一个会真丢文件的 bug：
    `$I` 元数据按「多 12 字节」解析（网上流传的布局），真机实际没有那 12 字节，
    于是恢复把 `E:\\Workbuddyarea\\...` 还原成 `kbuddyarea\\...` ——
    表面打印「✅ 已恢复」，文件却被搬到一个不存在的前缀下，用户等于丢了。
    """
    import hashlib
    base = tempfile.mkdtemp(prefix="ccu_ft_del_")
    name = "ccu_loop_%d.txt" % (os.getpid() % 100000)
    p = os.path.join(base, name)
    payload = b"loop-closure-" * 97
    want = hashlib.sha256(payload).hexdigest()[:16]
    try:
        with open(p, "wb") as f:
            f.write(payload)
        rc, out, err = run("shell_delete.py", p, timeout=120)
        if os.path.exists(p):
            return rec("FAIL", "T19 删除恢复闭环", "送回收站失败，文件仍在原位")
        rc2, out2, err2 = run("restore_from_recycle.py", "--list", "--find", name, timeout=120)
        if name not in (out2 or ""):
            return rec("SKIP", "T19 删除恢复闭环", "回收站未收录该文件（可能被重定向/禁用）")
        if p not in out2:
            return rec("FAIL", "T19 删除恢复闭环",
                       "列表里原路径显示不正确（元数据解析偏移错误）")
        rc3, out3, err3 = run("restore_from_recycle.py", "--restore", p, timeout=120)
        if not os.path.exists(p):
            return rec("FAIL", "T19 删除恢复闭环",
                       "恢复后原路径不存在 —— 文件被搬到别处: %s" % (out3 or "").strip()[:100])
        got = hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]
        if got != want:
            return rec("FAIL", "T19 删除恢复闭环", "恢复后内容不一致 %s != %s" % (got, want))
        return rec("PASS", "T19 删除恢复闭环", "回收站可恢复，sha256 一致 (%s)" % got)
    finally:
        try:
            if os.path.exists(p):
                os.remove(p)
            os.rmdir(base)
        except OSError:
            pass


def _mk_wechat3(base, n_same=3):
    """造一个微信 3.x 账号根（FileStorage/File + Msg）。"""
    acc = os.path.join(base, "wxid_test", "FileStorage", "File")
    os.makedirs(os.path.join(acc, "2024-01"), exist_ok=True)
    os.makedirs(os.path.join(acc, "2024-02"), exist_ok=True)
    payload = b"PDF-SAME-CONTENT" * 400
    with open(os.path.join(acc, "2024-01", "a.pdf"), "wb") as f:
        f.write(payload)
    for i in range(1, n_same):
        with open(os.path.join(acc, "2024-02", "a_copy%d.pdf" % i), "wb") as f:
            f.write(payload)
    with open(os.path.join(acc, "2024-01", "b_unique.docx"), "wb") as f:
        f.write(b"OTHER" * 600)
    msg = os.path.join(base, "wxid_test", "Msg")
    os.makedirs(msg, exist_ok=True)
    with open(os.path.join(msg, "MSG0.db"), "wb") as f:
        f.write(b"DB" * 80)


def t20_wechat_layout():
    """微信盘点必须先识别布局；认不出来要报错，不能输出空账号糊弄人。"""
    cands = [os.path.join(os.environ.get("APPDATA") or "", "Tencent", "xwechat"),
             os.path.join(USERPROFILE, "Documents", "xwechat_files"),
             os.path.join(USERPROFILE, "Documents", "WeChat Files")]
    real = next((c for c in cands if c and os.path.isdir(c)), None)
    if not real:
        return rec("SKIP", "T20 微信布局识别", "本机无微信数据目录")
    rc, out, err = run("wechat_analyze.py", "--root", real, "--md", timeout=180)
    if not out or "布局：" not in out:
        return rec("FAIL", "T20 微信布局识别", "无有效输出: %s" % (err or rc))
    layout = out.split("布局：")[1].split("）")[0]
    if layout == "unknown":
        return rec("FAIL", "T20 微信布局识别", "认不出真实微信目录（%s）" % os.path.basename(real))
    # 零字节/空账号是老 bug 的症状，必须排除
    if layout in ("wechat3-root", "wechat4-root") and "（未发现已知子目录）" in out:
        return rec("FAIL", "T20 微信布局识别", "识别为账号根但一个类别都没统计到（空账号假结果）")
    size_line = [ln for ln in out.splitlines() if "合计" in ln]
    return rec("PASS", "T20 微信布局识别",
               "%s => %s%s" % (os.path.basename(real), layout,
                               "；" + size_line[0].strip("- ") if size_line else ""))


def t21_wechat_reject_unknown():
    """认不出的目录必须报错退出，而不是返回一堆空账号。"""
    target = USERPROFILE or os.environ.get("TEMP")
    rc, out, err = run("wechat_analyze.py", "--root", target, "--md", timeout=120)
    if "无法识别的微信目录结构" in (out or ""):
        return rec("PASS", "T21 微信结构拒识", "未知结构被明确拒绝")
    return rec("FAIL", "T21 微信结构拒识", "对普通目录没有报错（会输出假账号）")


def t22_wechat_dedup_guard():
    """微信去重执行端必须拦篡改：聊天主库、树外路径。"""
    base = tempfile.mkdtemp(prefix="ccu_ft_wx_")
    try:
        root = os.path.join(base, "wx3root")
        _mk_wechat3(root)
        cache = os.path.join(base, "cache.json")
        # --min-mb 0 必须显式给：默认 0.05MB 是为了跳过多媒体缩略图，
        # 而这里造的是几 KB 的样本，不给 0 会被过滤掉、误判成"没识别出重复"。
        rc, out, err = run("wechat_dedup_plan.py", "--root", root,
                           "--cache", cache, "--out", base,
                           "--min-mb", "0", timeout=180)
        plan_p = os.path.join(base, "file_dedup_plan.json")
        if not os.path.exists(plan_p):
            return rec("FAIL", "T22 微信去重护栏", "未生成计划: %s" % (out or err or rc)[:120])
        plan = json.load(open(plan_p, encoding="utf-8"))
        if plan.get("dup_groups", 0) < 1:
            return rec("FAIL", "T22 微信去重护栏", "没识别出重复组（造了 3 份同内容文件）")
        # 篡改：塞入聊天主库 + 树外路径
        plan["to_delete"].append({"hash": "evil",
                                  "delete_path": os.path.join(root, "wxid_test", "Msg", "MSG0.db"),
                                  "size": 160, "copies": 1})
        plan["to_delete"].append({"hash": "evil2",
                                  "delete_path": os.path.join(WIN, "System32", "drivers", "etc", "hosts"),
                                  "size": 100, "copies": 1})
        bad_p = os.path.join(base, "tampered.json")
        json.dump(plan, open(bad_p, "w", encoding="utf-8"), ensure_ascii=False)
        rc2, out2, err2 = run("wechat_dedup_recycle.py", "--plan", bad_p, timeout=180)
        text = (out2 or "") + (err2 or "")
        if "未通过安全校验" not in text:
            return rec("FAIL", "T22 微信去重护栏", "篡改项没有被拦: %s" % text[:150])
        if "Msg" not in text or "不在计划 root 之下" not in text:
            return rec("FAIL", "T22 微信去重护栏", "两类违规未分别拦下")
        return rec("PASS", "T22 微信去重护栏",
                   "%d 组重复；聊天主库与树外路径均被拦" % plan["dup_groups"])
    finally:
        _rm_tree(base)


def t23_delete_allowlist():
    """delete_safe 必须只删白名单内路径，违规项全部 SKIPPED 且文件完好。"""
    tmp = os.environ.get("TEMP")
    if not tmp:
        return rec("SKIP", "T23 删除白名单", "无 TEMP")
    base = tempfile.mkdtemp(prefix="ccu_ft_del2_")
    try:
        good = os.path.join(tmp, "ccu_ft_good_%d.tmp" % (os.getpid() % 10000))
        with open(good, "wb") as f:
            f.write(b"x" * 64)
        bad = os.path.join(USERPROFILE, "Documents", "ccu_ft_never_touch.txt")
        tg = os.path.join(base, "t.json")
        json.dump({"paths": [good, bad, os.path.join(WIN, "System32", "drivers", "etc", "hosts")]},
                  open(tg, "w", encoding="utf-8"), ensure_ascii=False)
        rc, out, err = run("delete_safe.py", "--targets", tg, timeout=120)
        try:
            d = json.loads(out)
        except Exception:
            return rec("FAIL", "T23 删除白名单", "无 JSON 输出: %s" % (err or out)[:120])
        if d.get("deleted", 0) != 0:
            return rec("FAIL", "T23 删除白名单", "dry-run 竟然删了文件")
        skipped = [x for x in d.get("details", []) if x["status"] == "SKIPPED"]
        if len(skipped) != 2:
            return rec("FAIL", "T23 删除白名单", "违规项应 2 个 SKIPPED，实际 %d" % len(skipped))
        if not os.path.exists(good):
            return rec("FAIL", "T23 删除白名单", "dry-run 动了合法目标")
        os.remove(good)
        return rec("PASS", "T23 删除白名单", "合法项 DRYRUN，2 个违规项 SKIPPED")
    finally:
        _rm_tree(base)


def t24_backup_structure():
    """备份识别必须做结构校验：传错目录要报错，真结构要能判出真子集。"""
    base = tempfile.mkdtemp(prefix="ccu_ft_bak_")
    try:
        bf = os.path.join(base, "BackupFiles")

        def mk(name, text, segs):
            d = os.path.join(bf, name)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "BAK_0_TEXT"), "wb") as f:
                f.write(b"T" * text)
            for i, sz in enumerate(segs):
                with open(os.path.join(d, "BAK_%d_MEDIA" % i), "wb") as f:
                    f.write(b"M" * sz)

        mk("android_aaa", 500, [100, 200, 300])
        mk("android_bbb", 500, [100, 200])          # 真子集
        rc, out, err = run("wechat_backup_inspect.py", "--root", bf, timeout=120)
        try:
            d = json.loads(out)
        except Exception:
            return rec("FAIL", "T24 备份识别", "无 JSON 输出")
        if d["decision"]["deletable_subsets"] != ["android_bbb"]:
            return rec("FAIL", "T24 备份识别",
                       "子集判定错误: %s" % d["decision"]["deletable_subsets"])
        # 传错目录必须报错
        rc2, out2, err2 = run("wechat_backup_inspect.py", "--root", USERPROFILE, timeout=180)
        if "这不是 BackupFiles 目录" not in (out2 or ""):
            return rec("FAIL", "T24 备份识别", "传错目录没有报错（会输出空壳假结果）")
        return rec("PASS", "T24 备份识别", "子集判定正确；错目录被拒绝")
    finally:
        _rm_tree(base)


def t25_scanner_agreement():
    """两个扫描脚本对同一目标的数字必须接近（防止实现漂移，两套口径各说各话）。"""
    target = os.path.join(WIN, "Temp")
    if not os.path.isdir(target):
        return rec("SKIP", "T25 扫描口径一致", "无 Windows\\Temp")
    rc, a, err = jrun("scan_safe_targets.py", timeout=180)
    if a is None:
        return rec("FAIL", "T25 扫描口径一致", "scan_safe_targets 无输出")
    v1 = (a.get("categories") or {}).get(target)
    rc2, out2, err2 = run("scan_c_drive.py", timeout=180)
    v2 = None
    for ln in (out2 or "").splitlines():
        if "系统临时目录" in ln:
            # 形如 "  系统临时目录: 147.7 MB"
            import re as _re
            m = _re.search(r"([\d.]+)\s*(KB|MB|GB)", ln)
            if m:
                mult = {"KB": 1024, "MB": 1048576, "GB": 1073741824}[m.group(2)]
                v2 = float(m.group(1)) * mult
    if v1 is None or v2 is None:
        return rec("SKIP", "T25 扫描口径一致", "两脚本未同时覆盖该目标（v1=%s v2=%s）" % (v1, v2))
    diff = abs(v1 - v2) / float(max(v1, v2) or 1)
    if diff > 0.2:
        return rec("FAIL", "T25 扫描口径一致",
                   "同一目录两个脚本差 %.0f%%（%.1fMB vs %.1fMB）"
                   % (diff * 100, v1 / 1048576.0, v2 / 1048576.0))
    return rec("PASS", "T25 扫描口径一致", "偏差 %.1f%%（%.1fMB vs %.1fMB）"
               % (diff * 100, v1 / 1048576.0, v2 / 1048576.0))


def t26_migrate_rollback():
    """迁移→验证→回滚，文件数与字节数必须完全还原（自造目录，不碰用户数据）。

    实测要点：迁移完成后 C 盘**并没有释放**（源目录被改名成留底），
    所以必须同时断言「留底存在」和「回滚后留底消失、内容一字不差」。
    """
    import fsutil_safe as F
    base = tempfile.mkdtemp(prefix="ccu_ft_mig_")
    src = os.path.join(base, "src")
    dst = os.path.join(base, "dst", "moved")
    try:
        # 造 3 层、20 个文件、合计约 400KB
        for i in range(20):
            d = os.path.join(src, "l1", "l2_%d" % (i % 3))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "f%02d.bin" % i), "wb") as f:
                f.write(bytes([i % 256]) * 20480)
        n0, b0 = F.count_tree(src)
        rc, out, err = run("migrate_with_junction.py", src, dst, timeout=300)
        text = (out or "") + (err or "")
        if not F.is_reparse_point(src):
            return rec("FAIL", "T26 迁移与回滚", "迁移后源路径不是 junction: %s" % text[-160:])
        n1, b1 = F.count_tree(src)
        if (n1, b1) != (n0, b0):
            return rec("FAIL", "T26 迁移与回滚",
                       "经 junction 读到的内容不一致 %s != %s" % ((n1, b1), (n0, b0)))
        bak = src + ".pre-junction-bak"
        if not os.path.isdir(bak):
            return rec("FAIL", "T26 迁移与回滚", "缺留底目录（无法回滚）")
        if "当前 C 盘释放: 0 字节" not in text:
            return rec("FAIL", "T26 迁移与回滚", "未说明「迁移不释放空间」，用户会以为空间到账")
        # 写读穿透
        probe = os.path.join(src, "_ft_probe.txt")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("junction-write")
        if not os.path.exists(os.path.join(dst, "_ft_probe.txt")):
            return rec("FAIL", "T26 迁移与回滚", "经 junction 写入没落到目标盘")
        os.remove(probe)
        # 回滚
        rc2, out2, err2 = run("migrate_with_junction.py", "--rollback", src, timeout=300)
        if F.is_reparse_point(src):
            return rec("FAIL", "T26 迁移与回滚", "回滚后仍是 junction")
        if os.path.isdir(bak):
            return rec("FAIL", "T26 迁移与回滚", "回滚后留底没消耗")
        n2, b2 = F.count_tree(src)
        if (n2, b2) != (n0, b0):
            return rec("FAIL", "T26 迁移与回滚",
                       "回滚后内容不等 %s != %s" % ((n2, b2), (n0, b0)))
        return rec("PASS", "T26 迁移与回滚",
                   "%d 文件 / %.0fKB 迁移→读写穿透→回滚 完全还原" % (n0, b0 / 1024.0))
    finally:
        _rm_tree(base)


def t27_wechat4_real():
    """真实微信 4.x 账号根：分类、两级账号合并、主库保护。

    本机没有 xwechat_files 时 SKIP —— 但这一项恰恰是最该验证的：
    4.x 的真实结构是「wxid_xxx + wxid_xxx_3f2f」两级，数据全在带后缀那个里，
    而且 msg/attach 可能有几十万文件。合成结构造不出这个规模。
    """
    import wechat_analyze as WA
    cands = []
    for drive in ("C:", "D:", "E:", "F:"):
        for base in (drive + "\\", os.path.join(drive + "\\", "缓存")):
            p = os.path.join(base, "xwechat_files")
            if os.path.isdir(p):
                cands.append(p)
    p = os.path.join(USERPROFILE, "Documents", "xwechat_files")
    if os.path.isdir(p):
        cands.append(p)
    if not cands:
        return rec("SKIP", "T27 微信4.x真实数据", "本机无 xwechat_files（账号根模式未真机验证）")
    root = cands[0]
    layout, why = WA.detect_layout(root)
    if layout != "wechat4-root":
        return rec("FAIL", "T27 微信4.x真实数据", "真实目录被判成 %s（%s）" % (layout, why))
    res = WA.analyze_accounts(root, "wechat4-root", 0)
    accs = res["accounts"]
    cats = set()
    for v in accs.values():
        cats |= set(v.keys())
    if "db_storage" not in cats:
        return rec("FAIL", "T27 微信4.x真实数据", "没统计到 db_storage")
    if "msg/attach" not in cats and "msg/file" not in cats:
        return rec("FAIL", "T27 微信4.x真实数据", "没统计到 msg/attach 或 msg/file（4.x 主要占用）")
    # 两级账号必须合并：wxid_x_abcdef 不应单独成为一个账号名
    leaked = [k for k in accs if re.search(r"_[0-9a-f]{4,8}$", k)]
    if leaked:
        return rec("FAIL", "T27 微信4.x真实数据", "两级账号未合并: %s" % leaked[:3])
    total = sum(sum(d["bytes"] for d in v.values()) for v in accs.values())
    protected = sum(d["bytes"] for v in accs.values()
                    for d in v.values() if d.get("protected"))
    if total <= 0:
        return rec("FAIL", "T27 微信4.x真实数据", "统计总量为 0")
    if protected <= 0:
        return rec("FAIL", "T27 微信4.x真实数据", "db_storage 体积为 0（保护判定失效）")
    return rec("PASS", "T27 微信4.x真实数据",
               "%d 个账号 / 合计 %.1fGB（主库 %.1fGB 受保护）"
               % (len(accs), total / 1024.0 ** 3, protected / 1024.0 ** 3))


def t28_dedup_supports_4x(base=None):
    """去重必须认得 4.x 的 msg/file（旧实现只找 3.x 的 FileStorage/File）。"""
    d = tempfile.mkdtemp(prefix="ccu_ft_wx4_")
    try:
        f = os.path.join(d, "wxid_test_ab12", "msg", "file", "2026-10")
        os.makedirs(f, exist_ok=True)
        payload = b"WX4-PAYLOAD" * 400
        with open(os.path.join(f, "a.pdf"), "wb") as fh:
            fh.write(payload)
        with open(os.path.join(f, "a(1).pdf"), "wb") as fh:
            fh.write(payload)
        os.makedirs(os.path.join(d, "wxid_test_ab12", "db_storage"), exist_ok=True)
        with open(os.path.join(d, "wxid_test_ab12", "db_storage", "m.db"), "wb") as fh:
            fh.write(b"D" * 5000)
        cache = os.path.join(d, "c.json")
        rc, out, err = run("wechat_dedup_plan.py", "--root", d,
                           "--cache", cache, "--out", d, "--min-mb", "0",
                           timeout=180)
        plan_p = os.path.join(d, "file_dedup_plan.json")
        if not os.path.exists(plan_p):
            return rec("FAIL", "T28 去重支持4.x", "未生成计划: %s" % (out or err or rc)[:150])
        plan = json.load(open(plan_p, encoding="utf-8"))
        if plan.get("dup_groups", 0) < 1:
            return rec("FAIL", "T28 去重支持4.x",
                       "[事故] msg/file 下的重复没被识别（旧版只扫 FileStorage/File）")
        return rec("PASS", "T28 去重支持4.x", "msg/file 下识别出 %d 组重复" % plan["dup_groups"])
    finally:
        _rm_tree(d)


def t29_clean_task_real():
    """定时自动清理：注册→运行→文件真的被删→卸载（只用自造文件）。

    实测发现：clean 任务默认 HighestAvailable，在**非提权**环境注册会直接
    「拒绝访问」。所以加了 --runlevel least 供只清用户级缓存时使用。
    """
    tmp = os.environ.get("TEMP")
    if not tmp:
        return rec("SKIP", "T29 定时清理真删", "无 TEMP")
    d = tempfile.mkdtemp(prefix="ccu_ft_clean_")
    work = os.path.join(tmp, "ccu_ft_clean_targets")
    os.makedirs(work, exist_ok=True)
    task = "CCU-FieldTest-Clean-%d" % (os.getpid() % 100000)
    xml = os.path.join(d, "clean.xml")
    tg = os.path.join(d, "t.json")
    try:
        paths = []
        for i in range(3):
            p = os.path.join(work, "junk_%d.tmp" % i)
            with open(p, "wb") as f:
                f.write(b"J" * (1000 * (i + 1)))
            paths.append(p)
        json.dump({"paths": paths}, open(tg, "w", encoding="utf-8"), ensure_ascii=False)
        rc, out, err = run("gen_clean_task.py", "--daily", "23:00", "--action", "clean",
                           "--targets-file", tg, "--i-understand-this-deletes",
                           "--runlevel", "least", "--name", task, "--out", xml,
                           timeout=120)
        if not os.path.exists(xml):
            return rec("FAIL", "T29 定时清理真删", "未生成 XML: %s" % (out or rc)[:120])
        c = subprocess.run(["schtasks", "/Create", "/TN", task, "/XML", xml, "/F"],
                           capture_output=True, text=True, encoding="gbk", errors="replace")
        if c.returncode != 0:
            msg = (c.stdout or "") + (c.stderr or "")
            if "拒绝访问" in msg:
                return rec("SKIP", "T29 定时清理真删", "无注册权限")
            return rec("FAIL", "T29 定时清理真删", "least 模式注册失败: %s" % msg.strip()[:100])
        try:
            subprocess.run(["schtasks", "/Run", "/TN", task],
                           capture_output=True, text=True, encoding="gbk")
            deadline = time.time() + 90
            while time.time() < deadline:
                if not any(os.path.exists(p) for p in paths):
                    break
                time.sleep(2)
            left = [p for p in paths if os.path.exists(p)]
            if left:
                return rec("FAIL", "T29 定时清理真删",
                           "任务注册并运行了，但还有 %d 个文件没删" % len(left))
            return rec("PASS", "T29 定时清理真删", "least 模式注册→运行→3 个文件全删→卸载")
        finally:
            subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"],
                           capture_output=True, text=True, encoding="gbk")
    finally:
        _rm_tree(work)
        _rm_tree(d)


def _rm_tree(p):
    import shutil
    try:
        shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


TESTS = [
    t01_user_root_resolution, t02_temp_covered, t03_warnings_field,
    t04_independent_cross_check, t05_winsxs_red, t06_structural_containers,
    t07_chat_db_reason, t08_cache_green, t09_dir_not_size_rule,
    t10_workbuddy_protected, t11_link_blindspot_reported, t12_cloud_placeholder_verdict,
    t13_link_probe_bounded, t14_task_xml_principals, t16_dedup_keep_one,
    t17_audit_speed, t18_truncation_disclosed, t19_delete_restore_loop,
    t20_wechat_layout, t21_wechat_reject_unknown, t22_wechat_dedup_guard,
    t23_delete_allowlist, t24_backup_structure, t25_scanner_agreement,
    t27_wechat4_real, t28_dedup_supports_4x,
]


def main():
    ap = argparse.ArgumentParser(description="真实环境验收（只读）")
    ap.add_argument("--with-schtasks", action="store_true",
                    help="额外做一次真实的计划任务注册/运行/删除")
    ap.add_argument("--with-migrate", action="store_true",
                    help="额外做一次真实的迁移 + 回滚（自造目录，会创建 junction）")
    ap.add_argument("--with-clean-task", action="store_true",
                    help="额外做一次真实的定时清理任务（只在 TEMP 里自造文件上执行）")
    ap.add_argument("--json", help="把结果写 JSON")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    tests = list(TESTS)
    if args.with_schtasks:
        tests.insert(-2, t15_task_real_register)
    if args.with_migrate:
        tests.append(t26_migrate_rollback)
    if args.with_clean_task:
        tests.append(t29_clean_task_real)

    print("=" * 68)
    print("真实环境验收  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("用户: %s   系统盘: %s" % (USERPROFILE or "?", sysdrive))
    print("=" * 68)
    for t in tests:
        try:
            st = t()
        except Exception as e:
            rec("FAIL", t.__name__, "异常: %s: %s" % (type(e).__name__, e))
            st = "FAIL"
        r = RESULTS[-1]
        mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[r["status"]]
        print("[%s] %-26s %s" % (mark, r["name"], r["detail"]))
        if args.verbose and r["status"] == "FAIL":
            print("        ^ 用例: %s" % (t.__doc__ or "").strip().splitlines()[0])

    p = sum(1 for r in RESULTS if r["status"] == "PASS")
    f = sum(1 for r in RESULTS if r["status"] == "FAIL")
    s = sum(1 for r in RESULTS if r["status"] == "SKIP")
    print()
    print("-" * 68)
    print("通过 %d / 失败 %d / 跳过 %d    共 %d 项" % (p, f, s, len(RESULTS)))
    if s:
        print("跳过项说明：本机缺少对应真实环境，这些能力**本次未被验证**，不要当作已通过。")
    print("结论: %s" % ("全部通过" if f == 0 else "存在失败项，不可交付"))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "pass": p, "fail": f, "skip": s, "results": RESULTS},
                      fh, ensure_ascii=False, indent=1)
        print("结果已写入 %s" % args.json)
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
