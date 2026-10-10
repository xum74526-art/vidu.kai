import {App} from "@modelcontextprotocol/ext-apps";

const origin = "__YINGBAN_ORIGIN__";
const app = new App({name:"映伴官方操作面板", version:"0.8.0"});
const frame = document.getElementById("official-panel"), status = document.getElementById("status");
window.addEventListener("message", event => {
  if (event.origin !== origin || event.source !== frame.contentWindow) return;
  if (!["yingban.panel.connected", "yingban.panel.closed"].includes(event.data?.type)) return;
  frame.hidden = true; frame.src = "about:blank"; status.hidden = false;
  status.textContent = event.data.type === "yingban.panel.connected"
    ? "账号已连接，可以回到对话继续配置视频。" : "面板已收起。请在对话中查询账号或原订单状态；关闭面板不代表付款成功。";
});
app.ontoolresult = result => {
  const panel = result._meta?.yingbanPanel;
  if (!panel?.panel_url) {
    status.textContent = "宿主未提供面板授权，请重新调用官方面板工具。"; return;
  }
  try {
    const url = new URL(panel.panel_url);
    if (url.origin !== origin || url.pathname !== "/panel" || !url.hash.startsWith("#ticket=")) throw new Error();
    frame.src = url.href; frame.hidden = false; status.hidden = true;
  } catch { status.textContent = "面板地址不属于当前服务，已停止打开。"; }
};
await app.connect();
