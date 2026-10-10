"""Lazily provision private official-browser profiles for authenticated subjects.

One service process owns the pool. Profiles persist across restarts; active
browsers have a bounded lifetime. CDP endpoints never leave the service.
"""

import asyncio
import hashlib
import os
import re
import shutil
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from playwright.async_api import async_playwright

from vidu_purchase.browser import SUBSCRIPTION_URL, BrowserBinding
from vidu_purchase.models import DomainError
from vidu_purchase.process_lock import ProcessLock


def windows_app_directories():
    # MCP hosts may omit PROGRAMFILES/LOCALAPPDATA from the child environment.
    # Resolve the installed-app folders through Windows, not a personal profile.
    import ctypes

    paths = []
    for folder in (0x2A, 0x26, 0x1C):  # Program Files (x86), Program Files, Local AppData.
        value = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, folder, None, 0, value) == 0:
            paths.append(Path(value.value))
    return paths


def browser_executable():
    candidates = []
    if os.name == "nt":
        for base in (*windows_app_directories(), os.getenv("PROGRAMFILES(X86)"),
                     os.getenv("PROGRAMFILES"), os.getenv("LOCALAPPDATA")):
            if base:
                candidates.extend(Path(base) / relative for relative in (
                    "Microsoft/Edge/Application/msedge.exe",
                    "Google/Chrome/Application/chrome.exe",
                ))
    candidates.extend(Path(path) for path in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ))
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return str(path)
    raise DomainError("browser_missing", "请先安装 Edge 或 Chrome，再重新连接插件。")


class ChromiumLauncher:
    def __init__(self):
        self.runtime = None

    async def __call__(self, profile):
        executable = browser_executable()
        if self.runtime is None:
            self.runtime = await async_playwright().start()
        # This file belongs only to our locked, isolated profile. Never attach to
        # stale endpoints from an earlier process or an existing personal browser.
        endpoint_file = profile / "DevToolsActivePort"
        endpoint_file.unlink(missing_ok=True)
        context = None
        try:
            context = await self.runtime.chromium.launch_persistent_context(
                str(profile), executable_path=executable, headless=True,
                chromium_sandbox=True, no_viewport=True, timeout=20000,
                args=["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"],
            )
            for _ in range(50):
                if endpoint_file.is_file():
                    parts = endpoint_file.read_text(encoding="utf-8").splitlines()
                    if (len(parts) == 2 and parts[0].isdigit()
                            and 1024 <= int(parts[0]) <= 65535
                            and re.fullmatch(r"/devtools/browser/[a-f0-9-]{36}", parts[1])):
                        endpoint = f"http://127.0.0.1:{parts[0]}"
                        break
                await asyncio.sleep(0.1)
            else:
                raise ValueError("No owned browser endpoint")
            # Opening the official site never fills a field, authorizes or buys.
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(SUBSCRIPTION_URL, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                # A slow site is displayed in the user panel; do not duplicate tabs.
                pass
            return endpoint, context
        except BaseException:
            if context:
                await context.close()
            raise

    async def close(self):
        if self.runtime:
            await self.runtime.stop()
            self.runtime = None


@dataclass
class RunningBrowser:
    handle: object
    touched: float
    users: int = 0


class BrowserPool:
    def __init__(self, root: Path, *, max_active=4, idle_seconds=1800, launcher=None,
                 clock=time.monotonic):
        if not 1 <= max_active <= 64 or idle_seconds < 60:
            raise ValueError("Use 1–64 active browsers and at least 60 seconds idle time")
        self.root = root.resolve()
        self.max_active, self.idle_seconds = max_active, idle_seconds
        self.launcher, self.clock = launcher or ChromiumLauncher(), clock
        self.bindings, self.running = {}, {}
        self.lock = asyncio.Lock()
        self.ownership = ProcessLock(self.root / "pool.lock")
        self.protected = lambda: set()

    @staticmethod
    def valid_owner(owner):
        return isinstance(owner, str) and bool(owner.strip()) and len(owner) <= 512

    def profile(self, owner):
        if not self.valid_owner(owner):
            raise DomainError("invalid_subject", "需要已验证的用户身份。")
        if self.ownership.file is None and not self.ownership.acquire():
            raise DomainError("browser_pool_in_use", "此浏览器目录已由另一个服务管理。")
        parent = self.root / hashlib.sha256(owner.encode()).hexdigest()
        if parent.is_symlink() or parent.resolve().parent != self.root:
            raise DomainError("browser_profile_invalid", "浏览器目录配置无效。")
        parent.mkdir(mode=0o700, exist_ok=True)
        profile = parent / "profile"
        if profile.is_symlink() or profile.resolve().parent != parent:
            raise DomainError("browser_profile_invalid", "浏览器目录配置无效。")
        marker = parent / "session-id"
        if not profile.exists() or not marker.exists():
            # Replacing a profile invalidates old account-reference proofs.
            marker.write_text(uuid4().hex, encoding="ascii")
        session_id = marker.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise DomainError("browser_profile_invalid", "浏览器会话标识无效。")
        profile.mkdir(mode=0o700, exist_ok=True)
        return profile, session_id

    async def _stop(self, owner):
        entry = self.running.pop(owner)
        self.bindings.pop(owner, None)
        with suppress(Exception):
            await entry.handle.close()

    async def _reap(self):
        protected = self.protected()
        for owner, entry in list(self.running.items()):
            if (not entry.users and owner not in protected
                    and self.clock() - entry.touched >= self.idle_seconds):
                await self._stop(owner)

    async def _ensure(self, owner):
        if not self.valid_owner(owner):
            raise DomainError("invalid_subject", "需要已验证的用户身份。")
        entry = self.running.get(owner)
        if entry:
            browser = getattr(entry.handle, "browser", None)
            if browser is None or browser.is_connected():
                entry.touched = self.clock()
                return self.bindings[owner]
            await self._stop(owner)
        await self._reap()
        if len(self.running) >= self.max_active:
            raise DomainError("browser_capacity", "登录窗口正在使用中，请稍后重新连接。")
        profile, session_id = self.profile(owner)
        try:
            endpoint, handle = await self.launcher(profile)
        except DomainError:
            raise
        except Exception:
            raise DomainError(
                "browser_start_failed", "独立官网浏览器启动失败，请检查浏览器安装后重试。",
            ) from None
        binding = BrowserBinding(endpoint, session_id=session_id)
        self.running[owner] = RunningBrowser(handle, self.clock())
        self.bindings[owner] = binding
        return binding

    async def ensure(self, owner):
        async with self.lock:
            return await self._ensure(owner)

    @asynccontextmanager
    async def lease(self, owner):
        async with self.lock:
            binding = await self._ensure(owner)
            entry = self.running[owner]
            entry.users += 1
        try:
            yield binding
        finally:
            async with self.lock:
                entry.users -= 1
                entry.touched = self.clock()

    async def reap(self):
        async with self.lock:
            await self._reap()

    async def close(self):
        async with self.lock:
            for owner in list(self.running):
                await self._stop(owner)
            await self.launcher.close()
            self.ownership.close()
