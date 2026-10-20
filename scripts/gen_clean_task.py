# -*- coding: utf-8 -*-
"""生成「定时清理」的计划任务定义（只生成 XML，不注册）。

为什么只生成不注册
------------------
「自动清理」之所以常被做成付费特权，是因为自动删除最省用户心智。
但**自动删除也是最危险的**：一旦规则更新或路径判断出错，用户会在没有察觉的情况下丢文件。

所以本技能的定位是：
  * **自动扫描**（安全，默认动作）→ 定期把「这周新增了多少垃圾、哪些长大最快」推给用户；
  * **自动删除**（危险，需显式双开关）→ 也支持，但必须同时给出 `--action clean`
    + `--targets-file` + `--i-understand-this-deletes`，且脚本本身永不自行注册任务。

这样做同时解决三件事：免费提供自动化的价值、把最终授权留在用户手里、不给系统留后门
（见 references/anti_patterns.md 第 4、6 条）。

用法
----
    # 每天 10:00 自动扫描，输出 JSON 报告（推荐）
    python -S scripts/gen_clean_task.py --daily 10:00 --out-json "D:\\reports\\c_scan.json"

    # 每周日 09:30 扫描
    python -S scripts/gen_clean_task.py --weekly SUN,09:30

    # 空闲时扫描（不打扰）
    python -S scripts/gen_clean_task.py --idle

    # 自动删除（危险，三重开关）
    python -S scripts/gen_clean_task.py --daily 03:00 --action clean \
        --targets-file "D:\\reports\\targets.json" --i-understand-this-deletes

生成后用管理员 PowerShell 执行脚本打印的命令即可注册；卸载用 --print-unregister。
"""
import argparse
import ctypes
import datetime as dt
import os
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
DAYMAP = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday",
          "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday"}

TASK_TMPL = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>c-drive-cleanup-ultimate</Author>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Triggers>
{triggers}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user_id}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>{runlevel}</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>{stop_on_idle_end}</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>{run_only_if_idle}</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def daily_trigger(hhmm):
    today = dt.date.today()
    start = dt.datetime.combine(today, dt.time(*_parse_hm(hhmm)))
    if start < dt.datetime.now():
        start += dt.timedelta(days=1)
    return ("    <CalendarTrigger>\n"
            "      <StartBoundary>%s</StartBoundary>\n"
            "      <Enabled>true</Enabled>\n"
            "      <ScheduleByDay>\n"
            "        <DaysInterval>1</DaysInterval>\n"
            "      </ScheduleByDay>\n"
            "    </CalendarTrigger>" % start.strftime("%Y-%m-%dT%H:%M:%S"))


def weekly_trigger(day, hhmm):
    today = dt.date.today()
    start = dt.datetime.combine(today, dt.time(*_parse_hm(hhmm)))
    if start < dt.datetime.now():
        start += dt.timedelta(days=1)
    return ("    <CalendarTrigger>\n"
            "      <StartBoundary>%s</StartBoundary>\n"
            "      <Enabled>true</Enabled>\n"
            "      <ScheduleByWeek>\n"
            "        <DaysOfWeek><%s /></DaysOfWeek>\n"
            "        <WeeksInterval>1</WeeksInterval>\n"
            "      </ScheduleByWeek>\n"
            "    </CalendarTrigger>" % (start.strftime("%Y-%m-%dT%H:%M:%S"), DAYMAP[day]))


def idle_trigger():
    return ("    <BootTrigger>\n"
            "      <Enabled>true</Enabled>\n"
            "      <Delay>PT10M</Delay>\n"
            "    </BootTrigger>")


def _parse_hm(s):
    h, m = s.split(":")
    return int(h), int(m)


def current_user():
    """当前用户 `DOMAIN\\user` 形式（schtasks 认这个，也认裸用户名）。"""
    dom = (os.environ.get("USERDOMAIN") or "").strip()
    usr = (os.environ.get("USERNAME") or "").strip()
    if not usr:
        return ""
    return ("%s\\%s" % (dom, usr)) if dom else usr


def is_elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def build_xml(args):
    py = sys.executable
    script = os.path.join(HERE, "scan_safe_targets.py")
    if args.action == "clean":
        script = os.path.join(HERE, "delete_safe.py")
        arguments = '-S "%s" --targets "%s" --confirm' % (script, args.targets_file)
        description = ("[c-drive-cleanup-ultimate] 自动清理（危险：会真删）。"
                       "目标清单由用户预先审阅。")
        # 实测：HighestAvailable 在非提权环境注册会**直接被拒绝访问**（exit=1），
        # 而 LeastPrivilege 能注册成功。但删 C:\Windows\Temp 这类系统缓存确实需要提权，
        # 所以默认按动作区分，同时允许 --runlevel least 降级（只清用户级目标时够用）。
        runlevel = "HighestAvailable"
    else:
        arguments = '-S "%s"' % script
        if args.out_json:
            # 注意：Task Scheduler 直接 Exec 进程、不经过 shell，`> 文件` 重定向不会生效，
            # 必须用脚本自带的 --out 参数。
            arguments += ' --out "%s"' % args.out_json
        description = ("[c-drive-cleanup-ultimate] 定时只读扫描 C 盘垃圾，输出 JSON 报告。"
                       "不删除任何文件。")
        runlevel = "LeastPrivilege"

    if getattr(args, "runlevel", "auto") == "least":
        runlevel = "LeastPrivilege"
    elif getattr(args, "runlevel", "auto") == "highest":
        runlevel = "HighestAvailable"

    if args.daily:
        triggers = daily_trigger(args.daily)
        run_only_if_idle = "false"
        stop_on_idle_end = "true"
    elif args.weekly:
        day, hhmm = args.weekly.split(",")
        triggers = weekly_trigger(day.upper(), hhmm)
        run_only_if_idle = "false"
        stop_on_idle_end = "true"
    else:
        triggers = idle_trigger()
        run_only_if_idle = "true"
        stop_on_idle_end = "true"

    return TASK_TMPL.format(
        description=escape(description),
        triggers=triggers,
        user_id=escape(getattr(args, "user", None) or current_user()),
        runlevel=runlevel,
        run_only_if_idle=run_only_if_idle,
        stop_on_idle_end=stop_on_idle_end,
        command=escape(py),
        arguments=escape(arguments),
        workdir=escape(SKILL_ROOT),
    )


def validate(xml_text):
    """把生成的 XML 解析回来，确认结构完整（避免手误生成一个注册不上的任务）。

    这里**只做结构校验，不能证明能被 schtasks 接受** —— 实测过：只写
    `<GroupId>S-1-5-32-545</GroupId>` 的 XML 结构完全合法、本函数全绿，
    但 `schtasks /Create /XML` 直接报「拒绝访问」（exit=1），任务根本注册不上。
    真正的验收在 scripts/field_test.py 的 T4（真实注册 + 立即删除）。
    所以额外加一条硬断言：必须有 UserId，且不得出现裸 GroupId。
    """
    root = ET.fromstring(xml_text)
    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    errs = []
    trig = root.find(ns + "Triggers")
    if trig is None or len(list(trig)) == 0:
        errs.append("缺少 Triggers")
    act = root.find(ns + "Actions")
    if act is None:
        errs.append("缺少 Actions")
    else:
        ex = act.find(ns + "Exec")
        if ex is None or not (ex.findtext(ns + "Command") or "").strip():
            errs.append("Actions/Exec/Command 为空")
        if not (ex.findtext(ns + "Arguments") or "").strip():
            errs.append("Actions/Exec/Arguments 为空")
    if root.find(ns + "Settings") is None:
        errs.append("缺少 Settings")

    pr = root.find(ns + "Principals")
    if pr is None:
        errs.append("缺少 Principals")
    else:
        p = pr.find(ns + "Principal")
        if p is None:
            errs.append("缺少 Principal")
        else:
            uid = (p.findtext(ns + "UserId") or "").strip()
            gid = (p.findtext(ns + "GroupId") or "").strip()
            if not uid:
                errs.append("缺少 UserId（只写 GroupId 会导致 schtasks 拒绝访问）")
            if gid:
                errs.append("不应写 GroupId（改用 UserId）")
    return errs


def main():
    ap = argparse.ArgumentParser(description="生成定时清理/扫描的计划任务 XML（不自动注册）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--daily", metavar="HH:MM", help="每天执行，如 10:00")
    g.add_argument("--weekly", metavar="DAY,HH:MM", help="每周执行，如 SUN,09:30")
    g.add_argument("--idle", action="store_true", help="开机 10 分钟后 + 仅空闲时执行")
    ap.add_argument("--name", default="CCU-CDrive-Scan", help="任务名，默认 CCU-CDrive-Scan")
    ap.add_argument("--user", help="注册到哪个账户，形如 DOMAIN\\user；默认当前用户")
    ap.add_argument("--action", choices=["scan", "clean"], default="scan",
                    help="scan=只读扫描（默认）；clean=真删（需额外双开关）")
    ap.add_argument("--out-json", help="scan 动作的 JSON 输出路径")
    ap.add_argument("--targets-file", help="clean 动作的目标清单（必须已人工审阅）")
    ap.add_argument("--i-understand-this-deletes", action="store_true",
                    help="确认自动删除的风险（--action clean 必需）")
    ap.add_argument("--out", default="clean_task.xml", help="XML 输出路径")
    ap.add_argument("--runlevel", choices=["auto", "least", "highest"], default="auto",
                    help="运行级别。auto=按动作（scan→LeastPrivilege，clean→HighestAvailable）。"
                         "clean + least 可在**不提权**的情况下注册成功，代价是删不了系统级缓存"
                         "（C:\\Windows\\Temp 等），只适合只清用户级目标")
    ap.add_argument("--print-unregister", action="store_true", help="只打印卸载命令")
    args = ap.parse_args()

    if args.print_unregister:
        print('schtasks /Delete /TN "%s" /F' % args.name)
        return 0

    if args.action == "clean":
        if not args.targets_file or not os.path.exists(args.targets_file):
            ap.error("--action clean 需要有效的 --targets-file")
        if not args.i_understand_this_deletes:
            ap.error("--action clean 必须同时给 --i-understand-this-deletes（自动删除不可逆）")
    if not (args.daily or args.weekly or args.idle):
        ap.error("需要 --daily / --weekly / --idle 之一")

    xml_text = build_xml(args)
    errs = validate(xml_text)
    if errs:
        print("生成的 XML 结构不合法：" + "；".join(errs), file=sys.stderr)
        return 1

    # Task Scheduler 要求 UTF-16 编码的 XML 文件
    with open(args.out, "w", encoding="utf-16", newline="\r\n") as f:
        f.write(xml_text)

    print("已生成: %s" % os.path.abspath(args.out))
    print("任务名: %s   动作: %s" % (args.name, args.action))
    print("")
    print("注册（需管理员 PowerShell / CMD）：")
    print('  schtasks /Create /TN "%s" /XML "%s" /F' % (args.name, os.path.abspath(args.out)))
    print("")
    # 实测：HighestAvailable 在非提权环境注册会直接「拒绝访问」（exit=1），
    # 但用户看到的报错只有四个字，完全不知道该怎么办。这里提前说清楚两条出路。
    needs_elev = (args.action == "clean" and args.runlevel != "least") or args.runlevel == "highest"
    if needs_elev and not is_elevated():
        print("⚠ 当前不是管理员终端，而这个任务的运行级别是 HighestAvailable ——")
        print("  直接注册会报「错误: 拒绝访问」。两条出路：")
        print("   ① 用【管理员】PowerShell 执行上面的注册命令；或")
        print("   ② 目标只涉及用户级缓存（不需要删 Windows\\Temp 这类系统目录）时，")
        print("      重跑本命令并加 --runlevel least，即可在普通权限下注册。")
        print("")
    print("卸载：")
    print('  schtasks /Delete /TN "%s" /F' % args.name)
    print("")
    if args.action == "scan":
        print("说明：本任务只做只读扫描，不删除任何文件。删除仍需人工确认后执行。")
    else:
        print("警告：本任务会按 %s 的清单**真删**。确认清单已审阅、且已备份。" % args.targets_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
