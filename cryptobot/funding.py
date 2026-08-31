"""Модель впливу фандінгу на арбітражну зв'язку.

На відміну від «завжди рівно один інтервал», тут враховується:
- скільки разів фандінг нарахується протягом очікуваного вікна утримання;
- окремо для кожної ноги (біржі можуть мати різний інтервал розрахунку);
- чи не наближається несприятливе нарахування, під яке не варто входити.

Точний per-symbol інтервал поки не тягнемо — беремо ``config.FUNDING_INTERVAL_HOURS``
(здебільшого 8 год). Якщо ``nextFunding`` невідомий (0), відкочуємось до оцінки в
один інтервал, щоб не занулити ефект мовчки.
"""

from __future__ import annotations

import math
import time

from cryptobot import config


def _interval_ms(exchange: str) -> float:
    hours = config.FUNDING_INTERVAL_HOURS.get(exchange, 8.0)
    return max(0.5, hours) * 3_600_000


def _settlement_count(leg: dict, now_ms: float, end_ms: float) -> int:
    """Скільки разів фандінг нарахується для ноги у вікні (now, end]."""
    next_funding = float(leg.get("nextFunding") or 0)
    if next_funding <= 0:
        return 1  # немає даних про час — консервативна оцінка в один інтервал
    if next_funding > end_ms:
        return 0
    interval = _interval_ms(leg.get("exchange", ""))
    return 1 + math.floor(max(0.0, end_ms - next_funding) / interval)


def expected_effect(
    long_leg: dict, short_leg: dict, hold_hours: float, now_ms: float | None = None
) -> float:
    """Очікуваний внесок фандінгу у відсотках за час утримання.

    LONG сплачує фандінг при додатній ставці, SHORT — отримує.
    """
    now_ms = time.time() * 1000 if now_ms is None else float(now_ms)
    end_ms = now_ms + max(0.0, hold_hours) * 3_600_000
    long_count = _settlement_count(long_leg, now_ms, end_ms)
    short_count = _settlement_count(short_leg, now_ms, end_ms)
    long_rate = float(long_leg.get("funding") or 0)
    short_rate = float(short_leg.get("funding") or 0)
    return (-long_rate * long_count + short_rate * short_count) * 100


def blocks_entry(long_leg: dict, short_leg: dict, now_ms: float | None = None) -> bool:
    """True, якщо найближче нарахування фандінгу зіграє проти нас.

    Дивимось лише на нарахування в межах ``FUNDING_ENTRY_BUFFER_MIN`` хвилин.
    """
    now_ms = time.time() * 1000 if now_ms is None else float(now_ms)
    buffer_ms = config.FUNDING_ENTRY_BUFFER_MIN * 60_000
    imminent_net = 0.0
    seen_imminent = False
    for leg, sign in ((long_leg, -1.0), (short_leg, 1.0)):
        next_funding = float(leg.get("nextFunding") or 0)
        if 0 < next_funding - now_ms <= buffer_ms:
            seen_imminent = True
            imminent_net += sign * float(leg.get("funding") or 0)
    return seen_imminent and imminent_net < -1e-9
