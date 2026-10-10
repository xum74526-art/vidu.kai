"""User-operated, owner-bound views of an isolated official browser.

Only page pixels and user input pass through this service. There is no general
CDP/URL endpoint, cookie access, recording, or model tool for credentials/input.
"""

import asyncio
import logging
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from pydantic import Field, ValidationError, model_validator
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from vidu_purchase.browser import official_page
from vidu_purchase.checkout import stripe_reference
from vidu_purchase.models import DomainError, PurchaseState, Record

STATIC = Path(__file__).parent / "static"
APP_URI = "ui://yingban/official-panel.html"
WIDTH, HEIGHT = 1280, 800
AUTH_HOSTS = {"accounts.google.com", "appleid.apple.com"}
KEYS = {
    "Enter": 13,
    "Tab": 9,
    "Backspace": 8,
    "Delete": 46,
    "Escape": 27,
    "ArrowLeft": 37,
    "ArrowUp": 38,
    "ArrowRight": 39,
    "ArrowDown": 40,
    "Home": 36,
    "End": 35,
    "PageUp": 33,
    "PageDown": 34,
}
LOG = logging.getLogger(__name__)


class PanelRequest(Record):
    purpose: Literal["account", "payment"] = "account"
    purchase_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def payment_has_purchase(self):
        if (self.purpose == "payment") != (self.purchase_id is not None):
            raise ValueError("Payment requires an existing purchase; account takes no purchase")
        return self


class PanelInput(Record):
    kind: Literal["click", "wheel", "key", "text", "tab", "dismiss_token"]
    epoch: int = Field(ge=1, strict=True)
    x: float = Field(default=0, ge=0, le=WIDTH, allow_inf_nan=False)
    y: float = Field(default=0, ge=0, le=HEIGHT, allow_inf_nan=False)
    delta_x: float = Field(default=0, ge=-1500, le=1500, allow_inf_nan=False)
    delta_y: float = Field(default=0, ge=-1500, le=1500, allow_inf_nan=False)
    key: str = Field(default="", max_length=20)
    shift: bool = Field(default=False, strict=True)
    select_all: bool = Field(default=False, strict=True)
    text: str = Field(default="", max_length=2048, repr=False)
    tab_id: str = Field(default="", max_length=40)


class PanelStart(Record):
    ticket: str = Field(min_length=32, max_length=100, repr=False)
    viewport_width: int = Field(default=WIDTH, ge=360, le=WIDTH, strict=True)


@dataclass(repr=False)
class PanelSession:
    owner: str
    purpose: str
    expires: float
    binding: object
    revision: str | None
    purchase_id: str | None = None
    access_expires: float = float("inf")
    remote: object | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    viewport_width: int = WIDTH


class RemoteBrowser:
    """One connection per active panel, always to an operator-bound browser."""

    def __init__(self, provider, session, store):
        self.provider, self.session, self.store = provider, session, store
        self.runtime = self.browser = self.context = self.page = self.cdp = None
        self.pages = {}
        self.epoch = 1
        self.checkout_reference = None
        self.stage = "idle"
        self.lock = provider.locks.setdefault(session.owner, asyncio.Lock())

    def permitted(self, url):
        if official_page(url):
            return True
        value = urlsplit(url)
        if value.scheme != "https" or value.username or value.password:
            return False
        if value.port not in {None, 443}:
            return False
        if self.session.purpose == "account" and value.hostname in AUTH_HOSTS:
            return True
        if self.session.purpose == "payment" and self.checkout_reference:
            try:
                return stripe_reference(url) == self.checkout_reference
            except DomainError:
                return False
        return False

    async def start(self):
        try:
            self.stage = "connecting"
            self.runtime = await async_playwright().start()
            self.browser = await self.runtime.chromium.connect_over_cdp(
                self.session.binding.cdp_url, timeout=10000
            )
            if len(self.browser.contexts) != 1:
                raise DomainError("ambiguous_browser", "需要独立的单用户官网浏览器。")
            self.context = self.browser.contexts[0]
            self.stage = "selecting"
            official = [page for page in self.context.pages if official_page(page.url)]
            if not official:
                # Never redirect an in-progress identity-provider login.
                auth = [page for page in self.context.pages if self.permitted(page.url)]
                if self.session.purpose != "account" or len(auth) != 1:
                    raise DomainError("browser_page_missing", "专用浏览器没有可用的官网页面。")
                selected = auth[0]
            else:
                selected = await self.provider.select_page(official, self.session.binding)
            if self.session.purpose == "payment":
                purchase = self.store.purchase(self.session.owner, self.session.purchase_id)
                await self.provider.verify_purchase_identity(
                    selected, self.session.binding, purchase
                )
                self.checkout_reference = stripe_reference(purchase.handoff_url or "")
                matches = [
                    page
                    for page in self.context.pages
                    if self.permitted(page.url) and not official_page(page.url)
                ]
                if matches:
                    selected = matches[0]
                else:
                    # Reopen this exact checkout, never prepare a second purchase.
                    selected = await self.context.new_page()
                    await selected.goto(purchase.handoff_url, wait_until="domcontentloaded")
            await self.sync_pages()
            await self.select(selected)
        except BaseException:
            await self.close()
            raise

    async def sync_pages(self):
        present = [page for page in self.context.pages if not page.is_closed()]
        added = [
            page for page in present if page not in self.pages.values() and self.permitted(page.url)
        ]
        for page in added:
            self.pages[secrets.token_hex(8)] = page
        self.pages = {key: page for key, page in self.pages.items() if page in present}
        # Identity provider popups stay inside the same user-operated surface.
        if self.page is not None and added:
            await self.select(added[-1])
        if self.page is not None and self.page.is_closed():
            available = [page for page in self.pages.values() if self.permitted(page.url)]
            if not available:
                raise DomainError("browser_page_missing", "官网窗口已关闭，请重新连接。")
            await self.select(available[-1])

    async def select(self, page):
        if not self.permitted(page.url):
            raise DomainError("panel_origin_blocked", "这个页面不属于当前授权的官网流程。")
        if self.cdp:
            with suppress(Exception):
                async with asyncio.timeout(3):
                    await self.cdp.send("Emulation.clearDeviceMetricsOverride")
                    await self.cdp.detach()
        self.page = page
        self.stage = "preparing_view"
        self.page.set_default_timeout(5000)
        self.cdp = await self.context.new_cdp_session(page)
        await self.cdp.send("Page.enable")
        await self.cdp.send("Page.bringToFront")
        await self.cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": self.session.viewport_width,
                "height": HEIGHT,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        self.epoch += 1

    async def guard(self):
        if self.provider.bindings.get(self.session.owner) != self.session.binding:
            raise DomainError("panel_binding_changed", "浏览器绑定已变更，请重新打开面板。")
        if not self.permitted(self.page.url):
            raise DomainError(
                "panel_origin_blocked", "官方流程进入了尚未支持的验证页面，操作已暂停。"
            )
        if self.session.purpose == "payment":
            purchase = self.store.purchase(self.session.owner, self.session.purchase_id)
            if (
                purchase.state != PurchaseState.AWAITING_PAYMENT
                or purchase.provider_reference != self.checkout_reference
                or stripe_reference(purchase.handoff_url or "") != self.checkout_reference
            ):
                raise DomainError("panel_checkout_changed", "订单状态已变化，请回到对话核对。")
            official = [page for page in self.context.pages if official_page(page.url)]
            if not official:
                raise DomainError("login_required", "请先恢复同一账号的官网登录。")
            page = await self.provider.select_page(official, self.session.binding)
            await self.provider.verify_purchase_identity(page, self.session.binding, purchase)

    async def token_visible(self):
        if not official_page(self.page.url):
            return False
        title = self.page.get_by_text("Vidu Token", exact=True)
        return await self.page.get_by_role("dialog").filter(has=title).is_visible()

    def public_state(self):
        tabs = []
        for key, page in self.pages.items():
            if not self.permitted(page.url):
                continue
            host = urlsplit(page.url).hostname
            label = {
                "www.vidu.com": "Vidu 官网",
                "accounts.google.com": "Google 登录",
                "appleid.apple.com": "Apple 登录",
                "checkout.stripe.com": "官方支付",
            }.get(host, "官方验证")
            tabs.append({"id": key, "label": label, "active": page == self.page})
        return {
            "epoch": self.epoch,
            "width": self.session.viewport_width,
            "height": HEIGHT,
            "tabs": tabs,
            "origin": "https://" + (urlsplit(self.page.url).hostname or ""),
        }

    async def frame(self):
        # Captures cannot overlap the visible-Token import or automatic checkout actions.
        if self.lock.locked():
            return {"state": "busy", "message": "正在处理官网操作，画面暂时隐藏。"}
        async with self.lock:
            self.stage = "checking_page"
            await self.sync_pages()
            await self.guard()
            result = self.public_state()
            if await self.token_visible():
                return {**result, "state": "credential_hidden", "message": "授权凭据画面已隐藏。"}
            self.stage = "capturing"
            shot = await self.cdp.send(
                "Page.captureScreenshot",
                {
                    "format": "jpeg",
                    "quality": 65,
                    "captureBeyondViewport": False,
                },
            )
            self.stage = "checking_capture"
            await self.guard()
            if await self.token_visible():
                return {**result, "state": "credential_hidden", "message": "授权凭据画面已隐藏。"}
            return {**result, "state": "live", "image": shot["data"]}

    async def input(self, body):
        if self.lock.locked():
            raise DomainError("panel_busy", "正在处理官网操作，请稍后再操作。")
        async with self.lock:
            await self.sync_pages()
            if body.epoch != self.epoch:
                raise DomainError("panel_stale_view", "页面已切换，请等待新画面后操作。")
            if body.kind == "tab":
                target = self.pages.get(body.tab_id)
                if target is None:
                    raise DomainError("invalid_input", "页面不存在。")
                await self.select(target)
                return
            await self.guard()
            if body.kind == "dismiss_token":
                await self.provider.dismiss_account_view(self.page)
                return
            if await self.token_visible():
                raise DomainError("panel_credential_hidden", "请先关闭凭据面板。")
            if body.kind == "click":
                if body.x >= self.session.viewport_width:
                    raise DomainError("invalid_input", "点击位置不在当前画面内。")
                args = {"x": body.x, "y": body.y, "button": "left", "clickCount": 1}
                await self.cdp.send("Input.dispatchMouseEvent", {**args, "type": "mousePressed"})
                await self.cdp.send("Input.dispatchMouseEvent", {**args, "type": "mouseReleased"})
            elif body.kind == "wheel":
                await self.cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": body.x,
                        "y": body.y,
                        "deltaX": body.delta_x,
                        "deltaY": body.delta_y,
                    },
                )
            elif body.kind == "text":
                if not body.text or any(ord(c) < 32 for c in body.text):
                    raise DomainError("invalid_input", "请逐项填写官网输入框。")
                await self.cdp.send("Input.insertText", {"text": body.text})
            elif body.kind == "key":
                if body.select_all:
                    params = {
                        "key": "a",
                        "code": "KeyA",
                        "windowsVirtualKeyCode": 65,
                        "modifiers": 2,
                        "commands": ["selectAll"],
                    }
                else:
                    if body.key not in KEYS:
                        raise DomainError("invalid_input", "不支持该快捷键。")
                    params = {
                        "key": body.key,
                        "windowsVirtualKeyCode": KEYS[body.key],
                        "modifiers": 8 if body.shift else 0,
                    }
                await self.cdp.send("Input.dispatchKeyEvent", {**params, "type": "keyDown"})
                await self.cdp.send("Input.dispatchKeyEvent", {**params, "type": "keyUp"})

    async def close(self):
        if self.cdp:
            with suppress(Exception):
                async with asyncio.timeout(2):
                    await self.cdp.send("Emulation.clearDeviceMetricsOverride")
                    await self.cdp.detach()
        if self.runtime:
            with suppress(Exception):
                async with asyncio.timeout(3):
                    await self.runtime.stop()
        self.runtime = self.browser = self.context = self.page = self.cdp = None
        self.pages.clear()


class BrowserPanelService:
    def __init__(self, provider, store, connections, origin, *, frame_origins=(), clock=time.time):
        self.provider, self.store, self.connections = provider, store, connections
        self.origin, self.clock = origin.rstrip("/"), clock
        self.tickets, self.sessions = {}, {}
        self.remote_factory = RemoteBrowser
        origins = []
        for value in frame_origins:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
                or "*" in value
            ):
                raise ValueError("Panel embedding requires exact HTTPS host origins")
            origins.append(value.rstrip("/"))
        self.headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors " + " ".join(["'self'", *origins])
            ),
        }

    @property
    def available(self):
        return self.provider is not None

    def revision(self, owner):
        vault = self.connections.vault_for(owner)
        return vault.owner() if vault else None

    async def prune(self):
        for key, saved in list(self.tickets.items()):
            if saved.expires <= self.clock():
                del self.tickets[key]
        for key, saved in list(self.sessions.items()):
            if (
                saved.expires <= self.clock()
                or saved.revision != self.revision(saved.owner)
                or self.provider.bindings.get(saved.owner) != saved.binding
            ):
                with suppress(TimeoutError):
                    async with asyncio.timeout(6), saved.lock:
                        if saved.remote:
                            await saved.remote.close()
                        self.sessions.pop(key, None)

    async def issue(self, owner, access, body):
        await self.prune()
        if not self.provider or owner not in self.provider.bindings:
            raise DomainError("browser_not_bound", "请先为此用户配置独立的官网浏览器。")
        if access is None or not {"vidu:video", "vidu:purchase"}.issubset(access.scopes):
            raise DomainError("insufficient_scope", "缺少账号与购买权限。")
        if (access.subject or access.claims.get("sub")) != owner:
            raise DomainError("panel_forbidden", "面板必须使用当前登录用户的浏览器。")
        if body.purpose == "payment":
            purchase = self.store.purchase(owner, body.purchase_id)
            if purchase.state != PurchaseState.AWAITING_PAYMENT or not purchase.handoff_url:
                raise DomainError("panel_checkout_unavailable", "原订单尚无可确认的官方支付页。")
            if stripe_reference(purchase.handoff_url) != purchase.provider_reference:
                raise DomainError("panel_checkout_unavailable", "原订单支付会话未通过核对。")
        if len(self.tickets) >= 128 or sum(s.owner == owner for s in self.tickets.values()) >= 8:
            raise DomainError("panel_limit", "请使用已有面板入口，或稍后重试。")
        expires = min(self.clock() + 300, access.expires_at or float("inf"))
        if expires <= self.clock():
            raise DomainError("panel_expired", "用户授权已过期。")
        key = secrets.token_urlsafe(32)
        self.tickets[key] = PanelSession(
            owner,
            body.purpose,
            expires,
            self.provider.bindings[owner],
            self.revision(owner),
            body.purchase_id,
            access.expires_at or float("inf"),
        )
        return {
            "purpose": body.purpose,
            "purchase_id": body.purchase_id,
            "panel_url": self.origin + "/panel#ticket=" + key,
            "expires_in_seconds": int(expires - self.clock()),
            "interaction_by": "user",
            "auto_generate": False,
            "message": "请在对话内的官方面板中操作。关闭面板不会付款或生成视频。",
        }

    def same_origin(self, request):
        if (
            request.headers.get("x-yingban-panel") != "1"
            or request.headers.get("origin") not in {None, self.origin}
            or request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}
        ):
            raise DomainError("panel_forbidden", "请在已授权的官方面板中操作。")

    def session(self, request):
        self.same_origin(request)
        auth = request.headers.get("authorization", "")
        key = auth[7:] if auth.startswith("Bearer ") and len(auth) < 150 else ""
        saved = self.sessions.get(key)
        if (
            saved is None
            or saved.expires <= self.clock()
            or saved.revision != self.revision(saved.owner)
            or self.provider.bindings.get(saved.owner) != saved.binding
        ):
            raise DomainError("panel_expired", "面板授权已过期，请从对话重新打开。")
        return key, saved

    async def exchange(self, request):
        self.same_origin(request)
        await self.prune()
        body = PanelStart.model_validate_json(await request.body())
        saved = self.tickets.pop(body.ticket, None)
        if (
            not saved or saved.expires <= self.clock()
            or saved.revision != self.revision(saved.owner)
            or self.provider.bindings.get(saved.owner) != saved.binding
        ):
            raise DomainError("panel_expired", "入口已使用或过期，请重新打开面板。")
        # One operator per browser. A second entry cannot silently take over an active panel.
        if any(s.owner == saved.owner for s in self.sessions.values()):
            raise DomainError("panel_busy", "已有一个面板正在使用此账号，请先关闭原面板。")
        if len(self.sessions) >= 64:
            raise DomainError("panel_limit", "可用面板已满，请稍后再试。")
        key = secrets.token_urlsafe(32)
        # Do not extend past the original authenticated user's access lifetime.
        saved.expires = min(saved.access_expires, self.clock() + 15 * 60)
        saved.viewport_width = body.viewport_width
        self.sessions[key] = saved
        return {
            "session_token": key,
            "purpose": saved.purpose,
            "authorization_available": self.connections.available_for(saved.owner),
            "expires_in_seconds": int(saved.expires - self.clock()),
        }

    async def handle(self, request):
        try:
            if request.url.path == "/panel/session" and request.method == "POST":
                result = await self.exchange(request)
            else:
                key, saved = self.session(request)
                try:
                    async with asyncio.timeout(3):
                        await saved.lock.acquire()
                except TimeoutError:
                    raise DomainError(
                        "panel_busy", "官网正在处理上一步操作，请稍后再试。"
                    ) from None
                try:
                    async with asyncio.timeout(
                        60 if request.url.path.endswith("/authorize") else 18
                    ):
                        result = await self.operate(request, key, saved)
                except TimeoutError:
                    # Only a fixed stage name is logged, never input, images, URLs or credentials.
                    if saved.remote:
                        LOG.warning("Official panel timed out at stage=%s", saved.remote.stage)
                        await saved.remote.close()
                        saved.remote = None
                    raise
                finally:
                    saved.lock.release()
            return JSONResponse(result, headers=self.headers)
        except DomainError as error:
            status = (
                401
                if error.code == "panel_expired"
                else 403
                if (error.code in {"panel_forbidden", "insufficient_scope"})
                else 404
                if error.code.endswith("_not_found")
                else 409
            )
            return JSONResponse(
                {"error": {"code": error.code, "message": str(error)}},
                status,
                headers=self.headers,
            )
        except TimeoutError:
            return JSONResponse(
                {"error": {"code": "panel_timeout", "message": "官网画面响应超时，请重试连接。"}},
                503,
                headers=self.headers,
            )
        except (ValidationError, ValueError):
            return JSONResponse(
                {"error": {"code": "invalid_input", "message": "面板请求无效。"}},
                422,
                headers=self.headers,
            )
        except Exception:
            # Do not log input, CDP exceptions, URLs, frames, or upstream form data.
            return JSONResponse(
                {
                    "error": {
                        "code": "panel_unavailable",
                        "message": "官网画面暂时不可用，请重试连接。",
                    }
                },
                503,
                headers=self.headers,
            )

    async def operate(self, request, key, saved):
        if request.method == "DELETE":
            self.sessions.pop(key, None)
            if saved.remote:
                await saved.remote.close()
            result = {"state": "closed", "auto_generate": False}
        elif request.url.path.endswith("/authorize"):
            from vidu_purchase.console import BrowserAuthorizationInput, LocalConsole

            BrowserAuthorizationInput.model_validate_json(await request.body())
            if saved.purpose != "account":
                raise DomainError("panel_forbidden", "此面板只用于确认原订单。")
            if saved.remote:
                await saved.remote.close()
                saved.remote = None
            result = await self.connections.authorize(saved.owner, LocalConsole.validate_credential)
        else:
            body = (
                PanelInput.model_validate_json(await request.body())
                if request.url.path.endswith("/input")
                else None
            )
            if saved.remote is None:
                saved.remote = remote = self.remote_factory(self.provider, saved, self.store)
                async with remote.lock:
                    await remote.start()
            if body is not None:
                async with asyncio.timeout(8):
                    await saved.remote.input(body)
                result = {"state": "accepted"}
            else:
                async with asyncio.timeout(8):
                    result = await saved.remote.frame()
        return result

    def routes(self):
        async def index(_request):
            return FileResponse(STATIC / "panel.html", headers=self.headers)

        return [
            Route("/panel", index),
            Route("/panel/session", self.handle, methods=["POST", "DELETE"]),
            Route("/panel/frame", self.handle),
            Route("/panel/input", self.handle, methods=["POST"]),
            Route("/panel/authorize", self.handle, methods=["POST"]),
        ]

    async def close(self):
        for saved in list(self.sessions.values()):
            if saved.remote:
                await saved.remote.close()
        self.sessions.clear()
        self.tickets.clear()
