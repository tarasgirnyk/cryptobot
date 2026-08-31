"""Зв'язка execution-двигуна з фоновим циклом і точкою входу."""

from __future__ import annotations

import threading
import time

from cryptobot import config, runtime
from cryptobot.depth import depth_analysis
from cryptobot.execution import clients as _registry
from cryptobot.execution import engine, state
from cryptobot.execution.margin import margin_monitor_loop
from cryptobot.execution.plan import TradePlan
from cryptobot.execution.reconcile import startup_reconcile
from cryptobot.execution.state import HEDGED
from cryptobot.exchanges.base import ExchangeOpError
from cryptobot.risk import HardLimitHit, candidate_rejection_reason
from cryptobot.storage import audit, set_control_state
from cryptobot.telegram import telegram_send


_started = False


def enabled() -> bool:
    return _registry.enabled()


# --- виходи ---------------------------------------------------------------
def _run_exits(clients: dict, force_all: bool = False) -> None:
    now_ms = time.time() * 1000
    for pos in state.open_live_positions():
        if pos.get("state") != HEDGED:
            continue
        reason = "kill_switch" if force_all else None
        if not reason:
            reason = state.exit_reason(state.mark_live(pos), now_ms)
        if reason:
            try:
                engine.close_hedge(pos, reason, clients)
            except Exception as exc:  # noqa: BLE001
                audit("live_close_error", {"id": pos["id"], "error": str(exc)})
                telegram_send(f"⛔ LIVE CLOSE ERROR {pos['symbol']}: {str(exc)[:160]}")


# --- входи ----------------------------------------------------------------
def _try_open(candidate: dict, clients: dict, risk_engine) -> bool:
    symbol = candidate["symbol"]
    opened = state.open_live_positions()
    if candidate_rejection_reason(candidate, opened, now=time.time()):
        return False
    try:
        depth = depth_analysis(symbol, config.LIVE_NOTIONAL_USDT)
    except Exception as exc:  # noqa: BLE001
        audit("live_depth_error", {"symbol": symbol, "error": str(exc)})
        return False
    rejection = candidate_rejection_reason(
        candidate, opened, depth=depth, now=time.time(), notional=config.LIVE_NOTIONAL_USDT
    )
    if rejection:
        audit("live_candidate_rejected", {"symbol": symbol, "reason": rejection})
        return False

    plan = TradePlan.from_candidate(
        candidate, depth,
        notional=config.LIVE_NOTIONAL_USDT,
        leverage=config.DEFAULT_LEVERAGE,
        ttl_sec=config.SIGNAL_TTL_SEC,
    )
    try:
        position = engine.open_hedge(plan, clients, risk_engine)
    except HardLimitHit as exc:
        audit("hard_limit_hit", {"symbol": symbol, "reason": exc.reason})
        raise
    except ExchangeOpError as exc:
        audit("live_open_error", {"symbol": symbol, "error": str(exc)})
        return False
    except Exception as exc:  # noqa: BLE001
        audit("live_open_error", {"symbol": symbol, "error": str(exc)})
        return False
    runtime.symbol_cooldowns[symbol] = time.time() + config.SYMBOL_COOLDOWN_SEC
    return position.get("state") == HEDGED


def evaluate(payload: dict, allow_entries: bool = True) -> None:
    """Викликається з automation_loop замість paper-логіки в demo/live."""
    clients = _registry.get_clients()
    risk_engine = _registry.get_risk_engine()
    runtime.automation_state["lastRunAt"] = int(time.time() * 1000)

    kill = runtime.automation_state["killSwitch"]
    _run_exits(clients, force_all=kill)

    if kill or runtime.automation_state["paused"] or not allow_entries:
        return
    if risk_engine.daily_loss_tripped():
        if not runtime.automation_state.get("dailyLossAlerted"):
            runtime.automation_state["dailyLossAlerted"] = True
            set_control_state(kill_switch=True)
            telegram_send("🛑 LIVE: досягнуто денний ліміт збитку. STOP.")
        return

    slots = config.MAX_OPEN_POSITIONS - len(state.open_live_positions())
    if slots <= 0:
        return
    for candidate in payload.get("opportunities", []):
        if slots <= 0:
            break
        try:
            if _try_open(candidate, clients, risk_engine):
                slots -= 1
        except HardLimitHit:
            break


# --- запуск -------------------------------------------------------------
def startup() -> bool:
    """Готує клієнтів, звіряє стан, піднімає monitor маржі. True — можна торгувати."""
    global _started
    if _started:
        return enabled()
    _started = True
    clients = _registry.get_clients()
    if len(clients) < 2:
        print(
            f"[warn] executor: доступно {len(clients)} бірж (<2) — demo/live "
            "поводиться як paper. Додай ключі у .env."
        )
        audit("executor_unavailable", {"exchanges": list(clients)})
        return False
    startup_reconcile(clients)
    threading.Thread(
        target=margin_monitor_loop,
        args=(clients, engine.close_hedge),
        name="margin-monitor",
        daemon=True,
    ).start()
    print(f"[ok] executor готовий: {', '.join(clients)} (sandbox={config.use_sandbox()})")
    return True
