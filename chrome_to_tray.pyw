#!/usr/bin/env python3
"""ChromeToTray 聚合程序(Windows)—— 日常只需要运行它。

它本身不产生托盘图标,只负责管理后台服务(server + 托盘逻辑在同一个后台进程里):
  双击运行(无参数)
      后台服务没在运行 -> 启动它
      后台服务在运行   -> 弹窗询问:停止服务 / 打开设置 / 不动
  这样"开启 / 关闭后台服务"都通过再次运行本程序完成。

命令行(都是可选的):
  --start      服务没运行就启动,不弹任何窗口(适合放进计划任务 / 快捷方式)
  --stop       停止服务(并还原所有被隐藏的窗口),不弹窗口
  --settings   直接打开设置窗口
  --restore-all  应急:后台服务异常退出后,把被隐藏的窗口全部找回来
  --geometry   (与启动配合)命中测试失效时的后备方案,也可以在设置里勾选

内部用:
  --service      后台服务入口
  --native-host  Chrome 启动时由扩展拉起,用来自动启动后台服务

依赖: pip install pystray pillow
首次使用需在 Chrome 扩展程序页(开发者模式)加载本目录;之后 Chrome 每次启动会自动拉起服务(可在设置里关闭)。
"""
import argparse
import base64
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes as wt

import chrome_tray as ct

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ChromeToTray")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
LOG_FILE = os.path.join(DATA_DIR, "service.log")
HOST_NAME = "com.chrometotray.launcher"
HOST_REG_KEY = r"Software\Google\Chrome\NativeMessagingHosts" + "\\" + HOST_NAME
TITLE = "ChromeToTray"

DEFAULTS = {
    "autostart_with_chrome": True,   # Chrome 启动时自动启动后台服务
    "geometry": False,               # 命中测试失效时,按坐标判断 ✕
}

user32 = ct.user32 if ct.IS_WIN else None
if ct.IS_WIN:
    user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
    user32.FindWindowExW.restype = wt.HWND


# ======================================================================
# 配置
# ======================================================================
def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k in DEFAULTS:
            if k in data:
                cfg[k] = bool(data[k])
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ======================================================================
# 小界面(无控制台时的反馈)
# ======================================================================
def msgbox(text, flags=0x40, title=TITLE):
    """flags 默认 MB_ICONINFORMATION;总是置顶。返回按钮 ID(是=6 否=7 取消=2)"""
    return user32.MessageBoxW(None, text, title, flags | 0x40000)


def toast(text, error=False, ms=3000):
    """几秒后自动消失的提示框(MessageBoxTimeoutW 是未公开但长期稳定的接口,不可用时退回普通提示框)"""
    icon = 0x10 if error else 0x40
    if error:
        msgbox(text, icon)
        return
    try:
        f = user32.MessageBoxTimeoutW
        f.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT, wt.WORD, wt.DWORD]
        f.restype = ctypes.c_int
        f(None, text, TITLE, icon | 0x40000, 0, ms)
    except Exception:
        msgbox(text, icon)


# ======================================================================
# 后台服务的启动 / 停止
# ======================================================================
def find_service_window():
    return user32.FindWindowExW(ct.HWND_MESSAGE, None, ct.MSG_WND_CLASS, None)


def service_running():
    return bool(find_service_window())


def script_cmd(*args):
    """用于启动本程序自身的命令行(优先用 pythonw,不弹控制台)"""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    exe = sys.executable
    d, n = os.path.split(exe)
    if n.lower() == "python.exe":
        w = os.path.join(d, "pythonw.exe")
        if os.path.exists(w):
            exe = w
    return [exe, os.path.abspath(__file__), *args]


def spawn_detached(cmd):
    """启动一个脱离当前进程(以及 Chrome 的作业对象)的无窗口后台进程"""
    base = 0x00000008 | 0x00000200            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for extra in (0x01000000, 0):             # 先尝试 CREATE_BREAKAWAY_FROM_JOB,不被允许就去掉
        try:
            subprocess.Popen(cmd, cwd=APP_DIR, creationflags=base | extra, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            continue
    return False


def start_service(check_deps=True, wait=8.0):
    """返回 (成功?, 说明)"""
    if service_running():
        return True, "后台服务已经在运行"
    if check_deps:
        err = ct.check_deps()
        if err:
            return False, err
    cfg = load_config()
    args = ["--service"] + (["--geometry"] if cfg["geometry"] else [])
    if not spawn_detached(script_cmd(*args)):
        return False, "无法启动后台进程"
    deadline = time.time() + wait
    while time.time() < deadline:
        if service_running():
            return True, "后台服务已启动"
        time.sleep(0.2)
    return False, f"后台服务没有在 {wait:.0f} 秒内就绪,请查看日志:\n{LOG_FILE}"


def stop_service(wait=8.0):
    w = find_service_window()
    if not w:
        return True, "后台服务没有在运行"
    msg = user32.RegisterWindowMessageW(ct.MSG_STOP_NAME)
    res = ctypes.c_size_t(0)
    user32.SendMessageTimeoutW(w, msg, 0, 0, 0x2, 3000, ctypes.byref(res))
    deadline = time.time() + wait
    while time.time() < deadline:
        if not service_running():
            return True, "后台服务已停止,被隐藏的窗口已还原"
        time.sleep(0.2)
    return False, f"后台服务没有响应停止请求,请查看日志:\n{LOG_FILE}"


# ======================================================================
# Chrome 启动时自启动:Native Messaging 宿主
#   扩展(background.js)在 Chrome 启动时调用 sendNativeMessage -> Chrome 运行 native_host.bat
#   -> 本程序以 --native-host 模式启动后台服务。开关 = 注册 / 注销这个宿主。
# ======================================================================
def extension_id():
    """由 manifest.json 里的 key 推出扩展 ID(与 Chrome 的算法一致),这样不管目录放哪 ID 都固定"""
    try:
        with open(os.path.join(APP_DIR, "manifest.json"), encoding="utf-8") as f:
            key = json.load(f)["key"]
        h = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
        return h.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    except Exception:
        return None


def register_native_host():
    import winreg
    ext = extension_id()
    if not ext:
        return False, "manifest.json 里没有有效的 key,无法生成扩展 ID"
    os.makedirs(DATA_DIR, exist_ok=True)
    bat = os.path.join(DATA_DIR, "native_host.bat")
    line = " ".join(f'"{c}"' for c in script_cmd("--native-host"))
    with open(bat, "w", encoding="utf-8", newline="") as f:
        f.write("@echo off\r\nchcp 65001 >nul\r\n" + line + "\r\n")
    manifest_path = os.path.join(DATA_DIR, HOST_NAME + ".json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"name": HOST_NAME, "description": "ChromeToTray: Chrome 启动时自动启动后台服务",
                   "path": bat, "type": "stdio",
                   "allowed_origins": [f"chrome-extension://{ext}/"]}, f, ensure_ascii=False, indent=2)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, HOST_REG_KEY) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, manifest_path)
    return True, ext


def unregister_native_host():
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, HOST_REG_KEY)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def sync_native_host(cfg):
    try:
        if cfg["autostart_with_chrome"]:
            return register_native_host()
        unregister_native_host()
        return True, ""
    except Exception as ex:
        return False, str(ex)


def native_host():
    """由 Chrome 调用:按设置决定是否启动后台服务,然后回一条消息就退出"""
    started = False
    try:
        if load_config()["autostart_with_chrome"]:
            if not service_running():
                spawn_detached(script_cmd("--service"))
            started = True
    except Exception:
        pass
    data = json.dumps({"ok": started}).encode("utf-8")
    out = len(data).to_bytes(4, "little") + data
    try:
        if sys.stdout is not None:
            sys.stdout.buffer.write(out)
            sys.stdout.buffer.flush()
        else:
            os.write(1, out)
    except Exception:
        pass     # 回不回都无所谓,Chrome 只是看到宿主退出


# ======================================================================
# 后台服务入口(server + 托盘逻辑,同一个进程)
# ======================================================================
def service(geometry=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    try:    # pythonw 没有控制台:把输出写到日志文件
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1024 * 1024:
            os.remove(LOG_FILE)
        log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    except OSError:
        pass
    print(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 后台服务启动 (pid {os.getpid()}) ====")
    try:
        if not ct.acquire_instance():
            print("已经有一个后台服务在运行,本次退出")
            return
        err = ct.check_deps()
        if err:
            print(err)
            return
        geometry = geometry or load_config()["geometry"]
        import server
        try:
            server.start_background()
            print(f"标签页服务已启动: http://{server.HOST}:{server.PORT}/")
        except OSError as ex:
            print(f"标签页服务没能启动({ex});端口可能被旧的 server.py 占用,将使用已有的服务")
        ct.run_tray(geometry)
        print("后台服务已退出")
    except Exception:
        import traceback
        traceback.print_exc()


# ======================================================================
# 设置窗口
# ======================================================================
def settings_ui():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    import tkinter as tk
    from tkinter import ttk

    cfg = load_config()
    root = tk.Tk()
    root.title("ChromeToTray 设置")
    root.resizable(False, False)
    f = ttk.Frame(root, padding=16)
    f.grid()

    status = tk.StringVar()
    ttk.Label(f, textvariable=status, font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w")
    btn = ttk.Button(f, width=12)
    btn.grid(row=0, column=1, sticky="e", padx=(16, 0))

    def refresh():
        running = service_running()
        status.set("后台服务:运行中" if running else "后台服务:未运行")
        btn.config(text="停止服务" if running else "启动服务", state="normal")

    def toggle():
        btn.config(state="disabled")
        root.update()
        running = service_running()
        ok, msg = stop_service() if running else start_service()
        refresh()
        if not ok:
            msgbox(msg, 0x10)

    btn.config(command=toggle)

    v_auto = tk.BooleanVar(value=cfg["autostart_with_chrome"])
    v_geo = tk.BooleanVar(value=cfg["geometry"])
    ttk.Separator(f).grid(row=1, column=0, columnspan=2, sticky="ew", pady=12)
    ttk.Checkbutton(f, text="Chrome 启动时自动启动后台服务", variable=v_auto).grid(
        row=2, column=0, columnspan=2, sticky="w")
    ttk.Label(f, foreground="#666", justify="left",
              text="需要已在 Chrome 中加载本目录的扩展;更改后下次启动 Chrome 生效。").grid(
        row=3, column=0, columnspan=2, sticky="w", padx=(22, 0), pady=(0, 8))
    ttk.Checkbutton(f, text="备用模式:按窗口坐标判断 ✕(命中测试失效时才需要)", variable=v_geo).grid(
        row=4, column=0, columnspan=2, sticky="w")
    ttk.Label(f, foreground="#666", text="全屏时可能误触;重启后台服务后生效。").grid(
        row=5, column=0, columnspan=2, sticky="w", padx=(22, 0), pady=(0, 8))

    def save():
        new = {"autostart_with_chrome": v_auto.get(), "geometry": v_geo.get()}
        try:
            save_config(new)
        except OSError as ex:
            msgbox(f"保存设置失败:{ex}", 0x10)
            return
        ok, msg = sync_native_host(new)
        if not ok:
            msgbox(f"设置已保存,但注册自启动失败:\n{msg}", 0x30)
        root.destroy()

    bar = ttk.Frame(f)
    bar.grid(row=6, column=0, columnspan=2, sticky="e", pady=(8, 0))
    ttk.Button(bar, text="保存", width=10, command=save).grid(row=0, column=0, padx=4)
    ttk.Button(bar, text="取消", width=10, command=root.destroy).grid(row=0, column=1)
    refresh()
    root.mainloop()


# ======================================================================
# 入口
# ======================================================================
def interactive():
    sync_native_host(load_config())            # 顺手保证自启动注册与设置一致、路径是最新的
    if not service_running():
        ok, msg = start_service()
        if ok:
            toast("ChromeToTray 已在后台启动。\n\n再次运行本程序可以停止服务或打开设置。")
        else:
            toast(msg, error=True)
        return
    r = msgbox("ChromeToTray 正在运行。\n\n"
               "  是 —— 停止服务(并还原被隐藏的窗口)\n"
               "  否 —— 打开设置\n"
               "  取消 —— 保持运行,什么也不做",
               0x23)   # MB_YESNOCANCEL | MB_ICONQUESTION
    if r == 6:
        ok, msg = stop_service()
        toast(msg, error=not ok)
    elif r == 7:
        settings_ui()


def main():
    ap = argparse.ArgumentParser(description="ChromeToTray 聚合程序", allow_abbrev=False)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--service", action="store_true", help="(内部)后台服务入口")
    g.add_argument("--native-host", action="store_true", help="(内部)Chrome 启动时由扩展拉起")
    g.add_argument("--start", action="store_true", help="服务没运行就静默启动")
    g.add_argument("--stop", action="store_true", help="静默停止服务")
    g.add_argument("--settings", action="store_true", help="打开设置窗口")
    g.add_argument("--restore-all", action="store_true", help="应急:找回被隐藏的窗口")
    ap.add_argument("--geometry", action="store_true", help="按窗口坐标判断 ✕(命中测试失效时的后备)")
    args, _ = ap.parse_known_args()      # Chrome 会给宿主传 chrome-extension://... 参数,忽略它

    if not ct.IS_WIN:
        sys.exit("本程序只支持 Windows")
    if args.native_host:
        return native_host()
    if args.service:
        return service(args.geometry)
    if args.restore_all:
        n = ct.restore_all()
        return toast(f"已还原 {n} 个窗口")
    if args.start:
        start_service()
        return
    if args.stop:
        stop_service()
        return
    if args.settings:
        return settings_ui()
    interactive()


if __name__ == "__main__":
    main()
