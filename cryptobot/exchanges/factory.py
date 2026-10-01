"""Створення торгового клієнта біржі з акаунта."""

from __future__ import annotations

from cryptobot.exchanges.accounts import Account
from cryptobot.exchanges.base import CcxtExchangeClient, ExchangeClient, MexcExchangeClient


# внутрішня назва -> ccxt id (USD-M / linear perpetual)
_CCXT_ID = {
    "Binance": "binanceusdm",
    "Bybit": "bybit",
    "BingX": "bingx",
    "MEXC": "mexc",
}


def build_client(
    exchange: str,
    account: Account | None,
    *,
    sandbox: bool = True,
    enable_rate_limit: bool = True,
) -> ExchangeClient:
    if exchange not in _CCXT_ID:
        raise ValueError(f"Непідтримувана біржа для виконання: {exchange}")
    if account is None:
        raise ValueError(f"Немає акаунта для {exchange}")

    import ccxt  # лінива залежність — core-модулі ccxt не потребують

    cls = getattr(ccxt, _CCXT_ID[exchange])
    options = {"defaultType": "swap"}
    if exchange == "Bybit":
        # Private endpoints can spend several seconds behind DNS/TLS/API
        # latency. Keep the HTTP timeout fail-fast, but give the signed request
        # a wider validity window and let ccxt compensate for clock skew.
        options.update({"recvWindow": 20000, "adjustForTimeDifference": True})
    instance = cls(
        {
            "apiKey": account.key,
            "secret": account.secret,
            "enableRateLimit": enable_rate_limit,
            "timeout": 10000,
            "options": options,
        }
    )
    if sandbox:
        instance.set_sandbox_mode(True)
    if exchange == "MEXC":
        return MexcExchangeClient(instance)
    return CcxtExchangeClient(exchange, instance)
