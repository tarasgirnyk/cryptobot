"""Фоновий моніторинг маржі відкритих live-позицій."""

from __future__ import annotations

import time

from cryptobot import config
from cryptobot.execution import state
from cryptobot.execution.state import HEDGED
from cryptobot.exchanges.base import ExchangeOpError
from cryptobot.storage import audit
from cryptobot.telegram import telegram_send


_warned: set[str] = set()


def _leg_danger(pos_obj) -> tuple[bool, bool, dict]:
    """(critical, warn, info) для однієї ноги."""
    if pos_obj is None:
        return False, False, {}
    ratio = float(pos_obj.margin_ratio or 0)
    liq = float(pos_obj.liq_price or 0)
    mark = float(pos_obj.mark_price or 0)
    liq_dist_pct = abs(mark - liq) / mark * 100 if mark > 0 and liq > 0 else None
    critical = ratio >= config.MARGIN_CRITICAL or (
        liq_dist_pct is not None and liq_dist_pct <= config.MARGIN_LIQ_DISTANCE_CRIT_PCT
    )
    warn = ratio >= config.MARGIN_WARN
    return critical, warn, {"marginRatio": ratio, "liqDistancePct": liq_dist_pct}


def check_once(clients: dict, close_fn) -> None:
    for pos in state.open_live_positions():
        if pos.get("state") != HEDGED:
            continue
        for tag in ("long", "short"):
            client = clients.get(pos[f"{tag}Exchange"])
            if client is None:
                continue
            try:
                leg_pos = client.position(pos["symbol"])
            except ExchangeOpError:
                continue
            critical, warn, info = _leg_danger(leg_pos)
            if critical:
                audit("margin_critical", {"id": pos["id"], "leg": tag, **info})
                telegram_send(
                    f"🛑 MARGIN CRITICAL {pos['symbol']} ({pos[f'{tag}Exchange']})\n"
                    f"Закриваю хедж негайно."
                )
                try:
                    close_fn(pos, "margin", clients)
                except Exception as exc:  # noqa: BLE001
                    audit("margin_close_failed", {"id": pos["id"], "error": str(exc)})
                break
            if warn and pos["id"] not in _warned:
                _warned.add(pos["id"])
                audit("margin_warn", {"id": pos["id"], "leg": tag, **info})
                telegram_send(
                    f"⚠️ MARGIN WARN {pos['symbol']} ({pos[f'{tag}Exchange']})\n"
                    f"marginRatio={info['marginRatio']:.2f}. Слідкую."
                )


def margin_monitor_loop(clients: dict, close_fn) -> None:
    while True:
        try:
            check_once(clients, close_fn)
        except Exception as exc:  # noqa: BLE001
            audit("margin_monitor_error", {"error": str(exc)})
        time.sleep(config.MARGIN_MONITOR_INTERVAL_SEC)
