# 数据迁移手册（治本手段：让 C 盘不再失控）

清理只能救急，把持续增长的数据搬走才是治本。按目标类型选方案。

## 方案 1：junction 目录联接（通用首选，适合写死绝对路径的应用数据）

适用于：npm 全局目录、pip 缓存、聊天软件数据目录、任何 AppData 大户。原理：把 `C:\...\X` 整体搬到
`D:\CData\X`，原位置建 junction，所有绝对路径照常工作，程序无感知。junction（`/J`）比软链接（`/D`）
兼容性好，且不需要管理员权限。

### 自动化流程（推荐）：`scripts/migrate_with_junction.py <源目录> <目标目录> [--process 进程名] [--allow-junction-copy]`
1. **预检源目录内的 junction/symlink**（`fsutil_safe.find_reparse_points`）——`shutil.copytree`
   默认**跟随**链接，会把链接指向的外部数据**实体复制**到目标盘（目标盘平白多一份、可能撑爆；
   指向自身/上级目录时还会无限递归）。**默认拒绝迁移**并列出每个链接指向的真实目标；
   确认可接受实体复制后才加 `--allow-junction-copy`，或先单独迁移链接目标。
   手动流程里的 `/XJ`（排除 junction）是对应的正确做法，自动流程靠这一步预检。
2. 结束相关进程（tasklist 确认）
3. 复制数据到目标盘；**复制失败（超长路径/空间不足/权限）会自动清理目标盘残留副本**并报错退出
4. 校验文件数 + 字节数（两侧都用 `fsutil_safe.count_tree`，**不跟随链接**）——不一致立即中止，
   源目录原样。注意：若两侧都跟随链接，会同样虚高、差异被掩盖 → 静默放过错误迁移。
5. **源目录改名留底**为 `<源目录>.pre-junction-bak`（同盘原子操作，不删任何数据）
6. `_winapi.CreateJunction(目标, 源)` 创建联接（参数顺序：**目标在前、联接位置在后**，
   目标必须先存在；脚本入口已统一 normpath 规范化路径，避免 `//` 畸形目标）
7. 自动验证四项：`os.lstat` 确认 reparse 标记（**必须用 lstat，os.stat 会穿透 junction**）+
   原路径可达 + 经 junction 写读测试（写入原路径、从**解析出的目标盘路径**读回）+
   **指向校验**（`os.path.realpath` 解析出的目标必须等于 `<目标目录>`）。
   任何一项失败自动回滚（删联接 + 留底改名还原）。
   注意：空目录迁移是合法的，条目不参与成败判定。
8. 输出冷启动验证清单 + 回滚命令

### 保证"软件再次打开一如既往"（硬性要求）
- **冷启动验证**：让用户重新打开软件，确认能启动、能加载原有数据、能产生新数据且落在目标盘
- **留底 ≥7 天**：软件一切正常后才 `--delete-backup`（送回收站再加一层保险）
- **异常即回滚**：`--rollback <源目录>` 一条命令秒级恢复原状（删联接 + 留底还原）
- **随时复查**：`--verify <源目录>` 解析并显示 junction 的**真实指向**（含所在盘符）、
  写读测试、留底情况；指向与源同盘时会告警（说明没达到跨盘目的）

### 手动流程（robocopy 版，含回滚）
```cmd
robocopy "C:\...\X" "D:\CData\X.bak" /E /XJ /COPYALL /R:1 /W:1     :: 1. 备份
robocopy "C:\...\X" "D:\CData\X" /E /XJ /MOVE /R:1 /W:1            :: 2. 搬运
cmd /c mklink /J "C:\...\X" "D:\CData\X"                           :: 3. 建联接
fsutil reparsepoint query "C:\...\X"                               :: 4. 验证 <JUNCTION>
```
- **任一步失败立即回滚**：删联接 → 把 .bak 拷回来，绝不留半成品状态。
- robocopy 跳过"相同文件"问题：目标已有同名文件时即使 /IS 也可能跳过 → 先清空目标，
  或改源文件时间戳（`os.utime` 设为未来时间）让 robocopy 认为源更新。
- **注意**：个人目录（文档/下载/桌面）不要用 junction（部分系统组件兼容问题），用方案 4。

## 方案 2：WSL 发行版迁移（vhdx 大户，动辄 10-50GB）

```powershell
wsl --manage <发行版名> --move D:\WSL\<发行版名>        # 新版 WSL 内置搬迁（首选）
# 旧版：导出→注销→导入
wsl --export <发行版名> D:\wsl-backup.tar
wsl --unregister <发行版名>
wsl --import <发行版名> D:\WSL\<发行版名> D:\wsl-backup.tar
# 注意：--import 后默认用户变 root，需在 /etc/wsl.conf 加 [user] default=<原用户> 后 wsl --shutdown
```
导入后 vhdx 不会自动收缩：`Optimize-VHD -Path <vhdx> -Mode Full`（需 Hyper-V 模块），
或 `wsl --shutdown` 后 diskpart → `select vdisk file=...` → `compact vdisk`。

## 方案 3：Docker Desktop

首选官方通道：Docker Desktop → Settings → Resources → Advanced → Disk image location 改到 D 盘。
迁移前可先 `docker system prune -a` 瘦身（需用户确认）。

## 方案 4：系统"位置"选项卡（个人目录专用）

`Documents`/`Downloads`/`Desktop`/`Pictures` 等：资源管理器 → 右键目录 → 属性 → 位置 → 移动到 D 盘。
系统自动迁移文件并更新已知路径引用。**引导用户操作，Agent 不代做**。

## 方案 5：虚拟内存挪盘

控制面板 → 系统 → 高级系统设置 → 性能设置 → 高级 → 虚拟内存：取消 C 盘自动管理，
在 D 盘设系统管理大小（需重启；建议 C 盘保留少量 pagefile）。

## 应用数据迁移要点（微信/QQ）

- **新版微信（Weixin 4.x）**：存储路径写在配置中，可在微信内"文件管理"改，或直接迁移目录后在设置里
  指向新路径；旧版 `Documents\WeChat Files` 为历史遗留（微信专清见 wechat_module.md）。
- **QQ（NT 版）**：路径存在加密的 Registry.db 中无法直接改，**junction 是唯一可靠的自动化方案**。
- **已卸载软件残留**：从注册表 `HKLM\...\Uninstall` / `HKCU\...\Uninstall` 读官方卸载串优先；
  目录残留确认后可删。
- 迁移前后必须：结束进程 → 复制校验 → 删源 → 建联接 → 启动应用验证读写正常。
