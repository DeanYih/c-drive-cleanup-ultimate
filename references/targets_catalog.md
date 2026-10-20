# Windows C 盘垃圾文件完整目录（🟢 安全 / 🟡 谨慎 / 🔴 红线）

> 28 类垃圾目录完整清单，白名单与扫描/删除脚本保持一致。
> 清理前逐项对照：路径是否存在、是否被进程/杀软锁定、是否在白名单护栏内。

## 一、系统临时文件

| 路径 | 等级 | 说明 |
|---|---|---|
| `C:\Windows\Temp` | 🟢 | 系统临时文件，清内容留目录；需管理员 |
| `%TEMP%`（`AppData\Local\Temp`） | 🟢 | 用户级临时文件；被占用项跳过（WinError 32 正常） |
| `C:\Windows\Prefetch` | 🟢 | 预读取缓存，自动重建（收益小） |
| `C:\Windows\Panther` | 🟢 | 装机日志，常见 4-5GB，98% 可清 |

## 二、Windows 更新相关

| 路径/操作 | 等级 | 说明 |
|---|---|---|
| `C:\Windows\SoftwareDistribution\Download` | 🟢 | 更新下载缓存；先停 wuauserv/bits 再删，**DataStore 勿动** |
| 传递优化缓存 | 🟢 | `Delete-DeliveryOptimizationCache` 或 设置→系统→存储→传递优化 |
| WinSxS 组件存储 | 🟡 | **只能走 DISM**，严禁手动删目录内容；先 `AnalyzeComponentStore` 看真实可回收 |
| `C:\Windows.old` | 🟡 | 升级残留 10-40GB；引导「存储感知→以前的 Windows 安装」，确认不需回退后清 |
| cleanmgr「清理系统文件」 | 🟢 | 务必勾选：Windows 更新清理、传递优化文件、设备驱动程序包、以前的 Windows 安装 |

## 三、错误报告与转储

| 路径 | 等级 | 说明 |
|---|---|---|
| `C:\Windows\MEMORY.DMP` | 🟢 | 蓝屏完整转储，可达物理内存大小 |
| `C:\Windows\Minidump\*.dmp` | 🟢 | 蓝屏小转储 |
| `C:\ProgramData\Microsoft\Windows\WER` | 🟢 | 错误报告队列 |
| `AppData\Local\CrashDumps` | 🟢 | 应用崩溃转储 |

## 四、浏览器缓存（先关浏览器；登录态/书签/扩展绝不碰）

| 路径 | 等级 |
|---|---|
| `AppData\Local\Microsoft\Edge\User Data\<Profile>\Cache`、`Code Cache` | 🟢 |
| `AppData\Local\Google\Chrome\User Data\<Profile>\Cache`、`Code Cache` | 🟢 |
| `AppData\Local\Mozilla\Firefox\Profiles\*.default*\cache2` | 🟢 |
| `AppData\Local\Microsoft\Windows\INetCache` | 🟢 |

## 五、系统缓存与索引

| 路径 | 等级 | 说明 |
|---|---|---|
| `thumbcache_*.db`（Explorer 目录） | 🟢 | 缩略图缓存；需先关 explorer.exe，收益小放最后 |
| `AppData\Local\IconCache.db` | 🟢 | 图标缓存 |
| `C:\ProgramData\Microsoft\Search\Data` | 🟡 | 搜索索引；停 WSearch 后才可清（受保护服务走 SCM API `Set-Service`，注册表直写会 WinError 5） |
| `C:\Windows\System32\FNTCACHE.DAT` | 🟢 | 停 FontCache 服务后清 |

## 六、回收站

| 路径/操作 | 等级 | 说明 |
|---|---|---|
| `C:\$Recycle.Bin` | 🟡 | **先问用户**里面有没有要找回的东西，再按沙箱降级方案清空 |

## 七、休眠与虚拟内存（隐形巨无霸）

| 项 | 等级 | 操作 |
|---|---|---|
| `C:\hiberfil.sys`（内存的 40%-100%） | 🟡 | 管理员 `powercfg /h off`（失去休眠+快速启动）；恢复 `/h on` |
| `C:\pagefile.sys` | 🔴 | **只能挪不能删**：高级系统设置→虚拟内存→移到 D 盘（重启生效，C 盘留少量） |
| `C:\swapfile.sys` | 🔴 | 系统管理，勿动 |

## 八、开发工具链缓存

| 路径/命令 | 等级 | 说明 |
|---|---|---|
| `AppData\Local\npm-cache` | 🟢 | 优先 `npm cache clean --force`（2万+小文件直接删极慢） |
| `AppData\Local\pip\Cache` | 🟢 | `pip cache purge` |
| `~/.nuget/packages` | 🟡 | 下次构建需重新还原 |
| conda envs / venv / 闲置运行时 | 🟡 | 用户确认哪个还在用；保留最新 1-2 个 |
| Docker Desktop vhdx | 🟡 | `docker system prune -a`；迁移见 migration.md |

## 九、显卡/图形缓存

| 路径 | 等级 |
|---|---|
| `AppData\Local\NVIDIA\DXCache`、`GLCache` | 🟢 |
| `AppData\Local\D3DSCache` | 🟢 |

## 十、杂项

| 路径 | 等级 | 说明 |
|---|---|---|
| `C:\Windows\Downloaded Program Files` | 🟢 | 旧 ActiveX 残留 |
| `C:\Windows\Logs`（CBS/DISM 日志） | 🟢 | |
| `AppData\Local\*-updater\` | 🟢 | 第三方软件更新包残留，单个可达 500MB |
| `C:\Windows\Installer` 孤立包 | 🔴 | 手动删会破坏卸载功能；需 PatchCleaner 类工具 |
| `C:\System Volume Information` | 🟡 | 还原点；无管理员读不到，先 `vssadmin list shadowstorage`，多为瞬时虚高 |

## 十一、🔴 绝对禁区（永不列入清理项）

`C:\Windows`（除上述白名单）、`C:\Windows\System32\config`、`C:\Program Files` 及 (x86) 内软件本体、
`C:\Windows\Installer`、用户个人目录（Desktop/Documents/Downloads/Pictures，除非用户逐条指定）、
微信 `Msg/*.db`、任何用户标记"别动"的目录、杀软实时防护路径（内核自我保护锁定）。

## 清理前检查清单

- [ ] 已完成只读扫描，输出完整占用报告
- [ ] 所有清理项已按 🟢🟡🔴 分级标注
- [ ] 清理方案已提交用户确认并获得明确同意
- [ ] 🔴 红线项已排除
- [ ] 删除走白名单护栏（delete_safe.py）或回收站
- [ ] 完成后复测空间并出报告
