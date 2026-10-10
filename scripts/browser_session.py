"""Open a dedicated local Vidu login browser; never reuse a personal browser profile.

Run this yourself before starting selfhost_local.py. The HTTP/MCP service attaches
to this browser; passwords remain in the official login UI, not in tool arguments.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

from vidu_purchase.browser import SUBSCRIPTION_URL, BrowserBinding, read_bindings

ROOT = Path(__file__).resolve().parents[1]


def browser_executable():
    if os.name == "nt":
        for base in (
            os.getenv("PROGRAMFILES(X86)"),
            os.getenv("PROGRAMFILES"),
            os.getenv("LOCALAPPDATA"),
        ):
            if not base:
                continue
            for relative in (
                "Microsoft/Edge/Application/msedge.exe",
                "Google/Chrome/Application/chrome.exe",
            ):
                candidate = Path(base) / relative
                if candidate.is_file():
                    return str(candidate)
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("未找到已安装的 Edge/Chrome；请先安装官方浏览器。")


def port_open(port):
    with socket.socket() as connection:
        connection.settimeout(0.3)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def prepare_config(root: Path, port: int, *, profile_name="commerce-browser"):
    if not 1024 <= port <= 65535:
        raise ValueError("浏览器端口应在 1024–65535 范围内。")
    local = (root / ".local").resolve()
    local.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", profile_name):
        raise ValueError("专用浏览器名称只接受小写字母、数字和连字符。")
    profile = (local / profile_name).resolve()
    if profile.parent != local:
        raise ValueError("专用浏览器目录必须留在当前项目的 .local 内。")
    path = local / (
        "browser-bindings.json" if profile_name == "commerce-browser"
        else profile_name + "-bindings.json"
    )
    binding = BrowserBinding(cdp_url=f"http://127.0.0.1:{port}")
    if path.exists():
        current = read_bindings(path)
        if set(current) != {"local-user"} or current["local-user"].cdp_url != binding.cdp_url:
            raise ValueError("已有浏览器映射与本机配置不同；没有覆盖现有绑定。")
    return profile, path, binding


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument(
        "--headless", action="store_true", help="仅通过智能体面板操作，不显示独立窗口",
    )
    parser.add_argument("--profile-name", default="commerce-browser")
    args = parser.parse_args()
    try:
        profile, path, binding = prepare_config(ROOT, args.port, profile_name=args.profile_name)
        if port_open(args.port):
            raise RuntimeError(
                "该调试端口已被占用。若专用浏览器已打开，直接启动服务；否则换一个端口。"
            )
        executable = browser_executable()
        profile.mkdir(exist_ok=True)
        # No shell, existing personal profile, password flags, sandbox weakening or public bind.
        process = subprocess.Popen(
            [
                executable,
                f"--user-data-dir={profile}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={args.port}",
                "--no-first-run",
                "--no-default-browser-check",
                *(["--headless=new", "--window-size=1280,800"] if args.headless else []),
                SUBSCRIPTION_URL,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if port_open(args.port):
                break
            if process.poll() is not None:
                raise RuntimeError("专用浏览器未能启动；没有替换为其他浏览器会话。")
            time.sleep(0.2)
        else:
            raise RuntimeError("浏览器调试接口尚未就绪，请检查打开的浏览器窗口。")
        if not path.exists():
            with path.open("x", encoding="utf-8") as output:
                json.dump(
                    {"local-user": {"cdp_url": binding.cdp_url, "handoff_url": None}},
                    output,
                    indent=2,
                )
                output.write("\n")
        message = "可通过官方操作面板登录。" if args.headless else "可在官网窗口或面板登录。"
        print("专用浏览器已就绪。" + message)
        print("绑定配置：" + str(path))
        print("保留此浏览器会话，智能体可自动连接；不要公开调试端口。")
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
