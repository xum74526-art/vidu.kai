"""Install into Codex and prepare its resolved environment before the first MCP startup."""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).resolve().parents[1]


def run_json(args):
    return json.loads(subprocess.check_output(args, text=True, encoding="utf-8"))


async def install(*, prepare_only: bool):
    codex = shutil.which("codex")
    uv = shutil.which("uv")
    if not codex or not uv:
        raise SystemExit("请先安装 Codex 和 uv，并确保它们在 PATH 中。")
    if not prepare_only:
        subprocess.run([codex, "plugin", "marketplace", "add", str(ROOT)], check=True)
        # Re-install also refreshes an existing local plugin to the source version.
        subprocess.run([codex, "plugin", "add", "yingban-vidu@yingban-local"], check=True)

    servers = run_json([codex, "mcp", "list", "--json"])
    matches = [item for item in servers if item["name"] == "yingban-vidu" and item["enabled"]]
    if len(matches) != 1:
        raise SystemExit("Codex 未解析到唯一的映伴 MCP 服务，请检查插件安装和启用状态。")
    settings = matches[0]["transport"]
    plugin_root = Path(settings["env"]["PLUGIN_ROOT"]).resolve()
    manifest = json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8"))
    if manifest["name"] != "yingban-vidu":
        raise SystemExit("插件身份不匹配，停止配置。")
    runtime = plugin_root / "runtime"
    expected = json.loads((plugin_root / "mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "yingban-vidu"
    ]
    if settings["type"] != "stdio" or settings["command"] != expected["command"]:
        raise SystemExit("MCP 启动配置与安装包不匹配，停止配置。")
    env = {**os.environ, **settings["env"]}
    env.pop("VIRTUAL_ENV", None)
    subprocess.run(
        [
            uv,
            "sync",
            "--quiet",
            "--frozen",
            "--no-dev",
            "--compile-bytecode",
            "--python",
            "3.12",
            "--project",
            str(runtime),
        ],
        env=env,
        check=True,
    )
    started = time.monotonic()
    transport = StdioTransport(
        command=settings["command"],
        args=settings["args"],
        cwd=settings["cwd"],
        env=settings["env"],
    )
    async with Client(transport, timeout=30) as client:
        tools = await client.list_tools()
        capabilities = (await client.call_tool("vidu_capabilities", {})).data
        if (
            len(tools) != 9 or capabilities["mode"] != "live"
            or capabilities["version"] != manifest["version"]
            or capabilities.get("manual_token_required") is not False
        ):
            raise SystemExit("已启动的服务与映伴真实接口版不一致。")
        print(
            json.dumps(
                {
                    "plugin": "yingban-vidu@yingban-local",
                    "installed": True,
                    "version": capabilities["version"],
                    "official_panel_configured": capabilities["official_browser_panel_configured"],
                    "codex_resolved_mcp": "verified",
                    "tools": len(tools),
                    "warm_start_seconds": round(time.monotonic() - started, 2),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    asyncio.run(install(prepare_only=parser.parse_args().prepare_only))
