"use strict";
const element = id => document.getElementById(id);
const canvas = element("screen"), context = canvas.getContext("2d"), keyboard = element("keyboard");
let sessionToken = null, epoch = 0, stopped = false, paused = false, live = false;
let timer = null, generation = 0, inputQueue = Promise.resolve(), failures = 0, authorizing = false;
let previousTabs = "";
element("zoom").onclick = () => {
  const enlarged = element("viewport").classList.toggle("zoomed");
  element("zoom").textContent = enlarged ? "适应宽度" : "放大画面";
  element("zoom").setAttribute("aria-pressed", String(enlarged));
  element("help").textContent = enlarged ? "画面已放大。使用边缘滚动条移动视野，在官网画面内滑动可翻页。" : "点击操作，滑动翻页。手机输入时先点官网输入框，再点「输入文字」。";
};

async function api(path, {method = "GET", body, keepalive = false} = {}) {
  const headers = {"X-Yingban-Panel": "1", Accept: "application/json"};
  if (sessionToken) headers.Authorization = `Bearer ${sessionToken}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const result = await fetch(`/panel/${path}`, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "omit", cache: "no-store", redirect: "error", keepalive,
    signal: keepalive ? undefined : AbortSignal.timeout(path === "authorize" ? 65_000 : 25_000),
  });
  const data = await result.json();
  if (!result.ok) {
    const error = new Error(data?.error?.message || "连接暂时不可用。");
    error.code = data?.error?.code; throw error;
  }
  return data;
}
function cover(title, message, retry = false) {
  live = false; context.clearRect(0, 0, canvas.width, canvas.height);
  element("cover").hidden = false; element("cover-title").textContent = title;
  element("cover-message").textContent = message; element("retry").hidden = !retry;
}
function notify(type) {
  // A small status notification only: no images, inputs, credentials, or automatic generation.
  if (window.parent !== window) window.parent.postMessage({type}, "*");
}
function tabs(items) {
  const serialized = JSON.stringify(items);
  if (serialized === previousTabs) return;
  previousTabs = serialized; element("tabs").replaceChildren();
  for (const tab of items) {
    const button = document.createElement("button"); button.type = "button";
    button.textContent = tab.label; button.setAttribute("aria-pressed", String(tab.active));
    button.onclick = () => send({kind:"tab", tab_id:tab.id}); element("tabs").append(button);
  }
}
async function poll() {
  clearTimeout(timer);
  if (stopped || paused || !sessionToken) return;
  const version = generation;
  try {
    const frame = await api("frame");
    if (stopped || paused || version !== generation) return;
    failures = 0;
    if (frame.epoch) epoch = frame.epoch;
    if (frame.tabs) tabs(frame.tabs);
    if (frame.origin) element("origin").textContent = frame.origin;
    element("dismiss-token").hidden = frame.state !== "credential_hidden";
    if (frame.state !== "live") {
      cover(frame.state === "credential_hidden" ? "授权凭据不会传入面板" : "正在处理官网操作", frame.message);
    } else {
      const image = new Image();
      image.src = `data:image/jpeg;base64,${frame.image}`;
      await image.decode();
      if (stopped || paused || version !== generation) return;
      if (canvas.width !== frame.width) canvas.width = frame.width;
      if (canvas.height !== frame.height) canvas.height = frame.height;
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      element("cover").hidden = true; live = true;
      element("status").textContent = frame.account_stage === "loading"
        ? "已连接登录环境，Vidu 官网正在加载…"
        : "已连接 · 可以直接操作下方官方网站";
    }
  } catch (error) {
    if (version !== generation || stopped) return;
    failures++;
    const message = error.name === "TimeoutError" || error.name === "AbortError"
      ? "官网画面响应超时，请重试连接。" : error.message;
    cover("画面已暂停", message, true);
    element("status").textContent = message;
    if (error.code === "panel_expired") { sessionToken = null; stopped = true; }
    if (failures >= 3) paused = true;
  }
  if (!stopped && !paused) timer = setTimeout(poll, live ? 300 : 1200);
}
function send(value) {
  if (stopped || paused || !sessionToken || !epoch) return Promise.resolve();
  const body = {...value, epoch};
  inputQueue = inputQueue.then(() => api("input", {method:"POST", body})).catch(error => {
    element("status").textContent = error.message;
    if (error.code === "panel_expired") { stopped = true; cover("面板已过期", error.message); }
  });
  return inputQueue;
}
const input = YingbanPanelInput.attachPanelInput({canvas, keyboard, send,
  isLive:() => live && !paused && !stopped && !authorizing});
element("show-keyboard").onclick = () => { if (live) keyboard.focus({preventScroll:true}); };
element("hide-keyboard").onclick = () => keyboard.blur();
keyboard.addEventListener("focus", () => { element("hide-keyboard").hidden = false; });
keyboard.addEventListener("blur", () => { element("hide-keyboard").hidden = true; });
element("retry").onclick = () => { if (!sessionToken) return; paused = false; failures = 0; poll(); };
element("dismiss-token").onclick = () => send({kind:"dismiss_token"});
element("authorize").onclick = async () => {
  if (authorizing || stopped) return;
  authorizing = true; paused = true; generation++; clearTimeout(timer); keyboard.blur();
  await inputQueue;
  cover("正在验证授权", "连接成功后可回到对话继续。本次不会生成视频。");
  element("authorize").disabled = true;
  try {
    const result = await api("authorize", {method:"POST", body:{authorize:true}});
    stopped = true; element("status").textContent = result.message;
    cover("账号已连接", "可以回到对话继续配置视频。");
    element("authorize").textContent = "已连接";
    await api("session", {method:"DELETE"}); sessionToken = null;
    notify("yingban.panel.connected");
  } catch (error) {
    authorizing = false; element("authorize").disabled = false; paused = document.hidden;
    element("status").textContent = error.message; poll();
  }
};
async function closePanel(keepalive = false) {
  stopped = true; generation++; clearTimeout(timer); input.reset(); keyboard.blur();
  if (sessionToken) { try { await api("session", {method:"DELETE", keepalive}); } catch {} }
  sessionToken = null;
  cover("面板已收起", "可以回到对话继续。此操作不会确认支付或生成视频。");
  notify("yingban.panel.closed");
}
element("close").onclick = () => closePanel();
window.addEventListener("pagehide", event => {
  if (event.persisted) { paused = true; generation++; clearTimeout(timer); input.reset(); }
  else closePanel(true);
});
window.addEventListener("pageshow", event => {
  if (event.persisted && sessionToken && !stopped && !authorizing) { paused = false; poll(); }
});
document.addEventListener("visibilitychange", () => {
  generation++; clearTimeout(timer);
  if (document.hidden) { paused = true; input.reset(); cover("画面已暂停", "回到面板后继续。"); }
  else if (sessionToken && !stopped && !authorizing) { paused = false; poll(); }
});
(async () => {
  const ticket = new URLSearchParams(location.hash.slice(1)).get("ticket");
  history.replaceState(null, "", location.pathname);
  if (!ticket) { cover("请从智能体打开面板", "面板入口按用户授权，不能直接访问或重复使用。"); return; }
  try {
    const viewport_width = Math.max(360, Math.min(1280, Math.round(element("viewport").clientWidth)));
    const data = await api("session", {method:"POST", body:{ticket, viewport_width}});
    sessionToken = data.session_token;
    element("panel-title").textContent = data.purpose === "payment" ? "确认 Vidu 官方支付" : "连接你的 Vidu 账号";
    element("authorization").hidden = !data.authorization_available || data.purpose !== "account";
    element("payment-note").hidden = data.purpose !== "payment";
    await poll();
  } catch (error) { cover("无法打开面板", error.message); }
})();
