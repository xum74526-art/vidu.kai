# vidu.kai · 映伴 Vidu 视频

开源的 Vidu 视频工作流服务，为自研智能体提供远程 MCP、官网账号连接、积分费用确认、单次提交和生成结果查询。独立开发，非 Vidu 官方插件。

**当前版本：服务 0.9.0 / Codex 本机插件 0.4.0。**

[下载发布包](https://github.com/xum74526-art/vidu.kai/releases) · [手机 App 接入](docs/mobile-mcp.md) · [HTTP/MCP 服务](docs/selfhost.md) · [登录面板](docs/official-panel.md) · [本机验收](docs/local-acceptance-0.4.0.md)

## 手机智能体如何使用

手机 App 通过远程 MCP 连接运营者部署的 HTTPS 服务。GitHub 提供源码与下载；当前仓库没有托管中的公网生成服务，也没有可直接使用的远程服务地址。

部署后，App 可以下载 `/.well-known/yingban-plugin.json` 添加插件，或直接连接 `/mcp`。下载描述是映伴自研 App 的导入格式，不是所有 MCP 客户端通用的安装格式。服务未配置完成时会返回 `installable:false`。

用户流程：添加插件 → 在 App 内登录 Vidu 并授权 → 提出视频需求 → 确认真实积分费用 → 提交一次 → 查询并播放结果。

## 能力

- **远程 MCP 与 HTTP**：Streamable HTTP、用户 JWT 验证和标准授权资源发现。
- **官方登录面板**：每用户独立浏览器，用户自己登录与授权；支持 MCP Apps 或 App 内 WebView。
- **自动准备会话**：远程部署开启浏览器池后，新用户无需手工分配端口；保留各自登录状态，限制活跃浏览器数量并回收闲置进程。
- **完整本机插件**：仓库包含运行时、锁定依赖、技能和图标；首次连接自动启动登录助手，无需先开开发服务器。
- **手机操作**：触屏点击、滑动翻页、软件键盘、中文输入、删除和回车。
- **费用确认**：查询真实积分和预计消耗，用户确认当前配置后才提交。
- **任务恢复**：App 切后台、断网后继续查询原 `workflow_id`，不会自动重试生成。
- **隔离与保密**：账号凭据保存在系统凭据库；面板授权只交给当前用户界面，不进入模型上下文。

官网浏览器方式可准备部分真实月付套餐的支付页，但**支付成功和权益到账自动核验尚未完成**。关闭面板、用户说已付款或余额增加都不会自动生成视频。仓库公开不等于第三方插件市场已上架。

## 开发与本机体验

需要 Python 3.12、`uv`、Edge/Chrome 和系统原生凭据库。Windows 使用凭据管理器；Linux 需要可用的 Secret Service。

```sh
git clone https://github.com/xum74526-art/vidu.kai.git
cd vidu.kai
uv sync --locked
uv run python -X utf8 scripts/selfhost_local.py serve
```

按终端输出打开本机面板；新安装会在第一次打开登录面板时自动启动独立浏览器。已有 `browser-bindings.json` 的部署继续复用原浏览器。这个入口只用于本机体验，不能直接分享给手机。公网部署使用独立的用户身份系统、HTTPS、浏览器池和账号存储，详见 [手机部署说明](docs/mobile-mcp.md)。

只有实际用户完成授权后才能查询账号积分。仓库和发布包不含 Vidu Token、账号数据或测试视频；生成需要用户明确确认费用。

## App 集成入口

| 文件或工具 | 用途 |
| --- | --- |
| `vidu_open_official_panel` | 打开账号或原订单官方面板 |
| `vidu_prepare_video_request` | 保存视频描述与已知参数 |
| `vidu_confirm_generation` | 用户确认当前费用后提交 |
| `vidu_get_workflow` | 继续查询同一个视频请求 |
| [sdk/yingban-mobile.mjs](sdk/yingban-mobile.mjs) | 安装校验、面板元数据隔离、前台任务恢复 |
| [sdk/yingban-client.mjs](sdk/yingban-client.mjs) | HTTP 客户端 |
| [docs/openapi.json](docs/openapi.json) | HTTP 接口结构 |

手机 App 必须处理面板界面和原工作流恢复，不能把整个 `_meta` 交给模型。`submission_unknown` 需要核查原任务，禁止创建替代任务。

## 测试与构建

```sh
uv run pytest -q
uv run ruff check src tests scripts
node --test sdk/yingban-client.test.mjs sdk/yingban-mobile.test.mjs sdk/panel-input.test.mjs
uv run python -X utf8 scripts/build_selfhost.py
```

测试中的上游响应和浏览器夹具仅用于隔离验证，不作为真实手机、付款或 Vidu 视频生成的验收证据。真实手机 App 和公网部署仍需联调。

服务构建产物在 `dist/yingban-selfhost-0.9.0.zip`，包含 SHA-256 文件。发布采用允许清单，排除本机数据库、浏览器配置、凭据、日志和测试夹具。

Codex 本机插件源码位于 [plugins/yingban-vidu](plugins/yingban-vidu)，使用 `scripts/build_plugin.py` 构建。它是单用户 stdio 插件，与手机远程 MCP 的部署方式不同。

仓库版已包含本机插件运行时。在支持自定义插件市场的 Codex 中，从克隆的仓库添加并安装：

```sh
codex plugin marketplace add <仓库绝对路径>
codex plugin add yingban-vidu@yingban-local
```

重启已有对话的插件进程后，说“连接我的 Vidu 账号”即可准备官方面板。机器上需要 `uv`、Edge/Chrome 和原生凭据库。**本机 Codex 包提供登录与视频功能；完整套餐购买流程由自建智能体连接远程 `/mcp` 使用。** 添加自定义市场不等于进入 Codex 公共插件目录。

## 开源许可

[MIT](LICENSE)。MCP Apps 资源包含的第三方许可见 [ui/THIRD_PARTY_NOTICES.txt](ui/THIRD_PARTY_NOTICES.txt)。
