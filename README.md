# 股票实时行情监测悬浮窗

Windows 11 下运行的透明、无边框股票行情悬浮窗。程序启动时从同目录读取
`config.json` 与 `stock_list.json`；任一文件不存在时会自动创建默认模板。
配置 JSON 格式不正确、网络超时或接口内容异常时，窗口仍会保持运行，并在该轮
将无法获取的数值显示为 `--`。

## 本地运行

需要 Python 3.10 或更高版本：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python stock_monitor.py
```

窗口名为 `gp`，启动后只显示在 Windows 通知区域（通常为任务栏角溢出菜单），不会
显示下方任务栏按钮。右上角 `-` 和 `×` 始终以普通文字色显示，窗口默认置顶。Windows 是否
将图标固定在溢出菜单由系统的任务栏设置决定。

在文字之间或窗口空白处按住鼠标左键即可拖动；靠近窗口边缘按住左键可调整窗口尺寸。
窗口启动时默认展示一行，股票超过可视区域时，鼠标滚轮每次按完整一行滚动。

## 配置说明

`stock_list.json` 中仅接受带 `sh` 或 `sz` 前缀的六位 A 股代码。每条记录可设置
`shares` 持仓股数（整数，缺省为 0）。成本价为零时，盈亏比例会显示 `--`，以避免除以零。

`window_pos_x` 与 `window_pos_y` 使用屏幕像素坐标；填 `-1` 时分别自动贴靠主屏幕
右侧或顶部，因此默认位置为右上角。`always_on_top` 为 `true` 时窗口保持置顶。

`query_time_ranges` 是允许网络查询的本地时间段数组，格式为 `"HH:MM-HH:MM"`。默认
仅在 `09:15-11:30` 和 `14:30-15:00` 期间按 `refresh_interval` 查询。若程序在规定
时间外首次打开，仍会自动查询一次以显示价格，随后停止网络轮询并保留最后一次成功
获取的数值。时间段格式错误或字段缺失时，会安全回退到默认时段。

`minimize_hotkey` 与 `close_hotkey` 分别配置全局显示/隐藏切换和关闭快捷键，默认值为
`alt+z` 与 `alt+x`。快捷键由 Windows 注册；若热键被其他程序占用，程序会自动使用
全局按键状态检测兜底，即使窗口未获得焦点也可触发。

启用 `always_on_top` 后，程序会通过 Windows 原生 `HWND_TOPMOST` 每秒重新应用置顶状态，
并在收到自身 `WM_WINDOWPOSCHANGED`（Z 序变化）后立即重新应用，且使用
`SWP_NOACTIVATE` 避免抢占当前输入焦点，适用于全屏云桌面或全屏远程会话。未启用
`WS_EX_NOACTIVATE`，以保留点击窗口后使用上/下方向键的能力。云桌面客户端若使用
独立的系统级安全覆盖层，仍可能由客户端策略决定是否允许普通窗口覆盖。

## 排查日志

每行依次显示名称、当前价、当日涨跌幅、持仓盈亏比例和今日涨跌金额。最后一行 `total`
显示总盈亏金额（百元）、持仓总涨跌百分比、当日总涨跌百分比和当日总盈亏金额（百元）。
金额按四舍五入显示为整数，百分比按总成本和总昨日市值加权计算。

程序会在 EXE 同目录按日期写入 `gp_debug_YYYY-MM-DD.log`，其中包含热键注册结果和
Windows 错误码、热键触发、窗口上下边缘命中、窗口尺寸变化、鼠标滚轮/方向键事件及
当前行索引。启动时自动删除 3 天前的日志，仅保留最近 3 天文件。日志不显示控制台
窗口，可用于反馈交互问题时定位原因。

## 用户 RID

程序首次由某个 Windows 用户启动时，会在该用户的 `%APPDATA%\\gp\\rid.json` 生成唯一 RID，
并后台上报一次。后续启动直接读取已有 RID，不会重复生成或重复上报；上报失败不会影响行情功能。

## 打包为单文件 EXE

安装打包工具：

```powershell
pip install pyinstaller
```

无图标时：

```powershell
.\\.venv\\Scripts\\pyinstaller.exe --noconfirm --clean --distpath release --workpath release_build StockMonitor_onefile.spec
```

若同目录提供 `app.ico`，使用：

```powershell
.\\.venv\\Scripts\\pyinstaller.exe --noconfirm --clean --onefile --windowed --icon app.ico --name gp stock_monitor.py
```

最终生成的 `release\gp.exe` 是单个可执行文件，不需要携带 `_internal` 目录。
将 `config.json`、`stock_list.json` 和说明文件与 `gp.exe` 放在同一目录。
应用会从 EXE 所在目录读取配置，不会将其打包进临时目录。`--windowed` 确保启动时
不显示控制台黑窗口。
