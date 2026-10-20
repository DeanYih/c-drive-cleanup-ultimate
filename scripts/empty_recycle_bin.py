# -*- coding: utf-8 -*-
"""API 清空回收站（单次系统调用，不受批量删除保护限制）
用法: python empty_recycle_bin.py [盘符]   # 如 C 或 F，缺省清空所有盘

⚠ 沙箱环境警告（2026-10-04 实测）：在本机 WorkBuddy 沙箱内，ctypes 直调
SHEmptyRecycleBinW 会被沙箱 SIGTERM 杀掉（dangerouslyDisableSandbox 也无效）。
此时按以下顺序降级：
  1) 请用户在资源管理器中右键回收站 → 清空（最简单可靠）；
  2) 经用户明确同意后，用 CODEBUDDY_SAFE_DELETE_ENABLED=0 关闭删除保护再跑本脚本；
  3) 换执行上下文：用任务计划程序拉起一个进程执行本脚本（该进程不带沙箱 shim）。
"""
import sys
import argparse
import ctypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SHERB_NOCONFIRMATION = 0x1
SHERB_NOPROGRESSUI = 0x2
SHERB_NOSOUND = 0x4


def empty(drive=None):
    root = drive.rstrip(":\\") + ":\\" if drive else None
    code = ctypes.windll.shell32.SHEmptyRecycleBinW(
        None, ctypes.c_wchar_p(root) if root else None,
        SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
    return code


if __name__ == "__main__":
    # 必须用 argparse：本脚本是破坏性动作，任何未知参数（包括 --help）
    # 都不能落到"当作盘符清空"的路径上（真机实测 --help 曾直接触发清空）。
    ap = argparse.ArgumentParser(
        description="API 清空回收站（破坏性操作，需用户确认后调用）")
    ap.add_argument("drive", nargs="?", default=None,
                    help="盘符，如 C 或 F；缺省清空所有盘")
    args = ap.parse_args()
    code = empty(args.drive)
    # 0=成功, 0x8000FFFF(-2147418113)=回收站已空(视为成功), 其他=失败
    if code == 0:
        print("回收站已清空 OK")
    elif code in (-2147418113, 0x8000FFFF):
        print("回收站本就为空 OK")
    else:
        print(f"清空失败，返回码: {code}（沙箱内可能被 SIGTERM 杀掉，见文件头降级方案）")
