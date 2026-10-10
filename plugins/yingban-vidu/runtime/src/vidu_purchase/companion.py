"""On-demand loopback login service for the installed desktop plugin.

No purchase or video-submission routes are exposed here. The authenticated
self-hosted MCP server remains the full commerce host for a user's own agent.
"""

import argparse
import asyncio
import hashlib
import os
import secrets
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from vidu_purchase.account_connection import AccountConnectionService
from vidu_purchase.browser import ViduBrowserProvider
from vidu_purchase.browser_panel import STATIC, BrowserPanelService
from vidu_purchase.browser_pool import BrowserPool
from vidu_purchase.console import LocalConsole
from vidu_purchase.credentials import CredentialVault
from vidu_purchase.desktop_connection import SERVICE, register_official_panel
from vidu_purchase.http_limits import BodyLimit
from vidu_purchase.models import DomainError
from vidu_purchase.process_lock import ProcessLock
from vidu_purchase.store import Store
from vidu_purchase.video_provider import WebsiteVideoProvider

VERSION = "0.4.0"


def create_companion(vault, origin, credential, *, pool=None, idle_seconds=1800):
    pool = pool or BrowserPool(vault.path.parent / "browser-profiles", max_active=1)
    provider = ViduBrowserProvider({}, pool=pool)
    video = WebsiteVideoProvider({}, credentials=lambda _: vault.credential(vault.owner()))
    connections = AccountConnectionService(video, browser=provider, vault=vault)
    provider.linked_account = lambda _: vault.browser_link(vault.owner())
    panels = BrowserPanelService(
        provider, Store(vault.path.parent / "panel.sqlite3"), connections, origin,
    )
    pool.protected = panels.protected_owners
    verifier = StaticTokenVerifier(tokens={credential: {
        "client_id": "yingban-desktop", "sub": "local-user",
        "scopes": ["vidu:video", "vidu:purchase"],
    }})
    console = LocalConsole(
        SimpleNamespace(panels=panels), verifier, origin, vault=vault, connections=connections,
    )
    activity = [time.monotonic()]

    async def protected(request):
        console.same_origin(request)
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer ") or not secrets.compare_digest(value[7:], credential):
            raise DomainError("console_login_required", "需要本机服务授权。")

    async def health(request):
        await protected(request)
        return JSONResponse({"service": "yingban-login-companion", "version": VERSION})

    async def stop(request):
        await protected(request)
        app.state.stop()
        return JSONResponse({"status": "stopping"})

    async def desktop_panel(request):
        response = await console.desktop_panel(request)
        activity[0] = time.monotonic()
        return response

    async def asset(request):
        name = request.path_params["name"]
        if name not in {"panel.js", "panel-input.js", "panel.css", "icon.svg"}:
            return JSONResponse({"error": "not_found"}, 404)
        return FileResponse(STATIC / name, headers=panels.headers)

    @asynccontextmanager
    async def lifespan(_app):
        async def maintenance():
            while True:
                await asyncio.sleep(15)
                await panels.prune()
                await pool.reap()
                if panels.protected_owners():
                    activity[0] = time.monotonic()
                elif time.monotonic() - activity[0] >= idle_seconds:
                    app.state.stop()
                    return

        task = asyncio.create_task(maintenance())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await panels.close()
            await pool.close()

    app = Starlette(routes=[
        *panels.routes(), Route("/assets/{name}", asset),
        Route("/healthz", console.safe(health)),
        Route("/internal/stop", console.safe(stop), methods=["POST"]),
        Route("/console/desktop-panel", console.safe(desktop_panel), methods=["POST"]),
    ], lifespan=lifespan)
    app.add_middleware(BodyLimit)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])
    app.state.stop = lambda: None
    app.state.panels, app.state.pool = panels, pool
    return app


async def healthy(manager):
    try:
        origin, credential = manager.service_connection()
        async with httpx.AsyncClient(timeout=1, trust_env=False, follow_redirects=False) as client:
            response = await client.get(origin + "/healthz", headers={
                "Authorization": "Bearer " + credential, "X-Yingban-Console": "1",
            })
        return response.status_code == 200 and response.json().get("service") in {
            "yingban-login-companion", "yingban-selfhost",
        }
    except Exception:
        return False


async def ensure_companion(manager):
    if await healthy(manager):
        return
    lock = ProcessLock(manager.vault.path.parent / "companion-start.lock")
    deadline = time.monotonic() + 35
    while not lock.acquire():
        if time.monotonic() > deadline:
            raise DomainError("official_panel_starting", "登录环境正在准备，请稍后重新连接。")
        await asyncio.sleep(0.2)
    try:
        if await healthy(manager):
            return
        subprocess.Popen(
            [sys.executable, "-m", "vidu_purchase.companion", "--data-dir",
             str(manager.vault.path.parent.resolve())],
            cwd=manager.vault.path.parent,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        while time.monotonic() <= deadline:
            if await healthy(manager):
                return
            await asyncio.sleep(0.25)
        raise DomainError(
            "official_panel_start_failed", "登录服务未能启动，请检查系统凭据库与插件安装。",
        )
    finally:
        lock.close()


async def serve(directory):
    vault = CredentialVault(directory)
    ownership = ProcessLock(directory / "companion.lock")
    if not ownership.acquire():
        return
    sock = socket.socket()
    identity = None
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        identity = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24] + f":{port}"
        credential = secrets.token_urlsafe(48)
        vault.keyring().set_password(SERVICE, identity, credential)
        app = create_companion(vault, origin, credential)
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", access_log=False, proxy_headers=False, log_level="critical",
        ))
        app.state.stop = lambda: setattr(server, "should_exit", True)

        async def announce():
            while not server.started:
                await asyncio.sleep(0.05)
            register_official_panel(directory, origin, identity)

        task = asyncio.create_task(announce())
        try:
            await server.serve(sockets=[sock])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        sock.close()
        if identity:
            with suppress(Exception):
                vault.keyring().delete_password(SERVICE, identity)
        ownership.close()


def main():
    parser = argparse.ArgumentParser(description="映伴本机登录助手（由插件按需启动）")
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.data_dir.resolve()))


if __name__ == "__main__":
    main()
