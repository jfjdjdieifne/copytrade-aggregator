"""Turn `(body, headers)` into what the Cloudflare runtime will actually serve.

Why this module exists
----------------------
Every route in `cta.worker` returns a plain Python tuple::

    return (json.dumps(payload), {"status": 200, "content-type": "application/json"})

That shape is deliberate — it keeps the routing logic ordinary testable Python
with no dependency on the runtime. But it is **not** a shape the Python runtime
can hand to the browser. Following a request through the current runtime
(workerd `python-entrypoint-helper.ts` -> `introspection.wrapper_func`) shows
only two return values survive:

* a real JavaScript ``Response`` (the helper unwraps ``FetchResponse`` via
  ``result?.js_response ?? result``), or
* a Workers-RPC value, which reaches JavaScript through
  ``python_to_rpc`` -> ``to_js``.

A Python ``tuple`` is neither. `python_to_rpc` explicitly raises on tuples
(``_raise_on_disabled_type`` -> "cannot be sent over RPC"), and a dict of a
string plus an integer ``status`` is not a JS ``Response``, so the platform has
nothing to serve and returns its generic **Error 1101 "Worker threw
exception"** page. That — not the constructor — is why the live deployment
stayed blank after the entrypoint was fixed.

So the entrypoint converts on the way out. `to_response` takes our tuple and
returns the runtime's own ``Response`` when it is available, and a small
duck-typed fallback otherwise (local preview, unit tests, plain CPython), so the
same code path is exercised everywhere.

The conversion is total: it never raises. A broken header or a non-serialisable
body turns into a 500 that explains itself rather than an anonymous error page.
"""

from __future__ import annotations

import json
from typing import Any

try:  # Cloudflare runtime only
    from workers import Response as _Response  # type: ignore
except Exception:  # pragma: no cover - local runs
    _Response = None


# ── the fallback used when the runtime's Response is not importable ──────────
class PlainResponse:
    """Minimal stand-in with the interface the preview server and tests read.

    Not a JS object, so it is never used on Cloudflare — `_Response` is present
    there. It exists so `to_response` has one behaviour on every platform and
    the preview server can keep serving tuples unchanged.
    """

    __slots__ = ("body", "status", "headers")

    def __init__(self, body: str = "", status: int = 200, headers: dict | None = None):
        self.body = body
        self.status = status
        self.headers = dict(headers or {})


def runtime_response_available() -> bool:
    """True when the real Workers ``Response`` can be constructed."""
    return _Response is not None


def _split(payload: Any) -> tuple:
    """Accept our tuple shape, a bare string, or anything already usable."""
    status = 200
    headers: dict = {}
    if isinstance(payload, tuple):
        if len(payload) == 2:
            body, extras = payload
            if isinstance(extras, dict):
                extras = dict(extras)
                status = _as_status(extras.pop("status", 200))
                headers = {str(k): str(v) for k, v in extras.items()}
            else:  # (body, status)
                body, status = payload[0], _as_status(payload[1])
            return body, status, headers
        if len(payload) == 1 or len(payload) == 0:
            return (payload[0] if payload else ""), status, headers
    return payload, status, headers


def _as_status(value: Any) -> int:
    try:
        status = int(value)
    except (TypeError, ValueError):
        return 200
    # The fetch spec only allows 200-599 for a constructed Response.
    return status if 200 <= status <= 599 else 200


def _as_text(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body, ensure_ascii=False, default=str)
    except Exception:
        return str(body)


def to_response(payload: Any):
    """Convert a route's return value into something the runtime can serve."""
    body, status, headers = _split(payload)

    # Already a Response (runtime, or one this module built earlier).
    if isinstance(payload, PlainResponse):
        return payload
    if _Response is not None and isinstance(payload, _Response):
        return payload

    text = _as_text(body)
    if "content-type" not in {k.lower() for k in headers}:
        headers["content-type"] = "text/plain; charset=utf-8"

    if _Response is not None:
        try:
            return _Response(text, status=status, headers=headers)
        except Exception as exc:  # pragma: no cover - runtime only
            # Never let a formatting problem become a blank 1101 page.
            return _Response(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"could not build response: {type(exc).__name__}: {exc}"[:300],
                    },
                    ensure_ascii=False,
                ),
                status=500,
                headers={"content-type": "application/json; charset=utf-8"},
            )

    return PlainResponse(text, status=status, headers=headers)


def crash_response(exc: Exception):
    """Explain a fatal error instead of letting the platform blank it out."""
    import traceback

    body = json.dumps(
        {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "where": traceback.format_exc().splitlines()[-3:],
            "hint": "افتح Workers Logs لرؤية التتبع الكامل",
        },
        ensure_ascii=False,
        default=str,
    )
    return to_response((body, {"status": 500, "content-type": "application/json; charset=utf-8"}))
