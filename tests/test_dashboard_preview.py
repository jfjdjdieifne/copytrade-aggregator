"""The local preview server and dashboard renderer.

These guard the thing the user actually looks at. A dashboard that raises on a
missing field is worse than no dashboard, so the renderer is tested against
sparse input as well as the full scenario.
"""

from __future__ import annotations

import json

import pytest

from cta.dashboard import render_dashboard
from cta.preview_server import build_snapshot, narrate_signal_dict


@pytest.fixture(scope="module")
def snapshot():
    return build_snapshot()


# ── renderer ────────────────────────────────────────────────────────────────


def test_dashboard_renders_full_scenario(snapshot):
    html = render_dashboard(
        snapshot["signals"],
        snapshot["watchlist"],
        snapshot["stats"],
        snapshot["rejections"],
    )
    assert html.startswith("<!doctype html>") or html.lstrip().startswith("<!DOCTYPE")
    assert 'dir="rtl"' in html
    assert "BTC" in html
    assert "HYPOTHETICAL_" in html  # the disclaimer must never disappear


def test_dashboard_survives_empty_everything():
    """An empty radar is a normal state at 3am, not a crash."""
    html = render_dashboard([], [], {}, [])
    assert isinstance(html, str)
    assert "HYPOTHETICAL_" in html


def test_dashboard_survives_none_last_cycle():
    """Regression: html.escape(None) raised AttributeError and killed the page."""
    html = render_dashboard([], [], {}, [], last_cycle=None)
    assert isinstance(html, str)


def test_dashboard_escapes_untrusted_text():
    """Coin names come from venues; they must never be injected as raw HTML."""
    evil = {"coin": "<script>alert(1)</script>", "side": "LONG"}
    html = render_dashboard([evil], [], {}, [])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_dashboard_marks_live_versus_demo():
    live = render_dashboard([], [], {}, [], live=True)
    demo = render_dashboard([], [], {}, [], live=False)
    assert "بيانات حيّة" in live
    assert "عرض تجريبي" in demo
    assert "عرض تجريبي" not in live


def test_dashboard_is_self_contained():
    """No CDN, no fonts, no outbound requests: it must work offline and on 3G."""
    html = render_dashboard([], [], {}, [])
    assert "http://" not in html
    assert "https://" not in html
    assert "<link" not in html
    assert "<script" not in html


def test_watchlist_table_scrolls_instead_of_overflowing_on_phone():
    """Seven columns on a 375px screen must scroll inside the card, not drag the page."""
    html = render_dashboard([], [{"wallet": "0xabc", "source": "gmx", "grade": "REJECT",
                                  "score": 0, "n_trades": 260, "profit_factor": 0.7,
                                  "max_drawdown": 0.12}], {}, [])
    assert 'class="tablewrap"' in html
    assert "overflow-x:auto" in html
    assert "min-width:540px" in html


# ── scenario ────────────────────────────────────────────────────────────────


def test_snapshot_emits_the_expected_signal(snapshot):
    assert len(snapshot["signals"]) == 1
    sig = snapshot["signals"][0]
    assert sig["coin"] == "BTC"
    assert sig["side"] == "LONG"
    assert sig["score"] >= 80
    assert sig["level"] == "STRONG"
    assert sig["n_independent"] == 3


def test_snapshot_plan_has_a_tradeable_risk_reward(snapshot):
    plan = snapshot["signals"][0]["plan"]
    assert plan["stop"] < plan["entry"] < plan["target"]
    assert plan["rr"] >= 1.5


def test_snapshot_rejections_explain_themselves(snapshot):
    coins = {r["coin"] for r in snapshot["rejections"]}
    assert {"ETH", "SOL", "ARB"} <= coins
    assert all(r["reason"] for r in snapshot["rejections"])


def test_snapshot_watchlist_spans_grades(snapshot):
    """A roster where every wallet is ELITE proves the classifier does nothing."""
    grades = {w["grade"] for w in snapshot["watchlist"]}
    assert len(grades) >= 3
    assert "REJECT" in grades


def test_snapshot_is_json_serialisable(snapshot):
    """The /api/signals route serialises this directly."""
    json.dumps(snapshot, ensure_ascii=False)


def test_narrative_is_arabic_and_quotes_real_numbers(snapshot):
    text = narrate_signal_dict(snapshot["signals"][0])
    assert "BTC" in text
    assert "شراء" in text or "بيع" in text
    assert "HYPOTHETICAL_" in text
    # The narrator must state the score it was given, not invent one.
    assert str(snapshot["signals"][0]["score"]) in text
