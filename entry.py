"""Cloudflare Worker entrypoint — the file `wrangler.toml` points at.

Two handler styles exist and we support both, because which one the runtime
dispatches depends on `compatibility_date`:

* **class-based** — `class Default(WorkerEntrypoint)` with `fetch`/`scheduled`
  methods. This is what every current Cloudflare Python example uses and what
  we rely on in production.
* **legacy top-level** — `async def on_fetch(...)` / `on_scheduled(...)`, kept
  as a fallback for older accounts. Cloudflare deprecated this shape in
  August 2025; it now needs the `disable_python_no_global_handlers` flag.

The class methods read their bindings from `self.env`, but `cta.worker.handle`
takes `env` explicitly, so all the routing logic stays ordinary testable Python.

`RadarState` is re-exported at the bottom of this module on purpose. Cloudflare
resolves a Durable Object's `class_name` **inside the entrypoint module**, not in
whatever module happens to define it, so leaving it in `cta.durable_object`
alone makes deployment fail with "no registered event handlers" / an unresolved
class name. The import is what binds the name here.
"""

from __future__ import annotations

import os
import sys

# Workers Builds runs `pip install .` before `wrangler deploy`, which leaves a
# second copy of the package at build/lib/cta. Only the entrypoint's own
# directory is on sys.path, so `import cta` must resolve to the copy that sits
# next to this file. Say so explicitly rather than relying on import order.
_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(_HERE, "cta")) and _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from cta.http_bridge import crash_response, to_response  # noqa: E402
from cta.worker import handle, scheduled  # noqa: E402

try:  # Cloudflare runtime only
    from workers import WorkerEntrypoint  # type: ignore
except Exception:  # pragma: no cover - local test runs and plain CPython

    class WorkerEntrypoint:  # type: ignore
        """Fallback base class so this module imports under pytest."""

        def __init__(self, ctx=None, env=None):
            self.ctx = ctx
            self.env = env


class Default(WorkerEntrypoint):
    """Primary entrypoint: HTTP routes and the cron trigger.

    `__init__` is declared explicitly on purpose. The SDK's `_wrap_class` turns
    the constructor into `wrapped_init(self, *args)`, and binds the bindings
    object as **args[1]** — after wrapping it in `_EnvWrapper`:

        if len(args) > 1:
            args[1] = _EnvWrapper(args[1])

    A subclass with no `__init__` of its own never accepts that second argument,
    so `self.env` stays None and every route that touches state raises. That is
    exactly the Error 1101 this deployment produced. Declaring the parameter
    here is what makes the binding reach us.
    """

    def __init__(self, ctx, env):
        self.ctx = ctx
        self.env = env

    async def fetch(self, request, env=None, ctx=None):
        # The runtime calls fetch(request); env is already on the instance.
        #
        # Two conversions matter here:
        #   1. `to_response` — cta.worker returns `(body, headers)` tuples, which
        #      the runtime cannot serve. Only a JS Response (or an RPC value)
        #      survives the trip back; a tuple becomes Error 1101. This is the
        #      conversion whose absence produced the blank live page.
        #   2. the try/except — anything unexpected must be rendered as text the
        #      operator can act on, not an anonymous "Worker threw exception".
        try:
            payload = await handle(
                request,
                env if env is not None else getattr(self, "env", None),
                ctx if ctx is not None else getattr(self, "ctx", None),
            )
            return to_response(payload)
        except Exception as exc:
            return crash_response(exc)

    async def scheduled(self, controller=None, env=None, ctx=None):
        # Called as scheduled(controller, env, ctx); env is bound on the instance
        # already, so never trust the positional args here.
        return await scheduled(self.env, self.ctx)


# ── Durable Object ──────────────────────────────────────────────────────────
# `wrangler.toml` declares `class_name = "RadarState"`. Cloudflare looks that
# name up in THIS module, so it must be bound here even though the
# implementation lives in cta.durable_object.
from cta.durable_object import RadarState  # noqa: E402,F401

# ── legacy top-level handlers ───────────────────────────────────────────────
# Harmless when the class-based dispatcher is in use; required if a deployment
# is on an older compatibility date.


async def on_fetch(request, env, ctx=None):
    # Same conversion as Default.fetch: a tuple is not servable.
    try:
        return to_response(await handle(request, env, ctx))
    except Exception as exc:
        return crash_response(exc)


async def on_scheduled(env, ctx=None):
    return await scheduled(env, ctx)
