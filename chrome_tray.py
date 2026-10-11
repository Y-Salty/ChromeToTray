#!/usr/bin/env python3
"""Chrome 窗口"关闭到托盘"工具(Windows)。一个 Chrome 窗口对应一个托盘图标。

行为
  - 点击 Chrome 窗口的 ✕:窗口隐藏并出现该窗口专属的托盘图标(Ctrl+点击 ✕ 可放行,真正关闭)
  - 在任务栏上中键点击 Chrome 窗口按钮:效果同点击 ✕(需要配套的 Windhawk 模组,见 README)
  - 点击 "-":保持原样,收到任务栏
  - 托盘单击:还原窗口
  - 托盘右键菜单:还原窗口 / 切换到标签页 / 关闭标签页 / 关闭此窗口 / 结束 Chrome 进程 / 退出本程序
      * 切换标签页:还原窗口并切到该标签
      * 关闭标签页:无"离开确认"则直接关闭、窗口保持隐藏;
                    页面弹出"离开确认"则还原窗口并切到该标签,由你决定
  - 窗口被还原(无论谁还原的)后,该托盘图标自动消失

依赖
  pip install pystray pillow
  server.py 需要在运行,并且 Chrome 里已加载 extension(标签页信息与切换/关闭都靠它)

日常使用请运行聚合程序 chrome_to_tray.pyw(它会在后台同时带起 server 和本模块)。
下面的命令行仍可单独使用,主要用于调试:
  python chrome_tray.py                 正常运行(需另行运行 server.py)
  python chrome_tray.py --probe         诊断:把鼠标移到 ✕ / - / □ 上,看命中测试结果(✕ 应为 20)
  python chrome_tray.py --geometry      命中测试失效时的后备:按窗口右上角坐标判断 ✕(全屏时可能误触)
  python chrome_tray.py --restore-all   应急:本程序异常退出后,把被隐藏的窗口全部找回来
"""
import argparse
import atexit
import ctypes
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

IS_WIN = sys.platform == "win32"
SERVER = "http://127.0.0.1:8765"
CLOSE_WAIT = 1.5      # 关闭标签后等待多久;超时仍在 => 认为页面弹出了"离开确认"
STATE_FILE = os.path.join(tempfile.gettempdir(), "chrome_tray_hidden.json")

SW_HIDE, SW_SHOW, SW_RESTORE = 0, 5, 9
WM_CLOSE, WM_NCHITTEST = 0x0010, 0x0084
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WH_MOUSE_LL = 14
HTCLOSE = 20
MSG_WND_CLASS = "ChromeToTrayMsgWnd"          # 消息窗口类名:Windhawk 模组与启动器靠它找到本程序
MSG_HIDE_NAME = "ChromeToTray.HideWindow"     # wParam=Chrome 窗口句柄, lParam bit0=1 表示隐藏该进程的全部可见窗口
MSG_STOP_NAME = "ChromeToTray.Stop"           # 请求本程序退出(并还原所有窗口)
HWND_MESSAGE = -3
GW_OWNER = 4
PALETTE = ["#1a73e8", "#d93025", "#188038", "#f9ab00", "#9334e6", "#007b83", "#fa903e", "#d01884"]

# ======================================================================
# Windows API
# ======================================================================
if IS_WIN:
    from ctypes import wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)

    def _sig(fn, argtypes, restype=None):
        fn.argtypes = argtypes
        if restype is not None:
            fn.restype = restype

    _sig(user32.SetWindowsHookExW, [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD], wt.HHOOK)
    _sig(user32.UnhookWindowsHookEx, [wt.HHOOK], wt.BOOL)
    _sig(user32.CallNextHookEx, [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM], ctypes.c_ssize_t)
    _sig(user32.WindowFromPoint, [wt.POINT], wt.HWND)
    _sig(user32.GetAncestor, [wt.HWND, wt.UINT], wt.HWND)
    _sig(user32.GetClassNameW, [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int)
    _sig(user32.GetWindowTextLengthW, [wt.HWND], ctypes.c_int)
    _sig(user32.GetWindowTextW, [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int)
    _sig(user32.GetWindowThreadProcessId, [wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD)
    for _f in (user32.IsWindow, user32.IsWindowVisible, user32.IsIconic, user32.IsZoomed):
        _sig(_f, [wt.HWND], wt.BOOL)
    _sig(user32.ShowWindow, [wt.HWND, ctypes.c_int], wt.BOOL)
    _sig(user32.PostMessageW, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], wt.BOOL)
    _sig(user32.SendMessageTimeoutW,
         [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_size_t)],
         ctypes.c_ssize_t)
    _sig(user32.SetForegroundWindow, [wt.HWND], wt.BOOL)
    _sig(user32.GetWindowRect, [wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL)
    _sig(user32.GetCursorPos, [ctypes.POINTER(wt.POINT)], wt.BOOL)
    _sig(user32.GetAsyncKeyState, [ctypes.c_int], ctypes.c_short)
    _sig(user32.PeekMessageW, [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT], wt.BOOL)
    _sig(user32.TranslateMessage, [ctypes.POINTER(wt.MSG)], wt.BOOL)
    _sig(user32.DispatchMessageW, [ctypes.POINTER(wt.MSG)], ctypes.c_ssize_t)
    _sig(user32.MsgWaitForMultipleObjects,
         [wt.DWORD, ctypes.c_void_p, wt.BOOL, wt.DWORD, wt.DWORD], wt.DWORD)
    _sig(user32.MessageBoxW, [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT], ctypes.c_int)
    try:
        _sig(user32.GetDpiForWindow, [wt.HWND], wt.UINT)   # Win10 1607+
    except AttributeError:
        pass
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                    ("lpszClassName", wt.LPCWSTR)]

    _sig(user32.RegisterClassW, [ctypes.POINTER(WNDCLASSW)], wt.ATOM)
    _sig(user32.CreateWindowExW, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID], wt.HWND)
    _sig(user32.DefWindowProcW, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], ctypes.c_ssize_t)
    _sig(user32.DestroyWindow, [wt.HWND], wt.BOOL)
    _sig(user32.RegisterWindowMessageW, [wt.LPCWSTR], wt.UINT)
    _sig(user32.EnumWindows, [WNDENUMPROC, wt.LPARAM], wt.BOOL)
    _sig(user32.GetWindow, [wt.HWND, wt.UINT], wt.HWND)
    _sig(kernel32.OpenProcess, [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE)
    _sig(kernel32.CloseHandle, [wt.HANDLE], wt.BOOL)
    _sig(kernel32.QueryFullProcessImageNameW,
         [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)], wt.BOOL)
    _sig(kernel32.GetModuleHandleW, [wt.LPCWSTR], wt.HMODULE)
    _sig(kernel32.CreateMutexW, [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR], wt.HANDLE)


# ---- 对窗口的小封装(便于在非 Windows 环境下做逻辑测试时替换) ----
def window_alive(hwnd):
    return bool(user32.IsWindow(hwnd))


def window_visible(hwnd):
    return bool(user32.IsWindowVisible(hwnd))


def get_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def get_class(hwnd):
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def get_pid(hwnd):
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


_exe_cache = {}


def exe_of_pid(pid):
    if pid in _exe_cache:
        return _exe_cache[pid]
    name = ""
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        buf = ctypes.create_unicode_buffer(520)
        n = wt.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            name = os.path.basename(buf.value).lower()
        kernel32.CloseHandle(h)
    if name:
        _exe_cache[pid] = name
    return name


def is_chrome_top(hwnd):
    """顶层窗口,类名 Chrome_WidgetWin_1 且进程是 chrome.exe(排除 Edge/Electron 等同类名窗口)"""
    return get_class(hwnd) == "Chrome_WidgetWin_1" and exe_of_pid(get_pid(hwnd)) == "chrome.exe"


def show_window(hwnd):
    """还原并置前"""
    user32.ShowWindow(hwnd, SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW)
    user32.keybd_event(0x12, 0, 0, 0)   # 模拟按一下 Alt,绕过前台锁定
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 2, 0)


def hide_window(hwnd):
    user32.ShowWindow(hwnd, SW_HIDE)


def post_close(hwnd):
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def confirm_kill(pid):
    text = (f"将强制结束 Chrome 进程(PID {pid})。\n\n"
            "同一浏览器配置下的所有 Chrome 窗口都会一并关闭,未保存的内容会丢失。\n\n确定继续?")
    # MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
    return user32.MessageBoxW(None, text, "结束 Chrome 进程", 0x4 | 0x30 | 0x10000 | 0x40000) == 6


def kill_pid(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=0x08000000)


def chrome_windows_of_pid(pid):
    """某个 Chrome 进程当前可见的"主窗口"(有标题、无所有者;排除气泡/提示等辅助窗口)"""
    out = []

    def cb(h, _):
        try:
            if (window_visible(h) and get_class(h) == "Chrome_WidgetWin_1" and get_pid(h) == pid
                    and not user32.GetWindow(h, GW_OWNER) and user32.GetWindowTextLengthW(h) > 0):
                out.append(int(h))
        except Exception:
            pass
        return True

    proc = WNDENUMPROC(cb)
    user32.EnumWindows(proc, 0)
    return out


# ---- 命中测试 ----
def nc_hittest(hwnd, x, y):
    """向窗口询问某屏幕坐标属于哪个区域;✕ 按钮应返回 HTCLOSE(20)。超时返回 None"""
    res = ctypes.c_size_t(0)
    lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    ok = user32.SendMessageTimeoutW(hwnd, WM_NCHITTEST, 0, lparam, 0x2, 80, ctypes.byref(res))  # ABORTIFHUNG
    return (res.value & 0xFFFFFFFF) if ok else None


def in_close_rect(hwnd, x, y):
    """后备方案:按窗口右上角的固定尺寸估算 ✕ 按钮区域(46x32 DIP)"""
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    try:
        s = user32.GetDpiForWindow(hwnd) / 96.0
    except Exception:
        s = 1.0
    if user32.IsZoomed(hwnd):
        return r.right - int(54 * s) <= x <= r.right and r.top <= y <= r.top + int(40 * s)
    return r.right - int(47 * s) <= x <= r.right and r.top <= y <= r.top + int(34 * s)


def close_button_at(x, y, geometry=False):
    """(x, y) 处若是某个 chrome.exe 窗口的 ✕ 按钮,返回该窗口句柄"""
    h = user32.WindowFromPoint(wt.POINT(x, y))
    if not h:
        return None
    root = user32.GetAncestor(h, 2) or h   # GA_ROOT
    if not is_chrome_top(root):
        return None
    code = nc_hittest(root, x, y)
    if code == HTCLOSE:
        return root
    if geometry and code is not None and in_close_rect(root, x, y):
        return root
    return None


# ======================================================================
# 与 server.py 通信
# ======================================================================
def api(path, body=None, timeout=3.0):
    """成功返回 dict;HTTP 错误返回 {"error": 状态码};连不上返回 None"""
    try:
        if body is None:
            r = urllib.request.urlopen(SERVER + path, timeout=timeout)
        else:
            req = urllib.request.Request(SERVER + path, data=json.dumps(body).encode("utf-8"),
                                         method="POST", headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"error": e.code}
    except Exception:
        return None


# ======================================================================
# 纯逻辑:窗口匹配、菜单标签
# ======================================================================
def _norm(s):
    return " ".join((s or "").split())


def match_window_id(win_title, snap):
    """Chrome 窗口标题形如"<当前标签标题> - Google Chrome";用它在快照里反查 windowId。
    多个窗口匹配时取标题最长者,仍并列则取当前获得焦点的窗口。"""
    title = _norm(win_title)
    if not snap or not title:
        return None
    by_win = {}
    for t in snap.get("tabs", []):
        by_win.setdefault(t["windowId"], []).append(t)
    focused = {w["id"] for w in snap.get("windows", []) if w.get("focused")}

    def scan(pred):
        out = []
        for wid, tabs in by_win.items():
            act = next((t for t in tabs if t.get("active")), None)
            tt = _norm(act.get("title") if act else "")
            if tt and pred(title, tt):
                out.append((len(tt), wid))
        return out

    scored = scan(lambda w, t: w.startswith(t)) or scan(lambda w, t: t in w)
    if not scored:
        return None
    best = max(n for n, _ in scored)
    cands = [wid for n, wid in scored if n == best]
    pref = [w for w in cands if w in focused]
    return (pref or cands)[0]


def tab_label(t):
    s = _norm(t.get("title") or t.get("url") or "新标签页")
    if len(s) > 46:
        s = s[:45] + "…"
    s = s.replace("&", "&&")   # 菜单里 & 表示快捷键
    return ("📌 " if t.get("pinned") else "") + ("🔊 " if t.get("audible") else "") + s


def make_icon_image(num):
    from PIL import Image, ImageDraw, ImageFont
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, S - 2, S - 2), fill=PALETTE[(num - 1) % len(PALETTE)])
    d.ellipse((14, 14, S - 14, S - 14), fill="white")
    font = None
    for name in ("segoeuib.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(name, 30)
            break
        except Exception:
            pass
    txt = str(num)
    if font:
        d.text((S / 2, S / 2), txt, fill=PALETTE[(num - 1) % len(PALETTE)], font=font, anchor="mm")
    else:
        d.text((S / 2 - 4, S / 2 - 6), txt, fill=PALETTE[(num - 1) % len(PALETTE)])
    return img


# ======================================================================
# 托盘管理
# ======================================================================
class Entry:
    def __init__(self, hwnd, wid, num):
        self.hwnd, self.wid, self.num = hwnd, wid, num
        self.icon = None
        self.sig = None


class Manager:
    def __init__(self, geometry=False):
        self.entries = {}          # hwnd -> Entry
        self.lock = threading.RLock()
        self.snap = None           # 最近一次 /tabs 快照
        self.hide_q = queue.Queue()
        self.stop = threading.Event()
        self.geometry = geometry

    # ---------- 状态文件(应急找回用) ----------
    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump([int(h) for h in self.entries], f)
        except Exception:
            pass

    # ---------- 隐藏 / 新建托盘 ----------
    def request_hide(self, hwnd):
        self.hide_q.put(hwnd)

    def hide(self, hwnd):
        import pystray
        with self.lock:
            if hwnd in self.entries or not window_alive(hwnd) or not window_visible(hwnd):
                return
            snap = api("/tabs", timeout=1.5)
            if snap and "tabs" in snap:
                self.snap = snap          # 菜单首次构建时就要有标签数据
            wid = match_window_id(get_title(hwnd), self.snap) if self.snap else None
            used = {e.num for e in self.entries.values()}
            num = next(n for n in range(1, 100) if n not in used)
            e = Entry(hwnd, wid, num)
            tabs = self._tabs_of(e, self.snap)
            e.sig = self._signature(tabs)
            e.icon = pystray.Icon(f"chrome-tray-{int(hwnd)}", make_icon_image(num),
                                  self._tooltip(e, tabs), pystray.Menu(lambda: self._menu_items(e)))
            self.entries[hwnd] = e
            self._save_state()
            hide_window(hwnd)
        threading.Thread(target=e.icon.run, daemon=True).start()

    def hide_from_message(self, hwnd, whole_process=False):
        """处理 Windhawk 模组发来的"隐藏"请求(在消息窗口的回调里执行,必须快速返回)。
        返回 True 表示已接管(模组就不再关闭窗口)。"""
        if not window_alive(hwnd) or not is_chrome_top(hwnd):
            return False
        if hwnd in self.entries:
            return True
        targets = [hwnd] if window_visible(hwnd) else []
        if whole_process:
            pid = get_pid(hwnd)
            targets = [h for h in chrome_windows_of_pid(pid) if h not in self.entries] or targets
        for h in targets:
            self.request_hide(h)
        return bool(targets)

    def drop(self, e):
        with self.lock:
            if self.entries.pop(e.hwnd, None) is None:
                return
            self._save_state()
        try:
            e.icon.stop()
        except Exception:
            pass

    # ---------- 快照辅助 ----------
    @staticmethod
    def _tabs_of(e, snap):
        if not snap or e.wid is None or "tabs" not in snap:
            return None
        return sorted((t for t in snap["tabs"] if t["windowId"] == e.wid), key=lambda t: t["index"])

    @staticmethod
    def _signature(tabs):
        if tabs is None:
            return None
        return tuple((t["id"], t.get("title"), t.get("active"), t.get("pinned"), t.get("audible")) for t in tabs)

    @staticmethod
    def _tooltip(e, tabs):
        act = next((t for t in (tabs or []) if t.get("active")), None)
        s = f"Chrome 窗口 {e.num}" + (f" · {_norm(act['title'])}" if act and act.get("title") else "")
        return s[:120]

    # ---------- 菜单 ----------
    @staticmethod
    def _cb(fn, *args):
        """菜单回调:放到后台线程执行,避免阻塞托盘消息循环。(pystray 按参数个数识别回调,所以不能用默认参数)"""
        def cb(icon, item):
            threading.Thread(target=fn, args=args, daemon=True).start()
        return cb

    @staticmethod
    def _checked(flag):
        def chk(item):
            return flag
        return chk

    @staticmethod
    def _noop(icon, item):
        pass

    def _menu_items(self, e):
        import pystray
        Item, Menu = pystray.MenuItem, pystray.Menu
        tabs = self._tabs_of(e, self.snap)
        items = [Item("还原窗口", self._cb(self.restore, e), default=True), Menu.SEPARATOR]
        if tabs:
            sw = [Item(tab_label(t), self._cb(self.switch_tab, e, t["id"]), checked=self._checked(bool(t.get("active"))))
                  for t in tabs]
            cl = [Item(tab_label(t), self._cb(self.close_tab, e, t["id"])) for t in tabs]
            items += [Item(f"切换到标签页 ({len(tabs)})", Menu(*sw)), Item("关闭标签页", Menu(*cl))]
        else:
            items.append(Item("(无标签页信息:服务未运行 / 无痕窗口)", self._noop, enabled=False))
        items += [Menu.SEPARATOR,
                  Item("关闭此窗口", self._cb(self.close_window, e)),
                  Item("结束 Chrome 进程…", self._cb(self.kill_process, e)),
                  Menu.SEPARATOR,
                  Item("退出本程序(还原所有窗口)", self._cb(self.quit))]
        return tuple(items)

    # ---------- 菜单动作 ----------
    def restore(self, e):
        show_window(e.hwnd)
        self.drop(e)

    def switch_tab(self, e, tab_id):
        show_window(e.hwnd)
        self.drop(e)
        api("/activate", {"tabId": tab_id})

    def close_tab(self, e, tab_id):
        r = api("/close", {"tabId": tab_id, "wait": CLOSE_WAIT}, timeout=CLOSE_WAIT + 3)
        if r and r.get("closed") is False:
            # 标签还在 => 页面弹出了"离开确认":还原窗口并切到该标签,让用户处理
            show_window(e.hwnd)
            self.drop(e)
            api("/activate", {"tabId": tab_id})
        # closed 为 True:已直接关闭,窗口保持隐藏;菜单随下一次刷新更新

    def close_window(self, e):
        post_close(e.hwnd)
        time.sleep(CLOSE_WAIT)
        if window_alive(e.hwnd):         # 窗口还在 => 有标签弹出"离开确认"
            show_window(e.hwnd)
            self.drop(e)
        # 否则窗口已关闭,refresh 会清理托盘

    def kill_process(self, e):
        pid = get_pid(e.hwnd)
        if confirm_kill(pid):
            kill_pid(pid)

    def quit(self):
        self.stop.set()

    # ---------- 后台线程:处理隐藏请求 + 定期刷新 ----------
    def worker(self):
        while not self.stop.is_set():
            try:
                hwnd = self.hide_q.get(timeout=0.8)
            except queue.Empty:
                hwnd = None
            try:
                if hwnd:
                    self.hide(hwnd)
                self.refresh()
            except Exception as ex:
                print("worker 错误:", ex)

    def refresh(self):
        snap = api("/tabs", timeout=1.5)
        self.snap = snap if snap and "tabs" in snap else None
        for e in list(self.entries.values()):
            if not window_alive(e.hwnd) or window_visible(e.hwnd):
                self.drop(e)       # 窗口已关闭,或被别的途径重新显示
                continue
            if e.wid is None and self.snap:
                e.wid = match_window_id(get_title(e.hwnd), self.snap)
            tabs = self._tabs_of(e, self.snap)
            sig = self._signature(tabs)
            if sig != e.sig:
                e.sig = sig
                try:
                    e.icon.title = self._tooltip(e, tabs)
                    e.icon.update_menu()      # pystray 的菜单是静态的,标签变化后需要主动刷新
                except Exception:
                    pass

    def shutdown(self):
        for e in list(self.entries.values()):
            try:
                if window_alive(e.hwnd):
                    show_window(e.hwnd)
            except Exception:
                pass
            self.drop(e)
        try:
            os.remove(STATE_FILE)
        except OSError:
            pass


# ======================================================================
# 鼠标钩子:拦截 ✕
# ======================================================================
class MouseHook:
    def __init__(self, on_close_click, geometry=False):
        self.on_close_click = on_close_click
        self.geometry = geometry
        self.pending = None
        self.h = None
        self._cb = HOOKPROC(self._proc)   # 必须持有引用,否则会被回收

    def _proc(self, n_code, w_param, l_param):
        try:
            if n_code == 0 and w_param in (WM_LBUTTONDOWN, WM_LBUTTONUP):
                info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                x, y = info.pt.x, info.pt.y
                if w_param == WM_LBUTTONDOWN:
                    self.pending = None
                    if not (user32.GetAsyncKeyState(0x11) & 0x8000):   # 按住 Ctrl:放行,真正关闭
                        hwnd = close_button_at(x, y, self.geometry)
                        if hwnd:
                            self.pending = hwnd
                            return 1                                    # 吞掉按下
                elif self.pending:
                    hwnd, self.pending = self.pending, None
                    if close_button_at(x, y, self.geometry) == hwnd:    # 松开时仍在 ✕ 上才算点击
                        self.on_close_click(hwnd)
                    return 1                                            # 吞掉松开
        except Exception:
            pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def install(self):
        self.h = user32.SetWindowsHookExW(WH_MOUSE_LL, self._cb, kernel32.GetModuleHandleW(None), 0)
        if not self.h:
            raise OSError(f"安装鼠标钩子失败,错误码 {ctypes.get_last_error()}")

    def uninstall(self):
        if self.h:
            user32.UnhookWindowsHookEx(self.h)
            self.h = None


class MsgWindow:
    """消息窗口(仅消息的隐藏窗口)。作用:
      1. 接收 Windhawk 模组"中键点击任务栏"发来的隐藏请求;
      2. 接收启动器发来的"退出"请求;
      3. 让启动器/模组能判断本程序是否在运行。
    必须在运行 pump() 的那个线程里创建。"""

    def __init__(self, mgr):
        self.mgr = mgr
        self.msg_hide = user32.RegisterWindowMessageW(MSG_HIDE_NAME)
        self.msg_stop = user32.RegisterWindowMessageW(MSG_STOP_NAME)
        self._proc = WNDPROC(self._wndproc)       # 必须持有引用
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = MSG_WND_CLASS
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise OSError(f"注册消息窗口类失败,错误码 {ctypes.get_last_error()}")
        self.hwnd = user32.CreateWindowExW(0, MSG_WND_CLASS, "ChromeToTray", 0, 0, 0, 0, 0,
                                           HWND_MESSAGE, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"创建消息窗口失败,错误码 {ctypes.get_last_error()}")

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == self.msg_hide:
                return 1 if self.mgr.hide_from_message(int(wparam), bool(lparam & 1)) else 0
            if msg == self.msg_stop or msg == WM_CLOSE:
                self.mgr.stop.set()
                return 0
        except Exception as ex:
            print("消息窗口处理出错:", ex)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def destroy(self):
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None


def pump(stop):
    """消息循环(鼠标钩子的回调靠它触发)。用带超时的等待,这样 Ctrl+C 和退出信号都能及时响应。"""
    msg = wt.MSG()
    while not stop.is_set():
        user32.MsgWaitForMultipleObjects(0, None, False, 50, 0x04FF)   # QS_ALLINPUT
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


# ======================================================================
# 辅助命令
# ======================================================================
HT_NAMES = {0: "HTNOWHERE", 1: "HTCLIENT(网页区域)", 2: "HTCAPTION(标题栏)", 3: "HTSYSMENU",
            8: "HTMINBUTTON(最小化 -)", 9: "HTMAXBUTTON(最大化 □)", 20: "HTCLOSE(关闭 ✕)",
            0xFFFFFFFF: "HTTRANSPARENT", 0xFFFFFFFE: "HTERROR"}


def probe():
    print("把鼠标移到 Chrome 窗口右上角的 - □ ✕ 按钮上,观察结果。✕ 应显示 HTCLOSE(20)。Ctrl+C 退出。")
    last = None
    while True:
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        h = user32.WindowFromPoint(pt)
        root = (user32.GetAncestor(h, 2) or h) if h else None
        if root and is_chrome_top(root):
            code = nc_hittest(root, pt.x, pt.y)
            desc = "超时无响应" if code is None else HT_NAMES.get(code, f"其他({code})")
        else:
            desc = "(不是 chrome.exe 的窗口)"
        if desc != last:
            print(desc)
            last = desc
        time.sleep(0.15)


def restore_all():
    try:
        with open(STATE_FILE) as f:
            hs = json.load(f)
    except Exception:
        hs = []
    n = 0
    for h in hs:
        if window_alive(h) and get_class(h) == "Chrome_WidgetWin_1":
            user32.ShowWindow(h, SW_SHOW)
            n += 1
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass
    print(f"已还原 {n} 个窗口")
    return n


def enable_dpi_awareness():
    """让鼠标坐标与 Chrome 一致(物理像素)"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def check_deps():
    """依赖可用返回 None,否则返回错误说明"""
    try:
        import pystray                      # noqa: F401
        from PIL import Image, ImageDraw    # noqa: F401  (这里会真正加载 Pillow 的 C 扩展)
    except ImportError as ex:
        return (f"依赖不可用: {ex}\n"
                "请用运行本程序的同一个 Python 重装:\n"
                f'  "{sys.executable}" -m pip install --force-reinstall --no-cache-dir pillow pystray')
    return None


def acquire_instance():
    """单实例锁。已经有实例在运行返回 False"""
    kernel32.CreateMutexW(None, False, "Local\\ChromeTrayPerWindow")
    return ctypes.get_last_error() != 183     # ERROR_ALREADY_EXISTS


def run_tray(geometry=False):
    """运行托盘功能,直到收到退出请求 / Ctrl+C。调用前需已 acquire_instance()、check_deps()。"""
    enable_dpi_awareness()
    mgr = Manager(geometry)
    hook = MouseHook(mgr.request_hide, geometry)
    msgwin = None
    try:
        hook.install()
        msgwin = MsgWindow(mgr)
        atexit.register(mgr.shutdown)
        threading.Thread(target=mgr.worker, daemon=True).start()
        if api("/tabs", timeout=1.5) is None:
            print(f"提示:连不上 {SERVER},托盘菜单暂时没有标签页信息(请确认 server 在运行)")
        print("已运行:点击 Chrome 的 ✕ 或中键点击任务栏按钮会收到托盘(Ctrl+点击 ✕ 可真正关闭)。")
        pump(mgr.stop)
    except KeyboardInterrupt:
        pass
    finally:
        mgr.stop.set()
        hook.uninstall()
        if msgwin:
            msgwin.destroy()
        mgr.shutdown()


def main():
    global SERVER, CLOSE_WAIT
    ap = argparse.ArgumentParser(description="Chrome 窗口关闭到托盘(一窗口一托盘)")
    ap.add_argument("--server", default=SERVER, help="server.py 的地址")
    ap.add_argument("--close-wait", type=float, default=CLOSE_WAIT, help="关闭标签后等待多少秒判定是否有离开确认")
    ap.add_argument("--geometry", action="store_true", help="命中测试失效时,按窗口右上角坐标判断 ✕")
    ap.add_argument("--probe", action="store_true", help="诊断:显示鼠标下的命中测试结果")
    ap.add_argument("--restore-all", action="store_true", help="应急:找回被本程序隐藏的窗口")
    args = ap.parse_args()
    SERVER, CLOSE_WAIT = args.server.rstrip("/"), args.close_wait

    if not IS_WIN:
        sys.exit("本程序只支持 Windows")
    enable_dpi_awareness()

    if args.restore_all:
        return restore_all()
    if args.probe:
        try:
            return probe()
        except KeyboardInterrupt:
            return
    err = check_deps()
    if err:
        sys.exit(err)
    if not acquire_instance():
        sys.exit("已经有一个 chrome_tray 在运行了")
    run_tray(args.geometry)


if __name__ == "__main__":
    main()
