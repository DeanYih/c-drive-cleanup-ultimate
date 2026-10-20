# 应用缓存目录重定向对照表（让缓存源头离开 C 盘）

> 配合扫描结果使用：当 `scan_c_drive.py` / 全盘分析发现某个 AppData 或程序数据目录
> 在 C 盘持续膨胀时，先识别归属应用，再按本表**优先引导用户在软件设置内改存储位置**
> （官方通道最干净、升级不失效），无设置项才用 junction（见 migration.md 方案 1）。
>
> 三级策略：① 有官方设置 → 改设置；② 无设置 → junction；③ 个人目录 → "位置"选项卡。

## 聊天/办公

| 应用 | C 盘默认位置 | 重定向方式 |
|---|---|---|
| 微信（Weixin 4.x） | `x:\xwechat_files`（常默认 C）或 `Documents\WeChat Files`（旧版） | 微信 → 设置 → 文件管理 → 更改存储位置 |
| QQ（NT 版） | `Documents\Tencent Files` | 设置 → 存储管理 → 更改；**写入加密 Registry.db 改不动时用 junction** |
| 企业微信 | `Documents\WXWork` | 设置 → 文件与数据 → 更改 |
| 钉钉 | `AppData\Roaming\DingTalk` | 设置 → 通用 → 文件保存路径 |
| WPS | `AppData\Roaming\kingsoft`（备份/暂存） | 全局设置 → 备份中心 → 更改备份目录；云文档目录同步改 |
| 腾讯文档/桌面助手 | `AppData\Roaming\Tencent` | 应用内设置；无则 junction |
| 飞书 | `AppData\Roaming\Feishu`（SDK 缓存大） | 设置 → 通用 → 存储空间/文件下载位置 |

## 网盘/下载

| 应用 | 重定向方式 |
|---|---|
| 百度网盘 | 设置 → 传输 → 下载位置 + 缓存位置 都改走非 C 盘 |
| 坚果云 | 设置 → 同步文件夹位置（或迁移同步目录） |
| OneDrive | OneDrive 设置 → 同步和备份 → 更改位置 |
| 迅雷/IDM/qBittorrent | 各自设置中改默认下载目录 |
| Steam / Epic | 游戏库 → 添加/移动到非 C 盘库 |

## 浏览器

| 项 | 重定向方式 |
|---|---|
| 下载目录 | 浏览器设置 → 下载内容 → 位置更改（Chrome/Edge 通用） |
| Cache 等用户数据目录 | 无官方设置项 → 关浏览器后 junction 整个 `User Data` 下的大 Profile 子目录（登录态敏感，迁移前务必备份 + 验证） |

## 开发工具链

| 工具 | 重定向命令/方式 |
|---|---|
| npm | `npm config set cache D:\dev-cache\npm-cache`；全局目录同理 `npm config set prefix` |
| pip | `pip config set global.cache-dir D:\dev-cache\pip` |
| conda | `.condarc` 中 `pkgs_dirs`、`envs_dirs` 指向非 C 盘 |
| Docker Desktop | Settings → Resources → Advanced → Disk image location |
| WSL | `wsl --manage <发行版> --move D:\WSL\<发行版>`（见 migration.md 方案 2） |
| Hugging Face 模型缓存 | 环境变量 `HF_HOME=D:\dev-cache\huggingface` |
| Go / Rust / Maven / Gradle | `GOPATH`、`CARGO_HOME`、`maven.repo.local`、`GRADLE_USER_HOME` 环境变量 |

## 通用判定流程（对扫描出的每个 C 盘大户）

1. **归属识别**：目录名/上级路径 → 哪个软件；不确定时查该目录内配置文件或注册表 Uninstall 串。
2. **查本表/查该软件设置**：有"存储位置/文件保存路径/缓存位置"类设置 → 给出点击路径，引导用户改。
3. **无设置项** → junction 迁移（migrate_with_junction.py）：先复制校验 → 源目录改名留底 → 建联接；
   自动验证通过才算完成。
4. **改完验证（可用性保障协议，务必执行）**：让用户冷启动该软件，确认能正常打开、加载历史数据、
   产生新数据且落在新盘；配置类先 `get` 确认新值生效。**验证通过前不清理留底；有任何异常立即回滚**
   （`migrate_with_junction.py --rollback`，配置类还原备份值）。
5. **回报**：在清理报告的"持续增长源"一栏注明每项的处理方式（已改设置/已 junction/待用户操作）、
   验证结果、回滚命令与留底位置（建议留底 ≥7 天再 `--delete-backup`）。
