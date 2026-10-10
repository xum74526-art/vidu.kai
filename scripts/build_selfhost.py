"""Package the self-hosted service from an explicit allowlist; no development providers."""

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from vidu_purchase.hosted import create_hosted_app
from vidu_purchase.store import Store
from vidu_purchase.video_provider import WebsiteVideoProvider

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.9.0"
MODULES = (
    "__init__.py",
    "models.py",
    "service.py",
    "store.py",
    "catalog.py",
    "checkout.py",
    "browser.py",
    "browser_pool.py",
    "process_lock.py",
    "http_limits.py",
    "browser_account.py",
    "browser_panel.py",
    "account_connection.py",
    "video_models.py",
    "video_provider.py",
    "workflow.py",
    "workflow_store.py",
    "workflow_tools.py",
    "mcp_server.py",
    "billing_gateway.py",
    "hosted.py",
    "remote_plugin.py",
    "credentials.py",
    "desktop_connection.py",
    "console.py",
    "brief.py",
    "brief_models.py",
)
PROJECT = """[project]
name = "yingban-selfhost"
version = "0.9.0"
license = "MIT"
requires-python = ">=3.12,<3.15"
dependencies = [
  "fastmcp==4.1.0", "httpx>=0.28.1,<1", "pydantic>=2.11,<3",
  "starlette>=1.7,<2", "uvicorn>=0.54,<1", "keyring>=25.7,<26",
  "playwright>=1.55,<2",
]

[project.scripts]
vidu-selfhost = "vidu_purchase.hosted:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/vidu_purchase"]
"""


def build():
    app = create_hosted_app(
        store=Store(ROOT / ".local" / "schema-build.sqlite3"),
        verifier=StaticTokenVerifier(tokens={}),
        video=WebsiteVideoProvider({}),
    )
    schema = ROOT / "docs" / "openapi.json"
    schema.write_text(
        json.dumps(app.state.agent_api.openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    stage = ROOT / "dist" / f"yingban-selfhost-{VERSION}"
    package = stage / "src" / "vidu_purchase"
    package.mkdir(parents=True, exist_ok=True)
    files = []
    for name in MODULES:
        destination = package / name
        shutil.copy2(ROOT / "src" / "vidu_purchase" / name, destination)
        files.append(destination)
    for name in (
        "index.html", "console.js", "console.css", "icon.svg",
        "panel.html", "panel.js", "panel-input.js", "panel.css", "mcp-panel.html",
    ):
        destination = package / "static" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "src" / "vidu_purchase" / "static" / name, destination)
        files.append(destination)
    for relative in (
        "LICENSE",
        "docs/selfhost.md",
        "docs/commerce-gateway.md",
        "docs/agent-conversation.md",
        "docs/browser-commerce.md",
        "docs/official-panel.md",
        "docs/mobile-mcp.md",
        "docs/openapi.json",
        "sdk/yingban-client.mjs",
        "sdk/yingban-mobile.mjs",
        "scripts/selfhost_local.py",
        "scripts/browser_session.py",
        "examples/accounts.example.json",
        "examples/billing-gateway.example.json",
        "examples/browser-bindings.example.json",
        "ui/package.json",
        "ui/package-lock.json",
        "ui/build.mjs",
        "ui/mcp-panel.js",
        "ui/THIRD_PARTY_NOTICES.txt",
    ):
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
        files.append(destination)
    for asset in sorted((ROOT / "plugins" / "yingban-vidu" / "assets").glob("*")):
        if asset.suffix not in {".png", ".svg"}:
            continue
        destination = stage / "assets" / asset.name
        destination.parent.mkdir(exist_ok=True)
        shutil.copy2(asset, destination)
        files.append(destination)
    (stage / "pyproject.toml").write_text(PROJECT, encoding="utf-8")
    (stage / "README.md").write_text(
        "# 映伴 · 自建智能体服务 0.9.0\n\n"
        "HTTP/MCP 接口、JavaScript 客户端及图标。使用说明见 [接入文档](docs/selfhost.md)。\n\n"
        "手机智能体通过远程 MCP 连接已部署的 HTTPS 服务，GitHub 下载本身不运行服务。\n"
        "手机安装、授权发现、触屏登录及任务恢复见 [手机接入](docs/mobile-mcp.md)。\n\n"
        "新增对话内可交互的官方浏览器面板，用户可以在智能体内注册、登录及确认官方支付。\n"
        "支持 MCP Apps 和 HTTP 嵌入；每用户独立浏览器和凭据绑定。接法见 docs/official-panel.md。\n"
        "标准版、专业版月付可准备真实官方支付页，到账自动核验尚未完成。\n"
        "没有模拟交易模式，也不会自动生成视频。"
        "使用条件见 [浏览器购买](docs/browser-commerce.md)。\n\n"
        "启动：`uv sync --locked`，`uv run python scripts/selfhost_local.py serve`，\n"
        "在控制页的内嵌面板登录；新安装会自动创建独立浏览器。\n"
        "另一个终端检查：`uv run python scripts/selfhost_local.py check`。\n",
        encoding="utf-8",
    )
    subprocess.run(["uv", "lock", "--project", str(stage)], check=True)
    files += [stage / "pyproject.toml", stage / "uv.lock", stage / "README.md"]
    output = stage.parent / (stage.name + ".zip")
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(stage).as_posix())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    print(json.dumps({"archive": str(output), "files": len(files), "sha256": digest}))


if __name__ == "__main__":
    build()
