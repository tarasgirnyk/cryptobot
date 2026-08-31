"""Risk-фільтр кандидатів перед входом (paper і live) та тверді live-ліміти."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from cryptobot import config, funding
from cryptobot.runtime import symbol_cooldowns


def _leg(candidate, side):
    """Синтезує лег-дікт для модулю funding з полів кандидата."""
    return {
        "exchange": candidate.get(f"{side}Exchange", ""),
        "funding": candidate.get(f"{side}FundingPct", 0.0) / 100,
        "nextFunding": candidate.get(f"{side}NextFunding", 0),
    }


def candidate_rejection_reason(candidate, opened=(), depth=None, now=None, notional=None):
    now = time.time() if now is None else now
    symbol = candidate["symbol"]
    if any(row.get("symbol") == symbol for row in opened):
        return "already_open"
    same_route = sum(
        row.get("longExchange") == candidate["longExchange"]
        and row.get("shortExchange") == candidate["shortExchange"]
        for row in opened
    )
    if same_route >= config.MAX_SAME_ROUTE_POSITIONS:
        return "route_limit"
    if candidate["netSpreadPct"] < config.PAPER_ENTRY_NET_PCT:
        return "net_spread"
    top_price_capture = (
        candidate["grossSpreadPct"]
        - config.PAPER_EXIT_GROSS_PCT
        - candidate["roundTripFeesPct"]
    )
    if top_price_capture < config.PAPER_MIN_PRICE_CAPTURE_PCT:
        return "price_capture"
    if candidate["minTurnover24h"] < config.MIN_TURNOVER_USDT:
        return "turnover"
    if candidate.get("warning"):
        return candidate["warning"]
    if funding.blocks_entry(_leg(candidate, "long"), _leg(candidate, "short"), now * 1000):
        return "funding_window"
    if symbol_cooldowns.get(symbol, 0) > now:
        return "cooldown"
    if depth is None:
        return None
    # На малому номіналі глибина стакана рідко є вузьким місцем — лишаємо тільки
    # захист від фактично порожнього стакана.
    small_order = notional is not None and notional < config.DEPTH_GATE_MIN_NOTIONAL
    if small_order:
        if depth["longFillPct"] <= 0 or depth["shortFillPct"] <= 0:
            return "depth_empty"
        return None
    if depth["longFillPct"] < 99.5 or depth["shortFillPct"] < 99.5:
        return "depth_fill"
    if depth["slippagePct"] > config.MAX_SLIPPAGE_PCT:
        return "slippage"
    executable_net = candidate["netSpreadPct"] - depth["slippagePct"]
    if executable_net < config.PAPER_ENTRY_NET_PCT:
        return "executable_net"
    executable_price_capture = (
        depth["executableSpreadPct"]
        - config.PAPER_EXIT_GROSS_PCT
        - candidate["roundTripFeesPct"]
    )
    if executable_price_capture < config.PAPER_MIN_PRICE_CAPTURE_PCT:
        return "executable_price_capture"
    return None


class HardLimitHit(Exception):
    """Тверда межа live-ризику — вхід заборонено."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utc_day(now_ms: float) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class RiskEngine:
    """Тверді ліміти для live: кількість/номінал позицій і денний збиток.

    ``check`` кидає :class:`HardLimitHit`. Денний збиток рахується від закритих
    live-угод через ``register_close`` і скидається на межі доби UTC.
    """

    def __init__(self):
        self._day: str | None = None
        self._day_realized = 0.0

    def _roll(self, now_ms: float) -> None:
        day = _utc_day(now_ms)
        if day != self._day:
            self._day = day
            self._day_realized = 0.0

    def register_close(self, realized_pnl: float, now_ms: float | None = None) -> None:
        now_ms = time.time() * 1000 if now_ms is None else now_ms
        self._roll(now_ms)
        self._day_realized += float(realized_pnl or 0)

    def daily_loss(self, now_ms: float | None = None) -> float:
        now_ms = time.time() * 1000 if now_ms is None else now_ms
        self._roll(now_ms)
        return self._day_realized

    def daily_loss_tripped(self, now_ms: float | None = None) -> bool:
        return self.daily_loss(now_ms) <= -abs(config.LIVE_MAX_DAILY_LOSS_USDT)

    def check(self, plan, live_open, now_ms: float | None = None) -> None:
        now_ms = time.time() * 1000 if now_ms is None else now_ms
        if self.daily_loss_tripped(now_ms):
            raise HardLimitHit("daily_loss")
        if len(live_open) >= config.MAX_OPEN_POSITIONS:
            raise HardLimitHit("max_positions")
        if plan.notional_usdt > config.LIVE_MAX_NOTIONAL_PER_POS:
            raise HardLimitHit("notional_per_pos")
        open_notional = sum(float(row.get("notional") or 0) for row in live_open)
        if open_notional + plan.notional_usdt > config.LIVE_MAX_TOTAL_NOTIONAL:
            raise HardLimitHit("total_notional")
