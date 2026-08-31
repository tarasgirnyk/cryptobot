"""Звірка стану після рестарту: БД vs фактичні позиції на біржах."""

from __future__ import annotations

from cryptobot import config, runtime
from cryptobot.execution import state
from cryptobot.execution.state import HEDGED
from cryptobot.exchanges.base import ExchangeOpError
from cryptobot.storage import audit, set_control_state
from cryptobot.telegram import telegram_send


def _qty_matches(actual: float, expected: float) -> bool:
    tol = max(abs(expected) * config.FILL_TOLERANCE_PCT / 100, 1e-9)
    return abs(abs(actual) - abs(expected)) <= tol


def startup_reconcile(clients: dict) -> bool:
    """True, якщо все зійшлось. Інакше вмикає STOP і повідомляє в Telegram.

    Перевіряє два боки:
    - кожна HEDGED-позиція з БД має відповідні ноги на обох біржах;
    - на біржах немає позицій за символами, яких немає в БД (orphan після краху).
    """
    mismatches: list[dict] = []
    tracked = state.live_snapshot()
    tracked_symbols = {row["symbol"] for row in tracked}

    for pos in tracked:
        if pos.get("state") != HEDGED:
            mismatches.append({"id": pos["id"], "symbol": pos["symbol"],
                               "reason": f"незавершений стан {pos.get('state')}"})
            continue
        expected = pos.get("hedgedBaseQty") or pos.get("targetBaseQty") or 0.0
        for tag in ("long", "short"):
            exch = pos[f"{tag}Exchange"]
            client = clients.get(exch)
            if client is None:
                mismatches.append({"id": pos["id"], "symbol": pos["symbol"],
                                   "reason": f"немає клієнта {exch}"})
                continue
            try:
                actual = client.position(pos["symbol"])
            except ExchangeOpError as exc:
                mismatches.append({"id": pos["id"], "symbol": pos["symbol"],
                                   "reason": f"{exch}: {exc}"})
                continue
            qty = actual.base_qty if actual else 0.0
            if not _qty_matches(qty, expected):
                mismatches.append({"id": pos["id"], "symbol": pos["symbol"],
                                   "reason": f"{exch} обсяг {qty} != {expected}"})

    # orphan-позиції на біржах
    for name, client in clients.items():
        try:
            rows = client.ccxt.fetch_positions() if hasattr(client, "ccxt") else []
        except Exception as exc:  # noqa: BLE001
            audit("reconcile_scan_failed", {"exchange": name, "error": str(exc)})
            continue
        for row in rows or []:
            contracts = abs(float(row.get("contracts") or 0))
            if contracts <= 0:
                continue
            unified = row.get("symbol", "")
            base = unified.split("/")[0]
            internal = f"{base}USDT"
            if internal not in tracked_symbols:
                mismatches.append({"symbol": internal, "exchange": name,
                                   "reason": "позиція на біржі без запису в БД"})

    if mismatches:
        audit("reconcile_mismatch", {"items": mismatches})
        set_control_state(paused=True, kill_switch=True)
        runtime.automation_state["startupReconciled"] = False
        lines = "\n".join(f"• {m['symbol']}: {m['reason']}" for m in mismatches[:10])
        telegram_send(
            "🛑 STARTUP RECONCILE: розбіжність стану\n" + lines +
            "\nНові входи заблоковано. Перевір біржі вручну і зроби /resetstop."
        )
        return False

    runtime.automation_state["startupReconciled"] = True
    audit("reconcile_ok", {"tracked": len(tracked)})
    return True
