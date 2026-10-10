"""Single-desktop account binding. Only an OS credential store holds the Vidu token."""

import hashlib
import json
import re
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from vidu_purchase.models import DomainError
from vidu_purchase.video_provider import AccountBinding

SERVICE = "yingban-vidu/desktop"


class AccountVaults:
    """Per-authenticated-subject bindings; tokens remain in the native secret store."""

    def __init__(self, root: Path, subjects, *, backend=None):
        self.root, self.subjects, self.backend = root.resolve(), frozenset(subjects), backend
        self.vaults = {}

    def for_owner(self, owner):
        if owner not in self.subjects:
            return None
        if owner not in self.vaults:
            directory = self.root / hashlib.sha256(owner.encode()).hexdigest()
            self.vaults[owner] = CredentialVault(directory, backend=self.backend)
        return self.vaults[owner]

    def credential(self, owner):
        vault = self.for_owner(owner)
        return vault.credential(vault.owner()) if vault else None

    def browser_link(self, owner):
        vault = self.for_owner(owner)
        return vault.browser_link(vault.owner()) if vault else None

    def revision(self, owner):
        vault = self.for_owner(owner)
        return vault.owner() if vault else None


def native_keyring():
    # Select native backends explicitly; never fall back to plaintext or third-party keyrings.
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()
    if sys.platform.startswith("linux"):
        from keyring.backends.SecretService import Keyring

        return Keyring()
    raise DomainError("credential_store_unavailable", "此系统尚不支持安全保存 Vidu 授权。")


class CredentialVault:
    def __init__(self, data_dir: Path, *, backend=None):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "account.sqlite3"
        self.backend = backend
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY, owner TEXT)")
            db.execute("INSERT OR IGNORE INTO account VALUES (1, ?)", (uuid4().hex,))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def owner(self) -> str:
        with self.connection() as db:
            return db.execute("SELECT owner FROM account WHERE id=1").fetchone()[0]

    def keyring(self):
        if self.backend is None:
            try:
                self.backend = native_keyring()
            except Exception:
                raise DomainError(
                    "credential_store_unavailable", "系统凭据库不可用；不会改用明文保存 Token。"
                ) from None
        return self.backend

    def _read(self, owner: str) -> tuple[AccountBinding, str] | None:
        try:
            raw = self.keyring().get_password(SERVICE, owner)
            if raw is None:
                return None
            saved = json.loads(raw)
            token = saved["token"]
            if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
                raise ValueError()
            return AccountBinding(
                token_env="VIDU_ACCOUNT_DESKTOP_TOKEN", region=saved["region"]
            ), token
        except Exception:
            raise DomainError(
                "credential_store_unavailable", "无法读取系统凭据库，请解锁系统凭据库后重试。"
            ) from None

    def credential(self, owner: str) -> tuple[AccountBinding, str] | None:
        with self.connection() as db:
            current = db.execute("SELECT owner FROM account WHERE id=1").fetchone()[0]
            return self._read(owner) if owner == current else None

    def save(self, owner: str, token: str, region: str) -> None:
        if region not in {"global", "cn"} or not token or len(token) > 2048:
            raise DomainError("invalid_credentials", "请检查区域和 Vidu Token。")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT owner FROM account WHERE id=1").fetchone()[0]
            if owner != current or self._read(owner) is not None:
                raise DomainError("connection_changed", "账号连接已变化，请重新发起连接。")
            try:
                self.keyring().set_password(
                    SERVICE, owner, json.dumps({"region": region, "token": token})
                )
            except Exception:
                raise DomainError(
                    "credential_store_unavailable", "无法保存到系统凭据库；账号连接未完成。"
                ) from None

    def browser_link(self, owner: str) -> dict | None:
        """Only non-secret provenance; a browser login alone is never a video grant."""
        with self.connection() as db:
            current = db.execute("SELECT owner FROM account WHERE id=1").fetchone()[0]
            if current != owner or self._read(owner) is None:
                return None
            try:
                saved = json.loads(self.keyring().get_password(SERVICE, owner))
                proof = saved.get("browser_link")
                if proof is None:
                    return None
                if (
                    set(proof) != {"account_label", "account_ref"}
                    or not re.fullmatch(r"vidu_[A-Za-z0-9]{4,100}", proof["account_label"])
                    or not re.fullmatch(r"[a-f0-9]{64}", proof["account_ref"])
                ):
                    raise ValueError()
                return proof
            except Exception:
                raise DomainError(
                    "credential_store_unavailable", "无法可靠读取账号关联，请重新连接。"
                ) from None

    def save_browser(self, owner: str, token: str, account_label: str, account_ref: str):
        """Save a verified grant or attach provenance to the identical existing grant."""
        if (
            not token or len(token) > 2048 or not token.isascii()
            or any(char.isspace() for char in token)
            or not re.fullmatch(r"vidu_[A-Za-z0-9]{4,100}", account_label)
            or not re.fullmatch(r"[a-f0-9]{64}", account_ref)
        ):
            raise DomainError("invalid_credentials", "无法可靠保存这次官网授权。")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if owner != db.execute("SELECT owner FROM account WHERE id=1").fetchone()[0]:
                raise DomainError("connection_changed", "账号连接已变化，请重新发起连接。")
            current = self._read(owner)
            if current and (
                current[0].region != "global" or not secrets.compare_digest(current[1], token)
            ):
                raise DomainError(
                    "account_link_conflict",
                    "官网授权与已保存的视频授权不同。原连接已保留；切换账号前请先断开。",
                )
            saved = {
                "region": "global", "token": token,
                "browser_link": {"account_label": account_label, "account_ref": account_ref},
            }
            try:
                self.keyring().set_password(SERVICE, owner, json.dumps(saved))
            except Exception:
                raise DomainError(
                    "credential_store_unavailable", "系统凭据库未确认保存，不能报告连接成功。"
                ) from None

    def disconnect(self) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute("SELECT owner FROM account WHERE id=1").fetchone()[0]
            if self._read(owner) is not None:
                try:
                    self.keyring().delete_password(SERVICE, owner)
                except Exception:
                    raise DomainError(
                        "disconnect_failed", "凭据库未确认删除，不能报告已断开。"
                    ) from None
            # A different account must never inherit pending/confirmed tasks from this account.
            db.execute("UPDATE account SET owner=? WHERE id=1", (uuid4().hex,))
