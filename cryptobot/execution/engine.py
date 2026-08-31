"""Двоногове виконання: open_hedge / close_hedge з reconcile та RECOVERY."""

from __future__ import annotations

import threading
import time

from cryptobot import config
from cryptobot.execution import state
from cryptobot.execution.state import (
    CLOSED,
    CLOSING,
    FAILED,
    HEDGED,
    LEG_PENDING,
    RECOVERY,
)
from cryptobot.exchanges.base import ExchangeOpError, OrderResult
from cryptobot.storage import audit
from cryptobot.telegram import telegram_send


class ExecutionError(Exception):
    pass


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
    for thread in threads:
        thread.join(timeout=config.ORDER_TIMEOUT_SEC + 5)
    return out


def _resolve_fill(client, symbol, result, deadline) -> OrderResult:
    """Доганяє фінальний стан ордера, якщо market ще не 'closed'."""
    if not isinstance(result, OrderResult):
        raise result if isinstance(result, Exception) else ExecutionError(str(result))
    while not result.is_done and result.id and time.time() < deadline:
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


# --- open --------------------------------------------------------------
def open_hedge(plan, clients: dict, risk_engine) -> dict:
    """Відкриває хедж LONG/SHORT. Повертає позицію (HEDGED, RECOVERY або FAILED)."""
    now_ms = int(time.time() * 1000)
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
    if target <= 0:
        raise ExecutionError("нульовий обсяг після округлення precision")

    # вільна маржа з буфером
    need = plan.notional_usdt / max(1, plan.leverage) * (1 + config.MARGIN_BUFFER_PCT / 100)
    for client in (long_c, short_c):
        free = client.free_collateral()
        if free < need:
            audit("live_insufficient_margin", {"exchange": client.name, "free": free, "need": need})
            raise ExecutionError(f"недостатньо маржі на {client.name}: {free:.2f} < {need:.2f}")

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
        ok = isinstance(flat, OrderResult) and flat.is_filled
        _finish(position, FAILED if not ok else RECOVERY, note="recovered one-legged fill")
        audit("live_recovery", {"id": position["id"], "side": filled_side, "qty": qty, "flattened": ok})
        _alert(
            f"⚠️ LIVE RECOVERY {symbol}\nЗалилась лише нога {filled_side}. "
            f"{'Закрито' if ok else 'НЕ ВДАЛОСЯ закрити — перевір біржу!'}"
        )
        return position

    # --- обидві залиті, але перекіс > tolerance -> вирівнюємо ---
    if abs(long_fill - short_fill) > tol:
        deficit_side = "long" if long_fill < short_fill else "short"
        deficit_client = long_c if deficit_side == "long" else short_c
        delta = abs(long_fill - short_fill)
        side_word = position["legs"][deficit_side]["side"]
        try:
            fix = deficit_client.market_order(
                symbol, side_word, delta, f"{position['clientPrefix']}F{deficit_side[0]}", reduce_only=False
            )
            if deficit_side == "long":
                long_fill += fix.filled_base
            else:
                short_fill += fix.filled_base
            position["legs"][deficit_side]["filledBase"] = long_fill if deficit_side == "long" else short_fill
        except Exception as exc:  # noqa: BLE001
            audit("live_rebalance_failed", {"id": position["id"], "error": str(exc)})

    if abs(long_fill - short_fill) > tol:
        # не вирівнялось — зводимо до меншого і працюємо з ним
        bigger_side = "long" if long_fill > short_fill else "short"
        bigger_client = long_c if bigger_side == "long" else short_c
        excess = abs(long_fill - short_fill)
        _flatten_leg(bigger_client, symbol, position["legs"][bigger_side]["side"], excess,
                     f"{position['clientPrefix']}T")
        hedged_qty = min(long_fill, short_fill)
    else:
        hedged_qty = (long_fill + short_fill) / 2

    long_price = long_res.avg_price if long_res else 0.0
    short_price = short_res.avg_price if short_res else 0.0
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
    for tag, client in (("long", long_c), ("short", short_c)):
        try:
            live_pos = client.position(symbol) if client else None
            if live_pos and abs(live_pos.base_qty) > 0:
                qty = max(qty, abs(live_pos.base_qty))
        except ExchangeOpError:
            pass

    prefix = position["clientPrefix"]
    results = _place_both(
        lambda out, key: _place_leg(long_c, symbol, "sell", qty, f"{prefix}CA", True, out, key),
        lambda out, key: _place_leg(short_c, symbol, "buy", qty, f"{prefix}CB", True, out, key),
    )
    deadline = time.time() + config.ORDER_TIMEOUT_SEC
    long_res = _safe_resolve(long_c, symbol, results.get("long"), deadline)
    short_res = _safe_resolve(short_c, symbol, results.get("short"), deadline)

    # друга спроба добити залишок
    for tag, client, side in (("long", long_c, "sell"), ("short", short_c, "buy")):
        try:
            residual = client.position(symbol) if client else None
            if residual and abs(residual.base_qty) > _tolerance(qty):
                client.market_order(symbol, side, abs(residual.base_qty),
                                    f"{prefix}{tag[0].upper()}2", reduce_only=True)
        except ExchangeOpError as exc:
            audit("live_close_residual_failed", {"id": position["id"], "error": str(exc)})

    exit_long = long_res.avg_price if long_res else 0.0
    exit_short = short_res.avg_price if short_res else 0.0
    entry_long = position.get("entryLongPrice") or 0.0
    entry_short = position.get("entryShortPrice") or 0.0
    fees = position["notional"] * position.get("roundTripFeesPct", 0) / 100
    long_pnl = (exit_long - entry_long) * qty if entry_long and exit_long else 0.0
    short_pnl = (entry_short - exit_short) * qty if entry_short and exit_short else 0.0
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
