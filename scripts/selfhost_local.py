"""Run or inspect the live loopback service without putting its access token in chat/logs."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

import httpx
from fastmcp import Client

from vidu_purchase.credentials import native_keyring
from vidu_purchase.desktop_connection import register_official_panel

ROOT = Path(__file__).resolve().parents[1]
KEYRING_SERVICE = "yingban-selfhost/loopback"


def account_directory(data_dir, selected=None):
    """Persist only the explicit desktop storage path, never account IDs or credentials."""
    config = data_dir / "console-config.json"
    if selected is not None:
        chosen = selected.expanduser().resolve()
        if not (chosen / "account.sqlite3").is_file():
            raise ValueError("The selected desktop account directory has not been initialized")
        data_dir.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"account_directory": str(chosen)}) + "\n", encoding="utf-8")
        return chosen
    if not config.exists():
        return None
    saved = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(saved, dict) or set(saved) != {"account_directory"}:
        raise ValueError("Invalid local account directory configuration")
    chosen = Path(saved["account_directory"])
    if not chosen.is_absolute() or not (chosen / "account.sqlite3").is_file():
        raise ValueError("The shared desktop account directory is unavailable")
    return chosen


def local_identity(port):
    return hashlib.sha256(str(ROOT).encode()).hexdigest()[:24] + f":{port}"


def local_token(port, *, create=False):
    identity = local_identity(port)
    backend = native_keyring()
    token = backend.get_password(KEYRING_SERVICE, identity)
    if not token and create:
        token = secrets.token_urlsafe(48)
        backend.set_password(KEYRING_SERVICE, identity, token)
    if not token:
        raise RuntimeError("请先启动本机服务；本工具不会打印或写入明文访问令牌。")
    return token


def console_link(port, token):
    with httpx.Client(timeout=3, trust_env=False) as client:
        result = client.post(
            f"http://127.0.0.1:{port}/console/link",
            headers={"Authorization": f"Bearer {token}"},
        )
        result.raise_for_status()
        return result.json()["url"]


def announce_console(port, token, shared_account=None):
    for _ in range(120):
        try:
            url = console_link(port, token)
            if shared_account:
                register_official_panel(
                    shared_account, f"http://127.0.0.1:{port}", local_identity(port),
                )
            print("打开控制页：" + url, flush=True)
            return
        except (httpx.HTTPError, ValueError):
            time.sleep(0.25)


async def check(port, token):
    origin = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=origin, timeout=30, trust_env=False) as client:
        health = await client.get("/healthz")
        health.raise_for_status()
        homepage = await client.get("/")
        homepage.raise_for_status()
        assert 'id="video-form"' in homepage.text
        anonymous = await client.get("/v1/account")
        assert anonymous.status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        caps = await client.get("/v1/capabilities", headers=headers)
        caps.raise_for_status()
        account = await client.get("/v1/account", headers=headers)
        account.raise_for_status()
        connection = await client.get("/v1/account/connection", headers=headers)
        connection.raise_for_status()
        commerce = await client.get("/v1/commerce/session", headers=headers)
        commerce.raise_for_status()
        # Catalog reads can navigate to the member page. Do not do that while the
        # user is still completing authentication in the attached browser.
        plans = None
        if caps.json()["billing_mode"] != "browser_experimental" or (
            commerce.json()["status"] == "connected"
        ):
            plans = await client.get("/v1/plans", headers=headers)
        if not caps.json()["billing_adapter_configured"]:
            assert plans.status_code == 501
            assert plans.json()["error"]["code"] == "billing_not_configured"
        schema = await client.get("/v1/openapi.json", headers=headers)
        schema.raise_for_status()
    async with Client(origin + "/mcp", auth=token) as mcp:
        tool_names = sorted(tool.name for tool in await mcp.list_tools())
        mcp_caps = (await mcp.call_tool("vidu_purchase_capabilities", {})).data
        assert mcp_caps["billing_mode"] == caps.json()["billing_mode"]
        mcp_account = (await mcp.call_tool("vidu_account_status", {})).data
        assert mcp_account["status"] == account.json()["status"]
        mcp_connection = (await mcp.call_tool("vidu_connection_status", {})).data
        assert mcp_connection["account_link_verified"] == connection.json()["account_link_verified"]
        mcp_commerce = (await mcp.call_tool("vidu_commerce_session", {})).data
        assert mcp_commerce["mode"] == commerce.json()["mode"]
    report = {
        "service": origin,
        "version": caps.json()["version"],
        "health": health.status_code,
        "homepage": homepage.status_code,
        "anonymous": anonymous.status_code,
        "account_status": account.json()["status"],
        "connection_state": connection.json()["state"],
        "account_link_verified": connection.json()["account_link_verified"],
        "billing_adapter_configured": caps.json()["billing_adapter_configured"],
        "billing_mode": caps.json()["billing_mode"],
        "commerce_session_status": commerce.json()["status"],
        "mcp_commerce_session_status": mcp_commerce["status"],
        "payment_verification_available": caps.json()["real_payment_verification_available"],
        "generation_requires_confirmation": True,
        "generation_enabled": caps.json()["real_video_generation_enabled"],
        "plans_http_status": plans.status_code if plans is not None else None,
        "plans_skipped_reason": "browser_not_connected" if plans is None else None,
        "plans_error": plans.json().get("error") if plans is not None and plans.is_error else None,
        "rest_operations": sum(len(item) for item in schema.json()["paths"].values()),
        "mcp_tool_count": len(tool_names),
        "mcp_tools": tool_names,
        "payment_executed": False,
        "video_submitted": False,
        "authenticated_vidu_account_verified": account.json()["status"] == "connected",
        "authenticated_commerce_account_verified": commerce.json()["status"] == "connected",
        "scope": "Local HTTP/MCP and read-only account checks; not a real billing acceptance test",
    }
    output = ROOT / ".local" / "selfhost-verification.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if plans is not None and caps.json()["billing_adapter_configured"]:
        plans.raise_for_status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["serve", "check", "link"])
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".local")
    parser.add_argument("--accounts", type=Path, default=ROOT / ".local" / "accounts.json")
    parser.add_argument("--account-dir", type=Path, help="与已安装的本机插件共用账号存储")
    commerce = parser.add_mutually_exclusive_group()
    commerce.add_argument("--billing-config", type=Path)
    commerce.add_argument("--browser-bindings", type=Path)
    parser.add_argument("--enable-live-generation", action="store_true")
    parser.add_argument(
        "--read-only", action="store_true", help="Disable billable video submission"
    )
    args = parser.parse_args()
    try:
        token = local_token(args.port, create=args.action == "serve")
    except Exception:
        parser.error("无法访问原生系统凭据库；也可通过环境变量配置 vidu-selfhost。")
    if args.action == "check":
        asyncio.run(check(args.port, token))
        return
    if args.action == "link":
        print(json.dumps({"url": console_link(args.port, token)}))
        return
    try:
        shared_account = account_directory(args.data_dir, args.account_dir)
    except (ValueError, OSError, TypeError):
        parser.error("共享账号目录配置无效；没有回退到其他账号。")
    # Only the child service process receives this credential. It never contains a Vidu token.
    os.environ["VIDU_SELFHOST_TOKEN"] = token
    if any(os.getenv(key) for key in ("VIDU_JWKS_URI", "VIDU_TOKEN_ISSUER", "VIDU_TOKEN_AUDIENCE")):
        parser.error("此辅助脚本仅用于本机单用户；JWT 部署请直接运行 vidu-selfhost。")
    sys.argv = [
        "vidu-selfhost",
        "--host",
        "127.0.0.1",
        "--port",
        str(args.port),
        "--db",
        str(args.data_dir / "selfhost.sqlite3"),
        "--accounts",
        str(args.accounts),
        "--local-console",
    ]
    if shared_account:
        sys.argv += ["--local-account-dir", str(shared_account)]
    if args.billing_config:
        sys.argv += ["--billing-config", str(args.billing_config)]
    if args.browser_bindings:
        sys.argv += ["--browser-bindings", str(args.browser_bindings)]
    elif not args.billing_config and (ROOT / ".local" / "browser-bindings.json").exists():
        sys.argv += ["--browser-bindings", str(ROOT / ".local" / "browser-bindings.json")]
    if args.enable_live_generation and args.read_only:
        parser.error("--enable-live-generation and --read-only cannot be combined")
    if not args.read_only:
        sys.argv.append("--enable-live-generation")
    from vidu_purchase.hosted import main as serve

    print(f"HTTP http://127.0.0.1:{args.port}/v1  MCP http://127.0.0.1:{args.port}/mcp", flush=True)
    print("本机访问令牌保存在系统凭据库；未配置上游时不会提供假套餐。", flush=True)
    threading.Thread(
        target=announce_console, args=(args.port, token, shared_account), daemon=True,
    ).start()
    serve()


if __name__ == "__main__":
    main()
