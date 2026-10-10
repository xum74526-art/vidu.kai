"""Live, read-only installation acceptance in a NEW, unconnected account directory.

Uses the shipped stdio configuration and the plugin's own panel HTTP protocol.
Never enters account data, authorizes, purchases or submits a video. The captured
frame is only from the new, empty profile created by this run.
"""

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from vidu_purchase.credentials import CredentialVault
from vidu_purchase.desktop_connection import OfficialConnectionManager

ROOT = Path(__file__).resolve().parents[1]


async def verify(plugin, output):
    sandbox = ROOT / ".local" / ("install-check-" + uuid4().hex[:8])
    data = sandbox / "account"
    assert not sandbox.exists()
    sandbox.mkdir(parents=True)
    config = json.loads((plugin / "mcp.json").read_text(encoding="utf-8"))
    settings = config["mcpServers"]["yingban-vidu"]

    def resolve(value):
        return value.replace("${PLUGIN_ROOT}", str(plugin)).replace("${PLUGIN_DATA}", str(sandbox))

    transport = StdioTransport(
        command=settings["command"],
        args=[resolve(item) for item in settings["args"]] + ["--data-dir", str(data)],
        cwd=resolve(settings["cwd"]),
        env={key: resolve(value) for key, value in settings.get("env", {}).items()},
    )
    started = time.monotonic()
    panel_headers = None
    origin = service_headers = None
    try:
        async with Client(transport, timeout=100) as mcp:
            caps = (await mcp.call_tool("vidu_capabilities", {})).data
            assert caps["official_browser_panel_autostart"] is True
            status = (await mcp.call_tool("vidu_account_status", {})).data
            assert status["status"] == "not_connected"
            result = (await mcp.call_tool("vidu_connect_account", {})).data
            assert result["manual_token_required"] is False and result["auto_generate"] is False
        # The user must be able to finish login after the initiating client exits.
        manager = OfficialConnectionManager(CredentialVault(data))
        origin, credential = manager.service_connection()
        service_headers = {"Authorization": "Bearer " + credential, "X-Yingban-Console": "1"}
        async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=25) as api:
            health = await api.get("/healthz", headers=service_headers)
            health.raise_for_status()
            assert health.json()["service"] == "yingban-login-companion"
            panel = await api.get("/panel")
            panel.raise_for_status()
            assert "我已登录，授权并连接" in panel.text and 'name="token"' not in panel.text
            ticket = parse_qs(urlsplit(result["connection_url"]).fragment)["ticket"][0]
            headers = {"Origin": origin, "X-Yingban-Panel": "1"}
            session = await api.post("/panel/session", headers=headers, json={"ticket": ticket})
            session.raise_for_status()
            panel_headers = {
                **headers, "Authorization": "Bearer " + session.json()["session_token"],
            }
            deadline = time.monotonic() + 45
            ready_since = None
            while True:
                frame = await api.get("/panel/frame", headers=panel_headers)
                if frame.status_code != 200:
                    raise RuntimeError("Official panel: " + frame.json()["error"]["code"])
                frame = frame.json()
                if frame.get("account_stage") == "login":
                    ready_since = ready_since or time.monotonic()
                    # A DOM transition can precede the compositor's first painted frame.
                    if time.monotonic() - ready_since >= 3:
                        break
                else:
                    ready_since = None
                if time.monotonic() > deadline:
                    # This profile was created empty by this test and received no inputs.
                    if frame.get("image"):
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.with_suffix(".failed.jpg").write_bytes(
                            base64.b64decode(frame["image"], validate=True)
                        )
                    raise RuntimeError("Vidu has not displayed a usable login page")
                await asyncio.sleep(1)
            assert frame["state"] == "live" and frame["origin"] == "https://www.vidu.com"
            output.parent.mkdir(parents=True, exist_ok=True)
            shot = output.with_suffix(".jpg")
            shot.write_bytes(base64.b64decode(frame["image"], validate=True))
            proof = {
                "plugin_version": caps["version"], "stdio": "passed",
                "fresh_account": True, "automatic_browser": True,
                "helper_survived_stdio_exit": True, "official_panel_state": frame["state"],
                "official_login_visible": True,
                "official_origin": frame["origin"], "manual_token_required": False,
                "login_completed": False, "payment_submitted": False, "video_submitted": False,
                "seconds": round(time.monotonic() - started, 2), "screenshot": str(shot.resolve()),
            }
            output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
            print(json.dumps(proof, ensure_ascii=False))
    finally:
        # Only stop the helper created for this run; never touch the user's account.
        try:
            if origin is None and (data / "official-panel.json").exists():
                manager = OfficialConnectionManager(CredentialVault(data))
                origin, credential = manager.service_connection()
                service_headers = {
                    "Authorization": "Bearer " + credential, "X-Yingban-Console": "1",
                }
            if origin and service_headers:
                async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=10) as api:
                    if panel_headers:
                        await api.delete("/panel/session", headers=panel_headers)
                    await api.post("/internal/stop", headers=service_headers)
        except Exception:
            print("Temporary helper cleanup was unavailable; its idle timeout remains active.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / ".local" / "install-proof.json")
    args = parser.parse_args()
    asyncio.run(verify(args.plugin.resolve(), args.output.resolve()))
