"""Legacy development CLI. Release services import mcp_server directly."""

import argparse
import os
from pathlib import Path

from fastmcp.server.auth.providers.jwt import JWTVerifier, StaticTokenVerifier

from vidu_purchase.mcp_server import create_server as create_server
from vidu_purchase.service import PurchaseService
from vidu_purchase.store import Store
from vidu_purchase.video_provider import WebsiteVideoProvider, read_accounts
from vidu_purchase.workflow import WorkflowService


def main() -> None:
    # Legacy development modes are not imported by the live self-hosted entry point.
    from vidu_purchase.browser import ViduBrowserProvider, read_bindings
    from vidu_purchase.demo import DemoProvider
    from vidu_purchase.demo_video import DemoVideoProvider

    parser = argparse.ArgumentParser(description="Vidu purchase development server")
    parser.add_argument("--mode", choices=["browser", "demo"], default="browser")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--owner", default="local-user", help="Only for single-user stdio/dev auth")
    parser.add_argument("--bindings", type=Path, default=Path(".local/browser-bindings.json"))
    parser.add_argument("--accounts", type=Path, default=Path(".local/accounts.json"))
    parser.add_argument(
        "--enable-live-generation",
        action="store_true",
        help="Enable real, quota-consuming video submission after user confirmation",
    )
    parser.add_argument("--db", type=Path)
    parser.add_argument("--disable-commerce", action="store_true")
    args = parser.parse_args()
    db = args.db or Path(f".local/{args.mode}-purchases.db")
    provider = (
        DemoProvider()
        if args.mode == "demo"
        else ViduBrowserProvider(read_bindings(args.bindings) if args.bindings.exists() else {})
    )
    service = PurchaseService(Store(db), provider)
    video = (
        DemoVideoProvider()
        if args.mode == "demo"
        else WebsiteVideoProvider(
            read_accounts(args.accounts) if args.accounts.exists() else {},
            generation_enabled=args.enable_live_generation,
        )
    )
    workflow = WorkflowService(service, video, commerce_enabled=not args.disable_commerce)
    if args.transport == "stdio":
        create_server(
            service,
            local_owner=args.owner,
            commerce_enabled=not args.disable_commerce,
            workflow=workflow,
        ).run(transport="stdio")
        return
    loopback = args.host in {"localhost", "127.0.0.1", "::1"}
    if args.mode == "demo" and not loopback:
        parser.error("Demo mode is restricted to a loopback HTTP listener")
    jwks = os.getenv("VIDU_JWKS_URI")
    issuer = os.getenv("VIDU_TOKEN_ISSUER")
    audience = os.getenv("VIDU_TOKEN_AUDIENCE")
    if jwks and issuer and audience:
        if not jwks.startswith("https://"):
            parser.error("VIDU_JWKS_URI must use HTTPS")
        auth = JWTVerifier(jwks_uri=jwks, issuer=issuer, audience=audience, required_scopes=[])
    else:
        token = os.getenv("VIDU_DEV_TOKEN", "")
        if not loopback or len(token) < 32:
            parser.error(
                "HTTP requires JWT configuration, or loopback + VIDU_DEV_TOKEN (32+ chars)"
            )
        auth = StaticTokenVerifier(
            tokens={
                token: {
                    "client_id": "vidu-local-development",
                    "sub": args.owner,
                    "scopes": ["vidu:purchase", "vidu:video"],
                }
            },
            required_scopes=[],
        )
    create_server(
        service, auth=auth, commerce_enabled=not args.disable_commerce, workflow=workflow
    ).run(
        transport="http",
        host=args.host,
        port=args.port,
        path="/mcp",
    )


if __name__ == "__main__":
    main()
