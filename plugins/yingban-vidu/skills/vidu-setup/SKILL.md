---
name: vidu-setup
description: 设置映伴的 Vidu 账号连接，或处理映伴授权过期、断开和切换账号。用于本插件首次配置，不处理其他插件或 API 平台充值。
---

使用本插件的 `vidu_capabilities` 和 `vidu_account_status` 检查连接。未连接不代表用户没有 Vidu 账号。

需要首次连接时调用 `vidu_connect_account`，用宿主浏览器打开工具返回的 `connection_url`。该地址直接显示官方登录面板，用户自行登录或注册，然后点击「我已登录，授权并连接」。不要打开旧版手动 Token 表单，也不要要求用户复制 Token。密码、验证码与授权操作由用户在面板内完成，智能体不读取 Token 输入框、剪贴板、浏览器存储或系统凭据库。

用户完成后再次调用 `vidu_account_status`，仅按真实响应报告余额和连接状态。之前有 `workflow_id` 时查询该请求以保留参数；连接成功或额度增加不能授权生成。

授权失效时，用户授权重新连接后调用 `vidu_disconnect_account`，再打开官方登录面板。不同账号不能继承旧确认，按已有对话中的配置新建请求即可，不要求用户重新描述。

若出现 `official_panel_not_configured`、`official_panel_unavailable` 或账号存储不一致，明确说明登录面板服务需要配置或启动，不回退到 Token 表单。部署者需让配套 `selfhost_local.py` 服务以 `--account-dir` 指向本插件的账号目录，服务启动后自动登记面板入口；不要让普通用户填写服务令牌。

如果 MCP 没有启动，检查 `uv` 和插件启动错误。本机包使用 Python 3.12 和系统凭据库；Linux 还需要 Secret Service。不要把本机包描述为云端服务或已公开上架的插件。不要运行 demo 替代真实连接。
