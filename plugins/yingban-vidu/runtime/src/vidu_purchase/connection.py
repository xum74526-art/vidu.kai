"""One-use loopback credential form. Secrets are not MCP arguments or tool results."""

import asyncio
import html
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from vidu_purchase.credentials import CredentialVault
from vidu_purchase.models import DomainError
from vidu_purchase.video_provider import AccountBinding, WebsiteVideoProvider

STYLE = """
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f3f2ee;
color:#202d2e;font:16px/1.65 system-ui,sans-serif}main{max-width:560px;margin:8vh auto;
padding:32px}h1{font-size:30px;line-height:1.3;letter-spacing:-.04em;margin:16px 0}
.mark{display:inline-grid;place-items:center;width:48px;height:48px;border-radius:14px;
background:#145c50;color:#fff;font-size:23px}.eyebrow{font-size:13px;color:#506663}
p{margin:16px 0}label{display:block;font-weight:600;margin:22px 0 8px}
input,select,button{font:inherit;width:100%;padding:12px;border-radius:9px;
border:1px solid #c3ceca;background:white}input:focus,select:focus{outline:2px solid #145c50}
button{margin-top:26px;background:#145c50;border:0;color:white;font-weight:600;cursor:pointer}
small{display:block;color:#596b66;font-size:13px;margin-top:20px}a{color:#145c50}
.notice{padding:12px 16px;background:#fff;border-left:3px solid #145c50}
"""


def page(body: str) -> bytes:
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>映伴 · 连接 Vidu</title><style>" + STYLE + "</style><main>"
        '<div class="mark" aria-hidden="true">▶</div><p class="eyebrow">映伴 / VIDU 账号连接</p>'
        + body
        + "</main></html>"
    ).encode()


class ConnectionManager:
    def __init__(self, vault: CredentialVault, *, validator=None, clock=time.monotonic):
        self.vault = vault
        self.validator = validator or self.validate
        self.clock = clock
        self.server = None
        self.thread = None
        self.lock = threading.RLock()
        self.nonce = None
        self.expires = 0.0
        self.owner = None
        self._origin = ""

    @staticmethod
    async def validate(token: str, region: str):
        binding = AccountBinding(token_env="VIDU_ACCOUNT_CONNECT_TOKEN", region=region)
        provider = WebsiteVideoProvider({}, credentials=lambda _: (binding, token))
        account = await provider.account("connect-validation")
        if account.status != "connected":
            raise DomainError("login_required", "Token 无效或已过期，请从官网重新获取。")
        return account

    @property
    def origin(self) -> str:
        return self._origin

    def begin(self) -> dict:
        with self.lock:
            current_owner = self.vault.owner()
            if self.vault.credential(current_owner):
                return {"status": "already_bound", "message": "已保存账号授权，请查询账号状态。"}
            if self.server is None:
                self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
                self._origin = f"http://127.0.0.1:{self.server.server_port}"
                self.server.daemon_threads = True
                self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.thread.start()
            if not self.nonce or self.clock() >= self.expires or self.owner != current_owner:
                self.nonce = secrets.token_urlsafe(32)
                self.expires = self.clock() + 900
                self.owner = current_owner
            return {
                "status": "user_action_required",
                "connection_url": f"{self.origin}/connect/{self.nonce}",
                "expires_in_seconds": max(0, int(self.expires - self.clock())),
                "message": "请在本机连接页输入官网 Vidu Token；不要发到聊天中。连接不生成视频。",
            }

    def close(self):
        with self.lock:
            self.nonce = None
            server, self.server = self.server, None
        if server:
            server.shutdown()
            server.server_close()

    def handler(self):
        manager = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Never log a credential form, capability URL or submitted token.

            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def respond(self, status: int, body: str):
                content = page(body)
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; "
                    "style-src 'unsafe-inline'; form-action 'self'; "
                    "frame-ancestors 'none'; base-uri 'none'",
                )
                self.end_headers()
                self.wfile.write(content)

            def valid(self, *, post=False):
                expected_host = manager.origin.removeprefix("http://")
                return bool(
                    manager.nonce
                    and self.headers.get("Host") == expected_host
                    and secrets.compare_digest(self.path, f"/connect/{manager.nonce}")
                    and manager.clock() < manager.expires
                    and (not post or self.headers.get("Origin") == manager.origin)
                )

            def do_GET(self):
                with manager.lock:
                    if not self.valid():
                        self.respond(404, "<h1>连接入口已失效</h1><p>请回到智能体重新连接。</p>")
                        return
                    self.respond(
                        200,
                        """<h1>连接你自己的 Vidu 账号</h1>
<p>映伴是独立开发的连接工具。首次保存授权后，后续调用会自动连接。</p>
<p>在 <a href="https://www.vidu.com" target="_blank" rel="noreferrer noopener">Vidu 国际站</a>
或 <a href="https://www.vidu.cn" target="_blank" rel="noreferrer noopener">中国站</a>
登录，在头像菜单中获取 <strong>Vidu Token</strong>。没有账号请先在官网注册。</p>
<form method="post" autocomplete="off"><label for="region">账号所在区域</label>
<select id="region" name="region"><option value="global">国际站 · vidu.com</option>
<option value="cn">中国站 · vidu.cn</option></select>
<label for="token">Vidu Token</label><input id="token" name="token" type="password"
required maxlength="2048" autocomplete="off" spellcheck="false">
<button type="submit">验证并保存授权</button></form>
<small>Token 仅发送到本机插件及所选 Vidu 官方服务验证额度，保存到系统凭据库。
不会发进聊天、写入项目文件或提交视频。你可以通过“断开 Vidu 账号”删除授权。</small>""",
                    )

            def do_POST(self):
                with manager.lock:
                    if not self.valid(post=True):
                        self.respond(403, "<h1>请求未被接受</h1><p>请从智能体重新打开连接页。</p>")
                        return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if (
                            not 0 < length <= 8192
                            or self.headers.get("Transfer-Encoding")
                            or self.headers.get("Content-Type", "").split(";")[0]
                            != "application/x-www-form-urlencoded"
                        ):
                            raise ValueError()
                        form = parse_qs(self.rfile.read(length).decode(), max_num_fields=2)
                        if set(form) != {"token", "region"} or any(
                            len(v) != 1 for v in form.values()
                        ):
                            raise ValueError()
                        token, region = form["token"][0].strip(), form["region"][0]
                        if (
                            region not in {"global", "cn"}
                            or not 1 <= len(token) <= 2048
                            or any(c.isspace() for c in token)
                            or not token.isascii()
                        ):
                            raise ValueError()
                    except (ValueError, UnicodeError):
                        self.respond(400, "<h1>格式不正确</h1><p>请返回并检查区域和 Token。</p>")
                        return
                    try:
                        account = asyncio.run(manager.validator(token, region))
                        manager.vault.save(manager.owner, token, region)
                    except DomainError as error:
                        self.respond(
                            400,
                            "<h1>尚未连接</h1><p>"
                            + html.escape(str(error))
                            + "</p><p>请返回修改后重试。</p>",
                        )
                        return
                    except Exception:
                        self.respond(
                            503, "<h1>连接暂不可用</h1><p>请稍后重试，未确认连接成功。</p>"
                        )
                        return
                    manager.nonce = None
                    self.respond(
                        200,
                        '<h1>Vidu 账号已连接</h1><p class="notice">当前可用积分：'
                        + str(account.credits)
                        + "</p><p>回到智能体即可继续使用映伴。"
                        "本次连接没有提交视频任务。</p>",
                    )

        return Handler
