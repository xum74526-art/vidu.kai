"""Check package integrity and perform a real stdio handshake without submitting video."""

import argparse
import ast
import asyncio
import json
import tempfile
import zipfile
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).resolve().parents[1]


async def check(plugin: Path):
    manifest = json.loads((plugin / "plugin.json").read_text(encoding="utf-8"))
    config = json.loads((plugin / "mcp.json").read_text(encoding="utf-8"))
    package = plugin / "runtime" / "src" / "vidu_purchase"
    # Catch lazy imports too: a success-only stdio handshake misses error paths.
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.ImportFrom)
                    and (node.module or "").startswith("vidu_purchase.")):
                dependency = node.module.split(".")[1]
                assert (package / (dependency + ".py")).is_file(), (path.name, dependency)
    for field in ("composerIcon", "logo"):
        assert (plugin / manifest["extensions"]["com.openai"]["interface"][field]).is_file()
    server = config["mcpServers"]["yingban-vidu"]
    with tempfile.TemporaryDirectory(prefix="yingban-check-") as data_dir:

        def resolve(value):
            return value.replace("${PLUGIN_ROOT}", str(plugin)).replace(
                "${PLUGIN_DATA}", str(ROOT / ".local" / "plugin-check")
            )

        transport = StdioTransport(
            command=server["command"],
            args=[resolve(item) for item in server["args"]] + ["--data-dir", data_dir],
            env={key: resolve(value) for key, value in server.get("env", {}).items()},
            cwd=resolve(server["cwd"]),
        )
        async with Client(transport, timeout=90) as client:
            tools = await client.list_tools()
            assert len(tools) == 9
            assert not any("purchase" in tool.name or "plan" in tool.name for tool in tools)
            status = (await client.call_tool("vidu_account_status", {})).data
            assert status["status"] == "not_connected"
            assert status["credits"] is None
            caps = (await client.call_tool("vidu_capabilities", {})).data
            assert caps["mode"] == "live" and caps["auto_generate"] is False
            assert caps["version"] == manifest["version"]
            assert caps["manual_token_required"] is False
            assert caps["official_browser_panel_autostart"] is True
            print(
                json.dumps(
                    {
                        "stdio": "passed",
                        "tool_count": len(tools),
                        "account_status": status["status"],
                        "real_generation_tested": False,
                    }
                )
            )

    archive = ROOT / "dist" / f"yingban-vidu-{manifest['version']}.zip"
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert "plugin.json" in names and "mcp.json" in names
        assert not any(
            "demo" in name
            or ".local/" in name
            or name.endswith((".db", ".sqlite3", ".env"))
            for name in names
        )
        assert not any("tests/" in name or ".venv/" in name for name in names)
        assert "runtime/src/vidu_purchase/companion.py" in names
        assert "runtime/src/vidu_purchase/static/panel.html" in names
        print(json.dumps({"archive": "passed", "files": len(names)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", type=Path, default=ROOT / "plugins" / "yingban-vidu")
    asyncio.run(check(parser.parse_args().plugin.resolve()))
