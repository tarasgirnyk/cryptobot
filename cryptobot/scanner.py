"""Публічні ринкові дані бірж і побудова арбітражних зв'язок.

Використовуються лише публічні endpoints — ключі не потрібні.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from itertools import combinations

from cryptobot import config, funding
from cryptobot.util import fetch_json, number


cache_lock = threading.Lock()
cache = {"at": 0.0, "fee": None, "payload": None}
quotes_lock = threading.Lock()
market_quotes: dict[str, dict] = {}
specs_lock = threading.Lock()
exchange_specs = {"BingX": {"at": 0.0, "rows": {}}, "MEXC": {"at": 0.0, "rows": {}}}
history_lock = threading.Lock()
spread_history = defaultdict(lambda: deque(maxlen=720))


def load_binance():
    books = fetch_json(config.BINANCE_BOOK)
    stats = {row["symbol"]: row for row in fetch_json(config.BINANCE_STATS)}
    funding_rows = {row["symbol"]: row for row in fetch_json(config.BINANCE_FUNDING)}
    result = {}
    for row in books:
        symbol = row.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        result[symbol] = {
            "exchange": "Binance",
            "nativeSymbol": symbol,
            "bid": number(row.get("bidPrice")),
            "ask": number(row.get("askPrice")),
            "turnover": number(stats.get(symbol, {}).get("quoteVolume")),
            "funding": number(funding_rows.get(symbol, {}).get("lastFundingRate")),
            "nextFunding": number(funding_rows.get(symbol, {}).get("nextFundingTime")),
            "contractSize": 1.0,
        }
    return result


def load_bybit():
    payload = fetch_json(config.BYBIT_TICKERS)
    rows = payload.get("result", {}).get("list", [])
    result = {}
    for row in rows:
        symbol = row.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        result[symbol] = {
            "exchange": "Bybit",
            "nativeSymbol": symbol,
            "bid": number(row.get("bid1Price")),
            "ask": number(row.get("ask1Price")),
            "turnover": number(row.get("turnover24h")),
            "funding": number(row.get("fundingRate")),
            "nextFunding": number(row.get("nextFundingTime")),
            "contractSize": 1.0,
        }
    return result


def cached_exchange_specs(exchange, url):
    with specs_lock:
        cached = exchange_specs[exchange]
        if cached["rows"] and time.time() - cached["at"] < 3600:
            return cached["rows"]
    payload = fetch_json(url)
    rows = payload.get("data", [])
    parsed = {row.get("symbol"): row for row in rows if row.get("symbol")}
    with specs_lock:
        exchange_specs[exchange] = {"at": time.time(), "rows": parsed}
    return parsed


def load_bingx():
    specs = cached_exchange_specs("BingX", config.BINGX_CONTRACTS)
    tickers = fetch_json(config.BINGX_TICKERS).get("data", [])
    premium = {
        row.get("symbol"): row for row in fetch_json(config.BINGX_PREMIUM).get("data", [])
    }
    result = {}
    for row in tickers:
        native_symbol = row.get("symbol", "")
        spec = specs.get(native_symbol, {})
        if (
            not native_symbol.endswith("-USDT")
            or number(spec.get("status")) != 1
            or str(spec.get("apiStateOpen", "")).lower() != "true"
            or str(spec.get("apiStateClose", "")).lower() != "true"
        ):
            continue
        symbol = native_symbol.replace("-", "")
        funding_row = premium.get(native_symbol, {})
        result[symbol] = {
            "exchange": "BingX",
            "nativeSymbol": native_symbol,
            "bid": number(row.get("bidPrice")),
            "ask": number(row.get("askPrice")),
            "turnover": number(row.get("quoteVolume")),
            "funding": number(funding_row.get("lastFundingRate")),
            "nextFunding": number(funding_row.get("nextFundingTime")),
            "contractSize": number(spec.get("size"), 1.0),
        }
    return result


def load_mexc():
    specs = cached_exchange_specs("MEXC", config.MEXC_CONTRACTS)
    payload = fetch_json(config.MEXC_TICKERS)
    if not payload.get("success"):
        raise ValueError(f"MEXC ticker error: {payload.get('code')}")
    result = {}
    for row in payload.get("data", []):
        native_symbol = row.get("symbol", "")
        spec = specs.get(native_symbol, {})
        if (
            not native_symbol.endswith("_USDT")
            or number(spec.get("state"), -1) != 0
            or spec.get("apiAllowed") is not True
            or spec.get("isHidden") is True
            or spec.get("preMarket") is True
        ):
            continue
        symbol = native_symbol.replace("_", "")
        result[symbol] = {
            "exchange": "MEXC",
            "nativeSymbol": native_symbol,
            "bid": number(row.get("bid1")),
            "ask": number(row.get("ask1")),
            "turnover": number(row.get("amount24")),
            "funding": number(row.get("fundingRate")),
            "nextFunding": 0.0,
            "contractSize": number(spec.get("contractSize"), 1.0),
        }
    return result


def build_opportunities(fee_pct: float):
    data = {}
    errors = []

    def run(name, loader):
        try:
            data[name] = loader()
        except Exception as exc:  # API failures must not crash the terminal
            errors.append(f"{name}: {exc}")

    # Резолвимо лоадери за іменем на кожному виклику, щоб їх можна було мокати.
    loaders = {
        "Binance": load_binance,
        "Bybit": load_bybit,
        "BingX": load_bingx,
        "MEXC": load_mexc,
    }
    threads = [
        threading.Thread(target=run, args=(name, loaders[name]))
        for name in config.ENABLED_EXCHANGES
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with quotes_lock:
        market_quotes.clear()
        market_quotes.update(data)

    best_by_symbol = {}
    round_trip_fees = fee_pct * 4
    for left_name, right_name in combinations(sorted(data), 2):
        left, right = data[left_name], data[right_name]
        for symbol in left.keys() & right.keys():
            a, b = left[symbol], right[symbol]
            if min(a["bid"], a["ask"], b["bid"], b["ask"]) <= 0:
                continue
            price_deviation = abs(a["ask"] - b["ask"]) / min(a["ask"], b["ask"]) * 100
            if price_deviation > config.MAX_PRICE_DEVIATION_PCT:
                continue
            if a["ask"] <= b["ask"]:
                long_leg, short_leg = a, b
            else:
                long_leg, short_leg = b, a
            gross = (short_leg["bid"] - long_leg["ask"]) / long_leg["ask"] * 100
            funding_effect = funding.expected_effect(
                long_leg, short_leg, config.PAPER_MAX_HOLD_HOURS
            )
            net = gross - round_trip_fees + funding_effect
            candidate = {
                "symbol": symbol,
                "longExchange": long_leg["exchange"],
                "shortExchange": short_leg["exchange"],
                "longAsk": long_leg["ask"],
                "shortBid": short_leg["bid"],
                "grossSpreadPct": gross,
                "roundTripFeesPct": round_trip_fees,
                "fundingEffectPct": funding_effect,
                "netSpreadPct": net,
                "longFundingPct": long_leg["funding"] * 100,
                "shortFundingPct": short_leg["funding"] * 100,
                "longNextFunding": long_leg.get("nextFunding", 0),
                "shortNextFunding": short_leg.get("nextFunding", 0),
                "minTurnover24h": min(long_leg["turnover"], short_leg["turnover"]),
                "warning": "double-negative-funding"
                if long_leg["funding"] < 0 and short_leg["funding"] < 0
                else "",
            }
            if (
                symbol not in best_by_symbol
                or candidate["netSpreadPct"] > best_by_symbol[symbol]["netSpreadPct"]
            ):
                best_by_symbol[symbol] = candidate

    opportunities = list(best_by_symbol.values())
    opportunities.sort(key=lambda row: row["netSpreadPct"], reverse=True)
    timestamp = int(time.time() * 1000)
    with history_lock:
        for row in opportunities:
            spread_history[row["symbol"]].append(
                {
                    "at": timestamp,
                    "gross": row["grossSpreadPct"],
                    "net": row["netSpreadPct"],
                }
            )
    return {
        "generatedAt": timestamp,
        "feePctPerOrder": fee_pct,
        "exchanges": sorted(data),
        "errors": errors,
        "opportunities": opportunities,
    }


def opportunity_for(symbol: str):
    payload = cache.get("payload") or {}
    return next(
        (row for row in payload.get("opportunities", []) if row["symbol"] == symbol),
        None,
    )


def quote_for(exchange: str, symbol: str):
    with quotes_lock:
        quote = market_quotes.get(exchange, {}).get(symbol)
        return dict(quote) if quote else None


def market_payload(fee_pct):
    now = time.time()
    with cache_lock:
        if (
            cache["payload"] is None
            or cache["fee"] != fee_pct
            or now - cache["at"] > config.CACHE_TTL
        ):
            cache["payload"] = build_opportunities(fee_pct)
            cache["at"] = now
            cache["fee"] = fee_pct
        return cache["payload"]
