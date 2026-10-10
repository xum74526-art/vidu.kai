"""Authenticated REST and MCP for a self-hosted agent. No demo runtime or fake receipts."""

import argparse
import asyncio
import json
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier, StaticTokenVerifier
from pydantic import AnyHttpUrl, Field, ValidationError
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route

from vidu_purchase.account_connection import AccountConnectionService
from vidu_purchase.billing_gateway import BillingUnavailable, HttpCommerceGateway, load_billing
from vidu_purchase.brief import BriefService
from vidu_purchase.brief_models import BriefAnswers, BriefInput
from vidu_purchase.browser import ViduBrowserProvider, read_bindings
from vidu_purchase.browser_panel import BrowserPanelService, PanelRequest
from vidu_purchase.console import LocalConsole, public_routes
from vidu_purchase.mcp_server import create_server
from vidu_purchase.models import DomainError, PurchaseState, Record
from vidu_purchase.remote_plugin import VERSION, installation_routes
from vidu_purchase.service import PurchaseService
from vidu_purchase.store import Store
from vidu_purchase.video_models import ExplicitVideoConfig
from vidu_purchase.video_provider import WebsiteVideoProvider, read_accounts
from vidu_purchase.workflow import WorkflowService

INSTRUCTIONS = """为自建智能体提供 Vidu 官网账号的视频和购买工作流。先查询能力。
浏览器购买模式先调用 vidu_commerce_session 检查专用浏览器登录；其他浏览器的登录不会自动同步。
quote.checkout_available=false 的套餐只能比较，不能下单。当前浏览器模式支持标准版和专业版月付。
官网浏览器和视频 Token 是两种授权；浏览器登录成功不能报告视频授权已连接。
宿主支持 MCP Apps 时，首次连接调用 vidu_open_official_panel(purpose="account")。
用户在对话内的官方面板中登录、注册并确认授权，不发送密码、验证码或 Token 到聊天。
付款使用同一工具 purpose="payment" 并传原 purchase_id；不能传网址、账号或付款结果。
工具的 _meta 只交给宿主界面，不放入模型上下文。面板关闭不是支付成功。
只支持基础 MCP 的宿主可用 vidu_connect_account 提供独立连接入口。
授权由用户在连接窗口确认，工具不接收 Token。
之后用 vidu_connection_status 查询；account_link_verified=true 才表示官网与视频授权已经关联。
连接成功只通知可以继续配置，不能自动提交视频。官网直接连接需要每用户独立浏览器和原生凭据库。
vidu_website_orders 只返回官网可见记录，payment_verified=false 时禁止宣称付款到账。
real_payment_verification_available=false 时，在用户选择购买前说明尚不能自动核验到账。
只展示服务实时返回的套餐、首期价格、续费条件和积分，禁止编造缺少的套餐或结账链接。
未绑定账号不等于没有账号，当前不支持自动注册，不能通过 MCP 接收密码或 Token。
用户选择套餐后创建购买，重试复用同一 idempotency_key；付款由用户在真实支付页确认。
订单成功必须通过服务端查询可信订单及权益，用户说已付款、浏览器回跳或余额增加都不能代替核验。
付款后通知可以使用，再询问是否继续生成；用户明确回复后才提交已有视频配置。
参数对话通过 vidu_prepare_video_request 与 vidu_answer_video_questions 完成。
只有已明确全部参数的表单或高级调用方使用 vidu_start_video_request，不能绕过参数询问。
关联视频使用 vidu_purchase_for_video 和 vidu_get_workflow 延续。
始终使用最新 confirmation_id 确认生成；费用变化要重新询问，提交结果不明不重复下单或生成。
计费未接入时准确说明，不能调用测试脚本、模拟支付或要求用户自报支付成功。
"""


class PurchaseInput(Record):
    quote_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class VideoInput(Record):
    config: ExplicitVideoConfig


class RevisionInput(Record):
    revision: int = Field(ge=0, strict=True)


class VideoUpdateInput(RevisionInput, VideoInput):
    pass


class VideoPurchaseInput(RevisionInput, PurchaseInput):
    pass


class ConfirmInput(Record):
    confirmation_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class AgentJWTVerifier(JWTVerifier):
    """Require exp/sub and respect nbf in addition to signature, issuer and audience."""

    async def load_access_token(self, token):
        access = await super().load_access_token(token)
        if access is None or access.expires_at is None or access.expires_at <= time.time():
            return None
        subject = access.subject or access.claims.get("sub")
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 512:
            return None
        nbf = access.claims.get("nbf")
        if nbf is not None and (
            type(nbf) not in {int, float} or not math.isfinite(nbf) or nbf > time.time()
        ):
            return None
        return access


def error_response(code, message, status):
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


def domain_status(code):
    if code == "insufficient_scope":
        return 403
    if code in {"purchase_not_found", "quote_not_found", "workflow_not_found", "brief_not_found"}:
        return 404
    if code == "billing_not_configured":
        return 501
    if code in {"invalid_input", "invalid_idempotency_key"}:
        return 422
    if code.startswith(("receipt_", "billing_invalid", "billing_upstream", "billing_response")):
        return 502
    if code.startswith(("vidu_", "estimate_", "credits_", "quota_")):
        return 502
    return 409


class ApiGuard:
    """REST auth; MCP uses the same verifier via its own SDK auth middleware."""

    def __init__(self, app, verifier):
        self.app, self.verifier = app, verifier

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/v1/"):
            return await self.app(scope, receive, send)
        authorization = dict(scope.get("headers", [])).get(b"authorization", b"")
        if authorization[:7].lower() != b"bearer " or len(authorization) > 16384:
            return await error_response("authentication_required", "请先登录自建智能体。", 401)(
                scope, receive, send
            )
        try:
            token = await self.verifier.verify_token(authorization[7:].decode("ascii"))
            owner = (token.subject or token.claims.get("sub")) if token else None
            if not isinstance(owner, str) or not owner.strip() or len(owner) > 512:
                raise ValueError()
            if token.expires_at is not None and token.expires_at <= time.time():
                raise ValueError()
        except Exception:
            return await error_response("invalid_token", "授权无效或已过期。", 401)(
                scope, receive, send
            )
        scope.setdefault("state", {}).update(owner=owner, access_token=token)
        started = False

        async def guarded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except DomainError as error:
            if started:
                raise
            await error_response(error.code, str(error), domain_status(error.code))(
                scope, receive, send
            )
        except ValidationError:
            if started:
                raise
            # Validation errors can contain rejected secret inputs; do not echo them.
            await error_response("invalid_input", "请求字段、参数类型或配置不正确。", 422)(
                scope, receive, send
            )
        except Exception:
            if started:
                raise
            await error_response(
                "service_unavailable", "无法可靠处理请求，请查询原请求后再决定下一步。", 503
            )(scope, receive, send)


class BodyLimit:
    def __init__(self, app, limit=65536):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PATCH", "PUT"}:
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                return await error_response("body_too_large", "请求超过 64 KiB。", 413)(
                    scope, receive, send
                )
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body, delivered = b"".join(chunks), False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


class AgentApi:
    def __init__(
        self, purchases, workflow, capabilities, *, briefs, connections=None, panels=None,
        poll_interval=3, stream_seconds=55
    ):
        self.purchases, self.workflow, self.capabilities = purchases, workflow, capabilities
        self.briefs = briefs
        self.connections = connections
        self.panels = panels
        self.poll_interval, self.stream_seconds = poll_interval, stream_seconds

    @staticmethod
    def require(request, *scopes):
        if not set(scopes).issubset(request.state.access_token.scopes):
            raise DomainError("insufficient_scope", "当前授权不允许调用此功能。")
        return request.state.owner

    @staticmethod
    async def body(request, model):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise DomainError("invalid_input", "请使用 application/json。")
        try:
            return model.model_validate_json(await request.body())
        except ValueError:
            raise DomainError("invalid_input", "请求字段、参数类型或配置不正确。") from None

    @staticmethod
    def request_key(request):
        key = request.headers.get("idempotency-key", "")
        if not 8 <= len(key) <= 128:
            raise DomainError("invalid_idempotency_key", "请提供 8–128 位的 Idempotency-Key。")
        return key

    async def handle(self, request):
        name = request.scope["route_name"]
        scopes = (
            ("vidu:purchase",)
            if name
            in {"plans", "purchase", "purchase_get", "events", "commerce_session", "website_orders"}
            else ("vidu:video",)
        )
        if name in {"recommend", "video_purchase", "account_connect", "connection_status", "panel"}:
            scopes = ("vidu:purchase", "vidu:video")
        if name in {"capabilities", "openapi"}:
            scopes = ()
        owner = self.require(request, *scopes)
        params = request.path_params
        workflow_id = params.get("workflow_id")
        status = 200
        if name == "capabilities":
            result = self.capabilities
        elif name == "openapi":
            result = self.openapi()
        elif name == "account":
            result = await self.workflow.account_status(owner)
        elif name == "connection_status":
            result = await self.connections.status(owner)
        elif name == "account_connect":
            result = await self.connections.begin(owner, request.state.access_token)
        elif name == "panel":
            body = await self.body(request, PanelRequest)
            result = await self.panels.issue(owner, request.state.access_token, body)
        elif name == "commerce_session":
            result = await self.purchases.commerce_session(owner)
        elif name == "website_orders":
            result = await self.purchases.website_orders(owner)
        elif name == "brief":
            body = await self.body(request, BriefInput)
            result = await self.briefs.start(owner, body, self.request_key(request))
            status = 201
        elif name == "brief_get":
            result = await self.briefs.get(owner, params["brief_id"])
        elif name == "brief_answer":
            body = await self.body(request, BriefAnswers)
            result = await self.briefs.answer(owner, params["brief_id"], body)
        elif name == "plans":
            result = await self.purchases.list_plans(owner)
        elif name == "purchase":
            body = await self.body(request, PurchaseInput)
            result = await self.purchases.prepare_purchase(
                owner, body.quote_id, self.request_key(request)
            )
            status = 201
        elif name in {"purchase_get", "events"}:
            result = await self.purchases.check_purchase(owner, params["purchase_id"])
            if name == "events":
                return self.events(request, owner, params["purchase_id"], result)
        elif name == "video":
            body = await self.body(request, VideoInput)
            result = await self.workflow.start(owner, body.config, self.request_key(request))
            status = 201
        elif name == "video_get":
            result = await self.workflow.get(owner, workflow_id)
        elif name == "video_update":
            body = await self.body(request, VideoUpdateInput)
            result = await self.workflow.update(owner, workflow_id, body.config, body.revision)
        elif name == "cancel":
            body = await self.body(request, RevisionInput)
            result = await self.workflow.cancel(owner, workflow_id, body.revision)
        elif name == "recommend":
            result = await self.workflow.recommend(owner, workflow_id)
        elif name == "video_purchase":
            body = await self.body(request, VideoPurchaseInput)
            result = await self.workflow.buy(owner, workflow_id, body.quote_id, body.revision)
        elif name == "confirm":
            body = await self.body(request, ConfirmInput)
            result = await self.workflow.confirm(owner, workflow_id, body.confirmation_id)
        else:
            raise DomainError("invalid_input", "不支持的操作。")
        return JSONResponse(result, status, headers={"Cache-Control": "no-store"})

    def events(self, request, owner, purchase_id, initial):
        async def stream():
            deadline = time.monotonic() + self.stream_seconds
            result, previous = initial, None
            while True:
                token = request.state.access_token
                if token.expires_at is not None and token.expires_at <= time.time():
                    yield 'event: auth_expired\ndata: {"code":"invalid_token"}\n\n'
                    return
                encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                if encoded != previous:
                    yield f"event: purchase\ndata: {encoded}\n\n"
                    previous = encoded
                else:
                    yield ": waiting\n\n"
                if result["state"] in {
                    PurchaseState.READY,
                    PurchaseState.BLOCKED,
                    PurchaseState.PRICE_CHANGED,
                }:
                    return
                if time.monotonic() >= deadline or await request.is_disconnected():
                    return
                await asyncio.sleep(self.poll_interval)
                if token.expires_at is not None and token.expires_at <= time.time():
                    yield 'event: auth_expired\ndata: {"code":"invalid_token"}\n\n'
                    return
                if await request.is_disconnected():
                    return
                try:
                    result = await self.purchases.check_purchase(owner, purchase_id)
                except Exception:
                    yield 'event: verification_unavailable\ndata: {"retry_same_purchase":true}\n\n'
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    def routes(self):
        routes = []
        for method, path, name, _model in ROUTES:

            async def endpoint(request, route_name=name):
                request.scope["route_name"] = route_name
                return await self.handle(request)

            routes.append(Route(path, endpoint, methods=[method], name=name))
        return routes

    def openapi(self):
        paths, schemas = {}, {}
        for method, path, name, model in ROUTES:
            operation = {
                "operationId": name,
                "security": [{"bearerAuth": []}],
                "responses": {
                    "200": {"description": "Current state; never implies automatic generation"},
                    "401": {"description": "Authentication required"},
                    "403": {"description": "Required scope is missing"},
                    "404": {"description": "Record does not belong to this user or is absent"},
                    "409": {"description": "State, revision or idempotency conflict"},
                    "422": {"description": "Invalid request input"},
                    "501": {"description": "Real Vidu billing integration not configured"},
                    "502": {"description": "Upstream response cannot be verified"},
                    "503": {"description": "Service unavailable; check the same request"},
                },
            }
            if name == "events":
                operation["responses"]["200"].update(
                    content={"text/event-stream": {"schema": {"type": "string"}}},
                    description="Bounded purchase snapshot stream; reconnect the same ID",
                )
            parameters = []
            for parameter in ("purchase_id", "workflow_id", "brief_id"):
                if "{" + parameter + "}" in path:
                    parameters.append(
                        {
                            "name": parameter,
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    )
            if name in {"purchase", "video", "brief"}:
                parameters.append(
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string", "minLength": 8, "maxLength": 128},
                    }
                )
                operation["responses"]["201"] = {
                    "description": "Intent created or existing intent returned"
                }
            if parameters:
                operation["parameters"] = parameters
            if model:
                schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
                schemas.update(schema.pop("$defs", {}))
                schemas[model.__name__] = schema
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{model.__name__}"}
                        }
                    },
                }
            paths.setdefault(path, {})[method.lower()] = operation
        return {
            "openapi": "3.1.0",
            "info": {"title": "Yingban Self-hosted Agent API", "version": VERSION},
            "paths": paths,
            "components": {
                "schemas": schemas,
                "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            },
        }


ROUTES = [
    ("GET", "/v1/capabilities", "capabilities", None),
    ("GET", "/v1/openapi.json", "openapi", None),
    ("GET", "/v1/account", "account", None),
    ("GET", "/v1/account/connection", "connection_status", None),
    ("POST", "/v1/account/connection", "account_connect", None),
    ("POST", "/v1/browser-panels", "panel", PanelRequest),
    ("GET", "/v1/commerce/session", "commerce_session", None),
    ("GET", "/v1/commerce/orders", "website_orders", None),
    ("POST", "/v1/video-briefs", "brief", BriefInput),
    ("GET", "/v1/video-briefs/{brief_id}", "brief_get", None),
    ("PATCH", "/v1/video-briefs/{brief_id}", "brief_answer", BriefAnswers),
    ("GET", "/v1/plans", "plans", None),
    ("POST", "/v1/purchases", "purchase", PurchaseInput),
    ("GET", "/v1/purchases/{purchase_id}", "purchase_get", None),
    ("GET", "/v1/purchases/{purchase_id}/events", "events", None),
    ("POST", "/v1/videos", "video", VideoInput),
    ("GET", "/v1/videos/{workflow_id}", "video_get", None),
    ("PATCH", "/v1/videos/{workflow_id}", "video_update", VideoUpdateInput),
    ("POST", "/v1/videos/{workflow_id}/cancel", "cancel", RevisionInput),
    ("POST", "/v1/videos/{workflow_id}/recommend", "recommend", None),
    ("POST", "/v1/videos/{workflow_id}/purchase", "video_purchase", VideoPurchaseInput),
    ("POST", "/v1/videos/{workflow_id}/confirm", "confirm", ConfirmInput),
]


def create_hosted_app(
    *,
    store: Store,
    verifier,
    video,
    billing=None,
    public_url="http://127.0.0.1:8787",
    cors_origins=(),
    poll_interval=3,
    stream_seconds=55,
    local_console=False,
    console_vault=None,
    panel_origins=(),
    account_vaults=None,
    authorization_servers=(),
):
    url = urlsplit(public_url)
    if (
        not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise ValueError("Use a public origin without credentials, path, query or fragment")
    if url.scheme != "https" and not (
        url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("Remote clients require a public HTTPS origin")
    if verifier is None:
        raise ValueError("Self-hosted HTTP always requires a token verifier")
    if local_console and url.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("The local console cannot be exposed to remote users")
    if console_vault and account_vaults:
        raise ValueError("Choose single-user or per-user credentials, not both")
    for issuer in authorization_servers:
        issuer_url = urlsplit(issuer)
        if (issuer_url.scheme != "https" or not issuer_url.hostname
                or issuer_url.username or issuer_url.password
                or issuer_url.query or issuer_url.fragment):
            raise ValueError("Authorization servers require HTTPS without credentials")
    mcp_auth = (
        RemoteAuthProvider(
            token_verifier=verifier,
            authorization_servers=[AnyHttpUrl(value) for value in authorization_servers],
            base_url=public_url.rstrip("/"),
            scopes_supported=["vidu:video", "vidu:purchase"],
            resource_name="Yingban Vidu",
        ) if authorization_servers else verifier
    )
    billing = billing or BillingUnavailable()
    if billing.mode == "demo" or video.mode == "demo":
        raise ValueError("The self-hosted runtime does not support demo providers")
    gateway = isinstance(billing, HttpCommerceGateway)
    browser = isinstance(billing, ViduBrowserProvider)
    configured = gateway or browser
    connections = AccountConnectionService(
        video, browser=billing if browser else None,
        vault=console_vault if local_console else None,
        vaults=account_vaults,
    )
    if browser and local_console and console_vault:
        billing.linked_account = (
            lambda owner: console_vault.browser_link(console_vault.owner())
            if owner == "local-user" else None
        )
    elif browser and account_vaults:
        billing.linked_account = account_vaults.browser_link
    capabilities = {
        "version": VERSION,
        "distribution": "self_hosted",
        "commerce_enabled": True,
        "billing_adapter_configured": configured,
        "billing_mode": billing.mode,
        "billing_contract": "yingban-commerce-gateway-v1" if gateway else None,
        "browser_commerce_available": browser,
        "website_order_observation_available": browser,
        "supported_browser_checkouts": ["standard_monthly", "premium_monthly"] if browser else [],
        "top_up_checkout_available": False,
        "vidu_public_billing_api_available": False,
        "real_payment_verification_available": gateway,
        "embedded_payment_available": False,
        "payment_confirmation_by": "user",
        "account_registration_available": False,
        "account_connection_handoff_available": bool(local_console and console_vault),
        "browser_account_connection_available": connections.available,
        "workflow_available": True,
        "video_brief_available": True,
        "configuration_policy": "ask_missing_then_use_explicitly_delegated_recommendations",
        "video_mode": video.mode,
        "account_status_check_available": True,
        "video_generation_available": video.generation_enabled,
        "real_video_generation_enabled": video.generation_enabled,
        "auto_generate": False,
        "post_payment_behavior": "notify_then_ask_generation_confirmation",
        "message": (
            "已接入官网浏览器购买方式；先检查专用浏览器登录。支付到账核验尚未完成。"
            if browser
            else (
                "已配置自建计费网关；真实业务是否可用需验证上游 Vidu 接入。"
                if gateway
                else "未接入真实官网计费服务，套餐及下单将返回 billing_not_configured。"
            )
        ),
    }
    service = PurchaseService(store, billing)
    workflow = WorkflowService(
        service,
        video,
        always_confirm_generation=True,
        binding_revision=(
            (lambda _owner: console_vault.owner()) if console_vault
            else account_vaults.revision if account_vaults else None
        ),
    )
    briefs = BriefService(workflow)
    panels = BrowserPanelService(
        billing if browser else None, store, connections, public_url, frame_origins=panel_origins,
    )
    capabilities.update(
        official_browser_panel_available=browser,
        official_browser_panel_transport="page_pixels_and_user_input" if browser else None,
        mcp_apps_available=True,
    )
    mcp = create_server(
        service,
        auth=mcp_auth,
        workflow=workflow,
        briefs=briefs,
        name="映伴 · 自建智能体服务",
        instructions=INSTRUCTIONS,
        capabilities=capabilities,
        browser_tools=True,
        connections=connections,
        panels=panels,
    )
    api = AgentApi(
        service,
        workflow,
        capabilities,
        briefs=briefs,
        connections=connections,
        panels=panels,
        poll_interval=poll_interval,
        stream_seconds=stream_seconds,
    )
    mcp_app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    console = (
        LocalConsole(api, verifier, public_url, vault=console_vault, connections=connections)
        if local_console else None
    )
    if console:
        connections.launch = console.issue_link

    async def health(_request):
        return JSONResponse({"status": "ok", "service": "yingban-selfhost", "version": VERSION})

    @asynccontextmanager
    async def lifespan(application):
        async def reap_panels():
            while True:
                await asyncio.sleep(30)
                await panels.prune()

        async with mcp_app.lifespan(application):
            reaper = asyncio.create_task(reap_panels())
            try:
                yield
            finally:
                reaper.cancel()
                await asyncio.gather(reaper, return_exceptions=True)
                await panels.close()

    app = Starlette(
        routes=[
            *public_routes(),
            *panels.routes(),
            Route("/healthz", health),
            *installation_routes(
                public_url, authorization_servers=authorization_servers,
                browser_accounts=bool(browser and account_vaults),
                generation_enabled=video.generation_enabled,
            ),
            *api.routes(),
            *(console.routes(ROUTES) if console else []),
            Mount("/", mcp_app),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(ApiGuard, verifier=verifier)
    app.add_middleware(BodyLimit)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[url.hostname, "127.0.0.1", "localhost"]
    )
    if cors_origins:
        for origin in cors_origins:
            parsed = urlsplit(origin)
            if (
                "*" in origin
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError("CORS requires exact HTTPS origins without paths or credentials")
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "MCP-Protocol-Version",
                "MCP-Session-Id",
            ],
            expose_headers=["MCP-Session-Id", "WWW-Authenticate"],
            allow_credentials=False,
        )
    app.state.agent_api = api
    app.state.console = console
    app.state.panels = panels
    return app


def main():
    parser = argparse.ArgumentParser(description="映伴自建智能体 HTTP/MCP 服务（真实接口入口）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--public-url")
    parser.add_argument("--db", type=Path, default=Path(".local/selfhost.sqlite3"))
    parser.add_argument("--accounts", type=Path, default=Path(".local/accounts.json"))
    commerce = parser.add_mutually_exclusive_group()
    commerce.add_argument("--billing-config", type=Path)
    commerce.add_argument("--browser-bindings", type=Path)
    parser.add_argument("--enable-live-generation", action="store_true")
    parser.add_argument("--local-console", action="store_true")
    parser.add_argument("--local-account-dir", type=Path)
    parser.add_argument("--cors-origin", action="append", default=[])
    parser.add_argument("--panel-origin", action="append", default=[])
    parser.add_argument("--account-vault-root", type=Path)
    args = parser.parse_args()
    local = args.host in {"127.0.0.1", "localhost", "::1"}
    jwks, issuer, audience = (
        os.getenv(key) for key in ("VIDU_JWKS_URI", "VIDU_TOKEN_ISSUER", "VIDU_TOKEN_AUDIENCE")
    )
    if all((jwks, issuer, audience)):
        if not jwks.startswith("https://") or not issuer.startswith("https://"):
            parser.error("JWT issuer and JWKS must use HTTPS")
        if args.public_url and audience != args.public_url.rstrip("/") + "/mcp":
            parser.error("VIDU_TOKEN_AUDIENCE must match the public MCP URL ending in /mcp")
        verifier = AgentJWTVerifier(
            jwks_uri=jwks, issuer=issuer, audience=audience, required_scopes=[]
        )
    elif any((jwks, issuer, audience)):
        parser.error("JWT configuration requires JWKS, issuer and audience together")
    else:
        token = os.getenv("VIDU_SELFHOST_TOKEN", "")
        if not local or len(token) < 32:
            parser.error("Set JWT configuration, or use loopback + VIDU_SELFHOST_TOKEN (32+ chars)")
        if args.public_url and urlsplit(args.public_url).hostname not in {
            "127.0.0.1", "localhost", "::1",
        }:
            parser.error("A public service requires per-user JWT authentication")
        verifier = StaticTokenVerifier(
            tokens={
                token: {
                    "client_id": "selfhost-local",
                    "sub": "local-user",
                    "scopes": ["vidu:purchase", "vidu:video"],
                }
            }
        )
    if not local and not args.public_url:
        parser.error("Remote deployment requires --public-url https://your-host")
    if args.local_console and (not local or any((jwks, issuer, audience))):
        parser.error("Local console requires loopback and local single-user authentication")
    if args.local_account_dir and (
        not args.local_console or args.account_vault_root or args.billing_config
    ):
        parser.error("A shared desktop account requires the local single-user console")
    try:
        accounts = read_accounts(args.accounts) if args.accounts.exists() else {}
        if args.local_account_dir and accounts:
            raise ValueError("A shared desktop account cannot use manual account mappings")
        billing = (
            ViduBrowserProvider(read_bindings(args.browser_bindings))
            if args.browser_bindings
            else load_billing(args.billing_config)
        )
        vault = None
        vaults = None
        credentials = None
        if args.account_vault_root:
            from vidu_purchase.credentials import AccountVaults, native_keyring

            if args.local_console or accounts or not isinstance(billing, ViduBrowserProvider):
                raise ValueError(
                    "Per-user binding requires browser mappings and no manual accounts"
                )
            # Select only native secret storage, never a plaintext fallback.
            backend = native_keyring()
            vaults = AccountVaults(args.account_vault_root, billing.bindings, backend=backend)
            credentials = vaults.credential
        if args.local_console and not accounts and args.billing_config is None:
            from vidu_purchase.credentials import CredentialVault

            vault = CredentialVault(args.local_account_dir or args.db.parent / "console-account")

            def credentials(owner):
                return vault.credential(vault.owner()) if owner == "local-user" else None

        if isinstance(billing, HttpCommerceGateway) and not set(
            billing.settings.account_refs
        ).issubset(accounts):
            raise ValueError("Billing users must also have explicit video account bindings")
        app = create_hosted_app(
            store=Store(args.db),
            verifier=verifier,
            video=WebsiteVideoProvider(
                accounts,
                generation_enabled=args.enable_live_generation,
                credentials=credentials,
            ),
            billing=billing,
            public_url=args.public_url or f"http://127.0.0.1:{args.port}",
            cors_origins=args.cors_origin,
            local_console=args.local_console,
            console_vault=vault,
            panel_origins=args.panel_origin,
            account_vaults=vaults,
            authorization_servers=[issuer] if jwks else [],
        )
    except Exception:
        parser.error("部署配置无效，请检查账号映射、计费配置和公开地址；没有启动模拟模式。")
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
