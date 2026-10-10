# 对话内的官方操作面板 · 0.8.0

0.8.2 补充手机触屏滑动、手机键盘删除/回车、中文输入法及后台恢复支持。远程 MCP 的安装描述与标准授权发现见 [手机端接入](mobile-mcp.md)。现有手机 App 还需接入 MCP Apps 或将私有面板元数据交给自己的 WebView；源码下载不等于公网服务已部署。

用户留在你的智能体里，操作自己的 Vidu 官方注册、登录和官方结账页面。每个用户使用独立的服务端浏览器；映伴把浏览器画面放到对话面板里，将用户的点击和键盘输入送回该浏览器。官网仍在原来的官方域名运行，宿主不需要直接 iframe Vidu 或 Stripe。

这次提供了实际运行的 HTTP 接口、MCP Apps 工具和可交互面板。它不是一套独立聊天机器人；你的智能体负责理解需求和展示参数、套餐卡片。普通 MCP 只负责工具调用，宿主需要支持 MCP Apps，或通过下文 HTTP 方式显示面板。

## 用户流程

1. 用户提出视频需求。保存同一份需求，询问模型、时长、比例、分辨率；用户明确让插件选择时采用兼容推荐。
2. 查询 `vidu_connection_status`。没有绑定时说“请连接 Vidu 账号”，不要直接断言用户没有账号。
3. 调用 `vidu_open_official_panel(purpose="account")`。用户在对话里的官方界面注册或登录；密码、验证码和接受条款都由用户在面板操作。
4. 用户点击“我已登录，授权并连接”。面板停止画面采集，服务读取官网可见 Token 面板、验证实际额度并保存到原生凭据库。成功后查询连接状态；以后继续使用保存的同一账号。
5. 额度不足或用户主动购买时，读取真实套餐，展示首期金额、续费金额、积分与自动续费条款。用户选定后沿用报价和幂等键创建一次购买。
6. 调用 `vidu_open_official_panel(purpose="payment", purchase_id=原购买ID)`。用户在对话里的官方支付表单确认；该入口只会打开这笔已核对的结账会话。
7. 收起面板后查询原订单。关闭面板、用户说“已支付”或余额增加都不是支付成功证据。当前浏览器方式的到账自动核验仍未完成，因此不会报告虚假的到账成功。
8. 将来确认付款和权益后，通知“可以继续使用视频插件”，询问是否继续原视频。得到新的生成确认后才提交任务。

## MCP Apps 接入

远程 MCP 地址仍是 `/mcp`。新增工具 `vidu_open_official_panel` 的 `ui.resourceUri` 指向 `ui://yingban/official-panel.html`。资源已使用官方 `@modelcontextprotocol/ext-apps` SDK 构建为单文件 HTML。

工具的普通内容只返回用途、购买 ID 和说明。一次性 `panel_url` 在工具结果的 `_meta.yingbanPanel` 中，宿主必须仅转交该用户的 App 界面，不能把 `_meta` 加入模型上下文、埋点或聊天记录。不需要给模型浏览器点击、密码输入或支付确认工具。

将实际承载 MCP App 的 HTTPS origin 和智能体页面 origin 添加为服务启动参数 `--panel-origin`。MCP Apps 的 iframe 可能经过宿主的独立沙箱域，因此仅填写聊天首页域名不一定够；以宿主部署配置为准。该参数可重复，不支持 `*`。资源的 `ui.csp.frameDomains` 已包含服务 origin。

官方参考：[MCP Apps](https://apps.extensions.modelcontextprotocol.io/api/documents/overview.html)、[FastMCP 自定义 App UI](https://gofastmcp.com/apps/low-level)。不同市场的发布和宿主审批仍需分别办理。

## 自建聊天界面的 HTTP 接入

使用当前用户的 JWT，权限同时包含 `vidu:video` 和 `vidu:purchase`：

```javascript
const panel = await vidu.openOfficialPanel("account");
// 在当前用户的对话卡片内创建 iframe，验证 URL 属于你的映伴服务。
const url = new URL(panel.panel_url);
if (url.origin !== yingbanServiceOrigin || url.pathname !== "/panel") throw new Error("Invalid panel");
frame.sandbox = "allow-scripts allow-same-origin";
frame.referrerPolicy = "no-referrer";
frame.src = url.href;

// 仅在用户选择套餐且服务已成功准备原订单之后：
const paymentPanel = await vidu.openOfficialPanel("payment", purchase.purchase_id);
```

上述方法调用 `POST /v1/browser-panels`，请求只接收 `purpose` 和支付用途下的 `purchase_id`。HTTP 的 `panel_url` 同样只给用户界面，不能传入模型上下文。

面板发送两个不含秘密的 `postMessage` 通知：`yingban.panel.connected` 和 `yingban.panel.closed`。宿主必须同时校验 `event.origin === yingbanServiceOrigin` 和 `event.source === frame.contentWindow`。收到通知后查询真实状态，不能根据通知标记已付款或提交视频。面板打开期间暂停会读取官网 DOM 的后台轮询，避免与用户输入争用同一页面；收起后恢复。

## 本机运行

Codex 本机插件 0.3.1 可复用同一服务的官方登录面板。服务使用 `--account-dir <插件账号目录>` 启动后，自动在该账号目录登记本机路由信息；密钥保留在系统凭据库。首次配置后目录选择会保存在本机配置中。插件调用 `/console/desktop-panel` 时核对账号绑定版本，确保面板保存的授权会被当前插件读到；不一致时拒绝连接。

本机 stdio 宿主的兼容入口由 `vidu_connect_account` 返回一次性 `connection_url`，宿主浏览器直接打开。它只允许本机地址并且不含 Vidu Token；不要发布或长期保存该临时链接。前文远程 MCP Apps 的 `_meta` 约定不变。更新本机插件后，已有宿主进程需重启加载新工具与技能。

```powershell
uv sync --locked
uv run python -X utf8 scripts/browser_session.py --headless
uv run python -X utf8 scripts/selfhost_local.py serve
```

打开启动器给出的一次性本机入口，点击“连接 Vidu 账号”，官网登录界面会直接显示在面板中。`--headless` 不显示独立浏览器窗口；已运行的专用浏览器可继续复用，无须再次启动或注销。浏览器配置目录保留登录状态，Vidu Token 仅存系统凭据库。临时 QA 可使用 `--profile-name` 和独立端口、`selfhost_local.py --data-dir` 隔离数据。

## 多用户部署

先为每个经过验证的 JWT `sub` 分配独立浏览器配置目录和 CDP 端口，并在浏览器映射中绑定。不同用户不能共享配置目录、CDP 端口或 Vidu 账号。CDP 只监听服务器回环地址，公网仅暴露经 HTTPS 代理的应用服务。

在已有 JWKS/issuer/audience 配置基础上运行：

```powershell
uv run vidu-selfhost --host 127.0.0.1 --port 8787 --public-url https://vidu-service.example --browser-bindings deployment/browser-bindings.json --account-vault-root deployment/account-vaults --panel-origin https://agent.example
```

`--account-vault-root` 启用按用户隔离的自助绑定，不能同时使用本机控制台或手动 `accounts.json` 映射。目录中仅保存绑定版本；Token 存 Windows Credential Manager、macOS Keychain 或 Linux Secret Service，不能回退到明文。Linux 服务需要配置可用、解锁的 Secret Service。当前尚不包含浏览器集群自动创建、回收或云端密钥管理服务。

绑定版本改变、浏览器映射改变、授权过期都会拒绝继续使用原面板。用户切换 Vidu 账号前必须结束旧的待确认工作，不能把旧视频配置的扣费确认带到另一个账号。

## 边界与维护

- 一次性入口 5 分钟内使用，面板最多 15 分钟且不超过原访问令牌有效期。入口从地址栏移除，面板会话令牌仅在页面内存中；一个账号同时只允许一个面板。
- 图片实时传输，不录像、不落盘。服务中转用户输入但不写入日志，也不向模型提供输入、Cookie、存储、网络拦截或任意 CDP 接口。部署反向代理同样不要记录这些请求体。
- 可见 Vidu Token 弹窗不会显示或采集到面板，授权导入与画面采集互斥。页面输入不会自动重试，避免重复提交。
- 当前支持 Vidu 国际站、Google/Apple 官方登录页面，以及与原订单匹配的 Stripe 结账地址。尚未支持的验证域名会暂停面板；没有 CAPTCHA 绕过、设备密钥转发或完整桌面控制。
- 画面按打开时的面板宽度适配，支持点击、滚动、文本、中文输入法及有限编辑快捷键。窄侧栏可继续放大后用边缘滚动条移动视野。视力辅助和浏览器自动填充体验不同于原生 DOM，需要在目标宿主单独验收。
- `official_browser_panel_available:true` 表示提供浏览器面板。`embedded_payment_available:false` 仍表示没有 Vidu 授权的原生内嵌收银组件；面板展示的是实际官方浏览器。
- `real_payment_verification_available:false` 保持诚实；本轮只验收到真实未支付结账页。自动注册、年付、单次充值、支付后可信订单与权益核验及公开市场发布仍未完成。

重新构建 MCP App：`cd ui`，`npm ci`，`npm run build`。服务包已含构建产物，运行不需要 Node。第三方许可见 `ui/THIRD_PARTY_NOTICES.txt`。
