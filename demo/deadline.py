"""Deadline propagation: the caller tells the callee how long it will keep waiting, and the callee stops then.

The header carries REMAINING milliseconds, not a clock time, because two machines' clocks disagree.
"""
import asyncio
import contextvars
import time

HEADER = "x-deadline-ms"
SAFETY_MARGIN_S = 0.05  # leave room for the network, so the callee's "too late" reply beats the caller's timeout

_deadline = contextvars.ContextVar("deadline", default=None)  # time.monotonic() value when our caller stops waiting


def remaining_s(default_timeout_s: float) -> float:
    """Seconds we may still spend on a downstream call: our own timeout, capped by our caller's deadline."""
    deadline = _deadline.get()
    if deadline is None:
        return default_timeout_s
    return max(0.0, min(default_timeout_s, deadline - time.monotonic()))


def time_left_s() -> float | None:
    """Seconds until our caller stops waiting, or None if it sent no deadline."""
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


class DeadlineMiddleware:
    """If the request carries a deadline, cancel the handler when it passes and answer 504 straight away."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        budget_ms = _read_budget_ms(scope) if scope["type"] == "http" else None
        if budget_ms is None:
            return await self.app(scope, receive, send)

        _deadline.set(time.monotonic() + budget_ms / 1000)
        response_started = False

        async def tracking_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await asyncio.wait_for(self.app(scope, receive, tracking_send), timeout=budget_ms / 1000)
        except TimeoutError:
            if not response_started:
                await send({"type": "http.response.start", "status": 504, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"detail":"deadline exceeded"}'})


def _read_budget_ms(scope) -> int | None:
    for name, value in scope["headers"]:
        if name == HEADER.encode():
            try:
                return max(1, int(value))
            except ValueError:
                return None
    return None
