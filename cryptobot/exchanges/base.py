"""Інтерфейс торгового клієнта біржі та його ccxt-реалізація.

Клієнт свідомо вузький: рівно те, що потрібно execution-двигуну — плече,
market-ордер, стан ордера, позиція, вільна маржа, скасування, час сервера.
Уся арифметика прослизання/спреду лишається поза цим шаром.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


# --- Помилки ----------------------------------------------------------------
class ExchangeOpError(Exception):
    """Базова помилка торгової операції."""


class NotSupported(ExchangeOpError):
    """Операція недоступна для цієї біржі (напр., MEXC futures API)."""


class OrderRejected(ExchangeOpError):
    """Біржа відхилила ордер (маржа, ціна, розмір, статус контракту)."""


class ExchangeBanned(ExchangeOpError):
    """Акаунт заблоковано / призупинено — сигнал для ротації ключів."""


# --- Значення -------------------------------------------------------------
@dataclass
class OrderResult:
    id: str | None
    client_id: str | None
    symbol: str
    side: str  # "buy" | "sell"
    status: str  # "open" | "closed" | "canceled" | "rejected" | "unknown"
    filled_base: float
    avg_price: float
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_done(self) -> bool:
        return self.status in ("closed", "canceled", "rejected")

    @property
    def is_filled(self) -> bool:
        return self.status == "closed" and self.filled_base > 0


@dataclass
class Position:
    symbol: str
    base_qty: float  # знаковий: + long, - short
    entry_price: float
    liq_price: float
    mark_price: float
    margin_ratio: float
    notional: float
    unrealized_pnl: float
    raw: dict = field(default_factory=dict, repr=False)


_STATUS_MAP = {
    "open": "open",
    "closed": "closed",
    "filled": "closed",
    "canceled": "canceled",
    "cancelled": "canceled",
    "rejected": "rejected",
    "expired": "rejected",
}


# --- Інтерфейс ----------------------------------------------------------------
class ExchangeClient:
    name: str

    def load(self) -> None:  # pragma: no cover - тонкий прохід
        raise NotImplementedError

    def unified(self, symbol: str) -> str:
        """Внутрішній ``BTCUSDT`` -> ccxt-символ ``BTC/USDT:USDT``."""
        raise NotImplementedError

    def has_market(self, symbol: str) -> bool:
        raise NotImplementedError

    def set_leverage(self, symbol: str, leverage: int) -> None:
        raise NotImplementedError

    def amount_for_notional(self, symbol: str, notional_usdt: float, price: float) -> float:
        raise NotImplementedError

    def market_order(
        self, symbol: str, side: str, base_qty: float, client_id: str, reduce_only: bool = False
    ) -> OrderResult:
        raise NotImplementedError

    def fetch_order_result(self, symbol: str, order_id: str) -> OrderResult:
        raise NotImplementedError

    def position(self, symbol: str) -> Position | None:
        raise NotImplementedError

    def free_collateral(self) -> float:
        raise NotImplementedError

    def cancel_all(self, symbol: str) -> None:
        raise NotImplementedError

    def server_time_ms(self) -> int:
        raise NotImplementedError


# --- ccxt-реалізація --------------------------------------------------------
def _base_of(symbol: str) -> str:
    if not symbol.endswith("USDT"):
        raise NotSupported(f"Очікується символ *USDT, отримано {symbol!r}")
    return symbol[:-4]


class CcxtExchangeClient(ExchangeClient):
    def __init__(self, name: str, exchange: Any):
        self.name = name
        self.ccxt = exchange
        self._loaded = False

    # -- метадані --
    def load(self) -> None:
        self.ccxt.load_markets()
        self._loaded = True

    def unified(self, symbol: str) -> str:
        return f"{_base_of(symbol)}/USDT:USDT"

    def has_market(self, symbol: str) -> bool:
        try:
            return self.unified(symbol) in (self.ccxt.markets or {})
        except NotSupported:
            return False

    def market(self, symbol: str) -> dict:
        return self.ccxt.market(self.unified(symbol))

    # -- торгівля --
    def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            self.ccxt.set_leverage(int(leverage), self.unified(symbol))
        except Exception as exc:  # noqa: BLE001 - нормалізуємо нижче
            msg = str(exc).lower()
            if "not modified" in msg or "leverage not changed" in msg or "-4046" in msg:
                return  # плече вже виставлене — не помилка
            raise _translate(exc)

    def amount_for_notional(self, symbol: str, notional_usdt: float, price: float) -> float:
        if price <= 0:
            raise OrderRejected("Ціна для розрахунку обсягу має бути > 0")
        raw_amount = notional_usdt / price
        precise = self.ccxt.amount_to_precision(self.unified(symbol), raw_amount)
        return float(precise)

    def market_order(
        self, symbol: str, side: str, base_qty: float, client_id: str, reduce_only: bool = False
    ) -> OrderResult:
        params: dict[str, Any] = {"clientOrderId": client_id}
        if reduce_only:
            params["reduceOnly"] = True
        try:
            order = self.ccxt.create_order(
                self.unified(symbol), "market", side, base_qty, None, params
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc)
        return _parse_order(order, symbol, side, client_id)

    def fetch_order_result(self, symbol: str, order_id: str) -> OrderResult:
        try:
            order = self.ccxt.fetch_order(order_id, self.unified(symbol))
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc)
        return _parse_order(order, symbol, order.get("side", ""), order.get("clientOrderId"))

    def position(self, symbol: str) -> Position | None:
        try:
            rows = self.ccxt.fetch_positions([self.unified(symbol)])
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc)
        for row in rows or []:
            qty = abs(float(row.get("contracts") or 0)) * float(row.get("contractSize") or 1)
            if qty <= 0:
                continue
            signed = qty if str(row.get("side")).lower() == "long" else -qty
            return Position(
                symbol=symbol,
                base_qty=signed,
                entry_price=float(row.get("entryPrice") or 0),
                liq_price=float(row.get("liquidationPrice") or 0),
                mark_price=float(row.get("markPrice") or 0),
                margin_ratio=float(row.get("marginRatio") or 0),
                notional=float(row.get("notional") or 0),
                unrealized_pnl=float(row.get("unrealizedPnl") or 0),
                raw=row,
            )
        return None

    def free_collateral(self) -> float:
        try:
            balance = self.ccxt.fetch_balance()
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc)
        usdt = balance.get("USDT") or {}
        return float(usdt.get("free") or 0)

    def cancel_all(self, symbol: str) -> None:
        try:
            self.ccxt.cancel_all_orders(self.unified(symbol))
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc)

    def server_time_ms(self) -> int:
        try:
            return int(self.ccxt.fetch_time())
        except Exception:  # noqa: BLE001 - час сервера не критичний
            return int(time.time() * 1000)


class MexcStubClient(ExchangeClient):
    """MEXC futures API недоступний користувачу — усі торгові виклики падають."""

    def __init__(self, name: str = "MEXC"):
        self.name = name

    def _no(self, *_a, **_k):
        raise NotSupported("MEXC futures API не підключено (немає ключів)")

    load = has_market = unified = _no
    set_leverage = amount_for_notional = market_order = _no
    fetch_order_result = position = free_collateral = cancel_all = server_time_ms = _no


# --- ccxt helpers --------------------------------------------------------
def _translate(exc: Exception) -> ExchangeOpError:
    """ccxt-виняток -> наша типізована помилка."""
    try:
        import ccxt  # noqa: PLC0415 - лінива залежність
    except Exception:  # pragma: no cover
        return ExchangeOpError(str(exc))
    if isinstance(exc, ccxt.AccountSuspended):
        return ExchangeBanned(str(exc))
    if isinstance(exc, (ccxt.AuthenticationError, ccxt.PermissionDenied)):
        return ExchangeBanned(str(exc))
    if isinstance(exc, ccxt.NotSupported):
        return NotSupported(str(exc))
    if isinstance(exc, (ccxt.InsufficientFunds, ccxt.InvalidOrder)):
        return OrderRejected(str(exc))
    return ExchangeOpError(str(exc))


def _parse_order(order: dict, symbol: str, side: str, client_id: str | None) -> OrderResult:
    status = _STATUS_MAP.get(str(order.get("status") or "").lower(), "unknown")
    filled = float(order.get("filled") or 0)
    avg = float(order.get("average") or order.get("price") or 0)
    return OrderResult(
        id=str(order.get("id")) if order.get("id") is not None else None,
        client_id=order.get("clientOrderId") or client_id,
        symbol=symbol,
        side=side or str(order.get("side") or ""),
        status=status,
        filled_base=filled,
        avg_price=avg,
        raw=order,
    )
