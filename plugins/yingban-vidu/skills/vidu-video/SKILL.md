---
name: vidu-video
description: 使用映伴在用户自己的 Vidu 官网账号中配置文生视频、查询积分消耗、提交确认的生成任务及查询结果。适用于调用映伴生成或调整视频的请求。
---

将用户的视频描述与明确选择的配置传给 `vidu_start_video_request`，为同一次请求使用固定 `idempotency_key`。保存返回的 `workflow_id`；更新配置使用 `vidu_update_video_request` 与最新 `revision`。

按返回状态处理：

- `account_required`：通过本插件连接工具引导用户自行连接，不能断言没有账号。
- `blocked`：按真实原因说明额度或权限限制，不编造套餐、余额、订单或视频。
- `awaiting_generation_confirmation`：展示模型、时长、分辨率、比例、数量和真实预计积分；用户明确确认当前配置与费用后，使用最新 `confirmation_id` 调用 `vidu_confirm_generation`。
- `generating`：查询原工作流，不重复提交。`submitting` 或 `submission_unknown` 可能已经扣费，不能自动创建替代任务。
- `completed`：返回真实视频结果；只有任务返回的链接才是有效输出，缺少链接时说明此限制。

用户在插件之外补充额度后，先查询原请求，告诉用户现在可以使用，再询问是否继续，等待用户回答。“继续”可以确认此前展示的配置。额度增加不代表已核验某笔付款，也不触发自动生成。

本插件没有购买套餐、自动注册、支付确认或退款接口。不能用浏览器购买、模拟订单或其他未经配置的集成补足这些能力。
