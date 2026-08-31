"""Створення торгового клієнта біржі з акаунта."""

from __future__ import annotations

from cryptobot.exchanges.accounts import Account
from cryptobot.exchanges.base import CcxtExchangeClient, ExchangeClient, MexcStubClient


# внутрішня назва -> ccxt id (USD-M / linear perpetual)
_CCXT_ID = {
    "Binance": "binanceusdm",
    "Bybit": "bybit",
    "BingX": "bingx",
}


def build_client(
    exchange: str,
    account: Account | None,
    *,
    sandbox: bool = True,
    enable_rate_limit: bool = True,
) -> ExchangeClient:
    if exchange == "MEXC":
        return MexcStubClient()
    if exchange not in _CCXT_ID:
        raise ValueError(f"Непідтримувана біржа для виконання: {exchange}")
    if account is None:
        raise ValueError(f"Немає акаунта для {exchange}")

    import ccxt  # лінива залежність — core-модулі ccxt не потребують

    cls = getattr(ccxt, _CCXT_ID[exchange])
    instance = cls(
        {
            "apiKey": account.key,
            "secret": account.secret,
            "enableRateLimit": enable_rate_limit,
            "options": {"defaultType": "swap"},
        }
    )
    if sandbox:
        instance.set_sandbox_mode(True)
    return CcxtExchangeClient(exchange, instance)
