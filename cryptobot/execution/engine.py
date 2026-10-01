"""Двоногове виконання: open_hedge / close_hedge з reconcile та RECOVERY."""

from __future__ import annotations

import threading
import time
from functools import wraps

from cryptobot import config, runtime
from cryptobot.execution import state
from cryptobot.execution.state import (
    CLOSED,
    CLOSING,
    FAILED,
    HEDGED,
    LEG_PENDING,
    RECOVERY,
)
from cryptobot.exchanges.base import ExchangeOpError, OrderResult, OrderRejected
from cryptobot.storage import audit, set_control_state
from cryptobot.telegram import telegram_send


class ExecutionError(Exception):
    pass


_trade_lock = threading.RLock()


def serialized(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with _trade_lock:
            return fn(*args, **kwargs)
    return call


def _halt(position, reason):
    state.set_state(position, RECOVERY, note=reason)
    set_control_state(paused=True, kill_switch=True)
    audit("live_execution_halted", {"id": position["id"], "reason": reason})
    _alert(f"🛑 LIVE STOP {position['symbol']}: {reason}. Перевір позиції та ордери на біржах.")
    return position


# --- helpers --------------------------------------------------------------
def _tolerance(target: float) -> float:
    return abs(target) * config.FILL_TOLERANCE_PCT / 100


def _place_leg(client, symbol, side, qty, client_id, reduce_only, out: dict, key: str):
    try:
        out[key] = client.market_order(symbol, side, qty, client_id, reduce_only=reduce_only)
    except Exception as exc:  # noqa: BLE001 - зберігаємо для reconcile
        out[key] = exc


def _place_both(long_call, short_call) -> dict:
    """Виконує дві ноги паралельно зі спільним дедлайном."""
    out: dict = {}
    threads = [
        threading.Thread(target=long_call, args=(out, "long")),
        threading.Thread(target=short_call, args=(out, "short")),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + config.ORDER_TIMEOUT_SEC + 5
    for thread in threads:
        thread.join(timeout=max(0, deadline - time.monotonic()))
    return out


def _resolve_fill(client, symbol, result, deadline) -> OrderResult:
    """Доганяє фінальний стан ордера, якщо market ще не 'closed'."""
    if not isinstance(result, OrderResult):
        raise result if isinstance(result, Exception) else ExecutionError(str(result))
    while (not result.is_done or (result.filled_base > 0 and result.avg_price <= 0)) and result.id and time.time() < deadline:
        time.sleep(0.5)
        try:
            result = client.fetch_order_result(symbol, result.id)
        except ExchangeOpError:
            break
    return result


def _flatten_leg(client, symbol, side, qty, client_id):
    """Аварійно закриває вже залиту ногу market reduce-only."""
    opposite = "sell" if side == "buy" else "buy"
    try:
        return client.market_order(symbol, opposite, abs(qty), client_id, reduce_only=True)
    except Exception as exc:  # noqa: BLE001
        audit("live_flatten_failed", {"symbol": symbol, "side": opposite, "error": str(exc)})
        return exc


def _confirm_flattened(client, symbol, result, deadline) -> bool:
    """Підтверджує аварійне закриття за ордером і фактичною позицією.

    Деякі біржі (зокрема Bybit) повертають market-ордер зі статусом
    ``unknown`` одразу після прийняття. Тому не можна вважати recovery
    невдалим або успішним лише за первинною відповіддю create_order.
    """
    resolved = _safe_resolve(client, symbol, result, deadline)
    order_filled = isinstance(resolved, OrderResult) and resolved.is_filled
    while time.time() < deadline:
        try:
            residual = client.position(symbol)
            if not residual or abs(residual.base_qty) <= 0:
                return order_filled
        except ExchangeOpError:
            return False
        time.sleep(0.5)
    return False


# --- open --------------------------------------------------------------
@serialized
def open_hedge(plan, clients: dict, risk_engine) -> dict:
    """Відкриває хедж LONG/SHORT. Повертає позицію (HEDGED, RECOVERY або FAILED)."""
    now_ms = int(time.time() * 1000)
    if runtime.automation_state["killSwitch"] or runtime.automation_state["paused"]:
        raise ExecutionError("entries_stopped")
    if config.AUTOMATION_MODE == "live" and plan.symbol not in config.LIVE_ALLOWED_SYMBOLS:
        raise ExecutionError("symbol_not_allowlisted")
    if plan.expired(now_ms):
        audit("live_plan_expired", {"symbol": plan.symbol})
        raise ExecutionError("plan_expired")

    risk_engine.check(plan, state.open_live_positions(), now_ms)

    long_c = clients.get(plan.long_exchange)
    short_c = clients.get(plan.short_exchange)
    if not long_c or not short_c:
        raise ExecutionError(f"немає клієнта для {plan.long_exchange}/{plan.short_exchange}")

    symbol = plan.symbol
    if not long_c.has_market(symbol) or not short_c.has_market(symbol):
        raise ExecutionError("символ недоступний на одній із бірж")

    # плече (ідемпотентно) та цільовий обсяг
    long_c.set_leverage(symbol, plan.leverage)
    short_c.set_leverage(symbol, plan.leverage)
    qty_long = long_c.amount_for_notional(symbol, plan.notional_usdt, plan.long_ref_price)
    qty_short = short_c.amount_for_notional(symbol, plan.notional_usdt, plan.short_ref_price)
    target = min(qty_long, qty_short)
    # Round down to a quantity representable on BOTH exchanges.
    for _ in range(8):
        rounded = min(
            long_c.amount_for_notional(symbol, target * plan.long_ref_price, plan.long_ref_price),
            short_c.amount_for_notional(symbol, target * plan.short_ref_price, plan.short_ref_price),
        )
        if abs(rounded - target) <= 1e-10:
            break
        target = rounded
    if target <= 0:
        raise ExecutionError("нульовий обсяг після округлення precision")
    for client, price in ((long_c, plan.long_ref_price), (short_c, plan.short_ref_price)):
        if target * price > plan.notional_usdt + 1e-8:
            raise ExecutionError("rounded_notional_exceeds_plan")
        if hasattr(client, "validate_open"):
            client.validate_open(symbol, target, price)

    # вільна маржа з буфером
    need = plan.notional_usdt / max(1, plan.leverage) * (1 + config.MARGIN_BUFFER_PCT / 100)
    for client in (long_c, short_c):
        free = client.free_collateral()
        if free < need:
            audit("live_insufficient_margin", {"exchange": client.name, "free": free, "need": need})
            raise ExecutionError(f"недостатньо маржі на {client.name}: {free:.2f} < {need:.2f}")

    if plan.expired() or runtime.automation_state["killSwitch"] or runtime.automation_state["paused"]:
        raise ExecutionError("plan_expired_or_stopped_during_preflight")

    position = state.new_position(plan, target)
    long_id = f"{position['clientPrefix']}A"
    short_id = f"{position['clientPrefix']}B"
    position["legs"]["long"]["clientId"] = long_id
    position["legs"]["short"]["clientId"] = short_id
    state.set_state(position, LEG_PENDING)
    audit("live_open", {"id": position["id"], "symbol": symbol, "target": target, "plan": plan.symbol})

    results = _place_both(
        lambda out, key: _place_leg(long_c, symbol, "buy", target, long_id, False, out, key),
        lambda out, key: _place_leg(short_c, symbol, "sell", target, short_id, False, out, key),
    )
    deadline = time.time() + config.ORDER_TIMEOUT_SEC

    try:
        long_res = _resolve_fill(long_c, symbol, results.get("long"), deadline)
        long_err = None
    except Exception as exc:  # noqa: BLE001
        long_res, long_err = None, exc
    try:
        short_res = _resolve_fill(short_c, symbol, results.get("short"), deadline)
        short_err = None
    except Exception as exc:  # noqa: BLE001
        short_res, short_err = None, exc

    long_fill = long_res.filled_base if long_res else 0.0
    short_fill = short_res.filled_base if short_res else 0.0
    _record_leg(position, "long", long_res, long_err)
    _record_leg(position, "short", short_res, short_err)
    store_snapshot = {"long": long_fill, "short": short_fill, "target": target}
    audit("live_open_fills", {"id": position["id"], **store_snapshot,
                              "longErr": str(long_err or ""), "shortErr": str(short_err or "")})

    # A timeout/error or nonterminal order is NOT evidence of zero execution.
    # Keep the durable intent for reconciliation; never send a duplicate order.
    if (any(err is not None and not isinstance(err, OrderRejected) for err in (long_err, short_err))
            or any(res is not None and not res.is_done for res in (long_res, short_res))):
        return _halt(position, "order outcome uncertain; reconciliation required")

    tol = _tolerance(target)

    # --- обидві ноги провалились ---
    if long_fill <= 0 and short_fill <= 0:
        _finish(position, FAILED, note="both legs unfilled")
        _alert(f"⛔ LIVE OPEN FAIL {symbol}\nЖодна нога не залилась. Позиція не відкрита.")
        return position

    # --- одна нога залилась, інша ні -> RECOVERY ---
    if (long_fill <= 0) != (short_fill <= 0):
        filled_side = "long" if long_fill > 0 else "short"
        filled_client = long_c if filled_side == "long" else short_c
        filled_side_word = position["legs"][filled_side]["side"]
        qty = long_fill if filled_side == "long" else short_fill
        state.set_state(position, RECOVERY, note=f"one-legged fill on {filled_side}")
        flat = _flatten_leg(filled_client, symbol, filled_side_word, qty, f"{position['clientPrefix']}R")
        ok = _confirm_flattened(
            filled_client, symbol, flat, time.time() + config.ORDER_TIMEOUT_SEC
        )
        if not ok:
            return _halt(position, "one-legged recovery not confirmed flat")
        _finish(position, RECOVERY, note="recovered one-legged fill")
        set_control_state(paused=True, kill_switch=True)
        audit("live_recovery", {"id": position["id"], "side": filled_side, "qty": qty, "flattened": ok})
        _alert(
            f"⚠️ LIVE RECOVERY {symbol}\nЗалилась лише нога {filled_side}. "
            f"{'Закрито' if ok else 'НЕ ВДАЛОСЯ закрити — перевір біржу!'}"
        )
        return position

    # Unbalanced partial execution needs reconciliation, never an unverified top-up.
    position["entryLongPrice"] = long_res.avg_price
    position["entryShortPrice"] = short_res.avg_price
    if abs(long_fill - short_fill) > tol:
        _halt(position, "unequal partial fills")
        return close_hedge(position, "partial_fill", clients)
    hedged_qty = min(long_fill, short_fill)

    long_price = long_res.avg_price if long_res else 0.0
    short_price = short_res.avg_price if short_res else 0.0
    if long_price <= 0 or short_price <= 0:
        return _halt(position, "fill price missing")
    entry_exec_spread = (
        (short_price - long_price) / long_price * 100 if long_price > 0 and short_price > 0
        else position["entryExecutableSpreadPct"]
    )
    position["hedgedBaseQty"] = hedged_qty
    position["entryLongPrice"] = long_price
    position["entryShortPrice"] = short_price
    position["entryExecutableSpreadPct"] = entry_exec_spread
    state.set_state(position, HEDGED, hedgedAt=int(time.time() * 1000))
    audit("live_hedged", {"id": position["id"], "symbol": symbol, "qty": hedged_qty,
                          "entrySpreadPct": entry_exec_spread})
    _alert(
        f"🟢 LIVE HEDGED {symbol}\nLONG {plan.long_exchange} @ {long_price:.6g} / "
        f"SHORT {plan.short_exchange} @ {short_price:.6g}\n"
        f"Обсяг: {plan.notional_usdt:.0f} USDT · спред входу {entry_exec_spread:+.3f}%",
        [[("Закрити", f"lclose:{position['id']}"), ("⏸ Пауза", "pause")]],
    )
    return position


# --- close -----------------------------------------------------------
@serialized
def close_hedge(position: dict, reason: str, clients: dict) -> dict:
    """Закриває обидві ноги market reduce-only і фіксує реалізований PNL."""
    if position.get("state") in (CLOSED, FAILED):
        return position
    long_c = clients.get(position["longExchange"])
    short_c = clients.get(position["shortExchange"])
    symbol = position["symbol"]
    state.set_state(position, CLOSING, closeReason=reason)

    qty = position.get("hedgedBaseQty") or position.get("targetBaseQty") or 0.0
    # звіряємось із фактичною позицією на біржі, якщо доступно
    close_qty = {}
    for tag, client in (("long", long_c), ("short", short_c)):
        try:
            live_pos = client.position(symbol) if client else None
            close_qty[tag] = position["legs"][tag].get("filledBase") or qty
            if live_pos and abs(live_pos.base_qty) > 0:
                close_qty[tag] = abs(live_pos.base_qty)
        except ExchangeOpError:
            return _halt(position, "cannot read position before close")

    prefix = position["clientPrefix"]
    results = _place_both(
        lambda out, key: _place_leg(long_c, symbol, "sell", close_qty["long"], f"{prefix}CA", True, out, key),
        lambda out, key: _place_leg(short_c, symbol, "buy", close_qty["short"], f"{prefix}CB", True, out, key),
    )
    deadline = time.time() + config.ORDER_TIMEOUT_SEC
    long_res = _safe_resolve(long_c, symbol, results.get("long"), deadline)
    short_res = _safe_resolve(short_c, symbol, results.get("short"), deadline)

    if any(res is None or not res.is_done for res in (long_res, short_res)):
        return _halt(position, "close order outcome uncertain")

    for client in (long_c, short_c):
        try:
            residual = client.position(symbol)
            if residual and abs(residual.base_qty) > 0:
                return _halt(position, "close left residual exposure")
        except Exception:
            return _halt(position, "cannot verify flat after close")
    if any(not res.is_filled or res.avg_price <= 0 for res in (long_res, short_res)):
        return _halt(position, "close fill accounting incomplete")

    exit_long = long_res.avg_price if long_res else 0.0
    exit_short = short_res.avg_price if short_res else 0.0
    entry_long = position.get("entryLongPrice") or 0.0
    entry_short = position.get("entryShortPrice") or 0.0
    fees = position["notional"] * position.get("roundTripFeesPct", 0) / 100
    long_pnl = (exit_long - entry_long) * long_res.filled_base if entry_long and exit_long else 0.0
    short_pnl = (entry_short - exit_short) * short_res.filled_base if entry_short and exit_short else 0.0
    realized = long_pnl + short_pnl - fees
    realized_pct = realized / position["notional"] * 100 if position["notional"] else 0.0

    position["closeLegs"] = {
        "long": _leg_view(long_res),
        "short": _leg_view(short_res),
    }
    position["realizedPnl"] = realized
    position["realizedPnlPct"] = realized_pct
    position["closedAt"] = int(time.time() * 1000)
    position["state"] = CLOSED
    state.store_close(position)
    from cryptobot.execution.clients import get_risk_engine
    get_risk_engine().register_close(realized)
    audit("live_close", {"id": position["id"], "symbol": symbol, "reason": reason,
                         "realizedPnl": realized})
    _alert(
        f"🔵 LIVE CLOSE {symbol}\nПричина: {reason}\n"
        f"PNL: {realized:+.2f} USDT ({realized_pct:+.3f}%)"
    )
    return position


# --- дрібні helpers ------------------------------------------------------
def _safe_resolve(client, symbol, result, deadline):
    try:
        return _resolve_fill(client, symbol, result, deadline)
    except Exception:  # noqa: BLE001
        return None


def _record_leg(position, key, result, err):
    leg = position["legs"][key]
    if isinstance(result, OrderResult):
        leg.update(
            orderId=result.id,
            filledBase=result.filled_base,
            avgPrice=result.avg_price,
            status=result.status,
        )
    if err:
        leg["status"] = "error"
        leg["error"] = str(err)[:200]


def _leg_view(result):
    if not isinstance(result, OrderResult):
        return {"status": "missing"}
    return {"orderId": result.id, "filledBase": result.filled_base,
            "avgPrice": result.avg_price, "status": result.status}


def _finish(position, end_state, note=""):
    position["note"] = note
    position["closedAt"] = int(time.time() * 1000)
    position["state"] = end_state
    state.store_close(position)


def _alert(text, buttons=None):
    try:
        telegram_send(text, buttons)
    except Exception:  # noqa: BLE001 - алерт не має валити виконання
        pass
