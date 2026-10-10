"""Run the installed plugin's real connection flow, then verify only account status."""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def connect(plugin: Path, report: Path, *, open_only=False, data_dir=None):
    config = json.loads((plugin / "mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["yingban-vidu"]

    def resolve(value):
        return value.replace("${PLUGIN_ROOT}", str(plugin)).replace(
            "${PLUGIN_DATA}", str(report.parent / "installed-runtime")
        )

    transport = StdioTransport(
        command=server["command"],
        args=[resolve(item) for item in server["args"]]
        + (["--data-dir", str(data_dir.resolve())] if data_dir else []),
        env={key: resolve(value) for key, value in server["env"].items()},
        cwd=resolve(server["cwd"]),
    )
    async with Client(transport, timeout=90) as client:
        caps = (await client.call_tool("vidu_capabilities", {})).data
        account = (await client.call_tool("vidu_account_status", {})).data
        if account["status"] != "connected":
            connection = (await client.call_tool("vidu_connect_account", {})).data
            print(json.dumps({**connection, "plugin_version": caps["version"],
                              "source": str(plugin)}, ensure_ascii=False), flush=True)
            if open_only:
                return
            deadline = time.monotonic() + 890
            while time.monotonic() < deadline:
                await asyncio.sleep(5)
                account = (await client.call_tool("vidu_account_status", {})).data
                if account["status"] == "connected":
                    break
            else:
                print(json.dumps({"status": "connection_not_completed"}), flush=True)
                return
        result = {
            "checked_at": datetime.now(UTC).isoformat(),
            "account_status": account["status"],
            "region": account["region"],
            "credits": account["credits"],
            "source": "installed_plugin_live_Vidu_account_API",
            "video_submitted": False,
            "payment_submitted": False,
        }
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--open-only", action="store_true", help="只打开连接入口，不等待用户登录")
    parser.add_argument("--data-dir", type=Path, help="验收专用的独立账号目录，不改默认账号")
    parser.add_argument(
        "--report", type=Path, default=Path(".local/live-account-verification.json")
    )
    args = parser.parse_args()
    asyncio.run(connect(args.plugin.resolve(), args.report.resolve(),
                        open_only=args.open_only, data_dir=args.data_dir))
