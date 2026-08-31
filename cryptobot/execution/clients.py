"""Лінива побудова торгових клієнтів і RiskEngine (по одному на процес)."""

from __future__ import annotations

import threading

from cryptobot import config
from cryptobot.exchanges import AccountPool, build_client
from cryptobot.exchanges.base import MexcStubClient
from cryptobot.risk import RiskEngine
from cryptobot.storage import audit


_lock = threading.Lock()
_clients: dict | None = None
_pool: AccountPool | None = None
_risk: RiskEngine | None = None


def _build() -> dict:
    global _pool
    _pool = AccountPool.from_env()
    built: dict = {}
    for name in config.EXECUTION_ENABLED_EXCHANGES:
        if name == "MEXC":
            audit("executor_exchange_skipped", {"exchange": name, "reason": "futures API stub"})
            continue
        if not _pool.has(name):
            audit("executor_exchange_skipped", {"exchange": name, "reason": "no API keys"})
            continue
        try:
            client = build_client(name, _pool.active(name), sandbox=config.use_sandbox())
            client.load()
            built[name] = client
            audit("executor_exchange_ready", {"exchange": name, "sandbox": config.use_sandbox()})
        except Exception as exc:  # noqa: BLE001
            audit("executor_exchange_failed", {"exchange": name, "error": str(exc)[:200]})
    return built


def get_clients() -> dict:
    global _clients
    with _lock:
        if _clients is None:
            _clients = _build()
        return _clients


def get_pool() -> AccountPool | None:
    return _pool


def get_risk_engine() -> RiskEngine:
    global _risk
    if _risk is None:
        _risk = RiskEngine()
    return _risk


def enabled() -> bool:
    """Двоногове виконання можливе лише за наявності >= 2 робочих бірж."""
    return len(get_clients()) >= 2


def reset() -> None:
    """Для тестів."""
    global _clients, _pool, _risk
    with _lock:
        _clients = None
        _pool = None
        _risk = None
