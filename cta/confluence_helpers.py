"""Small pure helpers shared by the engine and the convergence layer."""

from __future__ import annotations

from typing import Sequence

from .models import Position


def dedupe_positions(positions: Sequence[Position]) -> list[Position]:
    """One netted position per (wallet, coin), netting across venues.

    Aggregating here — not in the venue adapters — is what makes hedging visible:
    a wallet long on Hyperliquid and short on dYdX collapses to a tiny net number,
    which `strip_hedged` then removes.
    """
    buckets: dict[tuple[str, str], list[Position]] = {}
    for p in positions:
        buckets.setdefault((p.wallet, p.coin), []).append(p)

    out: list[Position] = []
    for (wallet, coin), group in buckets.items():
        if len(group) == 1:
            out.append(group[0])
            continue

        gross = sum(abs(p.size_usd) for p in group)
        if gross <= 0:
            continue

        # Weighted-average entry across the wallet's exposure on this coin.
        entry = sum(p.entry * abs(p.size_usd) for p in group) / gross
        signed_usd = sum(
            (p.size_usd if p.signed_size >= 0 else -p.size_usd) for p in group
        )
        net_usd = abs(signed_usd)
        if net_usd <= 0:
            continue

        best = max(group, key=lambda p: p.quality_score)
        newest = max(
            (p for p in group if p.opened_at is not None),
            key=lambda p: p.opened_at,   # type: ignore[arg-type]
            default=None,
        )

        out.append(
            Position(
                wallet=wallet,
                source="+".join(sorted({p.source for p in group})),
                coin=coin,
                signed_size=(net_usd / entry) if signed_usd >= 0 else -(net_usd / entry),
                entry=entry,
                size_usd=net_usd,
                leverage=max(p.leverage for p in group),
                opened_at=newest.opened_at if newest else None,
                stop=best.stop,
                target=best.target,
                quality=best.quality,
                quality_score=best.quality_score,
            )
        )
    return out


def merge_members(positions: Sequence[Position]) -> list[Position]:
    """Stable ordering helper — keeps tests and messages deterministic."""
    return sorted(positions, key=lambda p: (p.coin, p.wallet, p.entry))
