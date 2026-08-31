"""Текстові зведення для Telegram."""

from __future__ import annotations

import time

from cryptobot import config, runtime
from cryptobot.paper import paper_metrics, paper_snapshot
from cryptobot.util import number


def automation_status_text():
    metrics = paper_metrics()
    state = (
        "STOP"
        if runtime.automation_state["killSwitch"]
        else "PAUSE"
        if runtime.automation_state["paused"]
        else "RUN"
    )
    return (
        f"CryptoBOT: {state}\n"
        f"Режим: {config.AUTOMATION_MODE}\n"
        f"Ринок: {metrics['market']['status']}\n"
        f"Біржі: {', '.join(metrics['market']['exchanges'])}\n"
        f"Paper-позицій: {metrics['openTrades']}/{config.MAX_OPEN_POSITIONS}\n"
        f"Відкритий PNL: {metrics['unrealizedPnl']:+.2f} USDT\n"
        f"Закритий PNL: {metrics['realizedPnl']:+.2f} USDT\n"
        f"Загальний PNL: {metrics['totalPnl']:+.2f} USDT"
    )


def positions_text():
    opened = paper_snapshot()["open"]
    if not opened:
        return "Відкритих paper-позицій немає."
    lines = [f"Відкриті paper-позиції: {len(opened)}/{config.MAX_OPEN_POSITIONS}"]
    for row in opened:
        age_hours = max(0.0, (time.time() * 1000 - number(row.get("openedAt"))) / 3_600_000)
        lines.append(
            f"{row['symbol']}: {row.get('unrealizedPnl', 0):+.2f} USDT | "
            f"{row['longExchange']}→{row['shortExchange']} | {age_hours:.1f} год"
        )
    return "\n".join(lines)


def readiness_text():
    metrics = paper_metrics()
    state = "ГОТОВО ДО DEMO" if metrics["readyForDemo"] else "ЗБІР СТАТИСТИКИ"
    profit_factor = metrics["profitFactor"]
    profit_factor_text = "—" if profit_factor is None else f"{profit_factor:.2f}"
    return (
        f"Paper readiness: {state}\n"
        f"Угоди: {metrics['closedTrades']}/{config.READINESS_MIN_CLOSED_TRADES}\n"
        f"Спостереження: {metrics['observationDays']:.1f}/{config.READINESS_MIN_DAYS} днів\n"
        f"Win rate: {metrics['winRatePct']:.1f}%\n"
        f"Stop rate: {metrics['stopRatePct']:.1f}%/{config.READINESS_MAX_STOP_RATE_PCT:.1f}% max\n"
        f"Profit factor: {profit_factor_text}\n"
        f"Закритий PNL: {metrics['realizedPnl']:+.2f} USDT\n"
        f"Max drawdown: {metrics['maxDrawdown']:.2f} USDT"
    )
