"""Telegram-пульт: команди, callback-кнопки, long-polling."""

from __future__ import annotations

import json
import time
import urllib.request

from cryptobot import config, runtime
from cryptobot.depth import depth_analysis
from cryptobot.paper import open_paper, close_paper, paper_snapshot
from cryptobot.reports import automation_status_text, positions_text, readiness_text
from cryptobot.risk import candidate_rejection_reason
from cryptobot.scanner import opportunity_for
from cryptobot.storage import audit, set_control_state


telegram_offset = 0


def telegram_call(method, payload=None, timeout=12):
    if not config.TELEGRAM_BOT_TOKEN:
        return None
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    return result.get("result") if result.get("ok") else None


def telegram_send(text, buttons=None):
    if not config.TELEGRAM_CHAT_ID:
        return None
    try:
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {"text": label, "callback_data": callback}
                        for label, callback in row
                    ]
                    for row in buttons
                ]
            }
        return telegram_call("sendMessage", payload)
    except Exception as exc:
        runtime.automation_state["telegramError"] = str(exc)
        return None


def handle_telegram_command(message):
    user_id = str(message.get("from", {}).get("id", ""))
    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id != config.TELEGRAM_CHAT_ID or (
        config.TELEGRAM_ALLOWED_USER_IDS and user_id not in config.TELEGRAM_ALLOWED_USER_IDS
    ):
        audit("telegram_denied", {"userId": user_id, "chatId": chat_id})
        return
    text = str(message.get("text", "")).strip()
    if not text:
        return
    command = text.split("@", 1)[0].split()[0].lower()
    if command == "/pause":
        set_control_state(paused=True)
        telegram_send("⏸ Нові входи призупинено. Відкриті позиції контролюються.")
    elif command == "/resume":
        if runtime.automation_state["killSwitch"]:
            telegram_send("🛑 Активний STOP. Для свідомого скидання використайте /resetstop.")
        else:
            set_control_state(paused=False)
            telegram_send("▶️ Автоматичний paper-режим продовжено.")
    elif command == "/stop":
        set_control_state(paused=True, kill_switch=True)
        telegram_send("🛑 STOP активовано і збережено. Нові входи заборонено.")
    elif command == "/resetstop":
        set_control_state(paused=False, kill_switch=False)
        telegram_send("▶️ STOP скинуто. Автоматичний paper-режим продовжено.")
    elif command == "/positions":
        telegram_send(positions_text() + _live_positions_suffix())
    elif command in {"/report", "/readiness"}:
        telegram_send(readiness_text())
    elif command == "/help":
        telegram_send(
            "/status — стан системи\n/positions — відкриті позиції\n"
            "/report — статистика готовності\n/pause — пауза нових входів\n"
            "/resume — продовжити\n/stop — аварійна зупинка"
        )
    elif command in {"/status", "/start"}:
        telegram_send(automation_status_text())


def handle_telegram_callback(callback):
    user_id = str(callback.get("from", {}).get("id", ""))
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
    callback_id = callback.get("id")
    if chat_id != config.TELEGRAM_CHAT_ID or (
        config.TELEGRAM_ALLOWED_USER_IDS and user_id not in config.TELEGRAM_ALLOWED_USER_IDS
    ):
        audit("telegram_callback_denied", {"userId": user_id, "chatId": chat_id})
        if callback_id:
            telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Немає доступу"})
        return

    data = str(callback.get("data", ""))
    answer = "Виконано"
    try:
        if data == "pause":
            set_control_state(paused=True)
            answer = "Нові входи призупинено"
            telegram_send("⏸ Нові входи призупинено. Відкриті позиції контролюються.")
        elif data.startswith("ignore:"):
            symbol = data.split(":", 1)[1].upper()
            runtime.symbol_cooldowns[symbol] = time.time() + config.SYMBOL_COOLDOWN_SEC
            answer = f"{symbol} пропущено"
        elif data.startswith("paper:"):
            symbol = data.split(":", 1)[1].upper()
            opened_now = paper_snapshot()["open"]
            if len(opened_now) >= config.MAX_OPEN_POSITIONS:
                raise ValueError("Досягнуто ліміт відкритих paper-позицій")
            candidate = opportunity_for(symbol)
            if not candidate:
                raise ValueError("Символ відсутній у поточному скані")
            rejection = candidate_rejection_reason(candidate, opened_now)
            if rejection:
                raise ValueError(f"Сигнал більше не проходить risk-фільтр: {rejection}")
            depth = depth_analysis(symbol, config.PAPER_NOTIONAL_USDT)
            rejection = candidate_rejection_reason(
                candidate, opened_now, depth=depth, notional=config.PAPER_NOTIONAL_USDT
            )
            if rejection:
                raise ValueError(f"Стакан більше не проходить risk-фільтр: {rejection}")
            opened = open_paper(symbol, config.PAPER_NOTIONAL_USDT, depth)
            telegram_send(
                f"🟢 PAPER OPEN {symbol}\n"
                f"LONG {opened['longExchange']} / SHORT {opened['shortExchange']}\n"
                f"Entry spread: {opened['entryGrossPct']:.3f}%",
                [[("Закрити", f"close:{opened['id']}"), ("⏸ Пауза", "pause")]],
            )
            answer = f"Paper {symbol} відкрито"
        elif data.startswith("close:"):
            position_id = data.split(":", 1)[1]
            closed = close_paper(position_id, "telegram")
            telegram_send(
                f"🔵 PAPER CLOSE {closed['symbol']}\n"
                f"PNL: {closed.get('unrealizedPnl', 0):+.2f} USDT"
            )
            answer = f"{closed['symbol']} закрито"
        elif data.startswith("lclose:"):
            answer = _close_live(data.split(":", 1)[1])
        else:
            answer = "Невідома команда"
    except Exception as exc:
        answer = str(exc)[:180]
        audit("telegram_callback_error", {"data": data, "error": str(exc)})
    if callback_id:
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": answer})


def _live_positions_suffix() -> str:
    if config.AUTOMATION_MODE not in ("demo", "live"):
        return ""
    try:
        from cryptobot.execution import state

        rows = [state.mark_live(row) for row in state.open_live_positions()]
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return "\n\nLive-позицій немає."
    lines = [f"\n\nLive-позиції: {len(rows)}"]
    for row in rows:
        lines.append(
            f"{row['symbol']} [{row.get('state')}]: {row.get('unrealizedPnl', 0):+.2f} USDT | "
            f"{row['longExchange']}→{row['shortExchange']} | id {row['id']}"
        )
    return "\n".join(lines)


def _close_live(position_id: str) -> str:
    try:
        from cryptobot.execution import clients as registry
        from cryptobot.execution import engine, state

        target = next(
            (row for row in state.open_live_positions() if row["id"] == position_id), None
        )
        if not target:
            return "Live-позицію не знайдено"
        engine.close_hedge(target, "telegram", registry.get_clients())
        return f"{target['symbol']} закривається"
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:180]


def telegram_poll_loop():
    global telegram_offset
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    while True:
        try:
            updates = telegram_call(
                "getUpdates",
                {
                    "offset": telegram_offset,
                    "timeout": 20,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=25,
            ) or []
            for update in updates:
                telegram_offset = max(telegram_offset, int(update["update_id"]) + 1)
                if update.get("message"):
                    handle_telegram_command(update["message"])
                elif update.get("callback_query"):
                    handle_telegram_callback(update["callback_query"])
            runtime.automation_state["telegramError"] = ""
        except Exception as exc:
            runtime.automation_state["telegramError"] = str(exc)
            time.sleep(5)
