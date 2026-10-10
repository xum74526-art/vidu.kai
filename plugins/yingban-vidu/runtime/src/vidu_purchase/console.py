"""A local browser console with one-use launch tickets and server-held credentials."""

import secrets
import time
from pathlib import Path

from pydantic import Field, ValidationError, field_validator
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from vidu_purchase.http_limits import domain_status
from vidu_purchase.models import DomainError, Record
from vidu_purchase.video_provider import AccountBinding, WebsiteVideoProvider

STATIC = Path(__file__).parent / "static"
COOKIE = "yingban_console"
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; frame-src 'self'; media-src https:; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
}


class CredentialInput(Record):
    region: str = Field(pattern="^(global|cn)$")
    token: str = Field(min_length=1, max_length=2048)

    @field_validator("token")
    @classmethod
    def valid_token(cls, value):
        value = value.strip()
        if not value or not value.isascii() or any(char.isspace() for char in value):
            raise ValueError("Invalid token")
        return value


class TicketInput(Record):
    ticket: str = Field(min_length=32, max_length=128)


class BrowserAuthorizationInput(Record):
    authorize: bool = Field(strict=True)

    @field_validator("authorize")
    @classmethod
    def consent_required(cls, value):
        if value is not True:
            raise ValueError("Explicit authorization required")
        return value


class DesktopPanelInput(Record):
    binding_revision: str = Field(pattern=r"^[a-f0-9]{32}$")


def public_routes():
    async def index(_request):
        return FileResponse(STATIC / "index.html", headers=HEADERS)

    async def asset(request):
        name = request.path_params["name"]
        if name not in {
            "console.css", "console.js", "icon.svg", "panel.js", "panel.css", "panel-input.js",
        }:
            return JSONResponse({"error": "not_found"}, 404, headers=HEADERS)
        return FileResponse(STATIC / name, headers=HEADERS)

    return [Route("/", index), Route("/assets/{name}", asset)]


class LocalConsole:
    def __init__(
        self, api, verifier, origin, *, vault=None, validator=None,
        connections=None, clock=time.monotonic,
    ):
        self.api, self.verifier, self.origin = api, verifier, origin.rstrip("/")
        self.vault, self.clock = vault, clock
        self.validator = validator or self.validate_credential
        self.connections = connections
        self.tickets, self.sessions = {}, {}

    @staticmethod
    async def validate_credential(token, region):
        binding = AccountBinding(token_env="VIDU_ACCOUNT_CONSOLE_TOKEN", region=region)
        provider = WebsiteVideoProvider({}, credentials=lambda _: (binding, token))
        account = await provider.account("credential-validation")
        if account.status != "connected":
            raise DomainError("login_required", "Token 无效或已过期，请从官网重新获取。")
        return account

    def prune(self):
        for records in (self.tickets, self.sessions):
            for key in list(records):
                if records[key]["expires"] <= self.clock():
                    del records[key]

    def same_origin(self, request):
        if (
            request.headers.get("x-yingban-console") != "1"
            or request.headers.get("origin") not in {None, self.origin}
            or request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}
        ):
            raise DomainError("console_origin_required", "请在本机控制页中操作。")

    def session(self, request, *, csrf=True):
        self.prune()
        self.same_origin(request)
        session = self.sessions.get(request.cookies.get(COOKIE, ""))
        if not session:
            raise DomainError("console_login_required", "请从启动窗口打开一次控制页。")
        token = session["access"]
        if token.expires_at is not None and token.expires_at <= time.time():
            raise DomainError("console_login_required", "本机控制页授权已过期。")
        if csrf and not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), session["csrf"]
        ):
            raise DomainError("console_csrf_required", "页面授权已更新，请刷新后重试。")
        return session

    def info(self, session):
        return {
            "csrf": session["csrf"], "account_connection_available": self.vault is not None,
            "browser_connection_available": bool(self.connections and self.connections.available),
        }

    async def link(self, request):
        self.prune()
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise DomainError("console_login_required", "需要本机服务授权。")
        access = await self.verifier.verify_token(auth[7:])
        return JSONResponse({"url": self.issue_link(access)}, headers=HEADERS)

    async def desktop_panel(self, request):
        self.same_origin(request)
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise DomainError("console_login_required", "需要本机服务授权。")
        access = await self.verifier.verify_token(authorization[7:])
        if (
            access is None or (access.subject or access.claims.get("sub")) != "local-user"
            or not {"vidu:video", "vidu:purchase"}.issubset(access.scopes)
        ):
            raise DomainError("console_login_required", "需要本机服务授权。")
        body = DesktopPanelInput.model_validate_json(await request.body())
        if not self.vault or body.binding_revision != self.vault.owner():
            raise DomainError("account_binding_changed", "插件和面板的账号存储不一致。")
        from vidu_purchase.browser_panel import PanelRequest

        result = await self.api.panels.issue("local-user", access, PanelRequest())
        result["binding_revision"] = body.binding_revision
        return JSONResponse(result, headers=HEADERS)

    def issue_link(self, access, *, account_view=False):
        self.prune()
        if (
            access is None
            or (access.subject or access.claims.get("sub")) != "local-user"
            or not {"vidu:video", "vidu:purchase"}.issubset(access.scopes)
        ):
            raise DomainError("console_login_required", "需要本机服务授权。")
        if len(self.tickets) >= 16:
            raise DomainError("console_link_limit", "请使用已有入口或稍后再试。")
        ticket = secrets.token_urlsafe(32)
        self.tickets[ticket] = {"access": access, "expires": self.clock() + 300}
        return self.origin + "/#connect=" + ticket + ("&view=account" if account_view else "")

    async def exchange(self, request):
        self.prune()
        self.same_origin(request)
        body = TicketInput.model_validate_json(await request.body())
        saved = self.tickets.pop(body.ticket, None)
        if not saved:
            raise DomainError("console_login_required", "入口已使用或过期，请重新打开控制页。")
        # Bound memory; sessions expire, and each new launch replaces its prior browser session.
        old = request.cookies.get(COOKIE)
        self.sessions.pop(old, None)
        if len(self.sessions) >= 32:
            raise DomainError("console_link_limit", "已打开过多控制页，请稍后再试。")
        sid = secrets.token_urlsafe(32)
        session = {**saved, "expires": self.clock() + 8 * 3600, "csrf": secrets.token_urlsafe(32)}
        self.sessions[sid] = session
        response = JSONResponse(self.info(session), headers=HEADERS)
        response.set_cookie(
            COOKIE,
            sid,
            max_age=8 * 3600,
            httponly=True,
            samesite="strict",
            secure=self.origin.startswith("https://"),
        )
        return response

    async def session_info(self, request):
        return JSONResponse(self.info(self.session(request, csrf=False)), headers=HEADERS)

    async def account(self, request):
        self.session(request)
        if not self.vault:
            raise DomainError("connection_managed", "这个账号由服务管理员连接。")
        if request.method == "DELETE":
            self.vault.disconnect()
            return JSONResponse({"status": "disconnected"}, headers=HEADERS)
        body = CredentialInput.model_validate_json(await request.body())
        owner = self.vault.owner()
        if self.vault.credential(owner) is not None:
            raise DomainError("connection_exists", "账号已连接。更换账号前请先断开。")
        snapshot = await self.validator(body.token, body.region)
        self.vault.save(owner, body.token, body.region)
        return JSONResponse(snapshot.model_dump(), headers=HEADERS)

    async def browser_account(self, request):
        self.session(request)
        if not self.connections:
            raise DomainError("account_connection_unavailable", "未配置官网连接入口。")
        if request.url.path.endswith("/open"):
            result = await self.connections.show_browser("local-user")
        else:
            BrowserAuthorizationInput.model_validate_json(await request.body())
            result = await self.connections.authorize("local-user", self.validator)
        return JSONResponse(result, headers=HEADERS)

    async def forward(self, request, name):
        session = self.session(request)
        access = session["access"]
        request.scope.setdefault("state", {}).update(owner="local-user", access_token=access)
        request.scope["route_name"] = name
        return await self.api.handle(request)

    @staticmethod
    def safe(handler):
        async def endpoint(request):
            try:
                return await handler(request)
            except DomainError as error:
                status = (
                    401
                    if error.code == "console_login_required"
                    else 403
                    if error.code.startswith("console_")
                    else domain_status(error.code)
                )
                return JSONResponse(
                    {"error": {"code": error.code, "message": str(error)}}, status, headers=HEADERS
                )
            except (ValidationError, ValueError):
                return JSONResponse(
                    {"error": {"code": "invalid_input", "message": "请检查填写内容。"}},
                    422,
                    headers=HEADERS,
                )
            except Exception:
                return JSONResponse(
                    {
                        "error": {
                            "code": "service_unavailable",
                            "message": "暂时无法确认结果，请稍后查询原请求。",
                        }
                    },
                    503,
                    headers=HEADERS,
                )

        return endpoint

    def routes(self, operations):
        routes = [
            Route("/console/link", self.safe(self.link), methods=["POST"]),
            Route("/console/desktop-panel", self.safe(self.desktop_panel), methods=["POST"]),
            Route("/console/session", self.safe(self.exchange), methods=["POST"]),
            Route("/console/session", self.safe(self.session_info), methods=["GET"]),
            Route("/console/account", self.safe(self.account), methods=["POST", "DELETE"]),
            Route(
                "/console/account/browser/open", self.safe(self.browser_account), methods=["POST"]
            ),
            Route("/console/account/browser", self.safe(self.browser_account), methods=["POST"]),
        ]
        for method, path, name, _ in operations:

            async def forward(request, operation=name):
                return await self.forward(request, operation)

            routes.append(Route("/console/api" + path, self.safe(forward), methods=[method]))
        return routes
