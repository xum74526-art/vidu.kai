"""Persist conversational configuration before touching accounts, billing or generation."""

import hashlib
import json
from uuid import uuid4

from vidu_purchase.brief_models import (
    VIDEO_FIELDS,
    BriefAnswers,
    BriefInput,
    VideoBrief,
    VideoPreferences,
)
from vidu_purchase.models import DomainError
from vidu_purchase.video_models import MODEL_DURATION_RANGES, MODEL_RESOLUTIONS, VideoConfig
from vidu_purchase.workflow import WorkflowService

LABELS = {
    "model_version": "模型",
    "duration": "时长",
    "aspect_ratio": "画面比例",
    "resolution": "分辨率",
}
MODEL_ORDER = ("3.2", "3.2_a", "3.1", "3.0")

CONVERSATION_INSTRUCTIONS = """用户通过对话提出视频需求时，先调用 vidu_prepare_video_request。
只传用户明确说过的参数；未提到的字段留空，不直接使用 vidu_start_video_request 的默认值。
把返回的缺失参数合在一次自然提问中，不重复问已选参数。先确认模型、时长、比例、分辨率。
用户说“不知道”“你来选”时，仅对其委托的字段填写 use_recommended_for；“其余帮我选”可覆盖
所有尚未明确的字段。用户已说明短视频/竖屏用途时可标记 vertical_short，一般用途标记 general。
缺少视频内容时先问内容，不编造提示词。不要把“生成一段视频”误解为授权任何默认设置。
用 vidu_answer_video_questions 补充同一 brief_id，保留明确的选择。冲突时询问调整，不静默覆盖。
配置齐全会返回 workflow_id 和实际工作流，之后使用该工作流；不得再创建重复视频请求。
向用户说明采用的模型、时长、比例、分辨率及推荐原因。推荐不代表费用最低，费用以实时查询为准。
“帮我选参数”不等于“购买套餐”或“确认生成”。付款后只通知可使用并询问是否继续，等待新确认。
独立网页只是授权、支付确认与结果展示的辅助入口；宿主应在对话内展示参数、套餐和任务进度。
"""


def resolve(brief: VideoBrief) -> dict:
    explicit = brief.preferences.model_dump(exclude_none=True)
    candidates = []
    for model in MODEL_ORDER:
        low, high = MODEL_DURATION_RANGES[model]
        if explicit.get("model_version", model) != model:
            continue
        if not low <= explicit.get("duration", min(8, high)) <= high:
            continue
        if explicit.get("resolution", "1080p") not in MODEL_RESOLUTIONS[model]:
            continue
        candidates.append(model)

    missing = [field for field in VIDEO_FIELDS if field not in explicit]
    unanswered = [field for field in missing if field not in brief.use_recommended_for]
    conflicts = []
    suggested = {}
    reasons = {}
    if candidates:
        model = candidates[0]
        low, high = MODEL_DURATION_RANGES[model]
        ratio = {"vertical_short": "9:16", "square": "1:1"}.get(brief.intended_use, "16:9")
        suggested = {
            "model_version": model,
            "duration": max(low, min(8, high)),
            "aspect_ratio": ratio,
            "resolution": "1080p",
            **explicit,
        }
        reasons = {
            "model_version": "从当前适配的模型中选取与已选时长、分辨率兼容的通用选项。",
            "duration": "以 8 秒作为短片起点；模型不支持时使用其支持的时长。",
            "aspect_ratio": {
                "vertical_short": "用户说明用于竖屏短视频。",
                "square": "用户说明需要方形画面。",
                "landscape": "用户说明用于横屏展示。",
                "general": "用途未限定，采用通用横屏比例。",
            }[brief.intended_use],
            "resolution": "采用当前模型支持的 1080p；不代表最低积分消耗。",
        }
    else:
        conflicts = [
            {
                "code": "incompatible_video_options",
                "fields": [
                    field
                    for field in ("model_version", "duration", "resolution")
                    if field in explicit
                ],
                "message": "已选模型、时长和分辨率没有兼容组合，请调整其中一项；已选值已保留。",
            }
        ]

    resolved = {
        **{
            field: suggested[field]
            for field in missing
            if field in brief.use_recommended_for and field in suggested
        },
        **explicit,
    }
    questions = []
    for field in unanswered:
        choices = {
            "model_version": candidates or list(MODEL_ORDER),
            "aspect_ratio": ["16:9", "9:16", "1:1", "4:3", "3:4"],
            "resolution": list(
                dict.fromkeys(res for model in candidates for res in MODEL_RESOLUTIONS[model])
            ),
            "duration": sorted(
                {
                    second
                    for model in candidates
                    for second in range(
                        MODEL_DURATION_RANGES[model][0], MODEL_DURATION_RANGES[model][1] + 1
                    )
                }
            ),
        }[field]
        questions.append(
            {
                "field": field,
                "label": LABELS[field],
                "choices": choices,
                "recommended": suggested.get(field),
                "can_choose_for_me": True,
            }
        )

    config = (
        VideoConfig(prompt=brief.initial_input.prompt, **resolved)
        if not unanswered and not conflicts
        else None
    )
    labels = "、".join(LABELS[field] for field in unanswered)
    return {
        "config": brief.config or config,
        "resolved_parameters": resolved,
        "questions": questions,
        "conflicts": conflicts,
        "question": (
            conflicts[0]["message"]
            if conflicts
            else f"还需要确认{labels}。你有偏好吗？不熟悉的参数可以回复‘其余帮我选’。"
            if unanswered
            else None
        ),
        "recommended_fields": {
            field: {"value": resolved[field], "reason": reasons[field]}
            for field in missing
            if field in resolved
        },
        "supported_models": {
            model: {
                "duration_seconds": list(MODEL_DURATION_RANGES[model]),
                "resolutions": list(MODEL_RESOLUTIONS[model]),
            }
            for model in MODEL_ORDER
        },
    }


class BriefService:
    def __init__(self, workflow: WorkflowService):
        self.workflow = workflow
        self.store = workflow.purchases.store
        with self.store.connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS video_briefs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(owner, idempotency_key)
                )
            """)

    def load(self, owner, brief_id):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT payload FROM video_briefs WHERE id=? AND owner=?", (brief_id, owner)
            ).fetchone()
        if not row:
            raise DomainError("brief_not_found", "未找到当前用户的视频参数对话。")
        return VideoBrief.model_validate_json(row[0])

    def save(self, brief, **changes):
        updated = VideoBrief.model_validate(
            {
                **brief.model_dump(),
                **changes,
                "revision": brief.revision + 1,
            }
        )
        with self.store.connection() as db:
            cursor = db.execute(
                "UPDATE video_briefs SET payload=?, revision=? "
                "WHERE id=? AND owner=? AND revision=?",
                (
                    updated.model_dump_json(),
                    updated.revision,
                    brief.brief_id,
                    brief.owner,
                    brief.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise DomainError("brief_changed", "参数已更新，请读取最新对话再继续。")
        return updated

    async def start(self, owner: str, request: BriefInput, key: str) -> dict:
        if not 8 <= len(key) <= 128:
            raise DomainError("invalid_idempotency_key", "同一次需求使用固定的 8–128 位标识。")
        brief = VideoBrief(
            brief_id=uuid4().hex,
            owner=owner,
            idempotency_key=key,
            initial_input=request,
            preferences=request.preferences,
            use_recommended_for=request.use_recommended_for,
            intended_use=request.intended_use,
        )
        with self.store.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO video_briefs VALUES (?, ?, ?, ?, ?)",
                (
                    brief.brief_id,
                    owner,
                    key,
                    brief.revision,
                    brief.model_dump_json(),
                ),
            )
            row = db.execute(
                "SELECT payload FROM video_briefs WHERE owner=? AND idempotency_key=?", (owner, key)
            ).fetchone()
        brief = VideoBrief.model_validate_json(row[0])
        if brief.initial_input != request:
            raise DomainError("idempotency_conflict", "需求标识已使用，请回答原对话或创建新需求。")
        return await self.finish(brief)

    async def answer(self, owner: str, brief_id: str, answers: BriefAnswers) -> dict:
        brief = self.load(owner, brief_id)
        canonical = json.dumps(answers.model_dump(exclude_unset=True), sort_keys=True)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        if brief.last_answer_revision == answers.revision and brief.last_answer_digest == digest:
            return await self.finish(brief)
        if brief.config or brief.workflow_id:
            raise DomainError("brief_finalized", "参数已交给视频工作流，请使用更新视频配置工具。")
        if brief.revision != answers.revision:
            raise DomainError("brief_changed", "参数已更新，请读取最新对话再回答。")
        patch = answers.preferences.model_dump(exclude_unset=True)
        delegated = (set(brief.use_recommended_for) - patch.keys()) | set(
            answers.use_recommended_for
        )
        brief = self.save(
            brief,
            preferences=VideoPreferences.model_validate(
                {**brief.preferences.model_dump(), **patch}
            ),
            use_recommended_for=[field for field in VIDEO_FIELDS if field in delegated],
            intended_use=answers.intended_use or brief.intended_use,
            last_answer_revision=answers.revision,
            last_answer_digest=digest,
        )
        return await self.finish(brief)

    async def get(self, owner: str, brief_id: str) -> dict:
        # Reading recovers a pending, already-approved configuration handoff. This
        # only checks account/estimate and cannot buy or submit a task.
        return await self.finish(self.load(owner, brief_id))

    async def finish(self, brief: VideoBrief) -> dict:
        resolution = resolve(brief)
        config = resolution.pop("config")
        workflow = None
        if config:
            if not brief.config:
                brief = self.save(brief, config=config)
            if not brief.workflow_id:
                workflow = await self.workflow.start(
                    brief.owner,
                    config,
                    "brief-" + brief.brief_id,
                )
                brief = self.save(brief, workflow_id=workflow["workflow_id"])
            else:
                workflow = await self.workflow.get(brief.owner, brief.workflow_id)
            # After a user edits the workflow, restoring the conversation must show
            # the current configuration, not the original briefing recommendation.
            config = VideoConfig.model_validate(workflow["config"])
            resolution["resolved_parameters"] = {
                field: getattr(config, field) for field in VIDEO_FIELDS
            }
            resolution["recommended_fields"] = {
                field: value
                for field, value in resolution["recommended_fields"].items()
                if value["value"] == getattr(config, field)
            }
        return {
            "brief_id": brief.brief_id,
            "revision": brief.revision,
            "state": "workflow_created" if workflow else "needs_clarification",
            "prompt": config.prompt if config else brief.initial_input.prompt,
            "preferences": brief.preferences.model_dump(),
            "intended_use": brief.intended_use,
            "use_recommended_for": brief.use_recommended_for,
            **resolution,
            "config": config.model_dump() if config else None,
            "workflow_id": brief.workflow_id,
            "workflow": workflow,
            "next_action": workflow["next_action"] if workflow else "ask_video_parameters",
            "auto_generate": False,
        }


def register_brief_tools(mcp, briefs: BriefService, invoke):
    annotations = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}

    @mcp.tool(annotations={**annotations, "idempotentHint": True})
    async def vidu_prepare_video_request(request: BriefInput, idempotency_key: str) -> dict:
        """对话生成视频的首选入口：保存明确参数，一次询问缺失项。不充值、不生成。

        use_recommended_for 只包含用户明确委托插件选择的字段。参数齐全后自动关联一个
        视频请求，返回 workflow_id；后续用该 ID 查询账号、费用和购买，不再新建需求。
        """
        return await invoke(
            lambda owner: briefs.start(owner, request, idempotency_key), ("vidu:video",)
        )

    @mcp.tool(annotations={**annotations, "idempotentHint": True})
    async def vidu_answer_video_questions(brief_id: str, answers: BriefAnswers) -> dict:
        """回答同一参数对话。传最新 revision，仅补充用户回答，明确‘不知道’才选推荐。

        preferences 中省略的字段保留，null 清除该选择并重新询问；相同回答重试可复用。
        返回冲突时询问用户调整，不能悄悄改变已选模型、时长、比例或分辨率。
        """
        return await invoke(lambda owner: briefs.answer(owner, brief_id, answers), ("vidu:video",))

    @mcp.tool(annotations=annotations)
    async def vidu_get_video_brief(brief_id: str) -> dict:
        """恢复参数对话和最新 revision；已完成时返回原视频工作流，不购买、不提交生成。"""
        return await invoke(lambda owner: briefs.get(owner, brief_id), ("vidu:video",))
