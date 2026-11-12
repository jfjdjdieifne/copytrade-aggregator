"""Cloudflare Worker entrypoint — the file `wrangler.toml` points at.

Wrangler resolves `async def on_fetch(request, env, ctx)` and, with
`[python] experimental_cron = true`, `async def on_scheduled(env, ctx)`.

Both delegate immediately to `cta.worker`, which is ordinary, testable Python.
"""

from __future__ import annotations

from cta.worker import handle, scheduled


async def on_fetch(request, env, ctx=None):
    return await handle(request, env, ctx)


async def on_scheduled(env, ctx=None):
    return await scheduled(env, ctx)
