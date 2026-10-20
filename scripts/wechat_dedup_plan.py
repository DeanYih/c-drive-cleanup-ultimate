#!/usr/bin/env python3
"""WeChat FileStorage/File deduplication planner (read-only).

Usage:
    python -S scripts/wechat_dedup_plan.py \
        --root <path-to-WeChat-Files> \
        --cache <path-to-cache.json> \
        --out <path-to-output-dir>

For every account under <root>, walks FileStorage/File, hashes each file
with md5 (block-by-block), groups files that share the same hash, and:

  * keeps one copy per group (shortest basename = most likely the original)
  * marks the rest for recycle-bin

Writes:
  <out>/file_dedup_plan.json  - the keep/delete plan
  <out>/wechat_dedup_report.html - interactive review report (search/filter)

This script never deletes anything.  Run scripts/wechat_dedup_recycle.py
with the plan JSON and --confirm to actually send duplicates to the
Windows Recycle Bin (recoverable, <=N per batch).
"""
import os, sys, json, argparse, hashlib, re, html

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fsutil_safe import iter_files  # noqa: E402  统一实现：不跟随 junction/symlink


CACHE_VERSION = 1
BLOCK = 1024 * 1024  # 1 MiB


def file_signature(p):
    try:
        st = os.stat(p)
        return (st.st_size, int(st.st_mtime), st.st_size)
    except OSError:
        return None


def md5_file(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        while True:
            b = f.read(BLOCK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def stem_of(path):
    """Strip trailing ' (n)' / '(n)(m)' decorative copies from a basename."""
    base = os.path.basename(path)
    name, _ext = os.path.splitext(base)
    name = re.sub(r"(\s*\(\d+\))+$", "", name)
    return name


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="WeChat Files root directory")
    ap.add_argument("--cache", required=True, help="JSON cache file path (reuse between runs)")
    ap.add_argument("--out", required=True, help="output directory for plan + HTML report")
    ap.add_argument("--min-mb", type=float, default=0.05,
                    help="只考虑大于此体积的文件（MB），默认 0.05。"
                         "微信附件里有几十万张缩略图级小图，全量哈希纯属浪费")
    ap.add_argument("--max-files", type=int, default=0,
                    help="最多哈希多少个文件（0=不限）。大库先小样本试跑，看命中率再放开")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit("ERROR: root not found: " + root)

    cache = {}
    if os.path.isfile(args.cache):
        try:
            cache = json.load(open(args.cache, encoding="utf-8"))
        except Exception:
            cache = {}

    files_to_scan = []
    # 同时支持两代微信的「收到的文件」目录：
    #   3.x  <root>/<账号>/FileStorage/File
    #   4.x  <root>/<账号>/msg/file          （账号目录形如 wxid_xxx_3f2f）
    # 旧实现只找 FileStorage/File —— 在真实 4.x 数据（本机 129GB / 52 万文件）上
    # 会**一个文件都扫不到**，然后输出「0 组重复」，看起来像"没有重复"，
    # 实际是根本没找对地方。这类"静默空结果"比报错难发现得多。
    scan_rels = [("FileStorage", "File"), ("msg", "file")]
    hit_dirs = 0
    for acct in sorted(os.listdir(root)):
        ap = os.path.join(root, acct)
        if not os.path.isdir(ap):
            continue
        for rel in scan_rels:
            d = os.path.join(ap, *rel)
            if not os.path.isdir(d):
                continue
            hit_dirs += 1
            # 用 iter_files（不跟随 junction/symlink）代替 os.walk：
            # os.walk 会跟进链接，把**目录树之外**的文件也列入 delete_path，
            # 执行端就会删到微信目录以外 —— 这是本技能最高危的一类错误。
            files_to_scan.extend(iter_files(d))
    if hit_dirs == 0:
        sys.exit("ERROR: 在 %s 下没有找到 FileStorage/File 或 msg/file 目录。\n"
                 "请确认 --root 指向微信账号根（文档\\WeChat Files 或 文档\\xwechat_files）。" % root)

    total = len(files_to_scan)
    min_bytes = int(args.min_mb * 1024 * 1024)
    kept = []
    for fp in files_to_scan:
        try:
            if os.lstat(fp).st_size < min_bytes:
                continue
        except OSError:
            continue
        kept.append(fp)
    skipped_small = total - len(kept)
    if args.max_files and len(kept) > args.max_files:
        kept = kept[:args.max_files]
    files_to_scan = kept
    print("候选文件 %d 个（跳过小于 %.2fMB 的 %d 个）%s"
          % (len(files_to_scan), args.min_mb, skipped_small,
             "，已按 --max-files 截断" if args.max_files and len(files_to_scan) >= args.max_files else ""),
          flush=True)

    hash_of = {}
    new_hashed = 0
    for i, fp in enumerate(files_to_scan, 1):
        sig = file_signature(fp)
        if sig is None:
            continue
        key = fp
        cached = cache.get(key)
        if cached and cached[0] == sig:
            hash_of[fp] = cached[1]
            continue
        try:
            h = md5_file(fp)
        except OSError:
            continue
        cache[key] = (sig, h)
        hash_of[fp] = h
        new_hashed += 1
        if new_hashed % 50 == 0:
            print("hashed %d new files (%.1fs)" % (new_hashed, 0.0), flush=True)

    # save cache
    with open(args.cache, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)

    # group by hash
    groups = {}
    for fp, h in hash_of.items():
        groups.setdefault(h, []).append(fp)

    unique_hashes = [h for h, lst in groups.items() if len(lst) == 1]
    dup_groups = [(h, lst) for h, lst in groups.items() if len(lst) > 1]

    keep = []
    delete = []
    flagged_groups = 0
    for h, lst in sorted(dup_groups, key=lambda kv: -len(kv[1])):
        # keep the one with the shortest basename; tie-breaker: earliest path
        lst_sorted = sorted(lst, key=lambda p: (len(os.path.basename(p)), p))
        kp = lst_sorted[0]
        dps = lst_sorted[1:]
        size = os.path.getsize(kp) if os.path.isfile(kp) else 0
        stems = {stem_of(p) for p in lst}
        if len(stems) > 1:
            flagged_groups += 1
        keep.append({"hash": h, "keep_path": kp, "size": size, "copies": len(lst)})
        for d in dps:
            delete.append({"hash": h, "delete_path": d, "size": size, "copies": len(lst)})

    to_delete_bytes = sum(d["size"] for d in delete)

    plan = {
        "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "root": root,
        "total_files_scanned": total,
        "unique": len(unique_hashes) + len(dup_groups),
        "dup_groups": len(dup_groups),
        "to_keep_count": len(keep),
        "to_delete_count": len(delete),
        "to_delete_bytes": to_delete_bytes,
        "flagged_groups": flagged_groups,
        "to_keep": keep,
        "to_delete": delete,
    }

    os.makedirs(args.out, exist_ok=True)
    plan_path = os.path.join(args.out, "file_dedup_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    # ---- HTML report ----
    def esc(s):
        return html.escape(s or "")

    rows = []
    # group deletes by keep_path so the HTML shows "keep + N deletes"
    del_by_hash = {}
    for d in delete:
        del_by_hash.setdefault(d["hash"], []).append(d["delete_path"])

    groups_for_html = []
    for k in keep:
        d_list = del_by_hash.get(k["hash"], [])
        groups_for_html.append({
            "keep": k["keep_path"],
            "deletes": d_list,
            "copies": k["copies"],
            "size": k["size"],
            "reclaim": k["size"] * max(0, len(d_list)),
            "flag": len({stem_of(k["keep_path"])} | {stem_of(p) for p in d_list}) > 1,
        })
    groups_for_html.sort(key=lambda g: -g["reclaim"])

    html_parts = ["""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WeChat FileStorage/File dedup review</title>
<style>
:root{--bg:#f6f8fb;--card:#fff;--ink:#1f2733;--sub:#5b6b7f;--line:#e6ebf1;
--accent:#2f6df6;--warn:#e8552a;--ok:#1a9d6b;--warnbg:#fff4ef;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
background:var(--bg);color:var(--ink);line-height:1.5;}
header{background:linear-gradient(135deg,#2f6df6,#5b8def);color:#fff;padding:22px 26px;}
header h1{margin:0 0 4px;font-size:22px}
header p{margin:0;opacity:.92;font-size:13px}
.wrap{max-width:1080px;margin:0 auto;padding:20px 18px 60px}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0 22px}
.card{flex:1 1 180px;background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:16px 18px;box-shadow:0 1px 3px rgba(20,40,80,.05)}
.card .n{font-size:26px;font-weight:800;color:var(--accent)}
.card .l{font-size:12px;color:var(--sub);margin-top:2px}
.card.warn .n{color:var(--warn)}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
input[type=text]{flex:1 1 260px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;font-size:14px}
.btn{padding:9px 14px;border:1px solid var(--line);border-radius:10px;background:#fff;cursor:pointer;font-size:13px;color:var(--ink)}
.btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}
th{background:#f0f4fa;color:var(--sub);font-weight:600;position:sticky;top:0}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}
.b-cnt{background:#eaf1ff;color:#2f6df6}
.b-flag{background:var(--warnbg);color:var(--warn)}
.b-ok{background:#e8f7f0;color:var(--ok)}
.keep{color:var(--ok);font-weight:600;word-break:break-all}
.del{color:var(--sub);word-break:break-all;font-size:12px}
details{margin-top:4px}
summary{font-size:12px;color:var(--accent);cursor:pointer}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:var(--sub)}
.note{background:#fff;border:1px solid var(--line);border-left:4px solid var(--warn);
border-radius:10px;padding:14px 16px;margin:18px 0;font-size:13px}
.foot{margin-top:22px;font-size:12px;color:var(--sub)}
</style></head><body>
<header><h1>WeChat FileStorage/File - dedup review</h1>
<p>generated __GEN__ - byte-identical files (md5). Only run delete after review.</p></header>
<div class="wrap">
  <div class="cards">
    <div class="card"><div class="n">__GROUPS__</div><div class="l">duplicate groups (keep 1 each)</div></div>
    <div class="card"><div class="n">__DEL__</div><div class="l">files safe to send to Recycle Bin</div></div>
    <div class="card"><div class="n">__GB__ GB</div><div class="l">reclaimable space</div></div>
    <div class="card warn"><div class="n">__FLAG__</div><div class="l">filename-mismatch groups (eyeball)</div></div>
  </div>
  <div class="note">
    <b>Plan:</b> every entry below will be sent to the Windows <b>Recycle Bin</b> (recoverable),
    in batches of <b>__BATCH__</b>.  Groups marked <span class="badge b-flag">filename mismatch</span>
    have byte-identical content but different filenames (e.g. a "Jan"
    rate-card vs a "Feb" rate-card sharing the same md5) -
    they are byte-identical so deleting them loses nothing, but if the filename matters for your
    own indexing, open the row and confirm.
  </div>
  <div class="toolbar">
    <input type="text" id="q" placeholder="search filename or path...">
    <button class="btn active" id="fAll" onclick="setF('all')">all</button>
    <button class="btn" id="fFlag" onclick="setF('flag')">flagged only</button>
    <span class="mono" id="cnt"></span>
  </div>
  <table>
    <thead><tr><th style="width:42px">#</th><th style="width:70px">copies</th>
      <th>kept (1)</th><th>duplicates</th><th style="width:90px">status</th></tr></thead>
    <tbody id="tb"></tbody>
  </table>
  <div class="foot">
    Keep rule: shortest basename per group.  Recycle Bin = recoverable; empty the bin only
    after you confirm nothing is needed.
  </div>
</div>
<script>
const DATA = __DATA__;
const tb=document.getElementById('tb'), q=document.getElementById('q'), cnt=document.getElementById('cnt');
let filt='all';
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function fmt(b){return (b/1048576).toFixed(2)+' MB';}
function row(g,i){
  let dels='';
  if(g.deletes.length){
    dels='<details><summary>show '+g.deletes.length+' duplicate paths</summary>'+
      g.deletes.map(d=>'<div class="del">'+esc(d)+'</div>').join('')+'</details>';
  }
  return '<tr>'+
    '<td>'+(i+1)+'</td>'+
    '<td><span class="badge b-cnt">'+g.copies+'x</span></td>'+
    '<td><div class="keep">'+esc(g.keep.split(/[\\\\/]/).pop())+'</div>'+
       '<div class="mono">'+esc(g.keep)+'</div></td>'+
    '<td><div class="del">reclaim '+fmt(g.reclaim)+' (drop '+g.deletes.length+')</div>'+dels+'</td>'+
    '<td>'+(g.flag?'<span class="badge b-flag">mismatch</span>':'<span class="badge b-ok">safe</span>')+'</td>'+
  '</tr>';
}
function render(){
  const t=q.value.trim().toLowerCase();
  let arr=DATA.groups.filter(g=>{
    if(filt==='flag'&&!g.flag)return false;
    if(!t)return true;
    const hay=(g.keep+' '+g.deletes.join(' ')).toLowerCase();
    return hay.includes(t);
  });
  tb.innerHTML=arr.map(row).join('');
  cnt.textContent='showing '+arr.length+' / '+DATA.groups.length+' groups';
}
function setF(f){filt=f;
  document.getElementById('fAll').classList.toggle('active',f==='all');
  document.getElementById('fFlag').classList.toggle('active',f==='flag');
  render();
}
q.addEventListener('input',render);
render();
</script></body></html>"""]
    html_parts = [
        html_parts[0]
        .replace("__GEN__", plan["generated_at"])
        .replace("__GROUPS__", str(plan["dup_groups"]))
        .replace("__DEL__", str(plan["to_delete_count"]))
        .replace("__GB__", "%.2f" % (plan["to_delete_bytes"] / 1e9))
        .replace("__FLAG__", str(plan["flagged_groups"]))
        .replace("__BATCH__", "10")
        .replace("__DATA__", json.dumps({"groups": groups_for_html}, ensure_ascii=False))
    ]
    html_path = os.path.join(args.out, "wechat_dedup_report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_parts[0])

    print("PLAN:", plan_path)
    print("HTML:", html_path)
    print("duplicates=%d  groups=%d  reclaim=%d bytes (%.2f GB)" %
          (plan["to_delete_count"], plan["dup_groups"], plan["to_delete_bytes"],
           plan["to_delete_bytes"] / 1e9))


if __name__ == "__main__":
    main()