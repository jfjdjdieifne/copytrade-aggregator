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
    """Primary entrypoint: HTTP routes and the cron trigger."""

    async def fetch(self, request, env=None, ctx=None):
        return await handle(request, env or self.env, ctx or self.ctx)

    async def scheduled(self, controller=None, env=None, ctx=None):
        # The class-based runtime calls `scheduled(controller, env, ctx)`; the
        # env is always supplied, so the defaults below only matter in tests.
        return await scheduled(env or self.env, ctx)


# ── legacy top-level handlers ───────────────────────────────────────────────
# Harmless when the class-based dispatcher is in use; required if a deployment
# is on an older compatibility date.


async def on_fetch(request, env, ctx=None):
    return await handle(request, env, ctx)


async def on_scheduled(env, ctx=None):
    return await scheduled(env, ctx)
