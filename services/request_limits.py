"""Bound request allocations before JSON/base64 decoding on a 512 MB worker."""

from fastapi.responses import JSONResponse

MIB = 1024 * 1024


class RequestLimitsMiddleware:
    def __init__(self, app):
        self.app = app
        self.heavy_active = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        path = scope["path"]
        heavy = path.endswith(("/facebook-group-post", "/image-prompt")) or path.startswith(
            "/admin/"
        )
        limit = 22 * MIB if path.endswith("/facebook-group-post") else (8 * MIB if heavy else MIB)
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Invalid Content-Length"}, 400)(
                scope, receive, send
            )
        if declared > limit:
            return await JSONResponse({"detail": "Request too large"}, 413)(scope, receive, send)
        # No await between checking and claiming: one large allocation at a time.
        if heavy and self.heavy_active:
            return await JSONResponse(
                {"detail": "Media worker busy; retry shortly"}, 503, headers={"Retry-After": "5"}
            )(scope, receive, send)
        if heavy:
            self.heavy_active = True
        try:
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > limit:
                    return await JSONResponse({"detail": "Request too large"}, 413)(
                        scope, receive, send
                    )
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            chunks.clear()
            consumed = False

            async def bounded_receive():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, bounded_receive, send)
        finally:
            if heavy:
                self.heavy_active = False
