"""Public installation metadata for the custom mobile host, without user credentials."""

from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.routing import Route

VERSION = "0.9.0"
MANIFEST_PATH = "/.well-known/yingban-plugin.json"


def installation_routes(origin, *, authorization_servers, browser_accounts, generation_enabled):
    origin = origin.rstrip("/")
    parsed = urlsplit(origin)
    remote = parsed.scheme == "https" and parsed.hostname not in {
        "localhost", "127.0.0.1", "::1",
    }
    missing = []
    for ready, name in (
        (remote, "public_https_service"),
        (bool(authorization_servers), "user_authentication"),
        (browser_accounts, "per_user_browser_accounts"),
        (generation_enabled, "video_generation"),
    ):
        if not ready:
            missing.append(name)
    descriptor = {
        "schema": "yingban.remote-plugin.v1",
        "id": "yingban-vidu",
        "name": "映伴 · Vidu 视频",
        "version": VERSION,
        "description": "连接你自己的 Vidu 账号，确认积分后生成视频。",
        "installable": not missing,
        "missing_configuration": missing,
        "mcp": {"transport": "streamable-http", "url": origin + "/mcp"} if remote else None,
        "auth": {
            "type": "oauth2",
            "resource": origin + "/mcp",
            "resource_metadata_url": origin + "/.well-known/oauth-protected-resource/mcp",
            "authorization_servers": list(authorization_servers),
            "scopes": ["vidu:video", "vidu:purchase"],
            "client_secret_required": False,
        } if remote and authorization_servers else None,
        "ui": {
            "resource_uri": "ui://yingban/official-panel.html",
            "panel_origin": origin if remote else None,
            "panel_tool": "vidu_open_official_panel",
            "private_metadata_key": "yingbanPanel",
        },
        "generation": {
            "confirmation_required": True,
            "resume_tool": "vidu_get_workflow",
            "auto_submit_after_login": False,
            "auto_retry_submission": False,
        },
        "runtime": "operator_hosted",
        "message": (
            "服务配置已就绪；安装后仍需检查当前用户的账号和可用积分。"
            if not missing else "服务配置尚未就绪，不能仅通过下载仓库开始生成视频。"
        ),
    }

    async def manifest(_request):
        return JSONResponse(descriptor, headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="yingban-vidu.remote.json"',
            "X-Content-Type-Options": "nosniff",
        })

    return [Route(MANIFEST_PATH, manifest)]
