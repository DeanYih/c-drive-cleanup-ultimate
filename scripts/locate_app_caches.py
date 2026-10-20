# -*- coding: utf-8 -*-
"""C 盘应用缓存/数据目录归属识别与重定向清单生成器（只读）
用法:
    python locate_app_caches.py                 # 全量测大小，输出 Markdown 清单 + JSON
    python locate_app_caches.py --json          # 仅输出 JSON
    python locate_app_caches.py --quick         # 跳过深度测大小（只报告存在性与方法），快

对每个识别到的应用缓存/数据目录，给出：
  app            归属应用
  path           C 盘实际路径
  bytes          占用字节
  redirect_type  settings=软件内改设置 / config=改配置命令 / junction=目录联接 / location-tab=个人目录"位置"选项卡
  entry          官方调整入口（点击路径或命令）
  agent_auto     Agent 可否直接代改（True=可执行命令/脚本完成；False=必须用户在软件 GUI 内手动改）

原则：有官方设置项优先引导手动改（最干净）；配置类工具（npm/pip/conda 等）Agent 可直接代改
（改前备份原配置）；无设置项用 junction（需用户确认后跑 migrate_with_junction.py）。
"""
import os
import sys
import json
import argparse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 统一用 fsutil_safe：本文件原先自己实现了一份 dir_size，正好违反了
# SKILL.md 第 4 节的纪律（"禁止在业务脚本里手写目录遍历"——历史上 4 份独立
# 实现全部踩了同一个 junction 坑）。
from fsutil_safe import dir_size, is_reparse_point  # noqa: E402


def build_catalog(profile):
    """返回 [(app, [候选路径...]), ...]，路径可含 {profile} 占位符。"""
    return [
        ("微信 WeChat(4.x)", [r"{profile}\xwechat_files",
                              r"{profile}\Documents\xwechat_files"]),
        ("微信 WeChat(旧版)", [r"{profile}\Documents\WeChat Files"]),
        # 注意：这是微信 4.x 的**程序数据目录**（xplugin/radium/log/net…），
        # 不是"旧版数据"，也不是聊天记录根。旧标签写成"微信(旧数据/缓存)"
        # 会让人以为它是过时残留，实际里面有 600MB+ 可清缓存（实测 1062MB）。
        ("微信 WeChat(程序数据)", [r"{profile}\AppData\Roaming\Tencent\xwechat"]),
        ("QQ(NT)", [r"{profile}\Documents\Tencent Files",
                    r"{profile}\AppData\Roaming\Tencent\QQNT"]),
        ("企业微信", [r"{profile}\Documents\WXWork"]),
        ("钉钉", [r"{profile}\AppData\Roaming\DingTalk"]),
        ("飞书", [r"{profile}\AppData\Roaming\Feishu"]),
        ("WPS", [r"{profile}\AppData\Roaming\kingsoft",
                 r"{profile}\AppData\Local\Kingsoft"]),
        ("百度网盘", [r"{profile}\AppData\Roaming\baidu\BaiduNetdisk"]),
        ("坚果云", [r"{profile}\AppData\Roaming\Nutstore"]),
        ("Chrome 用户数据", [r"{profile}\AppData\Local\Google\Chrome\User Data"]),
        ("Edge 用户数据", [r"{profile}\AppData\Local\Microsoft\Edge\User Data"]),
        ("Firefox", [r"{profile}\AppData\Local\Mozilla\Firefox"]),
        ("npm 缓存", [r"{profile}\AppData\Local\npm-cache"]),
        ("pip 缓存", [r"{profile}\AppData\Local\pip\Cache"]),
        ("HuggingFace 模型缓存", [r"{profile}\.cache\huggingface"]),
        ("Gradle", [r"{profile}\.gradle"]),
        ("Maven", [r"{profile}\.m2"]),
        ("Go 模块", [r"{profile}\go"]),
        ("Rust/Cargo", [r"{profile}\.cargo"]),
        ("Docker(WSL 数据盘)", [r"{profile}\AppData\Local\Docker\wsl"]),
        ("微信小程序/输入法等杂项", [r"{profile}\AppData\Roaming\Tencent\WeChatAppStore",
                                   r"{profile}\AppData\Roaming\Tencent\wechat_jump"]),
    ]


# 每类应用的重定向方法：type / 官方入口 / agent 可否代改
REDIRECT = {
    "微信 WeChat(4.x)":     ("settings", "微信 → 设置 → 文件管理 → 更改存储位置", False),
    "微信 WeChat(旧版)":    ("settings", "微信 → 设置 → 文件管理 → 更改（旧版）", False),
    "微信 WeChat(程序数据)": ("junction", "无官方设置项 → 关微信后 junction 迁移；"
                                          "其中 xplugin/radium/log 可先直接清（见 wechat_analyze.py）", True),
    "QQ(NT)":               ("settings", "QQ → 设置 → 存储管理 → 更改；改不动(加密 Registry.db)则 junction", False),
    "企业微信":             ("settings", "企业微信 → 设置 → 文件与数据 → 更改", False),
    "钉钉":                 ("settings", "钉钉 → 设置 → 通用 → 文件保存路径", False),
    "飞书":                 ("settings", "飞书 → 设置 → 通用 → 存储空间/文件下载位置", False),
    "WPS":                  ("settings", "WPS → 全局设置 → 备份中心 → 更改备份目录", False),
    "百度网盘":             ("settings", "百度网盘 → 设置 → 传输 → 下载/缓存位置", False),
    "坚果云":               ("settings", "坚果云 → 设置 → 同步文件夹位置", False),
    "Chrome 用户数据":      ("settings", "下载目录：设置→下载内容→位置；缓存目录无官方设置→关浏览器后 junction(备份先行)", False),
    "Edge 用户数据":        ("settings", "下载目录：设置→下载→位置；缓存目录无官方设置→关浏览器后 junction(备份先行)", False),
    "Firefox":              ("junction", "无官方设置项 → 关 Firefox 后 junction 迁移", True),
    "npm 缓存":             ("config",   "npm config set cache <新路径>（Agent 可直接执行）", True),
    "pip 缓存":             ("config",   "pip config set global.cache-dir <新路径>（Agent 可直接执行）", True),
    "HuggingFace 模型缓存": ("config",   "setx HF_HOME <新路径>（Agent 可直接执行，新终端生效）", True),
    "Gradle":               ("config",   "setx GRADLE_USER_HOME <新路径>（Agent 可直接执行）", True),
    "Maven":                ("config",   "setx MAVEN_OPTS 加 -Dmaven.repo.local=<新路径> 或改 settings.xml（Agent 可执行）", True),
    "Go 模块":              ("config",   "setx GOPATH <新路径>（Agent 可直接执行）", True),
    "Rust/Cargo":           ("config",   "setx CARGO_HOME <新路径>（Agent 可直接执行）", True),
    "Docker(WSL 数据盘)":   ("settings", "Docker Desktop → Settings → Resources → Disk image location", False),
    "微信小程序/输入法等杂项": ("junction", "无官方设置项 → 结束相关进程后 junction 迁移", True),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="仅输出 JSON")
    ap.add_argument("--quick", action="store_true", help="跳过深度测大小")
    args = ap.parse_args()

    profile = os.environ.get("USERPROFILE") or os.path.join(
        os.environ.get("SystemDrive", "C:") + os.sep, "Users", os.environ.get("USERNAME", ""))
    rows = []
    for app, candidates in build_catalog(profile):
        for tpl in candidates:
            p = tpl.format(profile=profile)
            if not os.path.isdir(p):
                continue
            size = -1 if args.quick else dir_size(p)
            rtype, entry, auto = REDIRECT[app]
            rows.append({
                "app": app, "path": p,
                "bytes": size, "gb": round(size / 1024**3, 2) if size >= 0 else None,
                "redirect_type": rtype, "entry": entry, "agent_auto": auto,
            })

    rows.sort(key=lambda r: r["bytes"] if r["bytes"] >= 0 else 0, reverse=True)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return

    print("# C 盘应用缓存/数据目录重定向清单\n")
    print("| 应用 | C 盘路径 | 占用 | 调整方式 | 官方入口 | Agent 可代改 |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        size = f"{r['gb']} GB" if r["gb"] is not None else "（quick 模式未测）"
        auto = "✅ 可" if r["agent_auto"] else "❌ 需手动"
        print(f"| {r['app']} | `{r['path']}` | {size} | {r['redirect_type']} | {r['entry']} | {auto} |")
    print("\n说明：✅ 可 = 你下指令后 Agent 直接调整（改配置前会备份原文件/输出回滚方法）；"
          "❌ 需手动 = 该软件只在 GUI 里能改，按官方入口自行调整；"
          "junction 类需你确认后由 Agent 执行迁移脚本（先复制校验后删源，失败自动回滚）。")


if __name__ == "__main__":
    main()
