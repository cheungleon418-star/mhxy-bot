# MHXY Bot：单窗口藏宝图实测版

这是一个 Windows 桌面视觉辅助原型。首版只开放单个游戏窗口的藏宝图流程：程序从绑定窗口的客户区截图，通过模板识别驱动非阻塞状态机，并在每次动作后验证界面是否发生了预期变化。

当前开发机没有安装游戏，因此仓库只包含运行框架、状态机、离线测试和未校准模板清单。**在实机截图完成校准前，只能使用诊断和干运行。**

## 安全边界

- GUI 每次启动都默认为干运行，不会发送鼠标或键盘输入。
- 实机模式要求所有必需模板已校准，并且每次运行都要经过两次确认；授权不会写进配置。
- `F11` 是紧急停止，`F12` 是暂停/恢复。
- `F9` 在游戏中是“屏蔽其他玩家”，不是自动战斗键。本项目的藏宝图流程绝不发送 `F9`、技能键、药品键或切换目标键；进入战斗后只观察，由游戏自身完成自动战斗。
- 死亡后不点击复活按钮。程序等待固定返回场景和世界 HUD 稳定，再核对当前藏宝图是否已消耗、是否仍有任务标记，然后继续或处理下一张。
- 验证码出现时暂停；断线时停止。未知界面、重复失败或死亡恢复超过 60 秒时保存诊断并结束本轮，不盲目点击。

请先确认游戏服务条款及所在地规则是否允许此类辅助。不要在包含账号、聊天、角色名等隐私信息的截图上公开分享模板。

## 实机电脑首次安装

要求：Windows 10/11 x64、Git、Python 3.11 x64。当前交付以源码为准，不使用仓库中的旧 PyInstaller/NSIS 打包脚本。

```powershell
git clone https://github.com/cheungleon418-star/mhxy-bot.git
cd mhxy-bot
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
```

`bootstrap.ps1` 会创建仓库内的 `.venv`、安装固定版本的直接基础依赖，并初始化：

```text
%LOCALAPPDATA%\MHXY_Bot\
├─ config.json
├─ templates\<profile>\
├─ captures\
├─ diagnostics\
└─ logs\
```

OCR/Paddle 不是首版藏宝图流程的依赖。只有需要尝试旧 OCR 模块时才运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -WithOcr
```

脚本会验证找到的解释器以及已有 `.venv` 均为 CPython 3.11 x64；不匹配时会停止并提示重建环境。需要运行测试时用 `-Dev` 安装开发依赖（可与 `-WithOcr` 同时使用）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -Dev
```

## 诊断、截图和启动

基础环境诊断不会连接窗口，也不会发送输入：

```powershell
.\scripts\doctor.ps1
```

打开游戏后执行只读实机诊断。它按进程名、窗口标题和可选 HWND 绑定客户区，报告尺寸与 DPI，验证模板配置档是否匹配当前客户区，并短暂注册后立即移除紧急停止热键以确认其可用，最后在内存中读取一帧：

```powershell
.\scripts\doctor.ps1 -Live
```

如果同时发现多个符合条件的游戏窗口，程序会拒绝猜测目标，并在错误中列出候选 HWND；请把确定的值写入私有 `config.json` 的 `window.preferred_hwnd` 后重试。

采集完整客户区截图及同名 JSON 元数据：

```powershell
.\scripts\doctor.ps1 -Capture
```

截图只会写入 `%LOCALAPPDATA%\MHXY_Bot\captures`。也可以从 GUI 点击“只读窗口诊断”和“采集客户区截图”。未校准模板在采集模式中会显示为“警告”，不会让一次成功的截图返回失败；窗口绑定、截图写入或基础环境出错仍会返回非零退出码。

启动 GUI：

```powershell
.\scripts\run.ps1
```

可按需指定其他私有数据目录或模板配置档：

```powershell
.\scripts\run.ps1 -DataDir "D:\MHXY_Bot_Data" -Profile "pc_1280x960"
```

私有数据目录必须放在 Git 克隆目录之外，避免截图、账号界面或本地配置被意外加入提交。

配置目录优先级固定为：命令行 `-DataDir` / `--data-dir`，其次环境变量 `MHXY_BOT_DATA_DIR`，最后 `%LOCALAPPDATA%\MHXY_Bot`。

开发机可用私有截图目录做离线回放；回放模式强制为干运行：

```powershell
.\.venv\Scripts\python.exe .\main.py treasure_map --replay "D:\private-replay-frames"
```

## 模板校准

仓库中的 [`config/template_manifest.json`](config/template_manifest.json) 只是清单示例，每项均为 `calibrated: false`。实机首轮需要以下裁剪模板：

- `world_hud_anchor`
- `backpack_open`
- `treasure_map_icon`
- `map_panel_anchor`
- `quest_target_marker`
- `dig_interact_prompt`
- `combat_hud_anchor`
- `death_return_scene_anchor`
- `task_panel_anchor`
- `active_treasure_task`
- `no_active_treasure_task`
- `reward_dialog`
- `reward_confirm_button`
- `captcha_dialog`
- `disconnect_dialog`

每个实机配置档目录应包含 `manifest.json` 和清单中引用的 PNG。清单的 `profile`、`client_size: [宽, 高]` 和 `dpi` 必须记录采集时的实际窗口环境，并与运行时绑定窗口一致。ROI 使用客户区归一化坐标 `[x, y, width, height]`；分辨率、DPI 或 UI 缩放不同时应使用独立 profile。

首版固定保持 `treasure_map.inventory_signature_enabled: false`。整块背包区域会受悬停和动画影响，不能安全证明藏宝图已经消耗；当前只接受连续帧模板、地图标记和任务面板中的明确状态。等实机截图可标定稳定的格子/数量区域后，再单独引入库存计数证据。

GUI 可以导入私有 ZIP 校准包。ZIP 根目录必须直接包含 `manifest.json` 及所有 PNG，不允许子目录或其他文件类型。导入完成后文件进入 LocalAppData，不进入 Git。

## 更新代码

在实机电脑上确认工作树干净后运行：

```powershell
.\scripts\update.ps1
```

脚本只允许在 `main` 分支执行 `git pull --ff-only origin main`；拉取成功后会重新运行基础初始化并按 `requirements.txt` 对齐依赖。它不会合并、强制覆盖或覆盖已有 LocalAppData 模板与配置。如果工作树有本地改动，它会直接拒绝更新。

## 开发与离线验证

```powershell
.\scripts\bootstrap.ps1 -Dev
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q config core modules launcher.py main.py
.\scripts\check_repo_hygiene.ps1
```

测试使用合成图像和录像式帧序列，不包含任何真实游戏图片。仓库卫生检查会阻止模板、截图、视频、本地配置、日志、诊断包、安装包和压缩包被 Git 跟踪。

## 当前验收目标

- 干净克隆后可完成初始化、基础诊断和 GUI 启动。
- 实机校准后连续处理至少 10 张藏宝图，无固定屏幕坐标误点。
- 进入战斗后零战斗输入；战斗结束后继续藏宝图。
- 死亡后自动识别固定返回场景并恢复任务，不等待人工复活。
- 验证码暂停、断线停止、超时保存诊断。

真实游戏验收必须在运行电脑上进行。首次实测建议先运行诊断，再使用干运行观察完整流程日志，最后才武装少量藏宝图。
