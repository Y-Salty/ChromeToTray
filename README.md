# ChromeToTray

把 Chrome 窗口收起到系统托盘(一个窗口一个托盘图标),Windows 专用。

## 日常使用

**只需要运行 `chrome_to_tray.pyw`**(双击;或 `pythonw chrome_to_tray.pyw`)。它本身没有托盘图标,只管后台服务:

| 当前状态 | 再次运行它会 |
| --- | --- |
| 后台服务未运行 | 启动后台服务(server + 托盘逻辑在同一个后台进程里,无窗口) |
| 后台服务运行中 | 询问:**是**=停止服务并还原被隐藏的窗口 / **否**=打开设置 / **取消**=不动 |

另有命令行开关:`--start`(静默启动)、`--stop`(静默停止)、`--settings`、`--restore-all`(应急:后台服务异常退出后找回被隐藏的窗口)。
后台服务的输出写在 `%LOCALAPPDATA%\ChromeToTray\service.log`。

收起窗口的方式:
- 点击 Chrome 窗口的 **✕**(Ctrl+点击 ✕ 真正关闭)
- **在任务栏上中键点击 Chrome 窗口按钮**(需安装下面的 Windhawk 模组)

## 首次部署

1. `pip install pystray pillow`(用运行 `chrome_to_tray.pyw` 的同一个 Python)。
2. Chrome → `chrome://extensions` → 打开"开发者模式" → "加载已解压的扩展程序" → 选本目录。
   > 本版 `manifest.json` 里加了固定的 `key`,扩展 ID 固定为 `heaagjaahhnbkdmjhmojdidkfphjkgal`,与目录位置无关。
   > 旧版已加载的扩展请先移除再重新加载(ID 变了),或点"重新加载"。
3. 运行一次 `chrome_to_tray.pyw`(它会顺手注册"Chrome 启动时自启动",见下)。
4. (可选)安装 Windhawk 模组,见下。

## 1. Windhawk 模组:中键点任务栏收起到托盘

文件:`middle_click_close_to_tray.wh.cpp`,是 *Middle click to close on the taskbar* 的修改版。

安装:Windhawk → Create a New Mod → 把文件内容整个粘贴进去 → Compile → Enable
(与原版模组同时启用会冲突,请先关掉原版)。

行为:
- ChromeToTray 在运行:中键点 Chrome 的任务栏按钮 = 点 ✕,窗口收到托盘。
- ChromeToTray 没运行(或在模组设置里关掉 *Hide Chrome to the tray*):照常关闭窗口。
- Chrome 窗口被合并成一组时,沿用原模组的 *Multiple items behavior*:
  *Close all windows* → 收起该 Chrome 进程所有可见窗口;*Close foreground window* → 只收起最近活动的那个。
- Ctrl+中键(原模组的"结束任务"组合键)不受影响,仍然强制结束进程。
- 只处理 `chrome.exe`,Edge / Electron 等不受影响,走原模组的关闭逻辑。

实现:模组通过 `FindWindowEx` 找到 ChromeToTray 的隐藏消息窗口(类名 `ChromeToTrayMsgWnd`),
发送已注册消息 `ChromeToTray.HideWindow`(wParam=窗口句柄,lParam bit0=整进程)。

## 2. Chrome 启动时自动启动(设置里可开关)

实现方式:扩展在 Chrome 启动(`onStartup`)时调用 Native Messaging 宿主 `com.chrometotray.launcher`,
宿主检查后台服务是否在运行,没有就启动它。不需要常驻进程。

- 开关:再次运行 `chrome_to_tray.pyw` → 选"否"打开设置 → 勾选/取消 *Chrome 启动时自动启动后台服务*。
  开启 = 在 `HKCU\Software\Google\Chrome\NativeMessagingHosts` 下注册宿主;关闭 = 注销。默认开启。
- 每次运行 `chrome_to_tray.pyw` 都会按设置刷新注册(目录或 Python 路径变了也能自动修正)。
- 只在 Chrome *启动*时触发;你手动停止服务后不会被立刻拉起。
- 宿主文件在 `%LOCALAPPDATA%\ChromeToTray\`。

## 排障

- 托盘没反应 / ✕ 没被拦截:`python chrome_tray.py --probe` 把鼠标移到 ✕ 上看是否为 `HTCLOSE(20)`;
  不是的话在设置里勾选"备用模式",重启后台服务。
- 自启动不生效:确认扩展 ID 是上面那个、设置里已勾选、扩展已重新加载;`chrome://extensions` → 扩展的 service worker → 控制台看是否有 native host 错误。
- 后台服务异常退出导致窗口"消失":运行 `chrome_to_tray.pyw --restore-all`。

## 文件

| 文件 | 作用 |
| --- | --- |
| `chrome_to_tray.pyw` | 聚合程序:启动/停止后台服务、设置、自启动宿主 |
| `chrome_tray.py` | 托盘逻辑(鼠标钩子、托盘图标、消息窗口);也可单独运行调试 |
| `server.py` / `index.html` | 标签页快照服务与实时标签栏页面 |
| `manifest.json` / `background.js` | Chrome 扩展 |
| `middle_click_close_to_tray.wh.cpp` | Windhawk 模组 |
