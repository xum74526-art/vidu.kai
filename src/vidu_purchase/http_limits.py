"""Shared HTTP body limits and safe domain error status codes."""

from starlette.responses import JSONResponse


def domain_status(code):
    if code == "insufficient_scope":
        return 403
    if code in {"purchase_not_found", "quote_not_found", "workflow_not_found", "brief_not_found"}:
        return 404
    if code == "billing_not_configured":
        return 501
    if code in {"invalid_input", "invalid_idempotency_key"}:
        return 422
    if code.startswith(("receipt_", "billing_invalid", "billing_upstream", "billing_response")):
        return 502
    if code.startswith(("vidu_", "estimate_", "credits_", "quota_")):
        return 502
    return 409


class BodyLimit:
    def __init__(self, app, limit=65536):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PATCH", "PUT"}:
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                response = JSONResponse(
                    {"error": {"code": "body_too_large", "message": "请求超过 64 KiB。"}}, 413,
                )
                return await response(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body, delivered = b"".join(chunks), False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
