/** Adapter for a mobile host's existing remote MCP client. No embedded access tokens. */
export class MobilePluginError extends Error {
  constructor(code, message) { super(message); this.name = "MobilePluginError"; this.code = code; }
}

function serviceOrigin(value) {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash ||
      url.pathname !== "/" || ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)) {
    throw new MobilePluginError("invalid_service", "手机插件需要已部署的 HTTPS 服务。");
  }
  return url.origin;
}

/** This is the Yingban custom import format, not a universal MCP package standard. */
export function validateInstallation(manifest, expectedOrigin) {
  const origin = serviceOrigin(expectedOrigin);
  if (manifest?.schema !== "yingban.remote-plugin.v1" || manifest.id !== "yingban-vidu") {
    throw new MobilePluginError("invalid_manifest", "插件安装文件格式不正确。");
  }
  if (manifest.installable !== true) {
    throw new MobilePluginError("deployment_not_ready", "映伴服务尚未部署完成，请联系插件提供方。");
  }
  if (manifest.mcp?.transport !== "streamable-http" || manifest.mcp.url !== origin + "/mcp" ||
      manifest.auth?.type !== "oauth2" || manifest.auth.resource !== manifest.mcp.url ||
      manifest.auth.resource_metadata_url !== origin + "/.well-known/oauth-protected-resource/mcp" ||
      manifest.ui?.panel_origin !== origin) {
    throw new MobilePluginError("invalid_manifest", "安装配置与已确认的服务地址不一致。");
  }
  return {serverUrl:manifest.mcp.url, resource:manifest.auth.resource,
    resourceMetadataUrl:manifest.auth.resource_metadata_url};
}

/** Run in host UI plumbing BEFORE forwarding a tool result to the language model. */
export function routeToolResult(result, expectedOrigin) {
  const origin = serviceOrigin(expectedOrigin);
  const model = {
    content: result.content ?? [],
    ...(result.structuredContent === undefined ? {} : {structuredContent:result.structuredContent}),
    ...(result.isError === undefined ? {} : {isError:result.isError}),
  };
  const privatePanel = result._meta?.yingbanPanel;
  if (!privatePanel || result.isError) return {model, panel:null};
  const url = new URL(privatePanel.panel_url);
  if (url.origin !== origin || url.pathname !== "/panel" || url.search || url.username ||
      url.password || !/^#ticket=[A-Za-z0-9_-]{32,100}$/.test(url.hash) ||
      !["account", "payment"].includes(privatePanel.purpose)) {
    throw new MobilePluginError("invalid_panel", "登录面板不属于当前插件服务。");
  }
  return {model, panel:{url:url.href, purpose:privatePanel.purpose}};
}

function snapshot(result, workflowId) {
  if (result.isError) throw new MobilePluginError("tool_error", "任务查询失败，请保留原任务后重试查询。");
  let data = result.structuredContent;
  if (!data) {
    const item = result.content?.find(value => value.type === "text");
    try { data = JSON.parse(item?.text); } catch { /* Reject a malformed tool response below. */ }
  }
  if (!data || data.workflow_id !== workflowId || typeof data.state !== "string") {
    throw new MobilePluginError("invalid_workflow", "查询结果与原任务不一致。");
  }
  return data;
}

function abortReason(signal) {
  return signal?.reason ?? new MobilePluginError("aborted", "已暂停查询；原视频任务继续保留。");
}

function wait(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(abortReason(signal)); return; }
    const finish = () => { signal?.removeEventListener("abort", abort); resolve(); };
    const timer = setTimeout(finish, milliseconds);
    const abort = () => {
      clearTimeout(timer); signal?.removeEventListener("abort", abort); reject(abortReason(signal));
    };
    signal?.addEventListener("abort", abort, {once:true});
  });
}

/** Persist workflowId per signed-in app user. Resume this READ loop after app foregrounding.
 * callTool({name, arguments}, {signal}) is supplied by the host's authenticated MCP client.
 * Errors stop polling; login, payment, reconnect and foregrounding never call confirm_generation.
 */
export async function* watchWorkflow(callTool, workflowId, {signal, pollMs = 3000} = {}) {
  if (typeof callTool !== "function" || !/^[a-f0-9]{32}$/.test(workflowId) ||
      !Number.isFinite(pollMs) || pollMs < 500 || pollMs > 60_000) {
    throw new MobilePluginError("invalid_input", "请使用原任务编号和有效的查询间隔。");
  }
  while (true) {
    if (signal?.aborted) throw abortReason(signal);
    const result = await callTool({name:"vidu_get_workflow", arguments:{workflow_id:workflowId}}, {signal});
    if (signal?.aborted) throw abortReason(signal);
    const current = snapshot(result, workflowId);
    yield current;
    if (!["generating", "submitting"].includes(current.state)) return;
    await wait(pollMs, signal);
  }
}
