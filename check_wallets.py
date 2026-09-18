"""Показує, ДЕ на кожній біржі лежить USDT (Spot / Funding / Futures / Unified).

Read-only. Мета: зрозуміти, який гаманець поповнювати перед live.
Бот торгує з USDT-M перп-фʼючерсів (на Bybit — Unified Trading Account).
"""
from __future__ import annotations
import os, socket, sys
from pathlib import Path

# BingX не приймає IPv6 у whitelist — форсуємо IPv4 (як check_connect.py / probe_mexc.py).
if os.environ.get("FORCE_IPV4", "1") != "0":
    _g = socket.getaddrinfo
    socket.getaddrinfo = lambda h, p, f=0, t=0, pr=0, fl=0: _g(h, p, socket.AF_INET, t, pr, fl)

try:
    import ccxt
except ImportError:
    sys.exit("ccxt не встановлено.")

HERE = Path(__file__).resolve().parent

def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env

env = load_env(HERE / ".env")

def pair(name):
    raw = (env.get(name, "") or "").split(",")[0].strip()
    if ":" not in raw:
        return None, None
    k, s = raw.split(":", 1)
    return k.strip(), s.strip()

def usdt(bal):
    u = bal.get("USDT") or {}
    return u.get("free"), u.get("total")

def show(label, fn):
    try:
        free, total = usdt(fn())
        mark = "  <-- є кошти" if (total or 0) else ""
        print(f"    {label:<26} USDT free={free} total={total}{mark}")
    except Exception as e:
        print(f"    {label:<26} — недоступно ({type(e).__name__}: {str(e)[:90]})")

print("=" * 66)
print("  ДЕ ЛЕЖИТЬ USDT (гаманці по біржах)")
print("=" * 66)

# ---- Binance ----
k, s = pair("BINANCE_API_KEYS")
if k:
    print("\n[Binance]  (боту потрібен USDⓈ-M Futures)")
    show("Spot", lambda: ccxt.binance({"apiKey": k, "secret": s, "options": {"defaultType": "spot"}}).fetch_balance())
    show("Funding", lambda: ccxt.binance({"apiKey": k, "secret": s}).fetch_balance({"type": "funding"}))
    show("USDⓈ-M Futures *", lambda: ccxt.binanceusdm({"apiKey": k, "secret": s}).fetch_balance())

# ---- Bybit ----
k, s = pair("BYBIT_API_KEYS")
if k:
    print("\n[Bybit]  (боту потрібен Unified / Contract)")
    show("Unified *", lambda: ccxt.bybit({"apiKey": k, "secret": s}).fetch_balance({"type": "unified"}))
    show("Funding", lambda: ccxt.bybit({"apiKey": k, "secret": s}).fetch_balance({"type": "funding"}))
    show("Spot", lambda: ccxt.bybit({"apiKey": k, "secret": s, "options": {"defaultType": "spot"}}).fetch_balance())

# ---- BingX ----
k, s = pair("BINGX_API_KEYS")
if k:
    print("\n[BingX]  (боту потрібен Perpetual/Swap)")
    show("Perp/Swap *", lambda: ccxt.bingx({"apiKey": k, "secret": s, "options": {"defaultType": "swap"}}).fetch_balance())
    show("Spot", lambda: ccxt.bingx({"apiKey": k, "secret": s, "options": {"defaultType": "spot"}}).fetch_balance())

# ---- MEXC (інфо; бот не торгує) ----
k, s = pair("MEXC_API_KEYS")
if k:
    print("\n[MEXC]  (бот НЕ торгує — лише для інфо)")
    show("Spot", lambda: ccxt.mexc({"apiKey": k, "secret": s, "options": {"defaultType": "spot"}}).fetch_balance())
    show("Swap", lambda: ccxt.mexc({"apiKey": k, "secret": s, "options": {"defaultType": "swap"}}).fetch_balance())

print("\n" + "=" * 66)
print("  * = гаманець, з якого торгує бот. Саме туди має потрапити USDT.")
print("=" * 66)
