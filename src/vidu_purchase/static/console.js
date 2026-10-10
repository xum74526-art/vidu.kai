"use strict";
const $ = id => document.getElementById(id);
let csrf = null, capabilities = null, connectionAvailable = false, flow = null, timer = null;
let busy = false, confirmAction = null, pendingIntent = null;
let standalonePurchase = null, purchaseTimer = null, pendingPurchase = null;
let browserConnectionAvailable = false;
const labels = {
  account_required: ["连接账号", "先连接你的 Vidu 账号"],
  needs_purchase: ["积分不足", "需要补充可用额度"],
  awaiting_payment: ["等待支付", "请核对并确认支付"],
  preparing_purchase: ["准备中", "正在准备支付入口"],
  verifying_payment: ["核验中", "正在核验原订单"],
  awaiting_entitlement: ["已付款", "等待官网权益生效"],
  awaiting_generation_confirmation: ["可以继续", "配置和费用已就绪"],
  submitting: ["提交中", "视频任务正在提交"],
  submission_unknown: ["等待核查", "提交结果暂时不确定"],
  generating: ["生成中", "视频正在生成"],
  completed: ["已完成", "视频任务已完成"],
  failed: ["未完成", "本次视频生成失败"],
  cancelled: ["已结束", "本次请求已结束"],
  blocked: ["暂不可用", "还需要处理一项条件"],
};

function feedback(message) { $("feedback").textContent = message || ""; $("feedback").hidden = !message; }
function errorMessage(error) { return error?.message || "暂时无法取得结果，请稍后再试。"; }
async function request(path, { method = "GET", body, key, auth = true } = {}) {
  const headers = { "X-Yingban-Console": "1", Accept: "application/json" };
  if (csrf) headers["X-CSRF-Token"] = csrf;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (key) headers["Idempotency-Key"] = key;
  const response = await fetch((auth ? "/console/api" : "") + path, {
    method, headers, credentials: "same-origin", cache: "no-store", redirect: "error",
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(45_000),
  });
  let data;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    const error = new Error(data?.error?.message || (response.status === 404 ? "当前服务未开启本机控制页，请使用本机启动入口。" : "服务暂时不可用，请稍后查询原请求。"));
    error.code = data?.error?.code;
    if (response.status === 401) { csrf = null; $("launch-notice").hidden = false; $("prepare-button").disabled = true; }
    throw error;
  }
  return data;
}
function addButton(text, action, primary = false) {
  const button = document.createElement("button"); button.type = "button";
  button.textContent = text; button.className = primary ? "primary" : "secondary";
  button.addEventListener("click", () => run(action)); $("flow-actions").append(button);
}
function addLink(text, url) {
  try { if (new URL(url).protocol !== "https:") return; } catch { return; }
  const link = document.createElement("a"); link.textContent = text; link.href = url;
  link.target = "_blank"; link.rel = "noopener noreferrer"; $("flow-actions").append(link);
}
async function openOfficialPanel(purpose = "account", purchaseId = null) {
  const body = {purpose}; if (purchaseId) body.purchase_id = purchaseId;
  const result = await request("/v1/browser-panels", {method:"POST", body});
  const url = new URL(result.panel_url);
  if (url.origin !== location.origin || url.pathname !== "/panel") throw new Error("面板地址无效。");
  if ($("account-dialog").open) $("account-dialog").close();
  clearTimeout(timer); clearTimeout(purchaseTimer);
  $("official-panel-frame").src = url.href;
  $("official-panel-dialog").showModal();
}
function closeOfficialPanel() {
  $("official-panel-frame").src = "about:blank";
  $("official-panel-dialog").close();
  if (flow) timer = setTimeout(() => refreshFlow().catch(error => feedback(errorMessage(error))), 1000);
  if (standalonePurchase) purchaseTimer = setTimeout(() => refreshPurchase().catch(error => feedback(errorMessage(error))), 1000);
}
$("close-official-panel").onclick = closeOfficialPanel;
$("official-panel-dialog").addEventListener("cancel", closeOfficialPanel);
window.addEventListener("message", event => {
  if (event.origin !== location.origin || event.source !== $("official-panel-frame").contentWindow) return;
  if (!["yingban.panel.connected", "yingban.panel.closed"].includes(event.data?.type)) return;
  closeOfficialPanel();
  run(async () => { await refreshAccount(); await refreshBrowser(); await refreshConnection(); });
});
async function run(action) {
  if (busy) return;
  busy = true; feedback(""); $("prepare-button").disabled = true;
  try { await action(); } catch (error) { feedback(errorMessage(error)); }
  finally { busy = false; $("prepare-button").disabled = !csrf; }
}
function showConfirmation(title, text, detail, label, action) {
  $("confirm-title").textContent = title; $("confirm-description").textContent = text;
  $("confirm-detail").textContent = detail; $("confirm-action").textContent = label;
  confirmAction = action; $("confirm-dialog").showModal();
}
function modelOptions() {
  const ranges = { "3.0": [5, 5], "3.1": [2, 8], "3.2": [1, 16], "3.2_a": [4, 15] };
  const model = $("model").value, old = Number($("duration").value || 8), [min, max] = ranges[model];
  $("duration").replaceChildren();
  for (let value = min; value <= max; value++) { const option = new Option(`${value} 秒`, String(value)); $("duration").add(option); }
  $("duration").value = String(Math.max(min, Math.min(max, old)));
  const resolution = $("resolution").value;
  $("resolution").replaceChildren(new Option("1080p", "1080p"));
  if (model === "3.2_a") $("resolution").add(new Option("720p", "720p"));
  if (resolution === "720p" && model === "3.2_a") $("resolution").value = resolution;
}
function config() {
  return { prompt: $("prompt").value.trim(), model_version: $("model").value,
    duration: Number($("duration").value), resolution: $("resolution").value,
    aspect_ratio: $("aspect").value, sample_count: Number($("samples").value), schedule_mode: $("schedule").value };
}
function restoreConfig(value) {
  $("prompt").value = value.prompt; $("model").value = value.model_version; modelOptions();
  $("duration").value = value.duration; $("resolution").value = value.resolution;
  $("aspect").value = value.aspect_ratio; $("samples").value = value.sample_count; $("schedule").value = value.schedule_mode;
}
async function refreshAccount() {
  const account = await request("/v1/account");
  const connected = account.status === "connected";
  $("account-badge").textContent = connected ? "已连接" : account.status === "login_required" ? "需要重新连接" : "未连接";
  $("account-badge").classList.toggle("neutral", !connected);
  $("credits").textContent = account.credits === null ? "—" : account.credits.toLocaleString();
  $("account-message").textContent = connected ? "已读取官网实际额度，可以检查视频生成条件。" : "连接一次 Vidu 授权，之后会自动连接。";
  $("connect-button").hidden = connected && !browserConnectionAvailable;
  $("connect-button").textContent = connected ? "管理账号连接" : "连接 Vidu 账号";
  $("connect-button").disabled = !connectionAvailable;
  $("disconnect-button").hidden = !connectionAvailable || account.status === "not_connected";
  $("refresh-account").disabled = false;
  if (!connectionAvailable && !connected) $("account-message").textContent = "当前使用服务端账号配置，请由服务管理员完成连接。";
}
function renderFlow(value) {
  clearTimeout(timer); timer = null; flow = value;
  sessionStorage.setItem("yingban.workflow", value.workflow_id);
  $("new-video").hidden = !["cancelled", "completed", "failed"].includes(value.state);
  $("empty-result").hidden = true; $("flow-result").hidden = false; $("refresh-flow").hidden = false;
  const [badge, title] = labels[value.state] || ["处理中", "正在检查生成条件"];
  $("flow-badge").textContent = badge; $("flow-title").textContent = title;
  $("flow-message").textContent = value.message; $("flow-actions").replaceChildren(); $("media").replaceChildren();
  $("flow-cost").hidden = !value.estimate;
  if (value.estimate) $("flow-cost").textContent = `预计 ${value.estimate.cost_credits} 积分 · 当前 ${value.estimate.current_credits} 积分`;
  if (value.state === "account_required" && connectionAvailable) {
    $("flow-message").textContent = "视频需求已保存。先连接你的 Vidu 账号，再继续检查配置和积分。";
    addButton("连接 Vidu 账号", openAccount, true);
  }
  if (value.state === "needs_purchase") {
    if (capabilities.billing_adapter_configured) addButton("比较套餐", () => showPlans(true), true);
    else $("flow-message").textContent = "视频需求已保存。当前积分不足，官网套餐购买尚未接通；补充额度后可刷新本次请求。";
  }
  if (value.purchase?.handoff_url && value.state === "awaiting_payment") {
    if (capabilities.official_browser_panel_available) addButton("在此确认官方支付", () => openOfficialPanel("payment", value.purchase_id), true);
    else addLink("打开支付确认页", value.purchase.handoff_url);
  }
  if (value.state === "awaiting_generation_confirmation") {
    $("flow-title").textContent = value.purchase_id ? "购买成功，可以继续使用" : "视频已经准备好";
    $("flow-message").textContent = value.purchase_id ? "是否继续生成刚才的视频？支付到账不会自动开始生成。" : "请核对当前配置和预计消耗，确认后才提交视频。";
    addButton("继续生成", () => {
      const chosen = value;
      showConfirmation("确认生成视频", "这一步会向 Vidu 提交视频任务，并消耗当前显示的积分。",
        `Vidu ${chosen.config.model_version} · ${chosen.config.duration} 秒 · ${chosen.config.resolution} · ${chosen.config.aspect_ratio}\n${chosen.config.sample_count} 条视频 · 预计 ${chosen.estimate.cost_credits} 积分`,
        "确认并生成", async () => renderFlow(await request(`/v1/videos/${chosen.workflow_id}/confirm`, { method: "POST", body: { confirmation_id: chosen.confirmation_id } })));
    }, true);
  }
  if (["account_required", "needs_purchase", "awaiting_generation_confirmation", "blocked"].includes(value.state)) addButton("暂不继续", async () => renderFlow(await request(`/v1/videos/${value.workflow_id}/cancel`, { method: "POST", body: { revision: value.revision } })));
  for (const url of value.task?.media_urls || []) {
    if (!url.startsWith("https://")) continue;
    const player = document.createElement("video"); player.controls = true; player.preload = "metadata"; player.src = url; $("media").append(player);
  }
  if (!$("official-panel-dialog").open && ["awaiting_payment", "verifying_payment", "awaiting_entitlement", "generating"].includes(value.state)) timer = setTimeout(() => refreshFlow().catch(error => feedback(errorMessage(error))), 5000);
}
async function refreshFlow() { if (flow) renderFlow(await request(`/v1/videos/${flow.workflow_id}`)); }
async function refreshConnection() {
  const result = await request("/v1/account/connection");
  $("connection-status").textContent = result.message;
  $("account-link-message").hidden = !browserConnectionAvailable;
  $("account-link-message").textContent = result.account_link_verified ? "官网与视频账号已关联" : "视频授权与官网登录可以在连接窗口关联";
  $("authorize-browser-account").disabled = result.account_link_verified;
  $("authorize-browser-account").textContent = result.account_link_verified ? "已连接，可返回对话继续" : "我已登录，授权并连接";
  return result;
}
async function openAccount() {
  if (browserConnectionAvailable && capabilities.official_browser_panel_available) {
    await openOfficialPanel("account"); return;
  }
  $("connection-error").hidden = true;
  $("browser-account-connect").hidden = !browserConnectionAvailable;
  $("manual-account-connect").open = !browserConnectionAvailable;
  if (!$("account-dialog").open) $("account-dialog").showModal();
  if (browserConnectionAvailable) await refreshConnection();
}
function price(plan, renewal = false) {
  const amount = renewal ? plan.renewal_amount_minor : plan.amount_minor;
  return amount === null ? "待官网确认" : new Intl.NumberFormat("zh-CN", { style: "currency", currency: plan.currency }).format(amount / 100);
}
async function refreshBrowser() {
  $("plans-button").disabled = true;
  $("browser-message").textContent = "正在检查专用浏览器的官网登录状态…";
  const session = await request("/v1/commerce/session");
  $("browser-message").textContent = session.message;
  $("billing-badge").textContent = session.status === "connected" ? "官网登录已连接" : "等待官网连接";
  $("plans-button").disabled = session.status !== "connected";
  $("website-orders").hidden = session.status !== "connected";
  $("browser-handoff").hidden = true;
  if (session.handoff_url) {
    try {
      const url = new URL(session.handoff_url);
      if (url.protocol === "https:" || (url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname))) {
        $("browser-handoff").href = url.href; $("browser-handoff").hidden = false;
      }
    } catch { /* Do not turn invalid provider text into a link. */ }
  }
}
function renderPurchase(value) {
  clearTimeout(purchaseTimer); standalonePurchase = value;
  sessionStorage.setItem("yingban.purchase", value.purchase_id);
  $("purchase-result").hidden = false;
  $("purchase-title").textContent = value.state === "ready" ? "可以继续使用" : value.state === "awaiting_payment_confirmation" ? "等待你确认支付" : "购买进度";
  $("purchase-message").textContent = value.message + (value.continuation_prompt ? ` ${value.continuation_prompt}` : "");
  $("purchase-actions").replaceChildren();
  if (value.payment_action?.url) {
    const url = new URL(value.payment_action.url);
    if (url.protocol === "https:") {
      if (capabilities.official_browser_panel_available) {
        const button = document.createElement("button"); button.type = "button"; button.className = "primary";
        button.textContent = "在此确认官方支付";
        button.onclick = () => run(() => openOfficialPanel("payment", value.purchase_id));
        $("purchase-actions").append(button);
      } else {
        const link = document.createElement("a"); link.href = url.href; link.textContent = "打开官方支付页";
        link.target = "_blank"; link.rel = "noopener noreferrer"; $("purchase-actions").append(link);
      }
    }
  }
  const refresh = document.createElement("button"); refresh.type = "button"; refresh.className = "text-button";
  refresh.textContent = "查询这次购买"; refresh.onclick = () => run(refreshPurchase); $("purchase-actions").append(refresh);
  if (!$("official-panel-dialog").open && (value.state === "awaiting_payment_confirmation" || value.state === "paid_pending_entitlement")) {
    purchaseTimer = setTimeout(() => refreshPurchase().catch(error => feedback(errorMessage(error))), 10000);
  }
}
async function refreshPurchase() {
  const id = standalonePurchase?.purchase_id;
  if (!id) return;
  const result = await request(`/v1/purchases/${id}`);
  if (standalonePurchase?.purchase_id === id) renderPurchase(result);
}
async function prepareStandalone(quoteId) {
  if (pendingPurchase && pendingPurchase.quote_id !== quoteId) throw new Error("上次购买结果尚未确认，请先恢复原请求，不要重复下单。");
  pendingPurchase ||= { quote_id: quoteId, key: crypto.randomUUID() };
  sessionStorage.setItem("yingban.purchase-intent", JSON.stringify(pendingPurchase));
  try {
    renderPurchase(await request("/v1/purchases", { method: "POST", key: pendingPurchase.key, body: { quote_id: quoteId } }));
  } catch (error) {
    if (["quote_expired", "quote_not_found"].includes(error.code)) {
      pendingPurchase = null; sessionStorage.removeItem("yingban.purchase-intent");
    }
    throw error;
  }
  pendingPurchase = null; sessionStorage.removeItem("yingban.purchase-intent");
}
async function showPlans(forVideo = false) {
  const result = await request(forVideo ? `/v1/videos/${flow.workflow_id}/recommend` : "/v1/plans", { method: forVideo ? "POST" : "GET" });
  $("offers").replaceChildren();
  for (const quote of result.quotes) {
    const plan = quote.plan, card = document.createElement("article"); card.className = "offer";
    const title = document.createElement("h3"); title.textContent = plan.name;
    const amount = document.createElement("strong"); amount.textContent = price(plan);
    const terms = document.createElement("p"); terms.textContent = `${plan.credits} 积分/月。${plan.recurring ? `下月起 ${price(plan, true)}/月，自动续费。` : "不自动续费。"}`;
    const button = document.createElement("button"); button.className = "secondary"; button.textContent = "选择此套餐";
    if (quote.checkout_available === false) {
      button.textContent = "支付页尚待验证"; button.disabled = true;
      card.append(title, amount, terms, button); $("offers").append(card);
      continue;
    }
    button.onclick = () => showConfirmation("确认所选套餐", capabilities.browser_commerce_available ? "创建支付入口后，由你在官方页面确认付款。当前版本还不能自动核验到账。" : "创建支付入口后，还需要你在官方支付页确认付款。", `${plan.name}${plan.account_label ? `\n官网账号：${plan.account_label}` : ""}\n首期 ${price(plan)} · ${terms.textContent}`, "准备支付入口", async () => {
      if (forVideo) renderFlow(await request(`/v1/videos/${flow.workflow_id}/purchase`, { method: "POST", body: { quote_id: quote.quote_id, revision: result.revision } }));
      else await prepareStandalone(quote.quote_id);
    });
    card.append(title, amount, terms, button); $("offers").append(card);
  }
}

$("model").addEventListener("change", modelOptions); modelOptions();
$("new-video").onclick = () => {
  flow = null; pendingIntent = null; clearTimeout(timer);
  sessionStorage.removeItem("yingban.workflow"); sessionStorage.removeItem("yingban.intent");
  $("video-form").reset(); modelOptions(); $("duration").value = "8"; $("empty-result").hidden = false;
  $("flow-result").hidden = true; $("refresh-flow").hidden = true; $("new-video").hidden = true; feedback("");
};
$("connect-button").onclick = () => run(openAccount);
$("open-login-browser").onclick = () => run(async () => {
  await openOfficialPanel("account");
});
$("authorize-browser-account").onclick = async () => {
  const button = $("authorize-browser-account");
  button.disabled = true; button.textContent = "正在验证官网授权…"; $("connection-error").hidden = true;
  try {
    const result = await request("/console/account/browser", { method: "POST", auth: false, body: { authorize: true } });
    await refreshAccount(); await refreshBrowser(); await refreshConnection();
    $("connection-status").textContent = result.message;
    feedback(result.message);
  } catch (error) {
    $("connection-error").textContent = errorMessage(error); $("connection-error").hidden = false;
    button.disabled = false; button.textContent = "我已登录，授权并连接";
  }
};
$("refresh-account").onclick = () => run(refreshAccount);
$("refresh-flow").onclick = () => run(refreshFlow);
$("plans-button").onclick = () => run(() => showPlans(Boolean(flow && flow.state === "needs_purchase")));
$("refresh-browser").onclick = () => run(refreshBrowser);
$("website-orders").onclick = () => run(async () => {
  const result = await request("/v1/commerce/orders");
  feedback(`官网当前显示 ${result.orders.length} 条购买记录。${result.message}`);
});
$("region").onchange = () => { $("official-account").href = $("region").value === "cn" ? "https://www.vidu.cn" : "https://www.vidu.com"; };
document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => $(button.dataset.close).close()));
$("account-dialog").addEventListener("close", () => { $("vidu-token").value = ""; });
$("confirm-action").onclick = () => { const action = confirmAction; confirmAction = null; $("confirm-dialog").close(); if (action) run(action); };
$("disconnect-button").onclick = () => showConfirmation("断开 Vidu 账号", "会删除本机保存的 Vidu 授权。官网会员和已提交的任务不受影响。", "旧账号的视频确认将失效。", "确认断开", async () => {
  await request("/console/account", { method: "DELETE", auth: false });
  clearTimeout(timer); flow = null; pendingIntent = null; sessionStorage.removeItem("yingban.workflow"); sessionStorage.removeItem("yingban.intent");
  clearTimeout(purchaseTimer); standalonePurchase = null; pendingPurchase = null;
  sessionStorage.removeItem("yingban.purchase"); sessionStorage.removeItem("yingban.purchase-intent");
  $("purchase-result").hidden = true; $("purchase-actions").replaceChildren(); $("offers").replaceChildren();
  $("flow-result").hidden = true; $("empty-result").hidden = false; $("refresh-flow").hidden = true; await refreshAccount();
  if (browserConnectionAvailable) await refreshConnection();
});
$("account-form").addEventListener("submit", async event => {
  event.preventDefault(); $("save-account").disabled = true; $("save-account").textContent = "正在验证…"; $("connection-error").hidden = true;
  const token = $("vidu-token").value; $("vidu-token").value = "";
  try {
    await request("/console/account", { method: "POST", auth: false, body: { region: $("region").value, token } });
    $("account-dialog").close(); await refreshAccount(); await refreshFlow(); feedback("账号已连接，可以继续检查视频条件。");
  } catch (error) { $("connection-error").textContent = errorMessage(error); $("connection-error").hidden = false; }
  finally { $("save-account").disabled = false; $("save-account").textContent = "验证并连接"; }
});
$("video-form").addEventListener("submit", event => {
  event.preventDefault();
  run(async () => {
    const current = config();
    if (flow && !["cancelled", "completed", "failed"].includes(flow.state)) {
      if (["generating", "submitting", "submission_unknown"].includes(flow.state)) throw new Error("请先查看原任务状态，不要重复提交。");
      renderFlow(await request(`/v1/videos/${flow.workflow_id}`, { method: "PATCH", body: { config: current, revision: flow.revision } }));
      return;
    }
    if (pendingIntent && JSON.stringify(pendingIntent.config) !== JSON.stringify(current)) throw new Error("上一次保存结果尚未确认，请先恢复上次内容并重试，避免创建重复请求。");
    pendingIntent ||= { key: crypto.randomUUID(), config: current };
    sessionStorage.setItem("yingban.intent", JSON.stringify(pendingIntent));
    renderFlow(await request("/v1/videos", { method: "POST", key: pendingIntent.key, body: { config: current } }));
    pendingIntent = null; sessionStorage.removeItem("yingban.intent");
  });
});

async function initialize() {
  try {
    const health = await fetch("/healthz", { cache: "no-store" });
    if (!health.ok) throw new Error();
    $("service-status").textContent = "本机服务已连接"; $("service-status").classList.add("online");
  } catch { $("service-status").textContent = "本机服务暂不可用"; }
  try {
    const fragment = new URLSearchParams(location.hash.slice(1));
    const ticket = fragment.get("connect"), accountView = fragment.get("view") === "account";
    if (location.hash) history.replaceState(null, "", "/");
    const session = ticket ? await request("/console/session", { method: "POST", auth: false, body: { ticket } }) : await request("/console/session", { auth: false });
    csrf = session.csrf; connectionAvailable = session.account_connection_available;
    browserConnectionAvailable = session.browser_connection_available;
    $("launch-notice").hidden = true; $("prepare-button").disabled = false;
    capabilities = await request("/v1/capabilities");
    $("billing-badge").textContent = capabilities.billing_adapter_configured ? "可查看套餐" : "购买尚未接通";
    $("billing-message").textContent = capabilities.billing_adapter_configured ? "读取官网实际套餐，选择后由你确认支付。" : "目前无法在此购买套餐或充值。官网计费接通后，套餐与支付进度会显示在这里。";
    $("plans-button").hidden = !capabilities.billing_adapter_configured;
    $("browser-session").hidden = !capabilities.browser_commerce_available;
    if (capabilities.browser_commerce_available) {
      $("billing-message").textContent = "对话或此处选择套餐，最后由你在官方支付页确认。到账核验尚未完成，不会自动生成视频。";
      await refreshBrowser();
    }
    await refreshAccount();
    if (browserConnectionAvailable) await refreshConnection();
    const savedPurchase = sessionStorage.getItem("yingban.purchase");
    if (savedPurchase && /^[a-f0-9]{32}$/.test(savedPurchase)) {
      try { renderPurchase(await request(`/v1/purchases/${savedPurchase}`)); }
      catch (error) { feedback(errorMessage(error)); }
    }
    const purchaseIntent = sessionStorage.getItem("yingban.purchase-intent");
    if (purchaseIntent) {
      pendingPurchase = JSON.parse(purchaseIntent);
      $("purchase-result").hidden = false; $("purchase-title").textContent = "上次购买结果尚待查询";
      const retry = document.createElement("button"); retry.type = "button"; retry.textContent = "恢复同一次购买";
      retry.onclick = () => run(() => prepareStandalone(pendingPurchase.quote_id)); $("purchase-actions").append(retry);
    }
    const saved = sessionStorage.getItem("yingban.workflow");
    if (saved && /^[a-f0-9]{32}$/.test(saved)) {
      try { const current = await request(`/v1/videos/${saved}`); restoreConfig(current.config); renderFlow(current); }
      catch (error) { sessionStorage.removeItem("yingban.workflow"); feedback(errorMessage(error)); }
    }
    const pending = sessionStorage.getItem("yingban.intent");
    if (pending && !flow) { pendingIntent = JSON.parse(pending); restoreConfig(pendingIntent.config); feedback("上次保存结果尚未确认。再次点击保存会查询同一个请求。"); }
    if (accountView) await openAccount();
  } catch (error) {
    $("launch-notice").hidden = false; $("launch-message").textContent = errorMessage(error);
    $("billing-badge").textContent = "等待控制页连接"; $("billing-message").textContent = "连接后可查看真实可用能力。";
  }
}
initialize();
window.addEventListener("hashchange", () => {
  if (new URLSearchParams(location.hash.slice(1)).has("connect")) initialize();
});
