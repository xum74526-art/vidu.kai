"""Account handoff and explicit browser grant binding; never accepts chat credentials."""

import asyncio

from vidu_purchase.browser_account import BrowserAccountReader
from vidu_purchase.models import DomainError


class AccountConnectionService:
    def __init__(self, video, *, browser=None, vault=None, vaults=None):
        self.video, self.browser, self.vault = video, browser, vault
        self.vaults = vaults
        self.reader = BrowserAccountReader(browser) if browser else None
        self.launch = None
        self.lock = asyncio.Lock()

    @property
    def available(self):
        return bool(
            self.browser is not None and (
                self.vaults is not None or (
                    self.vault is not None and self.browser.supports_owner("local-user")
                )
            )
        )

    def vault_for(self, owner):
        if not self.browser or not self.browser.supports_owner(owner):
            return None
        if self.vaults:
            return self.vaults.for_owner(owner)
        return self.vault if owner == "local-user" else None

    def available_for(self, owner):
        return self.vault_for(owner) is not None

    async def status(self, owner):
        account = await self.video.account(owner)
        website = await self.browser.session_status(owner) if self.browser else None
        linked = bool(
            account.status == "connected"
            and website and website["status"] == "connected"
            and website.get("video_account_link_verified")
        )
        if linked:
            state, message = "connected", "官网与视频授权已关联，可以继续配置视频。"
        elif account.status == "connected":
            state, message = "video_connected", "视频授权可用；官网账号关联尚未核对。"
        elif website and website["status"] == "connected":
            state, message = "authorization_required", "官网已登录，请在连接窗口确认授权。"
        else:
            state, message = "login_required", "请在连接窗口进入官网登录或注册，再确认授权。"
        return {
            "state": state,
            "message": message,
            "account": account.model_dump(),
            "website": website,
            "browser_connection_available": self.available_for(owner),
            "account_link_verified": linked,
            "automatic_registration_available": False,
            "auto_generate": False,
        }

    async def begin(self, owner, access):
        result = await self.status(owner)
        result["connection_action"] = None
        if result["account_link_verified"]:
            return result
        if not self.launch or self.vault is None or owner != "local-user":
            result["message"] = "当前部署没有自助绑定入口，请由运营方配置本用户的账号授权。"
            return result
        result["connection_action"] = {
            "type": "open_account_connection",
            "url": self.launch(access, account_view=True),
            "expires_in_seconds": 300,
            "authorization_by": "user",
            "secrets_in_chat": False,
        }
        return result

    def require_local(self, owner):
        if not self.available_for(owner):
            raise DomainError("account_connection_unavailable", "当前部署不支持官网直接连接。")

    async def show_browser(self, owner):
        self.require_local(owner)
        async with self.browser.page(owner) as (page, _binding):
            await page.bring_to_front()
        return {"message": "已显示专用官网窗口，请在该窗口登录或注册后返回确认授权。"}

    async def authorize(self, owner, validator):
        """Called only by the CSRF-protected UI after its authorization button is clicked."""
        self.require_local(owner)
        async with self.lock:
            vault = self.vault_for(owner)
            vault_owner = vault.owner()
            async with self.reader.read(owner) as grant:
                account = await validator(grant.token, "global")
                if account.status != "connected" or account.region != "global":
                    raise DomainError("login_required", "官网授权未通过验证，没有保存。")
                await grant.verify()
                vault.save_browser(
                    vault_owner, grant.token, grant.account_label, grant.account_ref
                )
                # Only the public account handle, never the token or browser endpoint.
                return {
                    "state": "connected", "account": account.model_dump(),
                    "account_label": grant.account_label, "account_link_verified": True,
                    "auto_generate": False,
                    "message": "账号已连接，可以继续使用视频插件。生成前仍需确认配置和积分。",
                }
