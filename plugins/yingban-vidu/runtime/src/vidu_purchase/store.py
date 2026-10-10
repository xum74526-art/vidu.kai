import sqlite3
from contextlib import contextmanager
from pathlib import Path

from vidu_purchase.models import DomainError, Purchase, PurchaseState, Quote


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS quotes (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS purchases (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(owner, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    reference TEXT PRIMARY KEY, purchase_id TEXT NOT NULL UNIQUE
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_quote(self, quote: Quote) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO quotes VALUES (?, ?, ?)",
                (quote.quote_id, quote.owner, quote.model_dump_json()),
            )

    def quote(self, owner: str, quote_id: str) -> Quote:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM quotes WHERE id=? AND owner=?", (quote_id, owner)
            ).fetchone()
        if not row:
            raise DomainError("quote_not_found", "未找到当前用户的报价，请重新获取套餐。")
        return Quote.model_validate_json(row[0])

    def purchase(self, owner: str, purchase_id: str) -> Purchase:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM purchases WHERE id=? AND owner=?", (purchase_id, owner)
            ).fetchone()
        if not row:
            raise DomainError("purchase_not_found", "未找到当前用户的购买记录。")
        return Purchase.model_validate_json(row[0])

    def by_key(self, owner: str, key: str) -> Purchase | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM purchases WHERE owner=? AND idempotency_key=?",
                (owner, key),
            ).fetchone()
        return Purchase.model_validate_json(row[0]) if row else None

    def insert_purchase(self, purchase: Purchase) -> tuple[Purchase, bool]:
        with self.connection() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO purchases VALUES (?, ?, ?, ?)",
                (
                    purchase.purchase_id,
                    purchase.owner,
                    purchase.idempotency_key,
                    purchase.model_dump_json(),
                ),
            )
            created = cursor.rowcount == 1
            row = db.execute(
                "SELECT payload FROM purchases WHERE owner=? AND idempotency_key=?",
                (purchase.owner, purchase.idempotency_key),
            ).fetchone()
        return Purchase.model_validate_json(row[0]), created

    def update(self, purchase: Purchase, *, receipt: str | None = None) -> Purchase:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM purchases WHERE id=? AND owner=?",
                (purchase.purchase_id, purchase.owner),
            ).fetchone()
            if not row:
                raise DomainError("purchase_not_found", "购买记录不存在。")
            current = Purchase.model_validate_json(row[0])
            if current.provider_reference:
                if (
                    purchase.provider_reference
                    and purchase.provider_reference != current.provider_reference
                ):
                    raise DomainError(
                        "checkout_reference_changed", "同一购买不能更换官方支付会话。"
                    )
                purchase = purchase.model_copy(
                    update={"provider_reference": current.provider_reference}
                )
            # A stale browser read must not downgrade already verified fulfillment.
            if current.state == PurchaseState.READY:
                return current
            if (
                current.state == PurchaseState.PAID_PENDING_ENTITLEMENT
                and purchase.state != PurchaseState.READY
            ):
                return current
            if receipt:
                linked = db.execute(
                    "SELECT purchase_id FROM receipts WHERE reference=?", (receipt,)
                ).fetchone()
                if linked and linked[0] != purchase.purchase_id:
                    raise DomainError("receipt_reused", "同一笔官方订单不能确认两次购买。")
                existing = db.execute(
                    "SELECT reference FROM receipts WHERE purchase_id=?", (purchase.purchase_id,)
                ).fetchone()
                if existing and existing[0] != receipt:
                    raise DomainError("receipt_changed", "购买记录关联的官方订单发生变化。")
                db.execute(
                    "INSERT OR IGNORE INTO receipts VALUES (?, ?)", (receipt, purchase.purchase_id)
                )
            db.execute(
                "UPDATE purchases SET payload=? WHERE id=? AND owner=?",
                (purchase.model_dump_json(), purchase.purchase_id, purchase.owner),
            )
        return purchase
