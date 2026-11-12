"""Shared HTTP plumbing for venue adapters.

`fetch` is injected, never imported. That single decision makes the entire
pipeline runnable under pytest with canned fixtures, and inside a Worker where
the only network primitive is the global `fetch`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional, Protocol

Fetcher = Callable[..., Awaitable[Any]]


class HttpError(RuntimeError):
    """Raised when a venue returns a non-2xx status."""

    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body[:300]

    @property
    def retryable(self) -> bool:
        return self.status == 429 or 500 <= self.status < 600


class RateLimited(RuntimeError):
    """The venue refused us: our own budget, or the venue's.

    Exists so that "we were not allowed to ask" can never be reported as "this
    wallet has no trades". `HyperliquidVenue.fills` used to return `[]` in this
    case, and `profile_watchlist` recorded every wallet after the 45th as "no
    fills" — a fabricated measurement covering for a refusal. The dashboard then
    showed "0 متداول" while all three venues were green.
    """


class RateLimiter:
    """Token-free sliding window over a fixed budget per minute.

    Hyperliquid allows 1200 weight/min per IP. We spend at most 900 by default,
    and we *reserve* before firing so a burst can never overshoot the ceiling.
    """

    def __init__(self, budget_per_min: int = 900, clock: Optional[Callable[[], float]] = None):
        self.budget = budget_per_min
        self._window: list[tuple[float, int]] = []
        self._clock = clock or (lambda: asyncio.get_event_loop().time())
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - 60.0
        self._window = [(t, w) for (t, w) in self._window if t > cutoff]

    def spent(self) -> int:
        self._prune(self._clock())
        return sum(w for _, w in self._window)

    def remaining(self) -> int:
        return max(0, self.budget - self.spent())

    def refusal(self, weight: int = 1) -> str:
        """Why `acquire(weight)` would fail, in words. Empty when it would pass."""
        self._prune(self._clock())
        used = sum(w for _, w in self._window)
        if used + weight > self.budget:
            return f"weight budget {used}/{self.budget} used, {weight} needed in this minute"
        return ""

    async def acquire(self, weight: int = 1) -> bool:
        """Reserve `weight`. Returns False when the budget cannot cover it now."""
        async with self._lock:
            now = self._clock()
            self._prune(now)
            if sum(w for _, w in self._window) + weight > self.budget:
                return False
            self._window.append((now, weight))
            return True

    async def wait_for(self, weight: int = 1, timeout_s: float = 30.0) -> bool:
        """Reserve, retrying until the window frees up or timeout."""
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            if await self.acquire(weight):
                return True
            await asyncio.sleep(1.0)
        return False


async def http_json(
    fetch: Fetcher,
    url: str,
    method: str = "GET",
    payload: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout_s: float = 15.0,
    retries: int = 2,
    backoff_s: float = 0.5,
) -> Any:
    """Fetch JSON with bounded retries and exponential backoff.

    Works with either signature the Worker world offers:
        fetch(url, opts)        -> Response-like
        fetch(url, **opts)      -> Response-like
    """
    hdrs = {"accept": "application/json", "user-agent": "cta-radar/2.0"}
    if headers:
        hdrs.update(headers)
    body = json.dumps(payload) if payload is not None else None
    if body:
        hdrs.setdefault("content-type", "application/json")

    last_error: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            resp = await _invoke(fetch, url, method, hdrs, body, timeout_s)
            status = getattr(resp, "status", 200)
            if status == 200:
                return await _read_json(resp)
            text = await _read_text(resp)
            err = HttpError(status, url, text)
            if not err.retryable or attempt == retries:
                raise err
            last_error = err
        except HttpError:
            raise
        except Exception as exc:  # network hiccup, timeout, bad JSON
            last_error = exc
            if attempt == retries:
                raise

        await asyncio.sleep(backoff_s * (2 ** attempt))

    raise last_error or RuntimeError(f"request failed: {url}")


async def _invoke(fetch: Fetcher, url: str, method: str, headers: dict,
                  body: Optional[str], timeout_s: float):
    opts = {"method": method, "headers": headers}
    if body is not None:
        opts["body"] = body
    try:
        return await fetch(url, opts)
    except TypeError:
        return await fetch(url, method=method, headers=headers, body=body)


async def _read_json(resp) -> Any:
    if hasattr(resp, "json"):
        try:
            result = resp.json()
            if isinstance(result, Awaitable):
                result = await result
            return result
        except TypeError:
            pass
    return json.loads(await _read_text(resp))


async def _read_text(resp) -> str:
    if hasattr(resp, "text"):
        text = resp.text
        if callable(text):
            text = text()
        if isinstance(text, Awaitable):
            text = await text
        if isinstance(text, str):
            return text
    if hasattr(resp, "body"):
        body = resp.body
        if isinstance(body, (bytes, bytearray)):
            return body.decode("utf-8", "replace")
        if isinstance(body, str):
            return body
    return ""


class Venue(Protocol):
    """Every adapter exposes exactly this."""

    name: str

    async def leaderboard(self) -> list[dict]: ...
    async def fills(self, wallet: str, limit: int = 2000) -> list[dict]: ...
    async def positions(self, wallet: str) -> list[dict]: ...
    async def prices(self) -> dict[str, float]: ...
