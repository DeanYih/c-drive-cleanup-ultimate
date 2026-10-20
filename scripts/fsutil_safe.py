# -*- coding: utf-8 -*-
"""文件系统安全原语 —— 统一处理 Windows junction / symlink。

为什么需要这个模块
------------------
Windows 上 junction（目录联接）是「目录 + 重解析属性」，导致两个直觉上正确的
API 都会给出误导性结果：

    os.path.islink(p)                          -> 对 junction 返回 **False**（只认 symlink）
    DirEntry.is_dir(follow_symlinks=False)     -> 对 junction 返回 **True**（junction 确实是目录）

后果是 junction 被当成普通目录，遍历时跟进去：
  * 体积统计把链接指向的**外部**数据算进来 -> 报告虚高、用户清理后空间不涨
  * 重复计数 / 校验失真（源和目标都跟随，两边同样虚高，差异被掩盖 -> 静默通过）
  * 自引用链接导致无限递归（实测可钻到 WinError 206 路径超长）
  * 去重类工具若跟随链接，可能把**目录树之外**的文件列入删除计划

唯一可靠判断：
    os.lstat(p).st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT

纪律
----
凡涉及「目录遍历 / 体积统计 / 链接判断」的脚本**必须**用本模块，
不要各自手写 os.walk 或 dir_size —— 本技能历史上 4 份独立实现全部踩了同一个坑。
"""
import os
import stat

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000            # 数据不在本地（云占位符）
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000


def is_cloud_placeholder(st):
    """True 表示这是「云文件占位符」：逻辑大小很大，本地只有元数据。

    实测本机 WPS 云盘挂载点里每个文件属性都是 4199968
    (= ARCHIVE|REPARSE_POINT|OFFLINE|RECALL_ON_DATA_ACCESS)，逻辑大小
    113.6GB，而 C 盘总共才用掉 119.7GB —— 显然那些字节不在本地。
    """
    attr = getattr(st, "st_file_attributes", 0)
    return bool(attr & (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))


def is_reparse_point(path):
    """True 表示 path 本身是 junction / symlink / 挂载点（不跟随）。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def is_link_like(path):
    """symlink 或 junction 都算。os.path.islink() 会漏掉 junction，故用它代替。"""
    return is_reparse_point(path)


def resolve_link_target(path):
    """解析 junction/symlink 指向的真实路径；失败返回 None。

    替代缺失的 _winapi.ReadJunction（CPython 的 _winapi 只有 CreateJunction，
    没有 ReadJunction）。os.path.realpath 在 Windows 上会调用
    GetFinalPathNameByHandleW，能正确解开 junction。
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    if os.path.normcase(os.path.normpath(real)) == os.path.normcase(os.path.normpath(os.path.abspath(path))):
        return None      # 不是链接（解析结果与自身相同）
    return real


def _entry_stat(e):
    """从 DirEntry 取一次 stat，同时判定"是否应跳过（不跟随）"。

    性能关键：DirEntry.stat() 会**缓存**结果，但 os.lstat(e.path) 是一次全新的
    系统调用。为了判重解析点而额外调 lstat，会让每条目产生 2 次系统调用
    （实测在真实目录上慢 2~11 倍）。因此这里只做一次 stat，从其属性位判断。
    返回 (st, skip)；stat 失败返回 (None, False)。

    **只有目录的重解析属性才意味着"跟进去会跑到树外/循环"。**
    文件带 REPARSE_POINT 但不是 symlink 的典型是 Win10 CompactOS / WOF 系统压缩
    —— 微软用重解析点承载压缩数据流，文件属性里连 COMPRESSED 位都没有
    （本机 CompactOS 未启用，造不出样本，但逻辑上必须区分，否则在启用了
    CompactOS 的机器上会失效）。旧实现把这类文件当链接丢掉，
    后果是**体积少算**：目录显示得比实际小，用户清完发现空间没涨那么多。
    """
    try:
        st = e.stat(follow_symlinks=False)
    except OSError:
        return None, False
    if stat.S_ISLNK(st.st_mode):
        return st, True
    attr = getattr(st, "st_file_attributes", 0)
    if stat.S_ISDIR(st.st_mode) and (attr & FILE_ATTRIBUTE_REPARSE_POINT):
        return st, True
    return st, False


def dir_size(path):
    """目录自有内容字节数 —— 不跟随 junction/symlink。

    用 os.scandir 显式实现：先判链接再决定是否入栈。
    注意顺序：不能只靠 is_dir(follow_symlinks=False) —— junction 确实是目录，
    该判断对它返回 True，必须看重解析属性位。
    """
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    st, is_link = _entry_stat(e)
                    if st is None or is_link:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(e.path)
                    else:
                        total += st.st_size
        except OSError:
            pass
    return total


def count_tree(path):
    """返回 (文件数, 总字节数)，不跟随 junction/symlink。"""
    files = size = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    st, is_link = _entry_stat(e)
                    if st is None or is_link:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(e.path)
                    else:
                        files += 1
                        size += st.st_size
        except OSError:
            pass
    return files, size


def iter_files(root, skip_dirs=None, on_link=None):
    """递归产出文件路径 —— os.walk 的安全替代（不跟随 junction/symlink）。

    os.walk 默认会跟进 junction，用于去重/删除计划时可能把树外文件卷进来。

    skip_dirs: 需要**剪枝**的目录前缀列表（不进入其子树）。
    为什么需要显式剪枝：只在取文件时做前缀过滤，仍然要遍历整棵子树 ——
    对 WinSxS、Windows.old 这类几十万文件的目录，光"路过"就要几分钟。
    常用值见 scan_tiers.DEFAULT_SKIP。

    on_link: 回调 `on_link(path, reason)`，在遇到链接/占位符、或 stat 失败时触发。
    为什么必须有这个口子：**静默跳过等于骗人**。实测用户目录里有个 WPS 云盘
    挂载点，跳过它会让「大文件统计」从 122GB 掉到 9GB，报告却只字不提。
    说到底就是一条：**少扫了就必须说**（见 anti_patterns 第 7 条）。
    reason: "reparse"（junction/symlink/云占位符根）或 "stat-failed"。
    """
    skip = [os.path.normcase(os.path.normpath(s)).rstrip("\\/") for s in (skip_dirs or [])]
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    st, is_link = _entry_stat(e)
                    if st is None:
                        if on_link:
                            on_link(e.path, "stat-failed")
                        continue
                    if is_link:
                        if on_link:
                            on_link(e.path, "reparse")
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if skip:
                            p = os.path.normcase(os.path.normpath(e.path))
                            if any(p == s or p.startswith(s + os.sep) for s in skip):
                                continue
                        stack.append(e.path)
                    else:
                        yield e.path
        except OSError:
            if on_link:
                on_link(cur, "stat-failed")


def link_probe(path, max_files=200000, max_depth=8):
    """**跟随**进入一个链接/挂载点，回答「它到底占了多少本地空间」。

    与 iter_files 的分工：iter_files 拒绝跟随（防止把树外数据算进体积），
    本函数故意跟随 —— 目的不是统计可清理量，而是**量化盲区**，
    好用一句话向用户解释「为什么这里扫不进去、要不要管」。

    返回 dict：
      files       文件数
      dirs        访问到的目录数（转圈时它会逼近 files，可作循环检测信号）
      logical     逻辑字节（st_size 之和，云盘会虚高）
      local       本地字节（**排除**云占位符）
      placeholders 云占位符文件数
      depth_capped / file_capped 是否触顶（触顶则数字是下限，必须标注）
    """
    files = logical = local = ph = dirs = 0
    capped_d = capped_f = False
    stack = [(path, 0)]
    # 防自引用循环：链接内部还可能有链接指回上层，跟着跟就跟进死循环，
    # 结果是「同一个目录被反复计数直到触顶」（实测 .workbuddy 报了
    # 20 万文件 / 1.2MB —— 平均 6 字节一个文件，明显是转圈）。
    visited = set()

    def _mark(p):
        try:
            k = os.path.normcase(os.path.realpath(p))
        except OSError:
            return False
        if k in visited:
            return False
        visited.add(k)
        return True

    _mark(path)
    while stack:
        if files >= max_files:
            capped_f = True
            break
        cur, d = stack.pop()
        if d > max_depth:
            capped_d = True
            continue
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=True)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if _mark(e.path):
                            dirs += 1
                            stack.append((e.path, d + 1))
                    else:
                        files += 1
                        logical += st.st_size
                        if is_cloud_placeholder(st):
                            ph += 1
                        else:
                            local += st.st_size
                        if files >= max_files:
                            capped_f = True
                            break
        except OSError:
            pass
    return {"files": files, "dirs": dirs, "logical": logical, "local": local,
            "placeholders": ph, "depth_capped": capped_d, "file_capped": capped_f}


def find_reparse_points(root, limit=50):
    """列出 root 子树内所有 junction/symlink 的路径（不跟随它们）。"""
    found = []
    stack = [root]
    while stack and len(found) < limit:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    st, is_link = _entry_stat(e)
                    if st is None:
                        continue
                    if is_link:
                        found.append(e.path)
                        if len(found) >= limit:
                            break
                    elif stat.S_ISDIR(st.st_mode):
                        stack.append(e.path)
        except OSError:
            pass
    return found


def is_under(path, root):
    """path 是否位于 root 之下（含 root 自身），大小写不敏感、防前缀误判。

    用于删除前的归属校验：拒绝处理目录树之外的文件。
    """
    if not path or not root:
        return False
    p = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    r = os.path.normcase(os.path.normpath(os.path.abspath(root)))
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)
