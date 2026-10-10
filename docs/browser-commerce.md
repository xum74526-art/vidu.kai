# 官网浏览器购买接入 · 0.8.0

0.8.0 可通过[对话内官方面板](official-panel.md)操作每个用户的独立浏览器。注册、登录和同一订单的官方支付确认不必跳出智能体；服务仍不代替用户付款，到账自动核验仍未完成。

这是无需 Vidu 合作购买 API 的接入方式：运营方为每名已鉴权用户提供独立浏览器，用户在这个浏览器中登录自己的 Vidu 官网账号。服务读取可见套餐，按用户选择打开官方 Stripe 支付页。智能体通过 HTTP/MCP 组织对话，最终支付由用户操作。

**目前可准备标准版、专业版月付的官方支付页；还不能完成支付成功及权益到账的自动核验。** 没有执行真实付款，也没有把测试响应、回跳页面或用户口头确认当作到账证据。`real_payment_verification_available` 保持 `false`。这是一版可联调的真实浏览器适配器，尚未达到公开运营的完整购买验收条件。

## 本机连接

```powershell
uv sync --locked
uv run python -X utf8 scripts/browser_session.py
# 在打开的专用 Edge/Chrome 中登录 Vidu，然后启动服务：
uv run python -X utf8 scripts/selfhost_local.py serve
```

启动器仅使用已安装的 Edge/Chrome，使用项目内 `.local/commerce-browser` 专用配置目录，调试端口只监听回环地址。登录态保存在这个专用浏览器中，不提取 Cookie 或 Token。它创建 `.local/browser-bindings.json`，服务启动脚本会自动读取。浏览器关闭后再次运行启动器；服务保留报价和购买记录，重复同一购买请求不会再次下单。

Codex 右侧浏览器中的登录仅用于页面联调，**不会自动复制到独立运行服务的浏览器**。普通浏览器或手机里登录，也不能替代服务绑定会话。不要将自己的日常浏览器配置目录用于此服务。

0.6.1 的连接检查和购买状态查询只读取已打开的 Vidu 页面，不创建标签页、不刷新，也不强制回到会员页。官网跳到登录或首页后继续复用原窗口；用户主动请求套餐或订单时，才在确认账号后打开会员页并再次核对账号。若登录尚在进行，本机 `check` 跳过套餐读取。旧版本遗留多个 Vidu 标签页时，优先复用已明确登录的页面；若可见账号不一致则返回 `ambiguous_page`，请保留要使用的账号页面。服务不会替用户关闭登录或支付页面。

宽屏官网会将折扣文字加入包月按钮的完整名称，因此适配器定位按钮内的固定标签，不依赖完整名称。订单观察会等待空白占位行加载完成，只有明确显示“暂无数据”才返回空列表；读取后清理本次打开的订单弹窗。官网已实测支持用 Escape 关闭订单视图，可避开固定宽度弹窗把关闭按钮挤出窗口的情况。再次请求套餐时可关闭只读订单视图，不会关闭订阅确认或支付页面。

如果端口冲突，用 `browser_session.py --port 9223`；已有映射不同会拒绝覆盖，应由运营者核对并修改配置。公共服务不运行此本机启动器，而是预先给每个用户提供隔离的浏览器和受鉴权的远程交互窗口。

## 自建智能体调用顺序

1. `vidu_purchase_capabilities`：确认购买方式和实际核验能力。
2. `vidu_commerce_session` / `GET /v1/commerce/session`：检查本用户的专用浏览器。`connected` 表示读取到了官网头像中的账号标识；它不表示视频 Token 已连接。
3. 未登录时，把用户交给绑定的登录窗口。远程部署使用返回的 `handoff_url`；没有此链接时，必须由运营者展示相同浏览器。登录、验证码和注册条款由用户完成，工具不接收这些输入。
4. `vidu_list_plans` / `GET /v1/plans`：实时读取月付套餐，展示账号、首期价、续费价、自动续费、月积分和报价期限。`checkout_available=false` 的套餐可比较，但不能选择下单。
5. 用户明确选择后调用 `vidu_prepare_purchase` / `POST /v1/purchases`。固定 `Idempotency-Key`，重试必须沿用它。服务重新检查账号、套餐、实付金额和 Stripe 商品标题。
6. 只有 `payment_action` 存在时，使用原 `purchase_id` 调用 `vidu_open_official_panel(purpose="payment")` 或 `POST /v1/browser-panels`，展示内嵌的官方浏览器。`embedded:false` 表示原上游 URL 不是独立的嵌入支付组件。用户在面板里确认支付；模型没有填写卡号或执行付款的工具。
7. 用户返回后查询同一 `purchase_id`，或使用原有 SSE 订阅。当前未核验支付时保持等待或 `verification_required`，不得让用户通过一句“已付款”手动标记成功。
8. `vidu_website_orders` / `GET /v1/commerce/orders` 可读取官网“我的订单”表。这是观察数据；返回 `payment_verified:false`，不将订单行推断为已支付、已到账。

已保存的视频需求保持原 `workflow_id`。买完后不创建第二个视频需求，不自动提交任务。只有可信供应方返回 `ready`，再按当前视频状态询问“是否继续生成视频？”，并等待新的生成确认。浏览器模式尚未能返回这种已核验状态。

JavaScript SDK 新增 `commerceSession()`、`websiteOrders()`；独立购买可直接调用 `listPlans()`、`preparePurchase(quoteId, key)`、`checkPurchase(purchaseId)`，不要求先填写视频表单。网页控制台只是这些接口的可选入口。

## 服务端绑定

配置文件按鉴权后的 JWT `sub` 隔离，不接受客户端输入 owner、CDP 地址或 Cookie：

```json
{
  "your-user-subject": {
    "cdp_url": "https://your-private-browser-gateway.example/session/user-unique",
    "handoff_url": "https://your-agent.example/browser/user-unique",
    "expected_account": "vidu_EXAMPLE1234"
  }
}
```

这是配置结构示例，域名和账号不是已部署服务。`expected_account` 是用户在头像处可见的账号标识，可省略；设置后会拒绝其他账号。无论是否设置，每份报价都会绑定读取时的账号及浏览器连接配置，切换账号或端点后不能用旧报价支付。端点不得被两个用户共享；提供方还必须保证不同地址背后不会实际连接到同一个浏览器。

```powershell
uv run vidu-selfhost --browser-bindings .local/browser-bindings.json --public-url https://your-agent.example
```

公网部署另需配置 JWT、HTTPS 和正确的监听地址，详见 [HTTP/MCP 接入](selfhost.md)。浏览器模式与 `--billing-config` 网关模式互斥。浏览器服务使用单进程单 worker；跨进程协调浏览器租约尚未实现。CDP 是控制浏览器的特权入口，必须留在私有网络或鉴权后的 TLS 网关内；`handoff_url` 也必须校验同一用户，不能公开浏览器会话。

视频授权继续使用官方 CLI 对应的 Token。0.7.0 本机国际站新增官网快捷连接：用户点击授权后读取官网可见 Token 面板，验证并保存，记录该浏览器账号与视频授权的关联。后续核对一致时 `video_account_link_verified:true`；仅登录、手动填写 Token 或公网管理员映射不会自动取得该标记。官网切换到其他账号后停止相关操作。完整可用性还需用 `vidu_connection_status` 检查视频授权是否有效，不能把官网登录直接呈现为全部授权完成。

## 本次真实验证与限制

2026-10-09，使用用户登录的国际站账号，通过官网可见界面验证了：

- 登录后会员页面、月付切换、标准版/专业版/当前旗舰档位价格与续费行。
- 标准版此前已验证官方 Stripe 标题和金额；本次新增专业版 `Vidu Premium` 的未付款结账页，首期和续费信息与官网一致。运行时价格实时读取，不使用这些历史数值作为价目表。
- “我的订单”表列为订阅、购买时间、生效时间、到期时间、订单编号、支付方式、金额；当前账号显示暂无数据。
- 官网确实存在积分包入口，但单次充值、年付和旗舰版支付页还未完成适配，不能承诺已支持。

0.6.1 还通过独立浏览器与实际运行服务验证：HTTP 和 MCP 均识别账号已连接，`GET /v1/plans` 返回 200，控制台展示三档实际月付报价，官网订单观察返回 0 条记录。没有用测试夹具替代上述上游响应。服务检查脚本会保存套餐错误并以失败状态退出，不把已登录但无法读套餐算作检查成功。

未完成：独立服务完整下单和支付验收、真实支付后的订单状态/权益/支付会话对应关系、远程用户自助开户授权和公开市场上架。当前页面结构改变、账号变化或结账结果不明都会停止相关操作；没有模拟回退，也没有客户端“标记已支付”接口。
