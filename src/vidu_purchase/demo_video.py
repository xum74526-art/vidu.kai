"""Test harness provider. No method that simulates login/payment is exposed through MCP."""

from uuid import uuid4

from vidu_purchase.models import DomainError
from vidu_purchase.video_models import AccountSnapshot, VideoConfig, VideoEstimate, VideoTask


class DemoVideoProvider:
    mode = "demo"
    generation_enabled = True

    def __init__(self):
        self.credits: dict[str, int] = {}
        self.tasks: dict[str, tuple[str, VideoTask]] = {}
        self.submission_count = 0
        self.unit_cost = 2

    def connect_for_test(self, owner: str, *, credits: int = 0) -> None:
        self.credits[owner] = credits

    def complete_for_test(self, task_id: str) -> None:
        owner, task = self.tasks[task_id]
        self.tasks[task_id] = (owner, task.model_copy(update={"state": "success"}))

    async def account(self, owner: str) -> AccountSnapshot:
        return AccountSnapshot(
            status="connected" if owner in self.credits else "not_connected",
            credits=self.credits.get(owner),
            message="演示账号已连接。"
            if owner in self.credits
            else "演示账号尚未连接；由测试程序模拟登录或注册。",
        )

    async def estimate(self, owner: str, config: VideoConfig) -> VideoEstimate:
        cost = self.unit_cost * config.duration * config.sample_count
        credits = self.credits.get(owner, 0)
        return VideoEstimate(
            cost_credits=cost,
            current_credits=credits,
            can_submit=credits >= cost,
            schedule_mode="normal",
            reason="available" if credits >= cost else "insufficient_credits",
        )

    async def submit(self, owner: str, config: VideoConfig, estimate: VideoEstimate) -> VideoTask:
        if self.credits.get(owner, 0) < estimate.cost_credits:
            raise DomainError("insufficient_credits", "演示积分不足。")
        self.credits[owner] -= estimate.cost_credits
        self.submission_count += 1
        task = VideoTask(task_id="demo-" + uuid4().hex, state="queued")
        self.tasks[task.task_id] = (owner, task)
        return task

    async def task(self, owner: str, task_id: str) -> VideoTask:
        pair = self.tasks.get(task_id)
        if not pair or pair[0] != owner:
            raise DomainError("task_not_found", "未找到当前用户的演示任务。")
        return pair[1]
