"""Contextvars-based structured context propagation.

Replaces hand-building an ``erragent_context`` dict at every log call site with a single
``with erragent.context(...):`` wrapper around the surrounding operation. Built on
``contextvars.ContextVar`` so it survives ``asyncio.create_task`` boundaries created inside
the ``with`` block, unlike thread-local storage.
"""

from __future__ import annotations

import functools
import inspect
from contextvars import ContextVar, Token
from typing import Any, Callable, TypeVar

_current_context: ContextVar[dict[str, Any]] = ContextVar("erragent_current_context", default={})

F = TypeVar("F", bound=Callable[..., Any])


def current_context() -> dict[str, Any]:
    """Return a shallow copy of the currently active context fields."""
    return dict(_current_context.get())


class context:
    """Context manager and decorator that merges fields into the active structured context.

    Nested uses merge additively (innermost fields win on key collision) and the merge is
    undone on exit, so sibling operations never see each other's fields.

    Usable as a context manager::

        with erragent.context(workflow_name="ingest", request_id=req_id):
            ...

    or as a decorator (works for both sync and async callables)::

        @erragent.context(node="fetch_docs")
        async def fetch_docs(...): ...
    """

    __slots__ = ("_fields", "_token")

    def __init__(self, **fields: Any) -> None:
        self._fields = fields
        self._token: Token[dict[str, Any]] | None = None

    def __enter__(self) -> "context":
        merged = {**_current_context.get(), **self._fields}
        self._token = _current_context.set(merged)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._token is not None:
            _current_context.reset(self._token)
            self._token = None

    def __call__(self, fn: F) -> F:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with context(**self._fields):
                    return await fn(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with context(**self._fields):
                return fn(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]
