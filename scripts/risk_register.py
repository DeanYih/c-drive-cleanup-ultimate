# -*- coding: utf-8 -*-
"""清理目标风险登记与二次确认 —— 把「删之前到底会损失什么」变成可审查的表。

为什么需要它
------------
本技能原来只有 delete_safe.py 的**路径白名单**：能删的放行、不能删的拒绝。
但白名单回答不了三个问题：

  1. 同一份白名单里，`%TEMP%` 和「微信 FileStorage」危险程度天差地别，
     报告里却看不出区别；
  2. 用户点「全部清理」时，没人告诉他"这一项会让你所有网站退出登录"；
  3. 想给某类目标加一句**具体**的警告（「删了要重新下载几十 GB」），
     原来只能写死在代码注释里。

解法是「风险登记表」：每个清理项一个 id，配定制警告文案 + 延迟确认按钮。
本脚本的登记表是**明文 JSON 规则 + 用户可覆盖 + 用户可排除记忆**
（见 anti_patterns 第 5 条）。

设计
----
* 规则库：`references/risk_rules.json`，三级 RED / AMBER / GREEN
* 匹配：Windows 路径小写化后 glob（`*` 可跨 `\\`），**RED 优先**，第一条命中即返回
* 尺寸/年龄类规则（patterns 为空）延后匹配，避免把大文件误判到缓存规则之前
* 未命中任何规则 -> UNKNOWN，按 AMBER 对待（宁可多问一句）
* 只读：不做任何删除、不联网、不写文件（除 --out 显式指定）

用法
----
    # 单条判定
    python -S scripts/risk_register.py --path "C:\\Users\\X\\AppData\\Local\\Temp\\a.tmp"

    # 审计一份删除计划（推荐：delete_safe 之前必跑）
    python -S scripts/risk_register.py --targets targets.json --md

    # 看规则
    python -S scripts/risk_register.py --list-rules
    python -S scripts/risk_register.py --list-rules --level RED

    # 用自定义规则覆盖内置（团队/自己维护）
    python -S scripts/risk_register.py --targets t.json --rules my_rules.json
"""
import argparse
import fnmatch
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsutil_safe import dir_size  # noqa: E402  统一实现：不跟随 junction/symlink

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(HERE, os.pardir, "references", "risk_rules.json")

LEVEL_ORDER = {"RED": 0, "AMBER": 1, "UNKNOWN": 1, "GREEN": 2}

# **匹配择优序**，与 LEVEL_ORDER 刻意不同：
#   RED(0) > GREEN(1) > AMBER(2) > UNKNOWN(3)
# 为什么 GREEN 要排在 AMBER 前面：GREEN 规则都是**具体缓存路径**
# （`*\appdata\local\temp`、`*...\cache\*`），AMBER 里则是泛化的
# `*\appdata\roaming\*` / `*\appdata\local\*`。若按「AMBER 更靠前」排，
# 泛化 AMBER 会吃掉所有缓存目录 —— 实测 TEMP 就是这样被从 GREEN 打成
# AMBER 的，报告里 92.9MB 纯缓存被写成「删了软件会重置」，属于**错误恐吓**。
MATCH_RANK = {"RED": 0, "GREEN": 1, "AMBER": 2, "UNKNOWN": 3}
LEVEL_ICON = {"RED": "[RED]", "AMBER": "[AMBER]", "UNKNOWN": "[?]", "GREEN": "[OK]"}

# 结构性红线：这些路径不是「某类文件」，而是**容器本身**。
# 用正则硬判而不是 JSON glob，两个原因：
#   1. glob 表达不了「恰好一层」—— `*\users\*` 会把 AppData\Local\Temp 一起拦掉；
#   2. 这几条属于「给了开关就会被误关」的底线，不该可配置。
STRUCT_RED = [
    (re.compile(r"^[a-z]:\\?$"), "盘符根目录",
     "整盘根目录。删除等于清空整个分区。"),
    (re.compile(r"^[a-z]:\\users$"), "用户目录容器",
     "所有用户的配置单元都在这里，含他人数据。"),
    (re.compile(r"^[a-z]:\\users\\[^\\]+$"), "用户配置单元根",
     "含 NTUSER.DAT 与桌面/文档/下载的全部入口；删了等于删掉这个账户。"),
    (re.compile(r"^[a-z]:\\windows$"), "Windows 根目录",
     "操作系统本体，删了系统无法启动。"),
    (re.compile(r"^[a-z]:\\program files( \(x86\))?$"), "程序安装根目录",
     "所有已安装软件都在这里。"),
    (re.compile(r"^[a-z]:\\programdata$"), "程序共享数据根目录",
     "大量软件的配置、授权与运行数据。"),
]


def structural_hit(np):
    """命中结构性红线时返回 (category, warning)，否则 None。"""
    for rx, cat, why in STRUCT_RED:
        if rx.match(np):
            return cat, why
    return None


def norm(p):
    """规范化用于匹配：绝对路径、小写、正斜杠转反斜杠、去尾分隔符。"""
    p = os.path.abspath(p)
    p = p.replace("/", "\\").lower()
    if len(p) > 3 and p.endswith("\\"):
        p = p.rstrip("\\")
    return p


def load_rules(path=None):
    path = path or DEFAULT_RULES
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    rules = data.get("rules", [])
    # 匹配顺序 = (级别优先级, 是否尺寸规则, 原始序)。
    # 为什么按级别排：`*\appdata\local\*`（AMBER，泛）会抢在
    # `*\appdata\local\temp\*`（GREEN，具体）之前命中，把纯缓存误判成"需逐项确认"。
    # 红线永远最先（宁严不宽），其次具体缓存，最后泛规则；尺寸/年龄规则最后兜底。
    rank = {"RED": 0, "GREEN": 1, "AMBER": 2, "UNKNOWN": 3}
    rules = sorted(
        enumerate(rules),
        key=lambda t: (rank.get(t[1].get("level"), 3), 0 if t[1].get("patterns") else 1, t[0]),
    )
    data["rules"] = [r for _, r in rules]
    return data


def _stat_lite(path):
    """只做一次 lstat：返回 (exists, is_dir, size_or_None, mtime)。

    size 对目录先返回 None —— 递归统计目录体积很贵（WinSxS 几十万文件），
    而绝大多数规则是 pattern 匹配、根本不需要体积。真正需要时再算（见 classify）。
    历史教训：不加区分地对每个候选目录 dir_size，16 条路径要跑十几秒。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False, False, None, 0
    import stat as _stat_mod
    is_dir = _stat_mod.S_ISDIR(st.st_mode)
    return True, is_dir, (None if is_dir else st.st_size), st.st_mtime


def _glob_hit(np, rule):
    """返回命中的**最具体** pattern（最长者），未命中返回 None。

    两个关键语义（都是实测踩出来的）：

    1. `dir\\*` 必须同时匹配 `dir` 本身。
       「删除 dir 目录」与「删除 dir 下一切」等价，目录自身的判定不能漏。
       否则 `c:\\windows\\winsxs\\*`（RED）匹配不到 `c:\\windows\\winsxs`（目录本身），
       该目录会掉到「大文件」规则里被判成 AMBER —— 实测 WinSxS 13.4GB 就是这么
       被标成「可确认清理」的，用户点确认就是系统组件被毁。

    2. 返回最长的 pattern 用于同级别择优。
       QQ 聊天库同时命中 `*\\documents\\*`（个人目录）与 `*\\tencent files\\*\\*.db`
       （聊天记录主库），两者都是 RED，但**理由必须说对**：
       报「个人目录」用户会以为是普通文档，报「聊天记录主库」他才知道不可恢复。
    """
    best = None
    for pat in rule.get("patterns", []):
        pat = pat.lower()
        hit = fnmatch.fnmatchcase(np, pat)
        if not hit and pat.endswith("\\*"):
            # 注意这里必须继续用 fnmatch，不能用 `np == pat[:-2]`。
            # pattern 里带 `*` 时（如 `*\appdata\local\pip\cache\*`），
            # 字符串相等永远为 False —— 实测就是这条把 PIP 缓存目录
            # 判成了 AMBER「删了软件会重置」，而它其实是纯缓存。
            hit = fnmatch.fnmatchcase(np, pat[:-2])
        if hit and (best is None or len(pat) > len(best)):
            best = pat
    return best


def _cond_hit(size, age_days, rule):
    """尺寸/年龄条件：全部满足才算命中。"""
    hit = False
    if rule.get("match_size_mb") is not None:
        if size >= rule["match_size_mb"] * 1024 * 1024:
            hit = True
        else:
            return False
    if rule.get("match_age_days") is not None:
        if age_days >= rule["match_age_days"]:
            hit = True
        else:
            return False
    return hit


def classify(path, rules_doc=None, known_size=None):
    """给单条路径定级。返回 dict（含 level / rule_id / warning / delay_seconds / block）。

    known_size: 调用方已经量好的体积（目录用）。批量场景下扫描阶段本就遍历过一次，
    这里再递归算一遍等于白跑一次全盘。传入即复用，不传则按需计算。

    判定流程（顺序即优先级，全部是真实场景倒逼出来的）：
      a. scope 过滤：`file` 规则不作用于目录、`dir` 规则不作用于文件。
         没有这层，「单文件 > 100MB」会把 13.4GB 的 WinSxS **目录** 标成「大文件」。
      b. 收集**所有**命中规则，而不是第一条即返回。
      c. 择优排序 (级别, -pattern 长度, 规则 id)：红线优先；同级别里越具体的
         pattern 越优先（聊天库 rules 赢过泛化的「个人目录」）。理由必须精确。
      d. size / age 条件规则需要真实体积，此时才递归算目录体积（懒加载）。
    """
    rules_doc = rules_doc or load_rules()
    defaults = rules_doc.get("defaults", {})
    excluded = {i.get("rule_id") for i in rules_doc.get("user_excluded", {}).get("items", [])}

    np = norm(path)
    exists, is_dir, size, mtime = _stat_lite(path)
    if size is None and known_size is not None:
        size = known_size
    import time
    age_days = (time.time() - mtime) / 86400.0 if exists else 0

    # 结构性红线最先判：盘根 / 用户根 / 系统根这类「容器」必须硬拦，
    # 不参与规则库竞争（否则一条泛化的 AMBER 规则就可能把它们放进来）。
    st_hit = structural_hit(np)
    if st_hit:
        return {
            "path": path, "norm": np, "exists": exists, "is_dir": is_dir,
            "bytes": 0, "bytes_known": False, "age_days": round(age_days, 1),
            "level": "RED", "rule_id": "STRUCT", "category": st_hit[0],
            "warning": st_hit[1],
            "advice": "这是容器而不是垃圾。只能在它**下面**挑具体项清理，永远不要删它本身。",
            "delay_seconds": 0, "block": True, "high_risk": True,
            "also_matched": [], "user_excluded": False,
        }

    cands = []          # (rank, -specificity, rule_id, rule)
    for r in rules_doc["rules"]:
        if r.get("id") in excluded:
            continue
        scope = r.get("scope", "any")
        if scope == "file" and is_dir:
            continue
        if scope == "dir" and not is_dir:
            continue
        if r.get("patterns"):
            matched = _glob_hit(np, r)
            if matched is None:
                continue
            spec = len(matched)
        else:
            # 条件规则：需要真实体积才能判断
            if size is None and exists and is_dir:
                size = dir_size(path)
            if not _cond_hit(size or 0, age_days, r):
                continue
            spec = 0
        level = r.get("level", "AMBER")
        cands.append((MATCH_RANK.get(level, 3), -spec, r.get("id") or "", r))

    if size is None and exists:
        size = dir_size(path) if is_dir else 0

    if cands:
        cands.sort(key=lambda t: (t[0], t[1], t[2]))
        r = cands[0][3]
        level = r.get("level", "AMBER")
        delay = r.get("delay_seconds", defaults.get("amber_delay_seconds", 8))
        if level in ("RED", "GREEN"):
            delay = 0
        # 红线项不计体积：它不进「可释放量」，而对 `C:\` 这类目录递归求体积
        # 等于全盘遍历（实测 2 分钟没跑完，看起来像卡死）。
        if size is None and exists and is_dir and level != "RED":
            size = dir_size(path)
        return {
            "path": path,
            "norm": np,
            "exists": exists,
            "is_dir": is_dir,
            "bytes": size if size is not None else 0,
            "bytes_known": size is not None,
            "age_days": round(age_days, 1),
            "level": level,
            "rule_id": r.get("id"),
            "category": r.get("category"),
            "warning": r.get("warning", ""),
            "advice": r.get("advice", ""),
            "delay_seconds": delay,
            "block": bool(r.get("block", False)),
            "high_risk": bool(r.get("high_risk", False)),
            "also_matched": [c[3].get("id") for c in cands[1:]],
            "user_excluded": False,
        }

    return {
        "path": path, "norm": np, "exists": exists, "is_dir": is_dir,
        "bytes": size if size is not None else 0, "bytes_known": size is not None,
        "age_days": round(age_days, 1),
        "level": "UNKNOWN", "rule_id": None, "category": "unregistered",
        "warning": "未登记目标：不在任何已知白名单或红线规则里，无法判断删了会损失什么。",
        "advice": "按 AMBER 处理：逐项确认；如是常见目标，请补进 risk_rules.json。",
        "delay_seconds": defaults.get("high_risk_delay_seconds", 15),
        "block": False, "high_risk": False, "also_matched": [], "user_excluded": False,
    }


def audit(paths, rules_doc=None, max_batch=None):
    """批量审计。输出含「检查量 vs 实际可释放量」两套数字，避免把扫描量当成果。"""
    rules_doc = rules_doc or load_rules()
    defaults = rules_doc.get("defaults", {})
    max_batch = max_batch or defaults.get("max_batch", 10)

    seen, items = set(), []
    for p in paths:
        np = norm(p)
        if np in seen:
            continue
        seen.add(np)
        items.append(classify(p, rules_doc))

    by_level = {"RED": [], "AMBER": [], "UNKNOWN": [], "GREEN": []}
    for it in items:
        by_level.setdefault(it["level"], []).append(it)

    blocked = by_level["RED"]
    confirm = by_level["AMBER"] + by_level["UNKNOWN"]
    green = by_level["GREEN"]

    checked_bytes = sum(i["bytes"] for i in items)
    reclaim_bytes = sum(i["bytes"] for i in green) + sum(i["bytes"] for i in confirm)
    blocked_bytes = sum(i["bytes"] for i in blocked)

    notes = []
    if blocked:
        notes.append("有 %d 项触发红线，必须从计划中移除（不是「需要确认」，是「不允许」）。"
                     % len(blocked))
    if len(items) > max_batch:
        notes.append("本批 %d 项 > 上限 %d 项。分批执行，每批后 measure_free.py 复测。"
                     % (len(items), max_batch))
    if confirm:
        need = max(i["delay_seconds"] for i in confirm)
        notes.append("有 %d 项需逐项确认，其中最长强制等待 %d 秒（防连点误删）。"
                     % (len(confirm), need))
    if any(i["level"] == "UNKNOWN" for i in items):
        notes.append("存在未登记目标：请先补规则，不要凭直觉删。")
    if not items:
        notes.append("输入为空：没有可审计的目标。")

    return {
        "rules_version": rules_doc.get("version"),
        "input_count": len(items),
        "max_batch": max_batch,
        "batch_ok": len(items) <= max_batch,
        "safe_to_execute": not blocked and bool(items),
        "requires_item_confirm": bool(confirm),
        "levels": {
            lv: {"count": len(v), "bytes": sum(x["bytes"] for x in v),
                 "size_unknown": sum(1 for x in v if not x.get("bytes_known", True))}
            for lv, v in by_level.items()
        },
        "checked_bytes": checked_bytes,
        "checked_gb": round(checked_bytes / 1073741824.0, 3),
        "reclaim_bytes": reclaim_bytes,
        "reclaim_gb": round(reclaim_bytes / 1073741824.0, 3),
        "blocked_bytes": blocked_bytes,
        "blocked": blocked,
        "needs_item_confirm": confirm,
        "batch_safe": green,
        "notes": notes,
    }


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f%s" % (n, unit) if unit != "B" else "%d%s" % (n, unit)
        n /= 1024.0


def to_markdown(res):
    """渲染成可直接贴进报告的表格。"""
    L = []
    L.append("### 清理计划风险登记（规则库 v%s）" % res.get("rules_version"))
    L.append("")
    L.append("| 级别 | 项数 | 体积 | 含义 |")
    L.append("|---|---|---|---|")
    meaning = {"GREEN": "纯缓存，可批量", "AMBER": "需逐项确认 + 延迟",
               "UNKNOWN": "未登记，按 AMBER 处理", "RED": "红线，禁止列入"}
    for lv in ("GREEN", "AMBER", "UNKNOWN", "RED"):
        d = res["levels"][lv]
        if d["count"]:
            vol = fmt_size(d["bytes"]) if d["bytes"] else ""
            if d.get("size_unknown"):
                vol = (vol + " + %d 项未统计" % d["size_unknown"]).strip(" +")
            L.append("| %s %s | %d | %s | %s |" % (LEVEL_ICON[lv], lv, d["count"], vol, meaning[lv]))
    L.append("")
    L.append("- 检查量（输入合计）：**%s**" % fmt_size(res["checked_bytes"]))
    L.append("- 可释放量（GREEN+AMBER）：**%s**" % fmt_size(res["reclaim_bytes"]))
    if res["blocked_bytes"]:
        L.append("- 红线体积（不计入可释放）：%s" % fmt_size(res["blocked_bytes"]))
    L.append("- 结论：%s" % ("可执行（需逐项确认）" if res["safe_to_execute"] else "禁止执行，先移除红线项"))
    L.append("")

    if res["blocked"]:
        L.append("#### 红线项（必须移除，不是「确认」）")
        for i in res["blocked"]:
            L.append("- `%s` — %s（%s）" % (i["path"], i["warning"], i["rule_id"]))
        L.append("")
    if res["needs_item_confirm"]:
        L.append("#### 需逐项确认")
        L.append("")
        L.append("| 路径 | 级别 | 体积 | 等待 | 会损失什么 |")
        L.append("|---|---|---|---|---|")
        for i in res["needs_item_confirm"]:
            L.append("| `%s` | %s | %s | %ds | %s |" % (
                i["path"], i["level"], fmt_size(i["bytes"]), i["delay_seconds"], i["warning"]))
        L.append("")
    if res["batch_safe"]:
        L.append("#### 可批量清理（%d 项 / %s）" % (
            len(res["batch_safe"]), fmt_size(sum(x["bytes"] for x in res["batch_safe"]))))
        L.append("")
    if res["notes"]:
        L.append("#### 纪律提示")
        for n in res["notes"]:
            L.append("- " + n)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="清理目标风险登记与二次确认（只读）")
    ap.add_argument("--path", action="append", default=[], help="待判定路径，可重复")
    ap.add_argument("--targets", help='JSON 文件，形如 {"paths": [...]}')
    ap.add_argument("--rules", help="自定义规则库（默认 references/risk_rules.json）")
    ap.add_argument("--list-rules", action="store_true", help="列出规则")
    ap.add_argument("--level", choices=["RED", "AMBER", "GREEN"], help="配合 --list-rules 过滤")
    ap.add_argument("--md", action="store_true", help="输出 Markdown 报告（默认 JSON）")
    ap.add_argument("--max-batch", type=int, help="本批上限，默认取规则库 defaults.max_batch")
    ap.add_argument("--out", help="把结果写入文件")
    ap.add_argument("--fail-on", choices=["red", "amber"], help="命中该级别时退出码 2（供自动化串联）")
    args = ap.parse_args()

    rules_doc = load_rules(args.rules)

    if args.list_rules:
        rows = [r for r in rules_doc["rules"] if not args.level or r.get("level") == args.level]
        for r in rows:
            cond = r.get("patterns") or []
            extra = ""
            if r.get("match_size_mb"):
                extra += " size>=%dMB" % r["match_size_mb"]
            if r.get("match_age_days"):
                extra += " age>=%dd" % r["match_age_days"]
            print("%-7s %-18s %-16s %s%s" % (
                r.get("level"), r.get("id"), r.get("category"), (cond[0] if cond else "(尺寸规则)"), extra))
            print("        警告: %s" % r.get("warning", ""))
        print("\n共 %d 条规则（v%s）" % (len(rows), rules_doc.get("version")))
        return 0

    paths = list(args.path)
    if args.targets:
        with open(args.targets, "r", encoding="utf-8") as f:
            doc = json.load(f)
        paths += doc.get("paths", []) if isinstance(doc, dict) else list(doc)
    if not paths:
        ap.error("需要 --path 或 --targets")

    res = audit(paths, rules_doc, args.max_batch)
    text = to_markdown(res) if args.md else json.dumps(res, ensure_ascii=False, indent=1)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)

    if args.fail_on == "red" and res["levels"]["RED"]["count"]:
        return 2
    if args.fail_on == "amber" and (res["levels"]["AMBER"]["count"] or res["levels"]["UNKNOWN"]["count"]):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
