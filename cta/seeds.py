"""Wallets we trust, so discovery can start before it has finished searching.

Why this module exists
----------------------
Hyperliquid publishes its leaderboard as a single ~39 MB JSON file of ~47,000
wallets. On the Workers **free plan** an invocation gets **10 ms of CPU** and
128 MB of memory. Downloading and deserialising that file cannot fit, and the
failure is silent: the invocation is terminated after the cheap health probes
have already written "three green platforms", and before the discovery cursor is
saved. The dashboard therefore reports green platforms, climbing cycle counts,
and a search that has never started — which is exactly what the operator saw,
repeatedly.

There is no way to make 47k rows cheap by trimming fields: the download and
`JSON.parse` happen before any Python code runs. So discovery must not *depend*
on the leaderboard for the radar to be watching somebody.

Design
------
A **seed** wallet is one we can poll directly. `clearinghouseState` costs one
subrequest for one address — cheap enough to run every minute within the budget.
Verified seeds give the radar something real to watch from the first tick, while
the patient leaderboard search continues in the background and adds to them.

Two consequences worth stating plainly
--------------------------------------
1. Seeding does not replace the search, it de-risks it. If the leaderboard never
   loads, the radar still watches the seeds. If it does load, the watchlist
   grows from the seeds upward.
2. Seeds are *unverified* until their fills are scored. A seed whose history
   fails the gates is dropped rather than silently trusted, because a watchlist
   of unvetted addresses would produce signals from unknown traders — worse than
   watching nobody.

Provenance
----------
Every seed records where it came from. An operator adding their own address and
a default list of well-known Hyperliquid wallets are different things, and the
watchlist should say which is which.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# An EVM address, and nothing else. Validating here stops a typo becoming a
# permanent zero-subrequest entry in the watchlist that silently never returns
# positions.
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def normalise(address: Any) -> str | None:
    """Return a canonical lowercase address, or None if it is not one."""
    text = str(address or "").strip()
    if not text:
        return None
    if not text.startswith("0x"):
        text = "0x" + text
    if not _ADDRESS.match(text):
        return None
    return text.lower()


def parse_addresses(raw: Any) -> list[str]:
    """Pull valid addresses out of whatever an operator pasted.

    People paste addresses with spaces, commas, newlines, and sometimes a whole
    explorer URL. Accepting all of it costs nothing and avoids the frustrating
    "it ignored my wallet" that looks like a bug in the radar.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        pieces: Iterable[Any] = raw
    else:
        pieces = re.split(r"[\s,;]+", str(raw))

    out: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        text = str(piece or "").strip()
        if not text:
            continue
        # Accept a full explorer URL: .../address/0xabc...
        if "/" in text:
            text = text.rstrip("/").rsplit("/", 1)[-1]
        addr = normalise(text)
        if addr and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


# ── defaults ────────────────────────────────────────────────────────────────
# Deliberately empty.
#
# Shipping a hard-coded list of popular Hyperliquid wallets would make the bot
# appear to work on first deploy while quietly copying strangers whose history
# we never scored. The honest starting state is "no verified seeds yet, the
# search is running", which is what the dashboard now says. An operator who has
# a trader they trust adds them with /add.
DEFAULT_SEEDS: tuple[str, ...] = ()


def seeds_from_config(cfg) -> list[str]:
    """Read seeds from configuration, tolerating a comma or space separated string."""
    raw = getattr(cfg, "seed_wallets", "")
    return parse_addresses(raw)


def merge_seeds(existing: Iterable[str], fresh: Iterable[str], limit: int = 200) -> list[str]:
    """Union two seed sets, order preserved, capped.

    Order is preserved rather than sorted so the operator's most recent addition
    is not buried, and the cap keeps a pasted list of thousands from making each
    tick unaffordable.
    """
    out: list[str] = []
    seen: set[str] = set()
    for source in (existing, fresh):
        for item in source or []:
            addr = normalise(item)
            if addr and addr not in seen:
                seen.add(addr)
                out.append(addr)
            if len(out) >= limit:
                return out
    return out


def describe(seeds: list[str]) -> str:
    """One line for `/health`, so seeding is visible rather than implicit."""
    count = len(seeds or [])
    if not count:
        return "لا محافظ مبدئية — البحث وحده يعمل"
    return f"{count} محفظة مبدئية تُراقَب فوراً"
