"""Read-only futures wallet snapshot for the dashboard."""

from __future__ import annotations

import threading
import time

from cryptobot import config
from cryptobot.exchanges import AccountPool, build_client


_lock = threading.Lock()
_cache: dict = {"updatedAt": None, "exchanges": []}
_cache_at = 0.0
_CACHE_SECONDS = 30


def balance_snapshot(*, force: bool = False) -> dict:
    """Return configured exchanges and real USDT futures wallet balances.

    This path always targets production read-only endpoints.  It never creates
    orders and is intentionally independent from AUTOMATION_MODE.
    """
    global _cache, _cache_at
    with _lock:
        if not force and _cache_at and time.time() - _cache_at < _CACHE_SECONDS:
            return _cache

        pool = AccountPool.from_env()
        rows = []
        for name in config.SUPPORTED_EXCHANGES:
            if not pool.has(name):
                rows.append({"exchange": name, "connected": False, "error": "API-ключ не заданий"})
                continue
            try:
                client = build_client(name, pool.active(name), sandbox=False)
                client.load()
                wallet = client.usdt_balance()
                rows.append({"exchange": name, "connected": True, **wallet})
            except Exception as exc:  # noqa: BLE001 - one exchange must not hide the others
                rows.append({
                    "exchange": name,
                    "connected": False,
                    "error": str(exc)[:180],
                })

        _cache_at = time.time()
        _cache = {"updatedAt": int(_cache_at * 1000), "exchanges": rows}
        return _cache
