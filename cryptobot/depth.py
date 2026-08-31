"""Аналіз стакана: VWAP на заданий обсяг, заповнення та прослизання."""

from __future__ import annotations

import urllib.parse

from cryptobot import config
from cryptobot.scanner import opportunity_for, quote_for
from cryptobot.util import fetch_json, number


def levels_vwap(levels, notional):
    remaining = notional
    base = 0.0
    quote = 0.0
    for price_raw, qty_raw in levels:
        price, qty = number(price_raw), number(qty_raw)
        available_quote = price * qty
        take_quote = min(remaining, available_quote)
        if price <= 0 or take_quote <= 0:
            continue
        base += take_quote / price
        quote += take_quote
        remaining -= take_quote
        if remaining <= 0.000001:
            break
    return {
        "vwap": quote / base if base else 0,
        "filledQuote": quote,
        "fillPct": min(100.0, quote / notional * 100) if notional else 0,
    }


def load_depth(exchange: str, symbol: str):
    quote = quote_for(exchange, symbol)
    if not quote:
        raise ValueError(f"Немає котирування {exchange} для {symbol}")
    native_symbol = quote.get("nativeSymbol", symbol)
    if exchange == "Binance":
        query = urllib.parse.urlencode({"symbol": native_symbol, "limit": 100})
        payload = fetch_json(f"{config.BINANCE_DEPTH}?{query}")
        return {"bids": payload.get("bids", []), "asks": payload.get("asks", [])}
    if exchange == "Bybit":
        query = urllib.parse.urlencode(
            {"category": "linear", "symbol": native_symbol, "limit": 200}
        )
        payload = fetch_json(f"{config.BYBIT_DEPTH}?{query}").get("result", {})
        return {"bids": payload.get("b", []), "asks": payload.get("a", [])}
    if exchange == "BingX":
        query = urllib.parse.urlencode({"symbol": native_symbol, "limit": 100})
        payload = fetch_json(f"{config.BINGX_DEPTH}?{query}").get("data", {})
        return {"bids": payload.get("bids", []), "asks": payload.get("asks", [])}
    if exchange == "MEXC":
        query = urllib.parse.urlencode({"limit": 100})
        payload = fetch_json(f"{config.MEXC_DEPTH}/{native_symbol}?{query}")
        if not payload.get("success"):
            raise ValueError(f"MEXC depth error: {payload.get('code')}")
        book = payload.get("data", {})
        contract_size = quote.get("contractSize", 1.0)

        def normalize(levels):
            return [[row[0], number(row[1]) * contract_size] for row in levels]

        return {"bids": normalize(book.get("bids", [])), "asks": normalize(book.get("asks", []))}
    raise ValueError(f"Непідтримувана біржа: {exchange}")


def depth_analysis(symbol: str, notional: float):
    opportunity = opportunity_for(symbol)
    if not opportunity:
        raise ValueError("Символ відсутній у поточному скані")
    long_book = load_depth(opportunity["longExchange"], symbol)
    short_book = load_depth(opportunity["shortExchange"], symbol)
    long_fill = levels_vwap(long_book["asks"], notional)
    short_fill = levels_vwap(short_book["bids"], notional)
    executable = 0.0
    if long_fill["vwap"] > 0 and short_fill["vwap"] > 0:
        executable = (short_fill["vwap"] - long_fill["vwap"]) / long_fill["vwap"] * 100
    return {
        "symbol": symbol,
        "notional": notional,
        "longExchange": opportunity["longExchange"],
        "shortExchange": opportunity["shortExchange"],
        "longVwap": long_fill["vwap"],
        "shortVwap": short_fill["vwap"],
        "longFillPct": long_fill["fillPct"],
        "shortFillPct": short_fill["fillPct"],
        "topSpreadPct": opportunity["grossSpreadPct"],
        "executableSpreadPct": executable,
        "slippagePct": opportunity["grossSpreadPct"] - executable,
    }
