"""Durable requests with compare-and-swap updates before any billable submission."""

from vidu_purchase.models import DomainError
from vidu_purchase.store import Store
from vidu_purchase.video_models import VideoWorkflow


class WorkflowStore:
    def __init__(self, store: Store):
        self.store = store
        with store.connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS video_workflows (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(owner, idempotency_key)
                )
            """)

    def get(self, owner: str, workflow_id: str) -> VideoWorkflow:
        with self.store.connection() as db:
            row = db.execute(
                "SELECT payload FROM video_workflows WHERE id=? AND owner=?",
                (workflow_id, owner),
            ).fetchone()
        if not row:
            raise DomainError("workflow_not_found", "未找到当前用户的视频请求。")
        return VideoWorkflow.model_validate_json(row[0])

    def insert(self, workflow: VideoWorkflow) -> tuple[VideoWorkflow, bool]:
        with self.store.connection() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO video_workflows VALUES (?, ?, ?, ?, ?)",
                (
                    workflow.workflow_id,
                    workflow.owner,
                    workflow.idempotency_key,
                    workflow.revision,
                    workflow.model_dump_json(),
                ),
            )
            row = db.execute(
                "SELECT payload FROM video_workflows WHERE owner=? AND idempotency_key=?",
                (workflow.owner, workflow.idempotency_key),
            ).fetchone()
        return VideoWorkflow.model_validate_json(row[0]), cursor.rowcount == 1

    def save(self, workflow: VideoWorkflow, **changes) -> VideoWorkflow:
        updated = VideoWorkflow.model_validate(
            {**workflow.model_dump(), **changes, "revision": workflow.revision + 1}
        )
        with self.store.connection() as db:
            cursor = db.execute(
                "UPDATE video_workflows SET payload=?, revision=? "
                "WHERE id=? AND owner=? AND revision=?",
                (
                    updated.model_dump_json(),
                    updated.revision,
                    workflow.workflow_id,
                    workflow.owner,
                    workflow.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise DomainError(
                    "workflow_changed", "视频请求已更新，请先读取最新状态，勿重复提交。"
                )
        return updated
