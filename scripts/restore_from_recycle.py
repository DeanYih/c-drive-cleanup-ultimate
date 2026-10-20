# -*- coding: utf-8 -*-
r"""回收站元数据解析与误删恢复
用法:
    python restore_from_recycle.py --list              # 列出各盘回收站内容（原路径/大小/删除时间）
    python restore_from_recycle.py --list --find 关键词 # 按原路径关键词过滤
    python restore_from_recycle.py --restore $XXXXXXX  # 恢复指定 $I 标识的项到原路径

原理: 回收站位于 盘符\$RECYCLE.BIN\<SID>\，$I 开头为元数据（含原路径/大小/删除时间），
$I 之后数字相同的 $R 文件即被删内容。恢复 = 把 $R 内容移回原路径。
"""
import os
import re
import sys
import struct
import shutil
import ctypes
from ctypes import wintypes
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_DRIVE_RE = re.compile(r'^[A-Za-z]:\\')


def parse_i_file(path):
    """解析 $I 元数据文件（Vista+ 格式）。

    【真机实测更正 2026-10-20】流传最广的 version 2 布局是
    `header(24) + pathlen(4) + 12 bytes unknown + path`，
    但 Win10 22H2 真机导出的 $I 实测为：

        0000  02 00 ...             version = 2
        0008  20 03 00 00 ...       filesize = 800
        0010  00 5c f2 69 3f 60 dd 01   FILETIME
        0018  37 00 00 00           pathlen = 0x37 = 55 **字符**
        001c  45 00 3a 00 5c 00 ...  'E:\\'  <- 路径直接从 offset 28 开始

    **没有那 12 个字节**。按"多 12 字节"解析会让起点后移 6 个字符，
    恢复时把 `E:\\Workbuddyarea\\...` 还原成 `kbuddyarea\\...`（实测事故，
    文件被搬到一个不存在的前缀下，用户等于丢文件）。

    所以这里不赌布局：按 28 解析 -> 校验盘符 -> 失败则全文搜 `X:\\` 兜底。
    """
    try:
        with open(path, 'rb') as f:
            raw = f.read(4096)
    except OSError:
        return None
    if len(raw) < 28:
        return None
    version, filesize = struct.unpack('<QQ', raw[:16])
    if version not in (1, 2):
        return None
    ftime = struct.unpack('<Q', raw[16:24])[0]

    orig = None
    n = struct.unpack('<I', raw[24:28])[0]
    if 0 < n < 2000:
        cand = raw[28:28 + n * 2].decode('utf-16-le', errors='replace').rstrip('\x00')
        if _DRIVE_RE.match(cand):
            orig = cand
    if orig is None:
        # 兜底：在 24 字节之后按偶数偏移找 "X:\"
        for off in range(24, min(len(raw) - 6, 160), 2):
            if (raw[off + 1] == 0 and raw[off + 2] == 0x3a and raw[off + 3] == 0
                    and raw[off + 4] == 0x5c and raw[off + 5] == 0
                    and 0x41 <= raw[off] <= 0x5a):
                orig = raw[off:].decode('utf-16-le', errors='replace').rstrip('\x00')
                break
    if not orig or not _DRIVE_RE.match(orig):
        return None

    # Windows FILETIME -> datetime
    ts = ftime / 10 ** 7 - 11644473600
    dt = datetime.fromtimestamp(ts) if 0 < ts < 4102444800 else None
    return {'path': orig, 'size': filesize, 'deleted': dt}


def recycle_items():
    """遍历所有盘的回收站，产出 (i_file, meta, r_path)

    盘符列表原来写死 'CDEFGH'，遇到 I:/X: 这类盘上的回收站会整段漏掉。
    改成 A-Z 全量枚举（不存在的盘 isdir 直接跳过，成本可忽略）。
    """
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        rb = f"{letter}:\\$RECYCLE.BIN"
        if not os.path.isdir(rb):
            continue
        try:
            sids = os.listdir(rb)
        except OSError:
            continue
        for sid in sids:
            sid_path = os.path.join(rb, sid)
            if not os.path.isdir(sid_path):
                continue
            try:
                for name in os.listdir(sid_path):
                    if name.startswith('$I'):
                        i_path = os.path.join(sid_path, name)
                        meta = parse_i_file(i_path)
                        if meta:
                            r_path = os.path.join(sid_path, '$R' + name[2:])
                            yield i_path, meta, r_path
            except OSError:
                pass


def fmt_size(n):
    if n >= 1024 ** 3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024:.1f} KB"


def list_items(keyword=None):
    print(f"{'标识':<14}{'大小':>10}  {'删除时间':<20}原路径")
    count = 0
    for i_path, meta, r_path in recycle_items():
        if keyword and keyword.lower() not in meta['path'].lower():
            continue
        dt = meta['deleted'].strftime('%Y-%m-%d %H:%M') if meta['deleted'] else '?'
        tag = os.path.basename(i_path)[2:]
        exists = '有' if os.path.exists(r_path) else '无'
        print(f"{tag:<14}{fmt_size(meta['size']):>10}  {dt:<20}{meta['path']} [内容{exists}]")
        count += 1
    print(f"共 {count} 项")


def restore(tag):
    """按 $I 标识**或原路径**恢复到原路径。

    支持原路径是因为标识（`MQT1BW.txt` 这种）对用户毫无意义：
    用户只知道文件原来在哪。两种写法都收，减少"找不到标识"的挫败感。
    """
    tag_norm = os.path.normcase(tag).replace('/', '\\')
    for i_path, meta, r_path in recycle_items():
        same_tag = os.path.basename(i_path)[2:] == tag
        same_path = os.path.normcase(meta['path']).replace('/', '\\') == tag_norm
        if not (same_tag or same_path):
            continue
        dst = meta['path']
        # 防呆：元数据损坏时 dst 可能不是绝对路径，shutil.move 会把它写到
        # 当前工作目录（实测曾出现 `kbuddyarea\tmp\...` 这种相对路径）。
        # 恢复是"把文件搬回去"，目标不对等于二次丢数据，必须硬拦。
        if not _DRIVE_RE.match(dst):
            print(f"❌ 元数据里的原路径不是绝对路径，拒绝恢复: {dst!r}")
            return
        if not os.path.exists(r_path):
            print(f"❌ 对应内容文件缺失: {r_path}")
            return
        if os.path.exists(dst):
            print(f"❌ 原路径已存在，避免覆盖: {dst}")
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(r_path, dst)
        os.remove(i_path)
        print(f"✅ 已恢复到原路径: {dst}")
        return
    print(f"未找到标识或原路径 {tag}，用 --list 查看可用项")


if __name__ == '__main__':
    args = sys.argv[1:]
    if not args or args[0] == '--list':
        kw = None
        if '--find' in args:
            kw = args[args.index('--find') + 1]
        list_items(kw)
    elif args[0] == '--restore' and len(args) >= 2:
        restore(args[1])
    else:
        print(__doc__)
