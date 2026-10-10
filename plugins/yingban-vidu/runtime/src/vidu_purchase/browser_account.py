"""Explicitly authorized import from Vidu's visible Token panel, never browser storage.

The credential stays inside the service. It is not a tool result, URL, clipboard
entry or screenshot. Selectors were inspected on the official UI on 2026-10-09.
"""

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from playwright.async_api import Error as BrowserError

from vidu_purchase.browser import account_reference
from vidu_purchase.models import DomainError


@dataclass(frozen=True)
class BrowserAccountGrant:
    token: str = field(repr=False)
    account_label: str
    account_ref: str
    verify: Callable[[], Awaitable[None]] = field(repr=False)


class BrowserAccountReader:
    def __init__(self, browser):
        self.browser = browser

    @asynccontextmanager
    async def read(self, owner):
        async with self.browser.page(owner) as (page, binding):
            label = await self.browser.identity(page, binding)
            title = page.get_by_text("Vidu Token", exact=True)
            dialog = page.get_by_role("dialog").filter(has=title)
            try:
                if not await dialog.is_visible():
                    # Leave purchase/login/other modal dialogs to their user.
                    if await page.locator('[role="dialog"][aria-modal="true"]').count():
                        raise DomainError(
                            "account_dialog_blocked", "请先完成或关闭官网当前弹窗，再连接账号。"
                        )
                    avatar = page.locator(
                        f'header img[alt="{label}"], div.cursor-pointer > img[alt="{label}"]'
                    )
                    if await avatar.count() != 1:
                        raise DomainError(
                            "account_controls_unavailable",
                            "无法可靠定位官网头像，请使用手动授权入口。",
                        )
                    await avatar.click()
                    await page.get_by_role("menu").get_by_text("Vidu Token", exact=True).click()
                    await dialog.wait_for(state="visible", timeout=10000)
                # The visible token is a single truncated text field beside Copy/Reset.
                # Never press Copy or Reset; clipboard and existing credentials stay intact.
                token_field = dialog.locator("div.truncate")
                await token_field.wait_for(state="visible", timeout=10000)
                if await token_field.count() != 1:
                    raise DomainError("account_token_unavailable", "官网授权面板结构已变化。")
                token = (await token_field.inner_text()).strip()
                if (
                    not 16 <= len(token) <= 2048
                    or not token.isascii()
                    or any(char.isspace() or char in "*<>" for char in token)
                ):
                    raise DomainError("account_token_unavailable", "无法读取有效的官网授权。")

                async def verify():
                    if await self.browser.identity(page, binding) != label:
                        raise DomainError(
                            "browser_account_changed", "连接期间官网账号发生变化，没有保存授权。"
                        )
                    if not await dialog.is_visible() or (
                        await token_field.inner_text()
                    ).strip() != token:
                        raise DomainError(
                            "account_token_changed", "连接期间官网授权发生变化，请重新连接。"
                        )

                await verify()
                yield BrowserAccountGrant(
                    token=token,
                    account_label=label,
                    account_ref=account_reference(owner, binding, label),
                    verify=verify,
                )
            except BrowserError:
                # Browser errors can quote DOM text. Never send them to logs or clients.
                raise DomainError(
                    "account_controls_unavailable",
                    "官网授权面板暂不可操作，请检查原窗口，或使用手动授权入口。",
                ) from None
            finally:
                try:
                    # Explicit authorization consumes this read-only panel even when
                    # the user left it open after a previous manual-token connection.
                    await self.browser.dismiss_account_view(page)
                except BrowserError:
                    pass
