"""Build a relocatable live-only plugin using an explicit runtime file allowlist."""

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "yingban-vidu"
RUNTIME_MODULES = (
    "__init__.py",
    "credentials.py",
    "connection.py",
    "desktop.py",
    "desktop_connection.py",
    "models.py",
    "service.py",
    "store.py",
    "video_models.py",
    "video_provider.py",
    "workflow.py",
    "workflow_store.py",
    "workflow_tools.py",
)
RUNTIME_PROJECT = """[project]
name = "yingban-vidu-runtime"
version = "0.3.1"
requires-python = ">=3.12,<3.15"
dependencies = [
  "fastmcp==4.1.0",
  "httpx>=0.28.1,<1",
  "pydantic>=2.11,<3",
  "keyring>=25.7,<26",
  "platformdirs>=4.4,<5",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/vidu_purchase"]
"""


def build():
    runtime = PLUGIN / "runtime"
    package = runtime / "src" / "vidu_purchase"
    package.mkdir(parents=True, exist_ok=True)
    unexpected = [p.name for p in package.glob("*.py") if p.name not in RUNTIME_MODULES]
    if unexpected:
        raise RuntimeError(f"Unexpected runtime files: {unexpected}; review before packaging")
    for name in RUNTIME_MODULES:
        shutil.copy2(ROOT / "src" / "vidu_purchase" / name, package / name)
    (runtime / "pyproject.toml").write_text(RUNTIME_PROJECT, encoding="utf-8")
    subprocess.run(["uv", "lock", "--project", str(runtime)], check=True)

    files = [PLUGIN / "plugin.json", PLUGIN / "mcp.json", PLUGIN / "README.md"]
    files.extend(sorted((PLUGIN / "assets").glob("*.svg")))
    files.extend(sorted((PLUGIN / "assets").glob("*.png")))
    files.extend(sorted((PLUGIN / "skills").glob("*/SKILL.md")))
    files.extend(package / name for name in RUNTIME_MODULES)
    files.extend([runtime / "pyproject.toml", runtime / "uv.lock"])
    version = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))["version"]
    output = ROOT / "dist" / f"yingban-vidu-{version}.zip"
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(PLUGIN).as_posix())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    print(json.dumps({"archive": str(output), "files": len(files), "sha256": digest}))


if __name__ == "__main__":
    build()
