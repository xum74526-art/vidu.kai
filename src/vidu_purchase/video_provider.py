"""Website account adapter based on the public shengshu-ai/vidu-cli protocol.

Protocol reference: commit 3b344ab69786d4ecb893024923404a7e710562b1,
src/client.rs and src/commands/{quota,tasks}.rs. No upstream implementation is copied.
Real billable submission requires both deployment opt-in and workflow confirmation.
"""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from vidu_purchase.models import DomainError, Record
from vidu_purchase.video_models import AccountSnapshot, VideoConfig, VideoEstimate, VideoTask


class VideoProvider(Protocol):
    mode: str
    generation_enabled: bool

    async def account(self, owner: str) -> AccountSnapshot: ...
    async def estimate(self, owner: str, config: VideoConfig) -> VideoEstimate: ...
    async def submit(
        self, owner: str, config: VideoConfig, estimate: VideoEstimate
    ) -> VideoTask: ...
    async def task(self, owner: str, task_id: str) -> VideoTask: ...


class AccountBinding(Record):
    token_env: str = Field(pattern=r"^VIDU_ACCOUNT_[A-Z0-9_]+_TOKEN$")
    region: Literal["global", "cn"] = "global"


def read_accounts(path: Path) -> dict[str, AccountBinding]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    bindings = {owner: AccountBinding.model_validate(item) for owner, item in raw.items()}
    names = [item.token_env for item in bindings.values()]
    if len(set(names)) != len(names):
        raise ValueError("Different users must not share a token environment binding")
    return bindings


def video_settings(config: VideoConfig, schedule_mode: str) -> dict:
    return {
        **config.model_dump(exclude={"prompt", "task_type", "schedule_mode"}, exclude_none=True),
        "schedule_mode": schedule_mode,
    }


def safe_media_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 443}
        )
    except ValueError:
        return False


class WebsiteVideoProvider:
    mode = "website_token_experimental"

    def __init__(
        self,
        bindings: dict[str, AccountBinding],
        *,
        generation_enabled: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        credentials: Callable[[str], tuple[AccountBinding, str] | None] | None = None,
    ):
        self.bindings = bindings
        self.generation_enabled = generation_enabled
        self.transport = transport
        self.credentials = credentials

    def credential(self, owner: str) -> tuple[AccountBinding, str] | None:
        if self.credentials is not None:
            return self.credentials(owner)
        binding = self.bindings.get(owner)
        token = os.getenv(binding.token_env, "") if binding else ""
        return (binding, token) if binding and token else None

    def connection_url(self, owner: str) -> str:
        credential = self.credential(owner)
        binding = credential[0] if credential else self.bindings.get(owner)
        return (
            "https://www.vidu.cn" if binding and binding.region == "cn" else "https://www.vidu.com"
        )

    async def request(self, owner: str, method: str, path: str, **kwargs) -> dict:
        credential = self.credential(owner)
        if credential is None:
            raise DomainError("account_not_connected", "尚未连接 Vidu 账号，请先完成账号绑定。")
        binding, token = credential
        base = (
            "https://service.vidu.com" if binding.region == "global" else "https://service.vidu.cn"
        )
        # No redirects, credential logs, proxy inheritance or automatic retries of POST requests.
        async with httpx.AsyncClient(
            transport=self.transport, timeout=30, follow_redirects=False, trust_env=False
        ) as client:
            try:
                response = await client.request(
                    method,
                    base + path,
                    headers={
                        "Authorization": f"Token {token}",
                        "User-Agent": "yingban-vidu/0.3.0",
                    },
                    **kwargs,
                )
            except httpx.HTTPError:
                raise DomainError(
                    "vidu_response_uncertain", "Vidu 响应不确定，请查询原请求。"
                ) from None
        if response.status_code in {401, 403}:
            raise DomainError("login_required", "Vidu 授权已失效或权限不足，请重新连接账号。")
        if not 200 <= response.status_code < 300:
            raise DomainError("vidu_request_failed", "Vidu 未接受请求，请检查账号或稍后查询。")
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
        except ValueError:
            raise DomainError("vidu_invalid_response", "Vidu 返回的数据无法可靠识别。") from None
        return data

    async def account(self, owner: str) -> AccountSnapshot:
        credential = self.credential(owner)
        binding = credential[0] if credential else self.bindings.get(owner)
        if credential is None:
            return AccountSnapshot(
                status="not_connected",
                region=binding.region if binding else "global",
                connection_url=self.connection_url(owner),
                message="尚未连接 Vidu；已有账号请登录，没有账号可在官网注册，再完成服务端绑定。",
            )
        try:
            data = await self.request(owner, "GET", "/credit/v1/credits/me")
        except DomainError as error:
            if error.code != "login_required":
                raise
            return AccountSnapshot(
                status="login_required",
                region=binding.region,
                connection_url=self.connection_url(owner),
                message=str(error),
            )
        credits = data.get("credits")
        if type(credits) is not int or credits < 0:
            raise DomainError("credits_unavailable", "无法可靠读取当前额度，不将未知额度当作零。")
        return AccountSnapshot(
            status="connected", region=binding.region, credits=credits, message="Vidu 账号已连接。"
        )

    async def estimate(self, owner: str, config: VideoConfig) -> VideoEstimate:
        schedule = config.schedule_mode
        if schedule == "auto":
            status = await self.request(owner, "GET", "/credit/v1/claw-pass/status")
            if type(status.get("has_pass")) is not bool:
                raise DomainError("quota_unavailable", "无法确认额度类型，请稍后重试。")
            schedule = "claw_pass" if status["has_pass"] else "normal"
        params = {
            "type": config.task_type,
            **{
                f"settings.{key}": str(value)
                for key, value in video_settings(config, schedule).items()
            },
        }
        data = await self.request(owner, "GET", "/vidu/v1/tasks/credits", params=params)
        try:
            cost, current, allowed = (
                data["cost_credits"],
                data["current_credits"],
                data["can_submit"],
            )
            insufficient = (
                type(cost) is int
                and type(current) is int
                and schedule == "normal"
                and current < cost
            )
            return VideoEstimate(
                cost_credits=cost,
                current_credits=current,
                can_submit=allowed,
                schedule_mode=schedule,
                reason=(
                    "available"
                    if allowed is True
                    else "insufficient_credits"
                    if insufficient
                    else "quota_or_entitlement_blocked"
                ),
            )
        except (KeyError, ValueError):
            raise DomainError(
                "estimate_unavailable", "无法可靠读取生成费用，不推测是否可生成。"
            ) from None

    async def submit(self, owner: str, config: VideoConfig, estimate: VideoEstimate) -> VideoTask:
        if not self.generation_enabled:
            raise DomainError("generation_disabled", "部署尚未启用真实视频提交。")
        data = await self.request(
            owner,
            "POST",
            "/vidu/v1/tasks",
            json={
                "type": config.task_type,
                "input": {
                    "prompts": [{"type": "text", "content": config.prompt}],
                    "editor_mode": "normal",
                    "enhance": True,
                },
                "settings": video_settings(config, estimate.schedule_mode),
            },
        )
        task_id = data.get("id")
        if type(task_id) is int:
            task_id = str(task_id)
        try:
            return VideoTask(task_id=task_id, state="queued")
        except ValueError:
            raise DomainError(
                "submission_uncertain", "提交响应缺少可靠任务编号，不能自动重试。"
            ) from None

    async def task(self, owner: str, task_id: str) -> VideoTask:
        # Task IDs are loaded from this user's persisted workflow, never arbitrary MCP input.
        VideoTask(task_id=task_id, state="queued")
        data = await self.request(owner, "GET", f"/vidu/v1/tasks/{task_id}")
        remote_id = data.get("id")
        if remote_id is not None and str(remote_id) != task_id:
            raise DomainError("task_mismatch", "任务返回的编号不匹配。")
        state = data.get("state")
        states = {
            "created": "queued",
            "queueing": "queued",
            "processing": "running",
            "running": "running",
            "success": "success",
            "failed": "failed",
        }
        if state not in states:
            raise DomainError("task_state_unknown", "无法识别任务状态，不能报告完成。")
        urls = []
        if state == "success":
            for item in data.get("creations", []):
                if not isinstance(item, dict):
                    continue
                for field in ("nomark_uri", "download_uri", "uri"):
                    if safe_media_url(item.get(field)):
                        urls.append(item[field])
                        break
        return VideoTask(task_id=task_id, state=states[state], media_urls=urls)
