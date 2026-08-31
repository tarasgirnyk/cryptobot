"""Фоновий цикл: скан ринку, входи/виходи, моніторинг, звіти."""

from __future__ import annotations

import time

from cryptobot import config, runtime
from cryptobot.depth import depth_analysis
from cryptobot.paper import (
    close_paper,
    market_health,
    open_paper,
    paper_metrics,
    paper_snapshot,
)
from cryptobot.reports import readiness_text
from cryptobot.risk import candidate_rejection_reason
from cryptobot.scanner import market_payload
from cryptobot.storage import audit, persist_app_state
from cryptobot.telegram import telegram_send


def evaluate_automation(payload, allow_entries=True):
    runtime.automation_state["lastRunAt"] = int(time.time() * 1000)

    # demo/live з реальним виконавцем — окрема гілка; інакше падаємо в paper.
    if config.AUTOMATION_MODE in ("demo", "live"):
        from cryptobot.execution import driver

        if driver.enabled():
            driver.evaluate(payload, allow_entries=allow_entries)
            return

    # Position exits continue even while new entries are paused.
    for marked in paper_snapshot()["open"]:
        pnl_pct = marked.get("unrealizedPnlPct", 0)
        current_gross = marked.get("currentGrossPct")
        reason = None
        if pnl_pct <= config.PAPER_TOTAL_STOP_PCT:
            reason = "total_stop"
        elif current_gross is not None and current_gross <= config.PAPER_EXIT_GROSS_PCT:
            reason = "spread_converged"
        elif (
            time.time() * 1000 - float(marked.get("openedAt") or 0)
            >= config.PAPER_MAX_HOLD_HOURS * 3_600_000
        ):
            reason = "max_hold"
        if reason:
            try:
                closed = close_paper(marked["id"], reason)
                telegram_send(
                    f"🔵 PAPER CLOSE {closed['symbol']}\n"
                    f"Причина: {reason}\nPNL: {closed.get('unrealizedPnl', 0):+.2f} USDT"
                )
            except ValueError:
                pass

    if (
        not allow_entries
        or config.AUTOMATION_MODE not in config.VALID_MODES
        or runtime.automation_state["paused"]
        or runtime.automation_state["killSwitch"]
    ):
        return

    auto_open = config.is_auto_open_mode()
    opened_now = paper_snapshot()["open"]
    opened_symbols = {row["symbol"] for row in opened_now}
    available_slots = config.MAX_OPEN_POSITIONS - len(opened_symbols)
    if auto_open and available_slots <= 0:
        return

    now = time.time()
    notifications_left = 3
    for candidate in payload.get("opportunities", []):
        symbol = candidate["symbol"]
        if auto_open and available_slots <= 0:
            break
        if not auto_open and notifications_left <= 0:
            break
        if candidate_rejection_reason(candidate, opened_now, now=now):
            continue
        try:
            depth = depth_analysis(symbol, config.PAPER_NOTIONAL_USDT)
            rejection = candidate_rejection_reason(
                candidate, opened_now, depth=depth, now=now,
                notional=config.PAPER_NOTIONAL_USDT,
            )
            if rejection:
                audit("candidate_rejected", {"symbol": symbol, "reason": rejection, "depth": depth})
                continue
            if not auto_open:
                runtime.symbol_cooldowns[symbol] = now + config.SYMBOL_COOLDOWN_SEC
                notifications_left -= 1
                audit("candidate_signal", {"symbol": symbol, "candidate": candidate, "depth": depth})
                telegram_send(
                    f"🟡 CANDIDATE {symbol}\n"
                    f"LONG {candidate['longExchange']} / SHORT {candidate['shortExchange']}\n"
                    f"Top spread: {candidate['grossSpreadPct']:.3f}%\n"
                    f"VWAP spread: {depth['executableSpreadPct']:.3f}%\n"
                    f"Net: {candidate['netSpreadPct'] - depth['slippagePct']:.3f}%\n"
                    f"Обсяг перевірки: {config.PAPER_NOTIONAL_USDT:.0f} USDT",
                    [
                        [("▶️ Paper-вхід", f"paper:{symbol}"), ("Ігнорувати", f"ignore:{symbol}")],
                        [("⏸ Пауза", "pause")],
                    ],
                )
                continue
            opened = open_paper(symbol, config.PAPER_NOTIONAL_USDT, depth)
            runtime.symbol_cooldowns[symbol] = now + config.SYMBOL_COOLDOWN_SEC
            opened_symbols.add(symbol)
            opened_now.append(opened)
            available_slots -= 1
            telegram_send(
                f"🟢 PAPER OPEN {symbol}\n"
                f"LONG {opened['longExchange']} / SHORT {opened['shortExchange']}\n"
                f"Entry spread: {opened['entryGrossPct']:.3f}%\n"
                f"Обсяг: {opened['notional']:.0f} USDT\n"
                f"VWAP slippage: {depth['slippagePct']:.3f}%",
                [[("Закрити", f"close:{opened['id']}"), ("⏸ Пауза", "pause")]],
            )
        except Exception as exc:
            audit("candidate_error", {"symbol": symbol, "error": str(exc)})


def update_market_monitor(payload):
    health = market_health(payload)
    previous = runtime.automation_state["marketStatus"]
    runtime.automation_state["marketStatus"] = health["status"]
    now_ms = int(time.time() * 1000)
    if health["status"] == "ok":
        runtime.automation_state["lastMarketSuccessAt"] = now_ms
        runtime.automation_state["marketFailures"] = 0
        if runtime.automation_state["marketAlerted"]:
            exchange_list = ", ".join(config.ENABLED_EXCHANGES)
            if telegram_send(f"✅ MARKET DATA RECOVERED\n{exchange_list} знову доступні.") is not None:
                runtime.automation_state["marketAlerted"] = False
                audit("market_recovered", health)
        return health

    runtime.automation_state["marketFailures"] += 1
    should_alert = (
        runtime.automation_state["marketFailures"] >= config.MARKET_ALERT_FAILURES
        and not runtime.automation_state["marketAlerted"]
    )
    if should_alert:
        details = "; ".join(health["errors"]) or f"status={health['status']}"
        if telegram_send(
            "⚠️ MARKET DATA PROBLEM\n"
            f"Стан: {health['status']}\n{details[:500]}\n"
            "Нові входи автоматично заблоковані."
        ) is not None:
            runtime.automation_state["marketAlerted"] = True
            audit("market_degraded", health)
    elif previous != health["status"]:
        audit("market_status", health)
    return health


def maybe_send_periodic_report():
    now_ms = int(time.time() * 1000)
    metrics = paper_metrics(now_ms=now_ms)
    if metrics["readyForDemo"] and not runtime.automation_state["readinessNotified"]:
        if telegram_send("🧪 КРИТЕРІЇ PAPER ВИКОНАНО\n" + readiness_text()) is not None:
            runtime.automation_state["readinessNotified"] = True
            persist_app_state("readinessNotified", True)
            audit("readiness_reached", metrics)
    if now_ms - runtime.automation_state["lastReportAt"] >= config.TELEGRAM_REPORT_INTERVAL_SEC * 1000:
        if telegram_send("📊 ДОБОВИЙ PAPER-ЗВІТ\n" + readiness_text()) is not None:
            runtime.automation_state["lastReportAt"] = now_ms
            persist_app_state("lastReportAt", now_ms)


def automation_loop():
    while True:
        try:
            payload = market_payload(config.AUTOMATION_FEE_PCT)
            health = update_market_monitor(payload)
            evaluate_automation(payload, allow_entries=health["status"] == "ok")
            maybe_send_periodic_report()
            runtime.automation_state["lastError"] = ""
        except Exception as exc:
            runtime.automation_state["lastError"] = str(exc)
            audit("automation_error", {"error": str(exc)})
        time.sleep(config.CACHE_TTL)
