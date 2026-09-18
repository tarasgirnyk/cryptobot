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

    def usdt_balance(self) -> dict:
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
        return self.usdt_balance()["free"]

    def usdt_balance(self) -> dict:
        try:
            balance = self.ccxt.fetch_balance()
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc)
        usdt = balance.get("USDT") or {}
        return {
            "free": float(usdt.get("free") or 0),
            "used": float(usdt.get("used") or 0),
            "total": float(usdt.get("total") or 0),
            "currency": "USDT",
        }

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


class MexcExchangeClient(CcxtExchangeClient):
    """MEXC USDT-M adapter.

    CCXT accepts and returns MEXC swap amounts in contracts, while the execution
    engine deliberately works in base-asset units.  Keep that conversion at the
    exchange boundary so cross-exchange fill comparisons remain meaningful.
    """

    def __init__(self, exchange: Any):
        super().__init__("MEXC", exchange)
        self._leverage_by_symbol: dict[str, int] = {}

    def _contract_size(self, symbol: str) -> float:
        size = float(self.market(symbol).get("contractSize") or 0)
        if size <= 0:
            raise NotSupported(f"MEXC {symbol}: некоректний contractSize={size}")
        return size

    def _contracts(self, symbol: str, base_qty: float) -> float:
        unified = self.unified(symbol)
        contracts = float(self.ccxt.amount_to_precision(
            unified, abs(float(base_qty)) / self._contract_size(symbol)
        ))
        minimum = float(((self.market(symbol).get("limits") or {}).get("amount") or {}).get("min") or 0)
        if contracts <= 0 or (minimum and contracts < minimum):
            raise OrderRejected(
                f"MEXC {symbol}: обсяг {base_qty:g} бази менший за мінімум {minimum:g} контрактів"
            )
        return contracts

    def amount_for_notional(self, symbol: str, notional_usdt: float, price: float) -> float:
        if price <= 0:
            raise OrderRejected("Ціна для розрахунку обсягу має бути > 0")
        unified = self.unified(symbol)
        size = self._contract_size(symbol)
        raw_contracts = notional_usdt / (price * size)
        contracts = float(self.ccxt.amount_to_precision(unified, raw_contracts))
        minimum = float(((self.market(symbol).get("limits") or {}).get("amount") or {}).get("min") or 0)
        contracts = max(contracts, minimum)
        if contracts <= 0:
            raise OrderRejected(f"MEXC {symbol}: нульовий обсяг після округлення")
        return contracts * size

    def set_leverage(self, symbol: str, leverage: int) -> None:
        # MEXC requires a position side when no position exists. Configure both
        # directions because this client may be either leg of the hedge.
        errors = []
        for position_type in (1, 2):  # 1 long, 2 short
            try:
                self.ccxt.set_leverage(
                    int(leverage), self.unified(symbol),
                    {"openType": 1, "positionType": position_type},
                )
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if "not modified" in msg or "leverage not changed" in msg:
                    continue
                errors.append(exc)
        if errors:
            raise _translate(errors[0])
        self._leverage_by_symbol[symbol] = int(leverage)

    def market_order(
        self, symbol: str, side: str, base_qty: float, client_id: str,
        reduce_only: bool = False,
    ) -> OrderResult:
        contracts = self._contracts(symbol, base_qty)
        leverage = self._leverage_by_symbol.get(symbol)
        if leverage is None:
            raise OrderRejected(f"MEXC {symbol}: плече не налаштоване перед ордером")
        params: dict[str, Any] = {
            "clientOrderId": client_id,
            "openType": 1,
            "leverage": leverage,
        }
        if reduce_only:
            params["reduceOnly"] = True
        try:
            order = self.ccxt.create_order(
                self.unified(symbol), "market", side, contracts, None, params
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc)
        return self._parse_contract_order(order, symbol, side, client_id)

    def fetch_order_result(self, symbol: str, order_id: str) -> OrderResult:
        try:
            order = self.ccxt.fetch_order(order_id, self.unified(symbol))
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc)
        return self._parse_contract_order(
            order, symbol, str(order.get("side") or ""), order.get("clientOrderId")
        )

    def _parse_contract_order(
        self, order: dict, symbol: str, side: str, client_id: str | None
    ) -> OrderResult:
        result = _parse_order(order, symbol, side, client_id)
        result.filled_base *= self._contract_size(symbol)
        return result


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
