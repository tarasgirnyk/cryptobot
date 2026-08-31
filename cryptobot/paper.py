"""Віртуальні (paper) позиції, mark-to-market і метрики готовності до demo."""

from __future__ import annotations

import time
import uuid

from cryptobot import config, runtime
from cryptobot.depth import depth_analysis
from cryptobot.scanner import cache, opportunity_for, quote_for
from cryptobot.storage import audit, persist_closed_paper, persist_paper
from cryptobot.util import number


def market_health(payload=None, now_ms=None):
    payload = payload or cache.get("payload") or {}
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    generated_at = int(number(payload.get("generatedAt"), 0))
    age_sec = max(0.0, (now_ms - generated_at) / 1000) if generated_at else None
    exchanges = set(payload.get("exchanges") or [])
    errors = payload.get("errors") or []
    if generated_at == 0:
        status = "starting"
    elif age_sec is not None and age_sec > config.MARKET_STALE_SEC:
        status = "stale"
    elif exchanges != set(config.ENABLED_EXCHANGES) or errors:
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "ageSec": age_sec,
        "exchanges": sorted(exchanges),
        "errors": errors,
    }


def mark_paper(position):
    long_quote = quote_for(position["longExchange"], position["symbol"])
    short_quote = quote_for(position["shortExchange"], position["symbol"])
    if not long_quote or not short_quote:
        return {**position, "status": "market-unavailable", "unrealizedPnl": 0}
    exit_long_bid = long_quote["bid"]
    exit_short_ask = short_quote["ask"]
    if min(exit_long_bid, exit_short_ask) <= 0:
        return {**position, "status": "market-unavailable", "unrealizedPnl": 0}
    current_gross = (exit_short_ask - exit_long_bid) / exit_long_bid * 100
    entry_spread = position.get("entryExecutableSpreadPct", position["entryGrossPct"])
    captured_pct = entry_spread - current_gross
    pnl_pct = captured_pct - position["roundTripFeesPct"]
    return {
        **position,
        "currentGrossPct": current_gross,
        "unrealizedPnlPct": pnl_pct,
        "unrealizedPnl": position["notional"] * pnl_pct / 100,
        "status": "open",
    }


def paper_snapshot():
    with runtime.paper_lock:
        opened = [mark_paper(row) for row in runtime.paper_positions.values()]
        return {"open": opened, "closed": list(runtime.paper_closed)}


def open_paper(symbol: str, notional: float, depth=None):
    current = opportunity_for(symbol)
    if not current:
        raise ValueError("Символ відсутній у поточному скані")
    if depth is None:
        depth = depth_analysis(symbol, notional)
    position = {
        "id": uuid.uuid4().hex[:10],
        "symbol": symbol,
        "openedAt": int(time.time() * 1000),
        "notional": notional,
        "longExchange": current["longExchange"],
        "shortExchange": current["shortExchange"],
        "entryGrossPct": current["grossSpreadPct"],
        "entryExecutableSpreadPct": depth["executableSpreadPct"],
        "entrySlippagePct": depth["slippagePct"],
        "entryFundingEffectPct": current["fundingEffectPct"],
        "roundTripFeesPct": current["roundTripFeesPct"],
    }
    with runtime.paper_lock:
        runtime.paper_positions[position["id"]] = position
    persist_paper(position)
    marked = mark_paper(position)
    audit("paper_open", marked)
    return marked


def close_paper(position_id: str, reason="manual"):
    with runtime.paper_lock:
        position = runtime.paper_positions.pop(position_id, None)
        if not position:
            raise ValueError("Paper-позицію не знайдено")
        closed = mark_paper(position)
        closed.update(
            {
                "status": "closed",
                "closedAt": int(time.time() * 1000),
                "closeReason": reason,
            }
        )
        runtime.paper_closed.appendleft(closed)
    persist_closed_paper(closed)
    audit("paper_close", closed)
    return closed


def paper_metrics(snapshot=None, now_ms=None):
    snapshot = snapshot or paper_snapshot()
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    opened = snapshot["open"]
    closed = snapshot["closed"]
    realized = sum(number(row.get("unrealizedPnl")) for row in closed)
    unrealized = sum(number(row.get("unrealizedPnl")) for row in opened)
    wins = [number(row.get("unrealizedPnl")) for row in closed if number(row.get("unrealizedPnl")) > 0]
    losses = [number(row.get("unrealizedPnl")) for row in closed if number(row.get("unrealizedPnl")) < 0]
    stop_count = sum(row.get("closeReason") == "total_stop" for row in closed)
    stop_rate = stop_count / len(closed) * 100 if closed else 0.0
    started_values = [
        int(number(row.get("openedAt")))
        for row in [*opened, *closed]
        if number(row.get("openedAt")) > 0
    ]
    started_at = min(started_values) if started_values else None
    observation_days = (now_ms - started_at) / 86_400_000 if started_at else 0.0

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in sorted(closed, key=lambda item: int(number(item.get("closedAt")))):
        cumulative += number(row.get("unrealizedPnl"))
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    market = market_health(now_ms=now_ms)
    unavailable_positions = sum(row.get("status") != "open" for row in opened)
    checks = [
        {
            "key": "sample",
            "ok": len(closed) >= config.READINESS_MIN_CLOSED_TRADES,
            "value": len(closed),
            "target": config.READINESS_MIN_CLOSED_TRADES,
        },
        {
            "key": "duration",
            "ok": observation_days >= config.READINESS_MIN_DAYS,
            "value": observation_days,
            "target": config.READINESS_MIN_DAYS,
        },
        {"key": "realizedPnl", "ok": realized > 0, "value": realized, "target": "> 0"},
        {
            "key": "stopRate",
            "ok": stop_rate <= config.READINESS_MAX_STOP_RATE_PCT,
            "value": stop_rate,
            "target": config.READINESS_MAX_STOP_RATE_PCT,
        },
        {"key": "marketData", "ok": market["status"] == "ok", "value": market["status"], "target": "ok"},
        {"key": "positionsAvailable", "ok": unavailable_positions == 0, "value": unavailable_positions, "target": 0},
    ]
    return {
        "openTrades": len(opened),
        "closedTrades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "winRatePct": len(wins) / len(closed) * 100 if closed else 0.0,
        "realizedPnl": realized,
        "unrealizedPnl": unrealized,
        "totalPnl": realized + unrealized,
        "averageClosedPnl": realized / len(closed) if closed else 0.0,
        "profitFactor": sum(wins) / abs(sum(losses)) if losses else None,
        "maxDrawdown": max_drawdown,
        "stopCount": stop_count,
        "stopRatePct": stop_rate,
        "observationStartedAt": started_at,
        "observationDays": observation_days,
        "market": market,
        "checks": checks,
        "readyForDemo": all(check["ok"] for check in checks),
    }
