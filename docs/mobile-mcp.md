# 手机端远程 MCP 与 GitHub 发布

适用版本：映伴自建服务 0.8.2。面向已经安装在手机上、支持远程 MCP 的自研智能体 App。

GitHub 可以承载源码、文档和 Release 下载，但不能把这个 Python MCP 服务当作常驻应用运行。GitHub Pages 只适合静态介绍页。手机不安装 Codex 的本机 stdio 包，也不连接开发电脑的 `127.0.0.1`。

本版本代码已包含下面的接入能力；真实手机 App 集成、公网部署、身份提供方和手机实机验收仍由运营者完成。仓库发布与具体 App 插件市场收录是独立步骤，没有承诺已被市场审核或搜索索引收录。

## 用户看到的流程

1. 在你的 App 插件目录里找到「映伴 · Vidu 视频」并添加。App 从你部署的服务下载安装描述，核对 `installable` 后连接远程 MCP。
2. 尚未连接 Vidu 时，打开 App 内的登录面板。用户操作官网，并点击「我已登录，授权并连接」。账号、验证码和密码不进入聊天。
3. 回到对话后查真实积分和原视频需求。展示模型、时长、比例、分辨率、数量及积分消耗，用户确认后只提交一次。
4. 查询同一 `workflow_id`。切到后台可暂停前台轮询，回来继续查询；断网、登录成功和付款都不能导致重新提交。
5. 成功后用任务返回的真实视频链接展示播放器。失败展示原任务原因。`submission_unknown` 提示核查原任务，不创建替代任务。

积分不足时只展示服务实际返回的套餐。当前官网浏览器支付仍不能自动核验到账；不把余额增加或关闭面板称为付款成功。

## 运营者先部署服务

准备一个可以运行 Python 3.12、独立 Chromium/Edge 浏览器和原生凭据库的常驻环境。Windows 使用凭据管理器；Linux 需要已解锁、可用的 Secret Service。此版本没有自动创建浏览器的集群管理器，必须先为允许使用的每个 App 用户配置独立浏览器、端口和账号目录。

App 使用自己的用户身份系统签发/取得访问令牌。公开 MCP 服务验证签名、issuer、audience、有效期和用户 `sub`；不要把共享服务令牌写入插件下载文件或手机安装包。App 的 OAuth 客户端采用授权码与 PKCE，并在现有身份服务登记手机回调；本项目不替你建立身份服务或登记 App。

下面都是部署示例地址，须替换为实际值，不能当作已上线的链接：

```powershell
$env:VIDU_JWKS_URI = 'https://login.example.com/.well-known/jwks.json'
$env:VIDU_TOKEN_ISSUER = 'https://login.example.com'
$env:VIDU_TOKEN_AUDIENCE = 'https://video.example.com/mcp'
uv sync --locked
uv run vidu-selfhost --host 127.0.0.1 --port 8787 `
  --public-url https://video.example.com `
  --browser-bindings deployment/browser-bindings.json `
  --account-vault-root deployment/account-vaults `
  --panel-origin https://app.example.com `
  --enable-live-generation
```

前置 HTTPS 反向代理把该独立域名转给回环服务，保留 Host，不记录 Authorization、面板票据和用户输入。浏览器调试端口仅在服务器本机监听。不要使用 `selfhost_local.py` 的单用户令牌作为公网手机服务。`--enable-live-generation` 只开启提交能力，每次生成仍需费用确认。

`VIDU_TOKEN_AUDIENCE` 必须等于完整 MCP URL。身份服务还必须提供 OAuth/OIDC 发现和手机客户端所需授权流程；只提供 JWKS 不能完成初次登录。若 App 已有适用于该 MCP audience 的用户令牌，可通过现有 MCP 客户端传入。

`--panel-origin` 填实际承载面板的 HTTPS origin；MCP Apps 沙箱可能有单独域名。原生 WebView 可直接加载面板 URL，不需要 iframe；H5 iframe 必须配置精确 origin。没有把 `null` 或通配域名加入允许列表。

## 安装描述与授权发现

服务提供三个入口：

| 路径 | 用途 |
| --- | --- |
| `/.well-known/yingban-plugin.json` | 下载 `yingban-vidu.remote.json`，包括名称、版本、MCP 地址和面板接法 |
| `/.well-known/oauth-protected-resource/mcp` | 标准受保护资源元数据，声明 MCP resource 和身份提供方 |
| `/mcp` | Streamable HTTP MCP，未授权响应提供 `WWW-Authenticate` 发现信息 |

`yingban.remote-plugin.v1` 是给自研 App 的映伴导入格式，**不是所有 MCP 客户端通用的插件安装标准**。App 可以直接添加 `/mcp`，或接入安装描述读取器。服务没有公开 HTTPS、用户认证、独立浏览器账号存储或生成能力时，描述返回 `installable:false` 及 `missing_configuration`；App 应显示“服务尚未部署完成”，不进入视频生成。

安装描述的 `installable:true` 仅表示服务配置齐全，不证明当前手机用户已分配浏览器、账号已授权、网络可达或积分充足；安装后仍需查询能力和账号。

```javascript
import {validateInstallation, routeToolResult, watchWorkflow} from './sdk/yingban-mobile.mjs';

// serviceOrigin 来自运营者发布并经用户确认的 HTTPS 地址。
const manifest = await fetch(serviceOrigin + '/.well-known/yingban-plugin.json').then(r => r.json());
const connection = validateInstallation(manifest, serviceOrigin);
// 交给 App 已有 MCP 客户端，用自己的 OAuth 会话连接 connection.serverUrl。
```

## 手机 App 的面板接法

有 MCP Apps 支持时，正常渲染 `ui://yingban/official-panel.html`。只有基础远程 MCP 时，App 需处理 `vidu_open_official_panel` 的私有 `_meta.yingbanPanel`：

```javascript
const raw = await mcp.callTool({name:'vidu_open_official_panel', arguments:{purpose:'account'}});
const {model, panel} = routeToolResult(raw, serviceOrigin);
sendToolResultToModel(model); // 不包括 _meta。
if (panel) openInAppWebView(panel.url); // URL 只留在当前用户界面内，不记日志、不持久化。
```

上面的 `mcp`、`sendToolResultToModel` 和 `openInAppWebView` 是 App 自己的集成函数，不是此 SDK 创建的全局对象。宿主必须将 MCP SDK 的结果转换为标准字段 `content`、`structuredContent`、`_meta` 和 `isError` 后传入；不要先把整个结果送给模型再过滤。

iframe 收到 `yingban.panel.connected` 或 `yingban.panel.closed` 时，同时校验消息 origin 和 source；原生 WebView 用户关闭页面后也可直接查询连接状态。状态通知只负责收起界面，真实结果仍通过 `vidu_connection_status`/`vidu_get_workflow` 查询。面板最多 15 分钟，入口 5 分钟内使用一次；过期重新调用面板工具，不重放旧 URL。

触屏面板支持点击、滑动翻页、中文输入法、手机键盘删除/回车。点击官网输入框后点“输入文字”显示键盘，结束时点“收起键盘”。切后台暂停画面读取，恢复前台继续。系统销毁 WebView 后应重新打开面板；不能从手机端恢复已销毁的一次性会话。

App 登录系统可能使用系统授权窗口，Vidu 登录面板则留在 App 内；个别官方验证方式是否可嵌入需要手机实机验证，不能承诺所有登录方式都不跳转。

## 视频任务恢复

按 App 用户保存 `workflow_id`，退出账号时清除该用户的界面缓存。不要保存 Vidu Token、面板票据或把别人的任务标识交给当前用户。后台任务是否继续由 App 自己管理；本 SDK 在前台执行只读查询：

```javascript
const controller = new AbortController();
for await (const flow of watchWorkflow(
  (request, options) => mcp.callTool(request, options),
  savedWorkflowId,
  {signal:controller.signal}
)) {
  renderWorkflow(flow);
}
// App 切后台时 controller.abort()；前台重新用相同 savedWorkflowId 调用。
```

查询失败会退出循环并保留原任务；显示网络错误后，重连只重试查询。只有用户看过当前费用并点击确认的处理器可以调用 `vidu_confirm_generation`。确认请求超时也先查询原工作流。App 客户端、代理和任务队列都不得自动重试提交工具。

## GitHub 分发与上架

使用发布包允许清单中的源码、SDK、说明和图标创建仓库，或把 `yingban-selfhost-0.8.2.zip` 与 `.sha256` 放进 GitHub Release。不要上传 `.local`、浏览器配置、系统凭据、账号映射、数据库、日志或个人验收截图。仓库可添加 `mcp`、`vidu`、`video-generation` 等主题，是否能被检索及何时被索引取决于 GitHub。

等真实服务上线，再把实际安装描述下载地址和 `/mcp` 端点加入仓库首页及你的 App 插件目录。当前没有提供真实域名，因此没有生成冒充可用的远程安装文件。第三方市场需要按各自清单和审核方式另行提交；公开 GitHub 仓库不等于市场已上架。

参考：[MCP HTTP 授权](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)、[MCP Apps 授权](https://apps.extensions.modelcontextprotocol.io/api/documents/authorization.html)。
