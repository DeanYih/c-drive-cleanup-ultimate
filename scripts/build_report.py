# -*- coding: utf-8 -*-
"""生成单文件 HTML 体检报告 —— 给「不想看命令行」的人看的交付物。

为什么要有这个
--------------
此前本技能的产出是聊天里的一段 Markdown 表格。问题很实际：
  * 用户关掉对话就没了，不能再翻；
  * 想给家里人/同事看，只能截图；
  * 一边清一边对照，得在聊天记录里来回滚。

所以补一个「双击就能看」的报告。设计上有三条硬约束：

1. **纯静态 HTML，不写一行 JS。**
   让模型现场写 HTML/JS 是这类任务最常见的翻车点：写坏了浏览器**不报错**，
   页面就是一片空白，用户和模型都看不出问题。
   所以这里更彻底——模板由 Python 拼装，前端零脚本，最坏情况也只是样式朴素。
2. **每个数字都必须来自实测。** 报告里不出现"一般来说 C 盘 XX 占多少"这种先验；
   量不到的就不写（宁缺不编）。
3. **不写「本机没装 XX」这种行。** 十行里九行是"没有"，会把真正要看的淹掉。

用法
----
    python -S scripts/build_report.py --out "%USERPROFILE%\\Desktop\\C盘体检报告.html"
    python -S scripts/build_report.py --drive D: --out report.html
    python -S scripts/build_report.py --json data.json      # 只导出数据，不生成 HTML
"""
import argparse
import ctypes
import html
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scan_safe_targets as SST      # noqa: E402
import scan_tiers as ST              # noqa: E402
import risk_register as RR           # noqa: E402

MAX_ROWS = 40        # 表格上限：超过就折叠，避免"把整块硬盘倒进报告"
MAX_BIG = 15

# 红线里哪些**值得写进报告**。
# 反面教训：第一版把所有 RED 都列出来，结果"不要碰"里塞满
# `Program Files\...\msedge.dll`、`libcef.dll` 这种程序本体 ——
# 用户根本不会去删 dll，这些行只会把真正该警示的（聊天记录、虚拟机盘）
# 淹掉。报告的价值在于「改变用户决策」，列他不可能做的操作就是噪音。
_RED_NOISE_PREFIX = ("c:\\program files\\", "c:\\program files (x86)\\", "c:\\windows\\")


def worth_listing_red(path, category, is_dir):
    low = path.lower()
    if is_dir:
        return True                      # 目录级红线（聊天记录/项目数据）都要看
    if low.startswith(_RED_NOISE_PREFIX):
        # 程序本体里的单文件不列；只有虚拟机盘/加密容器这类"整块数据"值得提醒
        return category in ("encrypted-container", "pagefile", "swap")
    return True


def hsize(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def esc(s):
    return html.escape(str(s or ""), quote=True)


def mask_path(p):
    """把真实用户名换成 %USERPROFILE%，避免用户截图转发泄露。"""
    up = os.environ.get("USERPROFILE") or ""
    if up and p.lower().startswith(up.lower()):
        return "%USERPROFILE%" + p[len(up):]
    return p


def disk_usage(drive):
    root = drive.rstrip("\\/") + "\\"
    free = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p(root), None, ctypes.byref(total), ctypes.byref(free))
    t, f = total.value, free.value
    return {"total": t, "free": f, "used": max(0, t - f),
            "pct": round((t - f) * 100.0 / t, 1) if t else 0.0}


def collect(drive="C:"):
    """跑一遍只读扫描，聚合成报告数据。全程不删任何东西。

    数据源必须是三类，只用白名单会得出「很干净」的假结论：
      1. 白名单缓存目录   —— 给 GREEN（能直接清）
      2. 应用数据大户     —— 给 AMBER（能清但有代价，多数更适合迁移）
      3. 大文件 Top N     —— 可能命中红线（虚拟机盘、备份、镜像）
    只扫第 1 类的话，报告会写「需确认 0 B / 红线 0 项」，
    而实际上本机 WPS 2.15 GB、Edge 1.19 GB、微信 1.09 GB 全都躺在那儿。
    """
    rules = RR.load_rules()
    cat = SST.collect()
    by_path = {}

    def add(path, size, extra=None):
        c = RR.classify(path, rules, known_size=size)
        rec = {
            "path": path, "bytes": size, "level": c["level"], "rule": c["rule_id"],
            "warning": c["warning"], "advice": c["advice"], "delay": c["delay_seconds"],
            "category": c["category"], "is_dir": c["is_dir"],
        }
        if extra:
            rec.update(extra)
        old = by_path.get(path)
        # 同一路径出现多次时，保留信息更全的那条（应用名/迁移方式）
        if old is None or (extra and not old.get("app")):
            by_path[path] = rec

    for path, size in cat["categories"].items():
        add(path, size)

    # 应用数据大户：判级 + 顺带给出「怎么搬」的官方入口
    try:
        import locate_app_caches as LAC
        profile = os.environ.get("USERPROFILE") or ""
        for app, cands in LAC.build_catalog(profile):
            redirect = LAC.REDIRECT.get(app)
            for p in cands:
                p = p.replace("{profile}", profile)
                if not os.path.isdir(p) or p in by_path:
                    continue
                try:
                    from fsutil_safe import dir_size
                    sz = dir_size(p)
                except Exception:
                    sz = 0
                if sz <= 0:
                    continue
                add(p, sz, {"app": app,
                            "how": (redirect[1] if redirect else ""),
                            "auto": bool(redirect[2]) if redirect else False})
    except Exception:
        pass

    # 大文件：命中的红线项（虚拟机盘/加密容器/备份）要进"不要碰"
    root = drive.rstrip("\\/") + "\\"
    r = ST.scan([root], 100, ST.DEFAULT_TIERS, 0, ST.DEFAULT_SKIP, False)
    exclusive, big_bytes = ST.bucketize(
        r["found"], sorted(ST.DEFAULT_TIERS, reverse=True), r["tree_bytes"])
    top_all = sorted(r["found"], key=lambda x: -x[0])
    for b, p in top_all[:60]:
        if p in by_path:
            continue
        c = RR.classify(p, rules, known_size=b)
        if c["level"] in ("RED", "AMBER"):
            by_path[p] = {"path": p, "bytes": b, "level": c["level"], "rule": c["rule_id"],
                          "warning": c["warning"], "advice": c["advice"],
                          "delay": c["delay_seconds"]}
    top = top_all[:MAX_BIG]
    spots, _ = ST.probe_links(r["links"], 10, [root])
    real_spots = [b for b in spots if b.get("verdict") not in ("internal", "cross-drive")]

    # 系统级大户：Windows.edb / 休眠 / 分页 / 还原点 / WinSxS 组件
    # 这些占了很大空间，但**一个都不能用普通删除**，所以单独成板块、只给正确做法。
    system = None
    try:
        import scan_system_targets as SST2
        system = SST2.collect()
    except Exception:
        system = None

    # 微信（本机有才算，没有就不写这一块）
    wx = None
    try:
        import wechat_analyze as WA
        cand = os.path.join(os.environ.get("APPDATA") or "", "Tencent", "xwechat")
        if os.path.isdir(cand):
            layout, why = WA.detect_layout(cand)
            if layout == "program-data":
                wx = WA.analyze_program_data(cand, 10)
    except Exception:
        wx = None

    by_level = {"GREEN": [], "AMBER": [], "RED": [], "UNKNOWN": []}
    items = sorted(by_path.values(), key=lambda i: -i["bytes"])
    for i in items:
        by_level.setdefault(i["level"], []).append(i)

    # 红线拆分：值得展示的 vs 程序本体里的噪音（只计数，不逐条列）
    red_listed = [i for i in by_level["RED"]
                  if worth_listing_red(i["path"], i.get("category"), i.get("is_dir"))]
    red_hidden = len(by_level["RED"]) - len(red_listed)

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "drive": drive,
        "disk": disk_usage(drive),
        "items": items,
        "by_level": by_level,
        "red_listed": red_listed,
        "red_hidden": red_hidden,
        "totals": {k: sum(x["bytes"] for x in v) for k, v in by_level.items()},
        "tiers": exclusive,
        "big": {"count": len(r["found"]), "bytes": big_bytes,
                "pct": round(big_bytes * 100.0 / r["tree_bytes"], 1) if r["tree_bytes"] else 0.0},
        "scanned_files": r["scanned"],
        "top": [{"path": p, "bytes": b} for b, p in top],
        "blindspots": real_spots,
        "link_total": len(r["links"]),
        "excluded": [s for s in ST.DEFAULT_SKIP
                     if os.path.normcase(s).startswith(os.path.normcase(root))],
        "wechat": wx,
        "system": system,
        "rules_version": rules.get("version"),
        "warnings": cat.get("warnings") or [],
    }


CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#1c1f23;--mut:#6b7280;--line:#e5e7eb;
--green:#15803d;--greenbg:#f0fdf4;--amber:#b45309;--amberbg:#fffbeb;--red:#b91c1c;--redbg:#fef2f2;}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px 64px;background:var(--bg);color:var(--ink);
font:16px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:960px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px;letter-spacing:.3px}
h2{font-size:19px;margin:36px 0 12px;padding-left:10px;border-left:4px solid var(--ink)}
.meta{color:var(--mut);font-size:13px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin-bottom:16px}
.bar{height:12px;background:#eceff3;border-radius:6px;overflow:hidden;margin:12px 0 8px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#f59e0b,#dc2626)}
.big{font-size:22px;font-weight:600}
.row{display:flex;gap:16px;flex-wrap:wrap}
.row>.card{flex:1 1 200px;margin:0}
.k{font-size:13px;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:8px}
th{text-align:left;font-weight:600;color:var(--mut);font-size:12.5px;
padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid #f1f2f4;vertical-align:top}
tr:last-child td{border-bottom:0}
td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
code{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:12.5px;
word-break:break-all;font-family:ui-monospace,Consolas,monospace}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;white-space:nowrap}
.g{background:var(--greenbg);color:var(--green)}
.a{background:var(--amberbg);color:var(--amber)}
.r{background:var(--redbg);color:var(--red)}
.u{background:#f3f4f6;color:var(--mut)}
.note{font-size:13.5px;color:var(--mut)}
.hl{background:var(--amberbg);border-left:3px solid var(--amber);
padding:10px 14px;border-radius:0 6px 6px 0;font-size:14px;margin:10px 0}
.ok{background:var(--greenbg);border-left:3px solid var(--green);
padding:10px 14px;border-radius:0 6px 6px 0;font-size:14px;margin:10px 0}
ol.steps{padding-left:22px}ol.steps li{margin:8px 0}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);
color:var(--mut);font-size:12.5px}
@media print{body{background:#fff;padding:0}.card{break-inside:avoid}}
"""


def render(d):
    L = []
    a = L.append
    dis = d["disk"]
    a('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width,initial-scale=1">')
    a('<title>%s 盘体检报告 %s</title><style>%s</style></head><body><div class="wrap">'
      % (esc(d["drive"]), esc(d["generated_at"][:10]), CSS))
    a('<h1>%s 盘体检报告</h1>' % esc(d["drive"]))
    a('<div class="meta">生成于 %s ｜ 本次共扫描 %s 个文件 ｜ 全程只读，'
      '本报告生成过程中没有删除、移动或修改任何文件</div>'
      % (esc(d["generated_at"]), format(d["scanned_files"], ",")))

    # ---- 容量 ----
    a('<div class="card"><div class="k">磁盘占用</div>')
    a('<div class="big">已用 %s / 共 %s　<span class="k">剩余 %s</span></div>'
      % (hsize(dis["used"]), hsize(dis["total"]), hsize(dis["free"])))
    a('<div class="bar"><i style="width:%.1f%%"></i></div>' % min(100.0, dis["pct"]))
    a('<div class="k">占用率 %.1f%%</div>' % dis["pct"])
    for w in d["warnings"]:
        a('<div class="hl">%s</div>' % esc(w))
    a('</div>')

    # ---- 汇总 ----
    t = d["totals"]
    a('<div class="row">')
    a('<div class="card"><div class="k">可安全清理</div><div class="big" style="color:var(--green)">%s</div>'
      '<div class="k">%d 项纯缓存/临时文件</div></div>'
      % (hsize(t["GREEN"]), len(d["by_level"]["GREEN"])))
    a('<div class="card"><div class="k">需逐项确认</div><div class="big" style="color:var(--amber)">%s</div>'
      '<div class="k">%d 项，删了会有代价</div></div>'
      % (hsize(t["AMBER"] + t["UNKNOWN"]), len(d["by_level"]["AMBER"]) + len(d["by_level"]["UNKNOWN"])))
    a('<div class="card"><div class="k">红线（不可删）</div><div class="big" style="color:var(--red)">%s</div>'
      '<div class="k">%d 项 / 共 %s　本报告不会建议清理这些</div></div>'
      % (hsize(t["RED"]) if t["RED"] else "—", len(d["by_level"]["RED"]),
         hsize(t["RED"]) if t["RED"] else "0"))
    a('</div>')

    # ---- 可安全清理 ----
    a('<h2>可以放心清理</h2>')
    a('<div class="card">')
    if d["by_level"]["GREEN"]:
        a('<div class="ok">这些是缓存、临时文件和日志。删掉之后系统和软件会自动重建，'
          '唯一的代价是下次启动稍慢一点。</div>')
        a('<table><tr><th>位置</th><th class="num">占用</th></tr>')
        for i in d["by_level"]["GREEN"][:MAX_ROWS]:
            a('<tr><td><code>%s</code></td><td class="num">%s</td></tr>'
              % (esc(mask_path(i["path"])), hsize(i["bytes"])))
        a('</table>')
        if len(d["by_level"]["GREEN"]) > MAX_ROWS:
            a('<div class="note">另有 %d 项未列出。</div>' % (len(d["by_level"]["GREEN"]) - MAX_ROWS))
    else:
        a('<div class="note">本机没有扫到可批量清理的缓存目录。</div>')
    a('</div>')

    # ---- 需确认 ----
    a('<h2>要动之前请想清楚</h2>')
    a('<div class="card">')
    conf = d["by_level"]["AMBER"] + d["by_level"]["UNKNOWN"]
    if conf:
        a('<table><tr><th>位置</th><th class="num">占用</th><th>删了会怎样</th><th>建议</th></tr>')
        for i in conf[:MAX_ROWS]:
            cls = "a" if i["level"] == "AMBER" else "u"
            if i.get("app"):
                where = '%s<br><code>%s</code>' % (esc(i["app"]), esc(mask_path(i["path"])))
                how = esc(i.get("how") or "建议迁移而非删除")
                if i.get("auto"):
                    how += '<br><span class="tag g">可直接代改</span>'
            else:
                where = '<code>%s</code>' % esc(mask_path(i["path"]))
                how = "按需评估"
            a('<tr><td>%s<br><span class="tag %s">%s</span></td>'
              '<td class="num">%s</td><td class="note">%s</td><td class="note">%s</td></tr>'
              % (where, cls, esc(i["level"]), hsize(i["bytes"]), esc(i["warning"]), how))
        a('</table>')
    else:
        a('<div class="note">没有需要逐项确认的项目。</div>')
    a('</div>')

    # ---- 红线 ----
    red_list = d.get("red_listed", d["by_level"]["RED"])
    if red_list:
        a('<h2>不要碰</h2>')
        a('<div class="card"><div class="hl">下面这些不是「垃圾」，删了就是真损失。'
          '本报告只把它们标出来，不会给出清理建议。</div>')
        a('<table><tr><th>位置</th><th class="num">占用</th><th>为什么不能删</th></tr>')
        for i in red_list[:MAX_ROWS]:
            a('<tr><td><code>%s</code></td><td class="num">%s</td><td class="note">%s</td></tr>'
              % (esc(mask_path(i["path"])), hsize(i["bytes"]), esc(i["warning"])))
        a('</table>')
        if d.get("red_hidden"):
            a('<div class="note">另有 %d 个系统/程序文件同样属于红线（如 Program Files 下的'
              '运行库），列出来没有意义，已略过——它们本来就不在清理范围内。</div>'
              % d["red_hidden"])
        a('</div>')

    # ---- 大文件 ----
    a('<h2>大文件（≥100 MB）</h2>')
    a('<div class="card">')
    a('<table><tr><th>档位</th><th class="num">文件数</th><th class="num">体积</th></tr>')
    for t2 in d["tiers"]:
        a('<tr><td>%s</td><td class="num">%d</td><td class="num">%s</td></tr>'
          % (esc(t2["label"]), t2["count"], hsize(t2["bytes"])))
    a('</table>')
    a('<div class="note">大文件合计 %s，占已扫描内容的 %.1f%%。体积大不等于没用，'
      '请逐个判断。</div>' % (hsize(d["big"]["bytes"]), d["big"]["pct"]))
    if d["top"]:
        a('<table><tr><th>体积</th><th>路径</th></tr>')
        for x in d["top"]:
            a('<tr><td class="num">%s</td><td><code>%s</code></td></tr>'
              % (hsize(x["bytes"]), esc(mask_path(x["path"]))))
        a('</table>')
    a('</div>')

    # ---- 盲区 ----
    if d["blindspots"]:
        a('<h2>扫不进去的地方（说明，不是遗漏）</h2>')
        a('<div class="card"><div class="note">下面这些是链接或云盘挂载点，'
          '刻意没有进入统计——跟进去会把别的盘、云端的数据算成 C 盘占用，数字就虚高了。'
          '列在这里是为了让你知道边界在哪儿。</div>')
        a('<table><tr><th>位置</th><th class="num">逻辑大小</th>'
          '<th class="num">本地实际占用</th><th>性质</th></tr>')
        vt = {"cloud-only": "云端占位，本地不占空间", "local-data": "本地真实数据",
              "empty": "空/读不到"}
        for b in d["blindspots"]:
            a('<tr><td><code>%s</code></td><td class="num">%s</td>'
              '<td class="num">%s</td><td class="note">%s</td></tr>'
              % (esc(mask_path(b["path"])), hsize(b.get("logical_bytes")),
                 hsize(b.get("local_bytes")), esc(vt.get(b.get("verdict"), "未知"))))
        a('</table></div>')

    # ---- 系统级大户 ----
    sysd = d.get("system")
    if sysd and sysd.get("items"):
        a('<h2>系统级大户（占了很大空间，但不能用普通删除）</h2>')
        a('<div class="card"><div class="note">这些是 C 盘真正的大头，但删法都特殊：'
          'pagefile 删了蓝屏、WinSxS 手删毁系统、搜索索引要先停服务。'
          '这里只列出<b>正确做法</b>，不提供清理按钮。</div>')
        a('<table><tr><th>项目</th><th class="num">体积</th><th>正确做法</th></tr>')
        for i in sysd["items"]:
            size = hsize(i["bytes"]) if i.get("bytes") else '<span class="k">需管理员</span>'
            if i.get("reclaimable"):
                size += '<br><span class="k">可回收 %s</span>' % hsize(i["reclaimable"])
            note = i.get("note") or ""
            a('<tr><td>%s<br><code>%s</code><br><span class="tag u">%s</span></td>'
              '<td class="num">%s</td><td class="note">%s%s</td></tr>'
              % (esc(i["name"]), esc(mask_path(i.get("path") or "-")),
                 esc(i.get("verdict", "")), size, esc(i.get("how", "")),
                 ('<br><span class="k">%s</span>' % esc(note)) if note else ""))
        a('</table>')
        if sysd.get("known_bytes"):
            a('<div class="note">已测到的体积合计 %s，其余需要管理员权限才能测量。</div>'
              % hsize(sysd["known_bytes"]))
        a('</div>')

    # ---- 微信 ----
    if d["wechat"]:
        w = d["wechat"]
        a('<h2>微信占用</h2>')
        a('<div class="card">')
        a('<div class="note">%s</div>' % esc(w["note"]))
        a('<table><tr><th>目录</th><th class="num">占用</th><th>能不能删</th></tr>')
        for r2 in w["dirs"][:MAX_BIG]:
            v = r2["verdict"]
            cls = "g" if v == "可清" else ("a" if v == "谨慎" else "u")
            a('<tr><td><code>%s</code></td><td class="num">%s</td>'
              '<td><span class="tag %s">%s</span> <span class="note">%s</span></td></tr>'
              % (esc(r2["name"]), hsize(r2["bytes"]), cls, esc(v), esc(r2["why"])))
        a('</table>')
        a('<div class="note">明确可清约 <b>%s</b>（删之前先退出微信）。</div>'
          % hsize(w["reclaimable_bytes"]))
        a('</div>')

    # ---- 怎么做 ----
    a('<h2>照着做</h2>')
    a('<div class="card"><ol class="steps">')
    a('<li><b>先清「可以放心清理」里的东西。</b>按体积从大到小，一次清三五个，'
      '清完回来看一眼容量有没有涨。</li>')
    a('<li><b>「要动之前请想清楚」里的项目，逐条看「删了会怎样」再决定。</b>'
      '拿不准就留着——腾空间是次要的，不丢东西是主要的。</li>')
    a('<li><b>大文件不要急着一删了之。</b>图片、视频、安装包这类东西，'
      '更划算的做法是搬到别的盘（本技能可以代做，迁移有留底可回滚）。</li>')
    a('</ol>')
    a('<div class="hl"><b>清完记得清空回收站。</b>文件被送进回收站后，'
      '磁盘空间并没有真正释放——它还在 C 盘上，只是换了个位置。'
      '进回收站点一下「清空回收站」，容量才会变。这是「我明明清了却没见空间涨」最常见的原因。</div>')
    a('<div class="note">想清得更彻底（系统组件、还原点、休眠文件），'
      '这些都要走 Windows 自带工具：DISM 组件清理、系统保护设置、'
      '<code>powercfg /h off</code>。手动删这些目录会损坏系统。</div>')
    a('</div>')

    a('<footer>规则库 v%s ｜ 扫描 %s 个文件 ｜ %s<br>'
      '本报告由 c-drive-cleanup-ultimate 生成，全部数据来自本机实测，未联网、未上传。'
      '路径中的用户名已隐去。</footer>'
      % (esc(d["rules_version"]), format(d["scanned_files"], ","), esc(d["generated_at"])))
    a('</div></body></html>')
    return "".join(L)


def main():
    ap = argparse.ArgumentParser(description="生成单文件 HTML 体检报告（只读）")
    ap.add_argument("--drive", default="C:", help="要体检的盘符，默认 C:")
    ap.add_argument("--out", help="输出 HTML 路径")
    ap.add_argument("--json", help="只导出扫描数据 JSON（不生成 HTML）")
    args = ap.parse_args()

    d = collect(args.drive)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        print("数据已写入 %s" % args.json)
        return 0

    out = args.out or os.path.join(os.path.expanduser("~"), "Desktop",
                                   "C盘体检报告-%s.html" % time.strftime("%Y-%m-%d"))
    d2 = os.path.dirname(os.path.abspath(out))
    if d2 and not os.path.isdir(d2):
        os.makedirs(d2, exist_ok=True)
    html_text = render(d)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(html_text)
    print("报告已生成: %s（%.1f KB）" % (out, len(html_text.encode("utf-8")) / 1024.0))
    print("可清 %s ｜ 需确认 %s ｜ 红线 %d 项"
          % (hsize(d["totals"]["GREEN"]),
             hsize(d["totals"]["AMBER"] + d["totals"]["UNKNOWN"]),
             len(d["by_level"]["RED"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
