"""Незмінний план угоди з терміном дії."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    long_exchange: str
    short_exchange: str
    notional_usdt: float
    leverage: int
    created_at_ms: int
    expires_at_ms: int
    expected_net_pct: float
    entry_ref_spread_pct: float  # grossSpreadPct на момент плану
    round_trip_fees_pct: float
    long_ref_price: float  # long ask
    short_ref_price: float  # short bid
    entry_executable_spread_pct: float  # VWAP-спред зі стакана

    def expired(self, now_ms: float | None = None) -> bool:
        now_ms = time.time() * 1000 if now_ms is None else now_ms
        return now_ms >= self.expires_at_ms

    @classmethod
    def from_candidate(
        cls,
        candidate: dict,
        depth: dict,
        *,
        notional: float,
        leverage: int,
        ttl_sec: float,
        now_ms: float | None = None,
    ) -> "TradePlan":
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return cls(
            symbol=candidate["symbol"],
            long_exchange=candidate["longExchange"],
            short_exchange=candidate["shortExchange"],
            notional_usdt=float(notional),
            leverage=int(leverage),
            created_at_ms=now_ms,
            expires_at_ms=now_ms + int(ttl_sec * 1000),
            expected_net_pct=float(candidate["netSpreadPct"] - depth.get("slippagePct", 0)),
            entry_ref_spread_pct=float(candidate["grossSpreadPct"]),
            round_trip_fees_pct=float(candidate["roundTripFeesPct"]),
            long_ref_price=float(candidate.get("longAsk") or 0),
            short_ref_price=float(candidate.get("shortBid") or 0),
            entry_executable_spread_pct=float(depth.get("executableSpreadPct", 0)),
        )
