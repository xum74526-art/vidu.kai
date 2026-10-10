# 映伴对话接入（0.8.0）

注册、登录及官方付款使用 `vidu_open_official_panel` 在对话内展示可交互官网。宿主需要支持 MCP Apps，或使用 HTTP 返回的私密面板入口；接法见[官方面板](official-panel.md)。旧的 `vidu_connect_account` 保留给基础 MCP 宿主。

用户在你的智能体中提出视频需求，智能体通过远程 MCP 调用映伴；自己的对话界面承载参数提问、套餐比较、支付进度和视频结果。独立网页是可选辅助入口，不必让用户另开创作后台填写同一份需求。

**当前参数对话、工作流和支付状态接口已实现；新增官网浏览器购买方式，可读取真实套餐并准备标准版、专业版月付支付页，到账核验尚未完成。** 详见[浏览器购买接入](browser-commerce.md)。先查 `vidu_commerce_session`，展示返回的账号标识，不能把浏览器登录当作视频 Token 授权。`checkout_available=false` 的套餐不能下单；订单观察的 `payment_verified=false` 不能用于宣布购买成功。未配置任何计费方式时返回 `billing_not_configured`，不填充示例套餐。公网自助授权和自动注册仍未实现。

## 参数对话

首次连接可调用 `vidu_connect_account`，把 `connection_action.url` 呈现为“连接 Vidu”按钮。用户在本机连接窗口登录并确认授权，不通过聊天传递 Token。使用 `vidu_connection_status` 查询；`account_link_verified=true` 才表示官网与有效的视频授权已关联。已连接时复用现有授权。当前快捷连接针对本机国际站专用浏览器，公网没有自助注册或 OAuth 回调入口。

首选 `vidu_prepare_video_request`，参数结构如下。这里仅为调用说明，不会下单或生成。

```json
{
  "request": {
    "prompt": "海边日落，镜头慢慢向前",
    "preferences": {"aspect_ratio": "9:16"},
    "intended_use": "vertical_short"
  },
  "idempotency_key": "conversation-request-001"
}
```

服务返回 `brief_id`、`revision`、`state: needs_clarification`、`questions` 和一次性合并提问 `question`。只询问缺少的参数，保留已经说过的竖屏比例；这个阶段不访问上游、不购买，也不提交视频。

用户可以分别选择，也可以回复“我不知道，其余帮我选”。智能体随后调用 `vidu_answer_video_questions`：

```json
{
  "brief_id": "使用上一调用返回的ID",
  "answers": {
    "revision": 0,
    "use_recommended_for": ["model_version", "duration", "resolution"]
  }
}
```

`revision` 必须使用实际最新值；上面的 0 仅说明首次回答结构。用户明确选择的值放入 `answers.preferences`。该对象省略的字段保持原样；`null` 表示清除某项选择、重新询问。`use_recommended_for` 只列本次明确委托的字段，不把“用户没提到”当成授权推荐。

默认推荐以当前适配能力为边界：一般请求从 3.2、8 秒、1080p 开始；用户要求竖屏短视频时采用 9:16，方形用途采用 1:1，通用或横屏展示采用 16:9。用户已选择 3.0 时，推荐时长改为其支持的 5 秒；用户明确要求 720p 且委托选模型时，选择支持这一分辨率的 3.2_a。推荐会在返回的 `recommended_fields` 中列明原因，不能称为“最便宜”或保证效果最好；实际消耗随后查询官网。

当前模型能力取自[官方 CLI 的固定版本](https://github.com/shengshu-ai/vidu-cli/tree/3b344ab69786d4ecb893024923404a7e710562b1)，具体组合由服务和 OpenAPI 校验；不是实时发现整个 Vidu 产品的全部模型。超出范围的模型、时长或图生视频需求应先说明范围、询问调整，不自动换模型或拆成多个收费任务。

四项全部明确且组合兼容后，返回 `state: workflow_created`、完整 `config`、`workflow_id` 和 `workflow`。从这里起复用该工作流，不再调用另一个创建工具。服务只检查账号与预计费用，推荐参数不构成购买或生成授权。

若模型、时长、分辨率不兼容，返回 `conflicts`，保留原选择并要求调整；不会静默降分辨率或改时长。重启后通过 `vidu_get_video_brief` 恢复；相同初始请求和相同回答的重试复用已有记录。两个页面同时回答时，过期 `revision` 返回 `brief_changed`，需读取最新记录。对话已交给工作流后，用户修改参数使用 `vidu_update_video_request`，旧费用确认随之失效。

直接使用 `vidu_start_video_request` 或 HTTP `POST /v1/videos` 时，四项参数全部必填。这适用于用户已经逐项填写的表单或明确配置的高级调用；不能拿来绕过提问。桌面 0.3.0 插件保持独立，没有随本次源码包安装或更新。

## 宿主展示与购买

| 返回内容或状态 | 对话界面的行为 |
| --- | --- |
| `needs_clarification` | 将 `question` 展示一次；可把 `questions` 转成选项，包含“帮我选” |
| `conflicts` | 解释哪几项不兼容，让用户调整；保留其他值 |
| `account_required` | 提示尚未连接；已有账号登录、没有账号注册，不断言用户没有账号 |
| `needs_purchase` | 用户愿意购买时调用 `vidu_recommend_plans`，展示真实 `quotes` |
| 套餐卡片 | 显示币种、首期金额、续费金额、积分、自动续费及报价有效期；金额单位为分 |
| 用户选套餐 | 用实际选择的 `quote_id` 调用 `vidu_purchase_for_video`，复用原 `workflow_id` |
| `payment_action` | 在对话中显示官方付款按钮和 `purchase.plan` 核对信息，由用户确认支付 |
| `awaiting_payment` / `verifying_payment` | 查询同一工作流；不能再创建订单或用回跳参数宣告付款成功 |
| `awaiting_entitlement` | 提示订单付款已核验、权益仍在等待；不邀请生成 |
| `awaiting_generation_confirmation` | 显示当前配置与实际积分费用，支付后提示“可以使用了”，询问是否继续 |
| 用户新确认 | 用最新 `confirmation_id` 调用 `vidu_confirm_generation` |
| 用户暂不继续 | 调用 `vidu_cancel_video_request`；不撤销套餐或订阅 |
| 生成中 / 完成 | 查询同一 `workflow_id`，展示返回的真实视频结果 |

原付款动作继续返回 `type: open_official_checkout`、`embedded: false`。0.8.0 可将原 `purchase_id` 传给 `vidu_open_official_panel(purpose="payment")`，在对话面板里直接操作该订单的官方浏览器。它不依赖直接嵌入 Stripe URL；面板关闭也不代表支付成功。缺少可核验订单与权益证据时保持等待或明确显示待核验。

用户付款之后，服务必须核验原订单以及权益，再提示可用；支付回调或 SSE 处理器只能更新状态，不能调用 `confirmGeneration`。后续生成必须由新的用户确认事件触发。

## HTTP 与客户端

MCP 服务端包含上述对话规则；宿主需要把服务器 instructions 和各工具描述提供给执行模型。不同智能体的安装格式不同，目前提供 Streamable HTTP MCP 地址与 Bearer 鉴权，并非已发布到所有市场或实现通用 OAuth 一键安装。

HTTP 对应为 `POST /v1/video-briefs`、`PATCH /v1/video-briefs/{brief_id}`、`GET /v1/video-briefs/{brief_id}`。创建请求的 `Idempotency-Key` 放在请求头，JSON 分别对应 MCP 的 `request`、`answers`。用户身份始终来自已验证的访问令牌，不接受客户端指定 owner。

JavaScript 客户端提供 `prepareVideoRequest`、`answerVideoQuestions`、`getVideoBrief`，以及既有套餐、付款状态和视频工具。连接配置见[自建服务文档](selfhost.md)；真实计费上游约定见[网关接入协议](commerce-gateway.md)。
