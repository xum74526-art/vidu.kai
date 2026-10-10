"""Connect the installed stdio plugin to its registered official login panel."""

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from vidu_purchase.models import DomainError

SERVICE = "yingban-selfhost/loopback"
CONFIG = "official-panel.json"


def panel_origin(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path or parsed.port is None or not 1024 <= parsed.port <= 65535
    ):
        raise ValueError("The official panel service must use a loopback origin")
    return value


def register_official_panel(directory: Path, origin: str, credential_account: str):
    """Save routing metadata only; the service credential stays in the OS keyring."""
    panel_origin(origin)
    if not (directory / "account.sqlite3").is_file():
        raise ValueError("The shared desktop account store is not initialized")
    if not re.fullmatch(r"[a-f0-9]{24}:[0-9]{4,5}", credential_account):
        raise ValueError("Invalid local service identity")
    target = directory / CONFIG
    staged = target.with_suffix(".tmp")
    staged.write_text(json.dumps({
        "origin": origin, "credential_account": credential_account,
    }) + "\n", encoding="utf-8")
    staged.replace(target)


class OfficialConnectionManager:
    def __init__(self, vault, *, transport=None):
        self.vault, self.transport = vault, transport
        self.path = vault.path.parent / CONFIG

    @property
    def configured(self):
        return self.path.is_file()

    async def begin(self):
        if not self.configured:
            raise DomainError(
                "official_panel_not_configured",
                "尚未连接本机官方登录面板服务，请先完成插件的面板服务配置；不需要提交 Token。",
            )
        try:
            config = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(config, dict) or set(config) != {"origin", "credential_account"}:
                raise ValueError()
            origin = panel_origin(config["origin"])
            identity = config["credential_account"]
            if not re.fullmatch(r"[a-f0-9]{24}:[0-9]{4,5}", identity):
                raise ValueError()
            if not identity.endswith(":" + str(urlsplit(origin).port)):
                raise ValueError()
            credential = self.vault.keyring().get_password(SERVICE, identity)
            if not credential:
                raise ValueError()
        except Exception:
            raise DomainError(
                "official_panel_configuration_invalid",
                "官方登录面板配置不可用，请重新启动本机服务。",
            ) from None
        revision = self.vault.owner()
        try:
            async with httpx.AsyncClient(
                timeout=12, trust_env=False, follow_redirects=False, transport=self.transport,
            ) as client:
                response = await client.post(
                    origin + "/console/desktop-panel",
                    headers={
                        "Authorization": "Bearer " + credential, "X-Yingban-Console": "1",
                    },
                    json={"binding_revision": revision},
                )
            if (
                response.status_code == 409
                and response.json().get("error", {}).get("code") == "account_binding_changed"
            ):
                raise DomainError(
                    "official_panel_account_mismatch",
                    "插件和登录服务的账号存储不一致，请重新启动共享账号的本机服务。",
                )
            response.raise_for_status()
            data = response.json()
            url = urlsplit(data["panel_url"])
            ticket = parse_qs(url.fragment).get("ticket", [])
            if (
                f"{url.scheme}://{url.netloc}" != origin or url.path != "/panel"
                or url.query or len(ticket) != 1
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", ticket[0])
                or data.get("binding_revision") != revision or self.vault.owner() != revision
                or data.get("purpose") != "account"
            ):
                raise ValueError()
        except DomainError:
            raise
        except Exception:
            raise DomainError(
                "official_panel_unavailable",
                "官方登录面板服务暂时不可用，请重启本机服务后重新连接；不用填写 Token。",
            ) from None
        return {
            "status": "awaiting_login",
            "connection_mode": "official_browser_panel",
            "connection_url": data["panel_url"],
            "expires_in_seconds": data["expires_in_seconds"],
            "secrets_in_chat": False,
            "manual_token_required": False,
            "auto_generate": False,
            "message": "请打开官方登录面板，登录后点击「我已登录，授权并连接」，再检查账号状态。",
        }

    def close(self):
        # The shared panel service owns its lifetime. Disconnect rotates the vault
        # revision, which invalidates every previously issued account panel.
        pass
