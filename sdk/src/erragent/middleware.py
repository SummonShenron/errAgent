"""Optional ASGI middleware that seeds request-scoped structured context automatically.

Requires Starlette/FastAPI to be installed in the target app (the SDK's base package does not
depend on it — this module is only imported when the target app actually uses ``erragent.Middleware``).
"""

from __future__ import annotations

import uuid
from typing import Awaitable, Callable

try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response
except ImportError as exc:  # pragma: no cover - exercised only when starlette is missing
    raise ImportError(
        "erragent.Middleware requires starlette/fastapi to be installed in the target app."
    ) from exc

from .context import context as erragent_context


class ErrAgentMiddleware(BaseHTTPMiddleware):
    """Seeds ``route``/``method``/``request_id`` context for every request's log records.

    Install once at app startup::

        app.add_middleware(erragent.Middleware)
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        with erragent_context(route=request.url.path, method=request.method, request_id=request_id):
            response = await call_next(request)
        response.headers.setdefault("x-request-id", request_id)
        return response
