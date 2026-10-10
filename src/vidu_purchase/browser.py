"""DOM-only checkout in one isolated, operator-provisioned browser per user.

Never reads cookies, calls private billing APIs or submits a payment form.
Paid-order verification stays disabled until the evidence is verified live.
"""

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from vidu_purchase.catalog import minor_units, parse_monthly_card, same_price
from vidu_purchase.checkout import CHECKOUT_PRODUCTS, stripe_reference, validate_stripe_text
from vidu_purchase.models import Checkout, DomainError, Plan, Purchase, PurchaseState

# Observed through the logged-in website's 购买积分 entry.
SUBSCRIPTION_URL = "https://www.vidu.com/zh/subscription/personal?tab=credits"
PRICING_URL = "https://www.vidu.com/zh/pricing"
ORDER_HEADERS = ("订阅", "购买时间", "生效时间", "到期时间", "订单编号", "支付方式", "金额")


@dataclass(frozen=True)
class BrowserBinding:
    cdp_url: str
    handoff_url: str | None = None
    expected_account: str | None = None

    def __post_init__(self):
        url = urlsplit(self.cdp_url)
        if (
            not url.hostname
            or url.fragment
            or (
                url.scheme not in {"https", "wss"}
                and not (
                    url.scheme in {"http", "ws"}
                    and url.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            )
        ):
            raise ValueError("CDP requires TLS or a loopback address")
        if self.handoff_url:
            handoff = urlsplit(self.handoff_url)
            if (
                not handoff.hostname
                or handoff.username
                or handoff.password
                or (
                    handoff.scheme != "https"
                    and not (
                        handoff.scheme == "http"
                        and handoff.hostname in {"localhost", "127.0.0.1", "::1"}
                    )
                )
            ):
                raise ValueError("Browser handoff requires HTTPS or loopback HTTP")
        if self.expected_account is not None and not re.fullmatch(
            r"vidu_[A-Za-z0-9]{4,100}", self.expected_account
        ):
            raise ValueError("Use the visible Vidu account handle, never a cookie or token")


def validate_bindings(bindings):
    endpoints = []
    for owner, binding in bindings.items():
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 512:
            raise ValueError("Browser owner must be an authenticated subject")
        url = urlsplit(binding.cdp_url)
        host = "loopback" if url.hostname in {"localhost", "127.0.0.1", "::1"} else url.hostname
        # Query credentials and localhost aliases do not create isolated browsers.
        endpoints.append(
            (
                host,
                url.port or (443 if url.scheme in {"https", "wss"} else 80),
                url.path.rstrip("/"),
            )
        )
    if len(set(endpoints)) != len(endpoints):
        raise ValueError("Each user must have a distinct, isolated browser endpoint")


def read_bindings(path: Path) -> dict[str, BrowserBinding]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Browser bindings must map authenticated subjects to sessions")
    bindings = {owner: BrowserBinding(**value) for owner, value in raw.items()}
    validate_bindings(bindings)
    return bindings


def official_page(url):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.vidu.com"
        and parsed.port in {None, 443}
        and not parsed.username
        and not parsed.password
    )


def account_reference(owner, binding, label):
    return hashlib.sha256(
        json.dumps(
            [owner, binding.cdp_url, label], ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


class ViduBrowserProvider:
    mode = "browser_experimental"

    def __init__(self, bindings: dict[str, BrowserBinding], *, linked_account=None):
        validate_bindings(bindings)
        self.bindings = dict(bindings)
        self.locks: dict[str, asyncio.Lock] = {}
        self.linked_account = linked_account

    @staticmethod
    def can_checkout(plan: Plan) -> bool:
        return plan.name in CHECKOUT_PRODUCTS

    @asynccontextmanager
    async def page(self, owner: str):
        binding = self.bindings.get(owner)
        if not binding:
            raise DomainError("browser_not_bound", "还未配置本用户的专用官网浏览器。")
        async with self.locks.setdefault(owner, asyncio.Lock()):
            async with async_playwright() as playwright:
                try:
                    browser = await playwright.chromium.connect_over_cdp(
                        binding.cdp_url, timeout=10000
                    )
                except Exception:
                    raise DomainError(
                        "browser_unavailable", "专用浏览器未启动或连接已中断，请重新打开后查询。"
                    ) from None
                if len(browser.contexts) != 1:
                    raise DomainError("ambiguous_browser", "浏览器必须只有一个专用账号上下文。")
                context = browser.contexts[0]
                # Authentication can redirect the member page to another Vidu route.
                # Status/receipt polling must never create, reload or navigate a tab.
                pages = [p for p in context.pages if official_page(p.url)]
                if not pages:
                    raise DomainError(
                        "browser_page_missing",
                        "未找到可读取的 Vidu 页面；若正在登录，请在原窗口完成后再检查。"
                        "检查不会刷新或新开登录页。",
                    )
                page = await self.select_page(pages, binding)
                page.set_default_timeout(10000)
                if not official_page(page.url):
                    raise DomainError("browser_origin_changed", "当前页面不是 Vidu 官方页面。")
                proof = self.linked_account(owner) if self.linked_account else None
                if proof:
                    label = await self.identity(page, binding)
                    if account_reference(owner, binding, label) != proof["account_ref"]:
                        raise DomainError(
                            "browser_account_changed",
                            "官网账号与已连接的视频账号不一致，请在原窗口切回已连接的账号。",
                        )
                yield page, binding
                # Playwright disconnects here, leaving the user's browser open.

    async def select_page(self, pages, binding):
        if len(pages) == 1:
            return pages[0]
        # Older releases left duplicate login tabs. Reuse a visibly authenticated
        # tab; never close tabs or silently choose between different accounts.
        identified = {}
        waiting = []
        for candidate in pages:
            try:
                label = await self.identity(candidate, binding)
            except DomainError as error:
                if error.code not in {"login_required", "browser_identity_unverified"}:
                    raise
                waiting.append((candidate, error.code))
            else:
                identified.setdefault(label, candidate)
        if len(identified) > 1:
            raise DomainError(
                "ambiguous_page",
                "专用浏览器的 Vidu 页面显示了不同账号，请保留要使用的账号页面后再检查。",
            )
        if identified:
            return next(iter(identified.values()))
        # An explicit login UI gives a clearer status than a still-loading tab.
        return next((page for page, code in waiting if code == "login_required"), pages[0])

    @staticmethod
    async def login_required(page):
        return (
            await page.get_by_role("button", name="Close login dialog", exact=True).is_visible()
            or await page.get_by_role("button", name="登录", exact=True).is_visible()
        )

    @staticmethod
    async def identity(page, binding):
        if not official_page(page.url):
            raise DomainError("browser_origin_changed", "当前页面不是 Vidu 官方页面。")
        if await ViduBrowserProvider.login_required(page):
            raise DomainError("login_required", "请在专用浏览器中登录 Vidu；没有账号可在官网注册。")
        # Visible avatar handle only; never read hidden app state or credentials.
        handles = set(
            await page.locator("header img[alt], div.cursor-pointer > img[alt]").evaluate_all(
                "els => els.filter(el => el.getClientRects().length)"
                ".map(el => el.getAttribute('alt'))"
            )
        )
        handles = {s for s in handles if re.fullmatch(r"vidu_[A-Za-z0-9]{4,100}", s or "")}
        if len(handles) != 1:
            raise DomainError(
                "browser_identity_unverified",
                "暂时无法识别官网账号，请查看已打开的专用浏览器。"
                "检查不会刷新或新开登录页。",
            )
        label = handles.pop()
        if binding.expected_account and binding.expected_account != label:
            raise DomainError(
                "browser_account_changed", "官网账号与服务端绑定不一致，请切回原账号。"
            )
        return label

    async def session_status(self, owner):
        binding = self.bindings.get(owner)
        result = {
            "mode": self.mode,
            "status": "not_configured",
            "account_label": None,
            "handoff_url": binding.handoff_url if binding else None,
            "login_url": SUBSCRIPTION_URL,
            "login_must_use_bound_browser": True,
            "video_account_link_verified": False,
            "payment_verification_available": False,
            "message": "请由服务端为本用户配置独立浏览器；本机可运行 browser_session.py。",
        }
        if not binding:
            return result
        try:
            async with self.page(owner) as (page, binding):
                result["account_label"] = await self.identity(page, binding)
                proof = self.linked_account(owner) if self.linked_account else None
                result["video_account_link_verified"] = bool(
                    proof
                    and proof["account_ref"]
                    == account_reference(owner, binding, result["account_label"])
                )
                result.update(status="connected", message="官网会话已连接，可以读取真实套餐。")
        except DomainError as error:
            result.update(status=error.code, message=str(error))
        except Exception:
            result.update(
                status="browser_unavailable", message="官网页面尚不可读取，请检查专用浏览器。"
            )
        return result

    async def subscription_page(self, page, binding):
        """Navigate only for an explicit catalog/order operation, after authentication."""
        label = await self.identity(page, binding)
        if urlsplit(page.url).path != "/zh/subscription/personal":
            await page.goto(SUBSCRIPTION_URL, wait_until="domcontentloaded")
            if await self.identity(page, binding) != label:
                raise DomainError(
                    "browser_account_changed", "打开会员页时账号发生变化，请重新读取。"
                )
        return label

    @staticmethod
    async def dismiss_account_view(page):
        """Close only the read-only credential panel/account menu, never checkout."""
        title = page.get_by_text("Vidu Token", exact=True)
        dialog = page.get_by_role("dialog").filter(has=title)
        if await dialog.is_visible():
            await dialog.get_by_role("button", name="Close", exact=True).click()
            await dialog.wait_for(state="hidden", timeout=3000)
        menu = page.get_by_role("menu").filter(has=title)
        if await menu.is_visible():
            await menu.press("Escape")

    @staticmethod
    async def dismiss_order_view(page):
        heading = page.get_by_role("heading", name="购买记录", exact=True)
        if await heading.is_visible():
            dialog = page.get_by_role("dialog").filter(has=heading)
            # The wide fixed-width order dialog can put its X outside the viewport.
            # Verified on the official UI: Escape dismisses this read-only view.
            await dialog.press("Escape")
            await heading.wait_for(state="hidden", timeout=3000)

    @staticmethod
    async def monthly_cards(page):
        if await ViduBrowserProvider.login_required(page):
            raise DomainError("login_required", "请在专用浏览器登录后重新读取套餐。")
        if await page.get_by_role("heading", name="订阅", exact=True).is_visible():
            raise DomainError("checkout_already_open", "请先处理已经打开的订阅确认页。")
        await ViduBrowserProvider.dismiss_account_view(page)
        # Returning to plans may follow a read-only order query. Close that view,
        # while leaving subscription review/payment dialogs alone.
        await ViduBrowserProvider.dismiss_order_view(page)
        step = "打开套餐分页"
        try:
            # The wide layout adds a visible discount badge to the button's
            # accessible name. Its inner label is stable across viewport sizes.
            monthly = page.get_by_text("包月套餐", exact=True)
            if not await monthly.is_visible():
                await page.get_by_text("套餐", exact=True).click()
            step = "切换包月套餐"
            await monthly.click()
            step = "读取月付套餐卡片"
            buttons = page.get_by_role("button", name="立即订阅", exact=True)
            await buttons.first.wait_for(state="visible")
            texts = await buttons.evaluate_all(
                "els => els.map(el => el.parentElement.parentElement.innerText)"
            )
        except BrowserError as error:
            # Emit only fixed diagnostic categories, never raw DOM/URLs/credentials.
            detail = str(error)
            if "intercepts pointer events" in detail:
                reason = "官网弹窗遮挡了套餐控件，请先在原窗口关闭遮挡的弹窗。"
            elif "strict mode violation" in detail:
                reason = "官网出现多个同名入口，需要更新页面适配。"
            else:
                reason = "官网控件尚未可操作，请检查原窗口的页面状态。"
            raise DomainError("catalog_controls_unavailable", f"{step}未完成。{reason}") from None
        if not texts:
            raise DomainError("catalog_unavailable", "官网没有返回可购买的月付套餐。")
        plans = [parse_monthly_card(text) for text in texts]
        if len({p.plan_id for p in plans}) != len(plans):
            raise DomainError("catalog_changed", "官网返回重复套餐，无法确认当前档位。")
        if any(p.renewal_amount_minor is None for p in plans):
            raise DomainError("renewal_unverified", "官网未明确显示月付续费价格，请稍后重新读取。")
        return plans

    async def list_plans(self, owner: str) -> list[Plan]:
        async with self.page(owner) as (page, binding):
            label = await self.subscription_page(page, binding)
            plans = await self.monthly_cards(page)
            if await self.identity(page, binding) != label:
                raise DomainError(
                    "browser_account_changed", "读取套餐期间账号发生变化，请重新读取。"
                )
            reference = account_reference(owner, binding, label)
            return [
                plan.model_copy(update={"account_ref": reference, "account_label": label})
                for plan in plans
            ]

    async def verify_purchase_identity(self, page, binding, purchase):
        label = await self.identity(page, binding)
        if purchase.quote.plan.account_ref != account_reference(purchase.owner, binding, label):
            raise DomainError(
                "browser_account_changed", "官网账号或绑定已改变，不能使用原报价或订单。"
            )

    async def prepare_checkout(self, purchase: Purchase) -> Checkout:
        async with self.page(purchase.owner) as (page, binding):
            if await self.login_required(page):
                return Checkout(
                    state=PurchaseState.LOGIN_REQUIRED,
                    message="请在同一浏览器重新登录，然后重新读取报价并选择套餐。",
                    handoff_url=binding.handoff_url,
                )
            await self.verify_purchase_identity(page, binding, purchase)
            if purchase.quote.plan.name not in CHECKOUT_PRODUCTS:
                raise DomainError(
                    "unverified_checkout_product", "此套餐支付页尚未完成验证，暂不可下单。"
                )
            # A different request key cannot bypass an already-open checkout.
            for candidate in page.context.pages:
                try:
                    stripe_reference(candidate.url)
                except DomainError:
                    continue
                raise DomainError(
                    "checkout_already_open", "已有官方支付页，请先处理原购买，勿重复下单。"
                )
            await self.subscription_page(page, binding)
            await self.verify_purchase_identity(page, binding, purchase)
            plans = await self.monthly_cards(page)
            matches = [
                (index, plan)
                for index, plan in enumerate(plans)
                if plan.plan_id == purchase.quote.plan.plan_id
            ]
            if len(matches) != 1 or not same_price(matches[0][1], purchase.quote.plan):
                return Checkout(
                    state=PurchaseState.PRICE_CHANGED,
                    message="官网价格、续费价或套餐档位已变化，请重新比较套餐。",
                )
            await self.verify_purchase_identity(page, binding, purchase)
            await page.get_by_role("button", name="立即订阅", exact=True).nth(matches[0][0]).click()
            try:
                await page.get_by_role("heading", name="订阅", exact=True).wait_for(
                    state="visible", timeout=8000
                )
            except BrowserTimeout:
                if await self.login_required(page):
                    return Checkout(
                        state=PurchaseState.LOGIN_REQUIRED,
                        message="官网要求重新登录；登录后重新读取报价。",
                        handoff_url=binding.handoff_url,
                    )
                raise DomainError(
                    "checkout_review_unavailable", "未识别官网订阅确认页，已停止下单。"
                ) from None
            review = await self.review_checkout(page, binding, purchase)
            if review.state != PurchaseState.AWAITING_PAYMENT:
                return review
            await self.verify_purchase_identity(page, binding, purchase)
            dialog = page.get_by_role("dialog").filter(
                has=page.get_by_role("heading", name="订阅", exact=True)
            )
            # Verified live: this opens Stripe; the user alone confirms payment there.
            async with page.expect_popup(timeout=15000) as popup:
                await dialog.get_by_role("button", name="立即订阅", exact=True).click()
            payment_page = await popup.value
            await payment_page.wait_for_load_state("domcontentloaded", timeout=15000)
            reference = stripe_reference(payment_page.url)
            try:
                await payment_page.get_by_role(
                    "heading",
                    name=f"订阅 {CHECKOUT_PRODUCTS[purchase.quote.plan.name]}",
                    exact=True,
                ).wait_for(state="visible", timeout=15000)
                validate_stripe_text(
                    await payment_page.locator("body").inner_text(), purchase.quote.plan
                )
                await self.verify_purchase_identity(page, binding, purchase)
            except Exception:
                return Checkout(
                    state=PurchaseState.VERIFICATION_REQUIRED,
                    message="已创建支付会话，但未完成金额或账号核对；请勿支付或重复下单。",
                    provider_reference=reference,
                )
            return Checkout(
                state=PurchaseState.AWAITING_PAYMENT,
                message=(
                    "已核对官方支付页的商品、首期和续费价格，请用户自行确认税费与支付。"
                    "本版本的到账核验尚未完成。"
                ),
                handoff_url=payment_page.url,
                provider_reference=reference,
            )

    @staticmethod
    async def review_checkout(page, binding, purchase):
        dialog = page.get_by_role("dialog").filter(
            has=page.get_by_role("heading", name="订阅", exact=True)
        )
        # The live review starts with a spinner and a disabled Subscribe button.
        for _ in range(20):
            body = await dialog.inner_text()
            amount = re.search(r"实付金额\s*\$\s*([\d,]+(?:\.\d{1,2})?)", body)
            enabled = await dialog.get_by_role("button", name="立即订阅", exact=True).is_enabled()
            if amount and enabled:
                break
            await asyncio.sleep(0.25)
        if not amount or not enabled:
            return Checkout(
                state=PurchaseState.BLOCKED,
                message="官网实付金额尚未加载，未创建支付会话。请稍后重新读取报价。",
            )
        if minor_units(amount.group(1)) != purchase.quote.plan.amount_minor:
            return Checkout(
                state=PurchaseState.PRICE_CHANGED,
                message="官网实付金额与报价不同，请重新比较套餐。",
            )
        return Checkout(state=PurchaseState.AWAITING_PAYMENT, message="官网实付金额已核对。")

    async def check_receipt(self, purchase: Purchase) -> Checkout:
        async with self.page(purchase.owner) as (page, binding):
            if await self.login_required(page):
                return Checkout(
                    state=PurchaseState.LOGIN_REQUIRED,
                    message="请在原浏览器重新登录后查询同一购买记录，勿重复购买。",
                    handoff_url=binding.handoff_url,
                    provider_reference=purchase.provider_reference,
                )
            await self.verify_purchase_identity(page, binding, purchase)
            if purchase.provider_reference:
                for candidate in page.context.pages:
                    try:
                        reference = stripe_reference(candidate.url)
                    except DomainError:
                        continue
                    if reference != purchase.provider_reference:
                        continue
                    text = await candidate.locator("body").inner_text()
                    product = CHECKOUT_PRODUCTS.get(purchase.quote.plan.name)
                    if product and f"订阅 {product}" in text:
                        validate_stripe_text(text, purchase.quote.plan)
                        return Checkout(
                            state=PurchaseState.AWAITING_PAYMENT,
                            message="官方支付页仍显示待确认，尚未核验付款及权益。",
                            handoff_url=candidate.url,
                            provider_reference=reference,
                        )
            return Checkout(
                state=PurchaseState.VERIFICATION_REQUIRED,
                message="当前无法核实此支付会话对应的已付订单及权益。请查询官网订单，勿重复购买；不会自动生成视频。",
                provider_reference=purchase.provider_reference,
            )

    @staticmethod
    async def read_order_rows(dialog):
        for _ in range(20):
            rows = await dialog.locator("table tr").evaluate_all(
                "rows => rows.map(row => [...row.querySelectorAll('th,td')]"
                ".map(c => c.innerText.trim()))"
            )
            if rows and tuple(rows[0]) != ORDER_HEADERS:
                raise DomainError("order_layout_changed", "官网订单列发生变化，暂时无法可靠读取。")
            # An empty/spinner row is not evidence of an empty order history.
            if len(rows) >= 2 and all(any(cell for cell in row) for row in rows[1:]):
                if any(len(row) != len(ORDER_HEADERS) and row != ["暂无数据"] for row in rows[1:]):
                    raise DomainError("order_layout_changed", "官网订单内容未完整加载。")
                return rows
            await asyncio.sleep(0.25)
        raise DomainError("order_loading", "官网购买记录仍在加载，请稍后查询。")

    async def order_observation(self, owner):
        """Visible records are observations, never paid receipts inferred from a row."""
        async with self.page(owner) as (page, binding):
            label = await self.subscription_page(page, binding)
            if await page.get_by_role("heading", name="订阅", exact=True).is_visible():
                raise DomainError(
                    "checkout_already_open", "请先处理官网订阅确认页，再查看购买记录。"
                )
            heading = page.get_by_role("heading", name="购买记录", exact=True)
            already_open = await heading.is_visible()
            if not already_open:
                await page.get_by_role("button", name="我的订单", exact=True).click()
                await heading.wait_for(state="visible")
            dialog = page.get_by_role("dialog").filter(has=heading)
            try:
                rows = await self.read_order_rows(dialog)
            finally:
                if not already_open:
                    try:
                        await self.dismiss_order_view(page)
                    except BrowserError:
                        pass  # Do not replace a useful read error with a cleanup timeout.
            orders = [
                dict(zip(ORDER_HEADERS, row, strict=True))
                for row in rows[1:]
                if len(row) == len(ORDER_HEADERS)
            ]
            if await self.identity(page, binding) != label:
                raise DomainError("browser_account_changed", "查询期间账号发生变化，请重试查询。")
            return {
                "mode": self.mode,
                "orders": orders,
                "source": "vidu_visible_order_table",
                "payment_verified": False,
                "entitlement_verified": False,
                "message": (
                    "官网可见购买记录；尚无已验证的支付状态及支付会话与订单对应关系，"
                    "不能据此宣布到账。"
                ),
            }
