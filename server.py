"""接收扩展推送的标签页快照,并通过 SSE 实时推送给网页。
通常不需要单独运行:聚合程序 chrome_to_tray.pyw 会在后台自动带起它。
单独运行: python server.py
然后浏览器打开 http://127.0.0.1:8765/ 即可看到实时标签栏。
接口:
  GET  /          实时标签栏页面 (index.html)
  GET  /events    SSE 实时事件流
  GET  /tabs      当前最新快照 (JSON)
  POST /tabs      扩展推送快照
  POST /activate  让 Chrome 切换到指定标签页
                  body: {"tabId": 123}  或  {"match": "github"}(按标题/URL 子串匹配第一个)
  POST /close     请求关闭标签页(等同点击标签的关闭按钮,页面的"关闭前确认"会正常弹出)
                  body: {"tabId": 123} 或 {"match": "github"},可选 "wait": 秒数
                  带 wait 时会等待标签真正消失,返回 {"closed": true/false}
  GET  /commands  扩展长轮询取指令(内部使用)
"""
import json
import os
import sys
import threading
import time
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 8765
HERE = os.path.dirname(os.path.abspath(__file__))

cond = threading.Condition()
version = 0
state = {"ts": 0, "windows": [], "groups": [], "tabs": []}


cmd_cond = threading.Condition()
commands = []


def find_tab(body):
    tabs = state["tabs"]
    if "tabId" in body:
        return next((t for t in tabs if t["id"] == body["tabId"]), None)
    kw = str(body.get("match", "")).lower()
    if kw:
        return next((t for t in tabs if kw in (t["title"] + " " + t["url"]).lower()), None)
    return None


def bring_to_front(title):
    """Windows 专用:把标题匹配的 Chrome 窗口提到最前(尽力而为)。
    Windows 的前台锁定可能让扩展的 focus 只闪烁任务栏,这里补一次置前。"""
    if sys.platform != "win32" or not title:
        return
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum(hwnd, _):
            if not u.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(hwnd, cls, 64)
            if cls.value != "Chrome_WidgetWin_1":
                return True
            n = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(title):
                found.append(hwnd)
            return True

        time.sleep(0.35)  # 等标签切换后窗口标题更新
        u.EnumWindows(enum, 0)
        if found:
            hwnd = found[0]
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 9)  # SW_RESTORE
            u.keybd_event(0x12, 0, 0, 0)       # 按下 Alt,解除前台锁定
            u.SetForegroundWindow(hwnd)
            u.keybd_event(0x12, 0, 2, 0)       # 松开 Alt
    except Exception as e:
        print("置前失败:", e)


def log_diff(old, new):
    o = {t["id"]: t for t in old}
    n = {t["id"]: t for t in new}
    for i in n.keys() - o.keys():
        print(f"[+ 新建] #{i} {n[i]['title']!r} {n[i]['url']}")
    for i in o.keys() - n.keys():
        print(f"[- 关闭] #{i} {o[i]['title']!r}")
    for i in n.keys() & o.keys():
        a, b = o[i], n[i]
        if a["title"] != b["title"]:
            print(f"[~ 标题] #{i} {a['title']!r} -> {b['title']!r}")
        if a["url"] != b["url"]:
            print(f"[~ 跳转] #{i} {a['url']} -> {b['url']}")
        if not a["active"] and b["active"]:
            print(f"[* 切换] 当前标签 -> #{i} {b['title']!r}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code=200, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/tabs":
            with cond:
                body = json.dumps(state, ensure_ascii=False).encode("utf-8")
            self._send(200, body)
        elif self.path == "/events":
            self._sse()
        elif self.path.startswith("/commands"):
            q = parse_qs(urlparse(self.path).query)
            wait = min(float(q.get("wait", ["25"])[0]), 25)
            with cmd_cond:
                if not commands:
                    cmd_cond.wait(timeout=wait)
                out = commands[:]
                commands.clear()
            self._send(200, json.dumps(out).encode("utf-8"))
        else:
            self._send(404)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        seen = -1
        try:
            while True:
                with cond:
                    if version == seen:
                        cond.wait(timeout=15)
                    cur, payload = version, json.dumps(state, ensure_ascii=False)
                if cur != seen:
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    seen = cur
                else:
                    self.wfile.write(b": keepalive\n\n")  # 心跳,防止连接被断开
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        global version
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/tabs":
            with cond:
                log_diff(state["tabs"], data.get("tabs", []))
                state.update({"windows": [], "groups": [], **data})
                version += 1
                cond.notify_all()
            return self._send(200, b"{}")
        if self.path == "/activate":
            with cond:
                tab = find_tab(data)
            if not tab:
                return self._send(404, json.dumps({"error": "找不到匹配的标签页"}, ensure_ascii=False).encode("utf-8"))
            with cmd_cond:
                commands.append({"type": "activate", "tabId": tab["id"]})
                cmd_cond.notify_all()
            print(f"[> 切换] #{tab['id']} {tab['title']!r}")
            threading.Thread(target=bring_to_front, args=(tab["title"],), daemon=True).start()
            return self._send(200, json.dumps({"queued": True, "tab": tab["id"]}).encode("utf-8"))
        if self.path == "/close":
            with cond:
                tab = find_tab(data)
            if not tab:
                return self._send(404, json.dumps({"error": "找不到匹配的标签页"}, ensure_ascii=False).encode("utf-8"))
            with cmd_cond:
                commands.append({"type": "close", "tabId": tab["id"]})
                cmd_cond.notify_all()
            print(f"[x 关闭请求] #{tab['id']} {tab['title']!r}")
            wait = min(float(data.get("wait", 0)), 30)
            closed = None
            if wait > 0:
                deadline = time.time() + wait
                with cond:
                    while any(t["id"] == tab["id"] for t in state["tabs"]):
                        left = deadline - time.time()
                        if left <= 0:
                            break
                        cond.wait(timeout=left)
                    closed = not any(t["id"] == tab["id"] for t in state["tabs"])
            out = {"queued": True, "tab": tab["id"]}
            if closed is not None:
                out["closed"] = closed  # False 通常表示页面弹出了"确认离开"提示,等待用户处理
            return self._send(200, json.dumps(out).encode("utf-8"))
        self._send(404)

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 会允许两个进程同时监听同一端口,这里关掉,
    # 这样端口已被占用(比如旧的 server.py 还在跑)时能如实报错
    allow_reuse_address = False
    daemon_threads = True


def start_background(host=HOST, port=PORT):
    """在后台线程启动服务(聚合程序用)。端口被占用时抛出 OSError。"""
    srv = Server((host, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="tab-server").start()
    return srv


if __name__ == "__main__":
    print(f"实时标签栏: http://{HOST}:{PORT}/   (Ctrl+C 退出)")
    Server((HOST, PORT), Handler).serve_forever()
