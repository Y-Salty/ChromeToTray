// 标签页/窗口/分组变化 -> 防抖 -> 推送完整快照到本地服务
const ENDPOINT = "http://127.0.0.1:8765/tabs";
let timer = null;

async function pushSnapshot() {
  const [tabs, wins, groups] = await Promise.all([
    chrome.tabs.query({}),
    chrome.windows.getAll({ windowTypes: ["normal", "popup"] }),
    chrome.tabGroups ? chrome.tabGroups.query({}) : Promise.resolve([])
  ]);
  const payload = {
    ts: Date.now(),
    windows: wins.map(w => ({ id: w.id, focused: w.focused, type: w.type, incognito: w.incognito, state: w.state })),
    groups: groups.map(g => ({ id: g.id, title: g.title, color: g.color, collapsed: g.collapsed })),
    tabs: tabs.map(t => ({
      id: t.id,
      windowId: t.windowId,
      index: t.index,
      active: t.active,
      title: t.title || "",
      url: t.url || t.pendingUrl || "",
      favIconUrl: t.favIconUrl || "",
      status: t.status,
      audible: !!t.audible,
      muted: !!(t.mutedInfo && t.mutedInfo.muted),
      pinned: t.pinned,
      groupId: t.groupId,
      incognito: t.incognito
    }))
  };
  try {
    await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
  } catch (e) {
    // 本地服务没启动时忽略,下次事件再推
  }
}

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(pushSnapshot, 80); // 80ms 内的连续事件合并为一次
}

[
  chrome.tabs.onCreated,
  chrome.tabs.onRemoved,
  chrome.tabs.onActivated,
  chrome.tabs.onMoved,
  chrome.tabs.onAttached,
  chrome.tabs.onDetached,
  chrome.tabs.onReplaced,
  chrome.windows.onCreated,
  chrome.windows.onRemoved,
  chrome.windows.onFocusChanged
].forEach(ev => ev.addListener(schedule));

if (chrome.tabGroups) {
  chrome.tabGroups.onCreated.addListener(schedule);
  chrome.tabGroups.onUpdated.addListener(schedule);
  chrome.tabGroups.onRemoved.addListener(schedule);
}

// 只在会影响显示的属性变化时推送
chrome.tabs.onUpdated.addListener((id, c) => {
  if ("title" in c || "url" in c || "status" in c || "favIconUrl" in c ||
      "audible" in c || "mutedInfo" in c || "pinned" in c || "groupId" in c) {
    schedule();
  }
});

// Chrome 启动时让本机的 ChromeToTray 启动后台服务(Native Messaging)。
// 宿主由 chrome_to_tray.pyw 注册;设置里关闭"Chrome 启动时自动启动"后宿主会被注销,
// 这里就会收到"找不到宿主"的错误,直接忽略即可。
const NATIVE_HOST = "com.chrometotray.launcher";
function ensureService() {
  try {
    chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd: "ensure" }, () => {
      void chrome.runtime.lastError; // 读一下以免控制台报"未检查的错误"
    });
  } catch (e) {
    // 忽略
  }
}

// 启动/安装时推一次;每分钟兜底一次,防止 service worker 休眠后状态过期
chrome.runtime.onStartup.addListener(() => {
  ensureService();
  pushSnapshot();
});
chrome.runtime.onInstalled.addListener(() => {
  ensureService();
  pushSnapshot();
  chrome.alarms.create("keepalive", { periodInMinutes: 1 });
});
chrome.alarms.onAlarm.addListener(pushSnapshot);
pushSnapshot();

// ---------- 接收服务端指令(长轮询) ----------
const CMD_URL = "http://127.0.0.1:8765/commands?wait=25";
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function activateTab(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    await chrome.tabs.update(tabId, { active: true });
    const win = await chrome.windows.get(tab.windowId);
    await chrome.windows.update(tab.windowId, {
      focused: true,
      ...(win.state === "minimized" ? { state: "normal" } : {})
    });
  } catch (e) {
    console.warn("切换失败(标签可能已关闭):", e);
  }
}

async function closeTab(tabId) {
  // 与点击标签关闭按钮同一路径:页面若注册了 beforeunload,会弹出确认,用户可取消
  chrome.tabs.remove(tabId).catch(e => console.warn("关闭失败(标签可能已不存在):", e));
  await sleep(700);
  try {
    await chrome.tabs.get(tabId);   // 700ms 后还在 -> 多半在等页面确认
    await activateTab(tabId);       // 切过去并聚焦,让用户能看到确认提示
  } catch (e) {
    // 已经关闭
  }
}

async function pollCommands() {
  for (;;) {
    try {
      const r = await fetch(CMD_URL);
      if (!r.ok) { await sleep(2000); continue; }
      for (const c of await r.json()) {
        if (c.type === "activate") await activateTab(c.tabId);
        else if (c.type === "close") closeTab(c.tabId); // 不 await,避免等待用户确认时阻塞后续指令
      }
    } catch (e) {
      await sleep(2000); // 服务没启动,稍后重试
    }
  }
}
pollCommands();
