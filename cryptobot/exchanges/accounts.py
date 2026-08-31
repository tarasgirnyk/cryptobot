"""Пул API-акаунтів на біржу.

Ця ітерація: працюємо з одним акаунтом на біржу, але формат конфігу і
методи ``report_ban`` / ``rotate`` вже закладені під майбутню ротацію при бані.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptobot.util import read_secret


_KEY_ENV = {
    "Binance": "BINANCE_API_KEYS",
    "Bybit": "BYBIT_API_KEYS",
    "BingX": "BINGX_API_KEYS",
    "MEXC": "MEXC_API_KEYS",
}


@dataclass(frozen=True)
class Account:
    exchange: str
    key: str
    secret: str
    label: str = ""


def _parse_pairs(exchange: str, raw: str) -> list[Account]:
    """``key:secret,key2:secret2`` -> [Account, ...]."""
    accounts: list[Account] = []
    for index, chunk in enumerate(part.strip() for part in raw.split(",")):
        if not chunk or ":" not in chunk:
            continue
        key, secret = chunk.split(":", 1)
        key, secret = key.strip(), secret.strip()
        if key and secret:
            accounts.append(
                Account(exchange=exchange, key=key, secret=secret, label=f"{exchange}#{index}")
            )
    return accounts


class AccountPool:
    def __init__(self, accounts: dict[str, list[Account]]):
        self._accounts = {k: list(v) for k, v in accounts.items() if v}
        self._active: dict[str, int] = {k: 0 for k in self._accounts}
        self._banned: dict[str, set[int]] = {k: set() for k in self._accounts}

    # -- конструктори --
    @classmethod
    def from_env(cls, getenv=os.getenv) -> "AccountPool":
        parsed = {
            exchange: _parse_pairs(exchange, read_secret(env_name, getenv))
            for exchange, env_name in _KEY_ENV.items()
        }
        return cls(parsed)

    # -- доступ --
    def has(self, exchange: str) -> bool:
        return bool(self._accounts.get(exchange))

    def active(self, exchange: str) -> Account:
        if not self.has(exchange):
            raise KeyError(f"Немає API-ключів для {exchange}")
        return self._accounts[exchange][self._active[exchange]]

    def count(self, exchange: str) -> int:
        return len(self._accounts.get(exchange, []))

    # -- ротація (заглушка цієї ітерації) --
    def report_ban(self, exchange: str, detail: str = "") -> None:
        """Позначає активний ключ як забанений і намагається переключитись.

        Реальний детект бану та алерти додаються в Milestone C+; тут — облік.
        """
        if not self.has(exchange):
            return
        self._banned.setdefault(exchange, set()).add(self._active[exchange])
        self.rotate(exchange)

    def rotate(self, exchange: str) -> bool:
        """Переходить на наступний незабанений ключ. False, якщо таких немає."""
        if not self.has(exchange):
            return False
        total = len(self._accounts[exchange])
        banned = self._banned.get(exchange, set())
        current = self._active[exchange]
        for step in range(1, total + 1):
            candidate = (current + step) % total
            if candidate not in banned:
                self._active[exchange] = candidate
                return candidate != current
        return False
