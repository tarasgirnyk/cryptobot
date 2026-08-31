"""Стан live-позиції: машина станів, персистенція, mark-to-market.

Позиція зберігається як dict (як у paper) — простіше для JSON і для звірки.
"""

from __future__ import annotations

import time
import uuid

from cryptobot import config, runtime
from cryptobot.scanner import quote_for
from cryptobot.storage import audit, persist_closed_live, persist_live


# Машина станів (див. ARCHITECTURE.md):
# PLANNED -> LEG_A_PENDING/LEG_B_PENDING -> HEDGED -> CLOSING -> CLOSED
#                        |                     |
#                        └----> RECOVERY <-----┘ ----> FAILED
PLANNED = "PLANNED"
LEG_PENDING = "LEG_PENDING"
HEDGED = "HEDGED"
CLOSING = "CLOSING"
CLOSED = "CLOSED"
RECOVERY = "RECOVERY"
FAILED = "FAILED"

OPEN_STATES = (PLANNED, LEG_PENDING, HEDGED, CLOSING, RECOVERY)


def new_position(plan, target_base_qty: float) -> dict:
    pid = uuid.uuid4().hex[:10]
    now = int(time.time() * 1000)
    return {
        "id": pid,
        "clientPrefix": f"cb{pid}",
        "symbol": plan.symbol,
        "state": PLANNED,
        "mode": config.AUTOMATION_MODE,
        "longExchange": plan.long_exchange,
        "shortExchange": plan.short_exchange,
        "notional": plan.notional_usdt,
        "leverage": plan.leverage,
        "targetBaseQty": target_base_qty,
        "openedAt": now,
        "hedgedAt": None,
        "closedAt": None,
        "entryRefSpreadPct": plan.entry_ref_spread_pct,
        "entryExecutableSpreadPct": plan.entry_executable_spread_pct,
        "roundTripFeesPct": plan.round_trip_fees_pct,
        "legs": {
            "long": _blank_leg("buy"),
            "short": _blank_leg("sell"),
        },
        "closeLegs": {},
        "closeReason": None,
        "realizedPnl": None,
        "realizedPnlPct": None,
        "note": "",
    }


def _blank_leg(side: str) -> dict:
    return {
        "side": side,
        "clientId": None,
        "orderId": None,
        "filledBase": 0.0,
        "avgPrice": 0.0,
        "status": "new",
    }


def store_put(position: dict) -> None:
    with runtime.live_lock:
        runtime.live_positions[position["id"]] = position
    persist_live(position)


def store_close(position: dict) -> None:
    with runtime.live_lock:
        runtime.live_positions.pop(position["id"], None)
        runtime.live_closed.appendleft(position)
    persist_closed_live(position)


def set_state(position: dict, state: str, **fields) -> dict:
    position["state"] = state
    position.update(fields)
    store_put(position)
    audit("live_state", {"id": position["id"], "symbol": position["symbol"], "state": state})
    return position


def live_snapshot() -> list[dict]:
    with runtime.live_lock:
        return [dict(row) for row in runtime.live_positions.values()]


def open_live_positions() -> list[dict]:
    return [row for row in live_snapshot() if row.get("state") in OPEN_STATES]


def mark_live(position: dict) -> dict:
    """Поточний PNL за котируваннями сканера — та сама формула, що в paper."""
    long_q = quote_for(position["longExchange"], position["symbol"])
    short_q = quote_for(position["shortExchange"], position["symbol"])
    if not long_q or not short_q:
        return {**position, "markStatus": "market-unavailable", "unrealizedPnl": 0}
    long_bid = long_q["bid"]
    short_ask = short_q["ask"]
    if min(long_bid, short_ask) <= 0:
        return {**position, "markStatus": "market-unavailable", "unrealizedPnl": 0}
    current_gross = (short_ask - long_bid) / long_bid * 100
    entry_spread = position.get("entryExecutableSpreadPct") or position.get("entryRefSpreadPct", 0)
    captured_pct = entry_spread - current_gross
    pnl_pct = captured_pct - position.get("roundTripFeesPct", 0)
    return {
        **position,
        "currentGrossPct": current_gross,
        "unrealizedPnlPct": pnl_pct,
        "unrealizedPnl": position.get("notional", 0) * pnl_pct / 100,
        "markStatus": "open",
    }


def exit_reason(marked: dict, now_ms: float | None = None) -> str | None:
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    pnl_pct = marked.get("unrealizedPnlPct", 0)
    current_gross = marked.get("currentGrossPct")
    if pnl_pct <= config.PAPER_TOTAL_STOP_PCT:
        return "total_stop"
    if current_gross is not None and current_gross <= config.PAPER_EXIT_GROSS_PCT:
        return "spread_converged"
    if now_ms - float(marked.get("openedAt") or 0) >= config.PAPER_MAX_HOLD_HOURS * 3_600_000:
        return "max_hold"
    return None
