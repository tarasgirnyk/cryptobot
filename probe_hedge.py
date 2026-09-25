"""Controlled Binance/Bybit two-leg micro test (dry-run by default)."""

from __future__ import annotations

import os
import sys
import time

from cryptobot.execution.engine import close_hedge, open_hedge
from cryptobot.execution.plan import TradePlan
from cryptobot.exchanges import AccountPool, build_client
from cryptobot.risk import RiskEngine
from cryptobot.storage import init_storage


SYMBOL = os.getenv("PROBE_SYMBOL", "XRPUSDT").upper()
NOTIONAL = float(os.getenv("PROBE_NOTIONAL_USDT", "6"))
LEVERAGE = int(os.getenv("PROBE_LEVERAGE", "3"))
CONFIRM = os.getenv("PROBE_CONFIRM", "").strip().upper() == "YES"


def die(message: str) -> None:
    print(f"\n❌ {message}")
    raise SystemExit(1)


pool = AccountPool.from_env()
clients = {}
for name in ("Binance", "Bybit"):
    if not pool.has(name):
        die(f"немає API-ключа {name}")
    client = build_client(name, pool.active(name), sandbox=False)
    client.load()
    clients[name] = client

quotes = {}
for name, client in clients.items():
    ticker = client.ccxt.fetch_ticker(client.unified(SYMBOL))
    quotes[name] = {
        "bid": float(ticker.get("bid") or ticker.get("last") or 0),
        "ask": float(ticker.get("ask") or ticker.get("last") or 0),
    }
    if min(quotes[name].values()) <= 0:
        die(f"{name}: немає коректного bid/ask")

long_name = min(quotes, key=lambda name: quotes[name]["ask"])
short_name = next(name for name in quotes if name != long_name)
long_price = quotes[long_name]["ask"]
short_price = quotes[short_name]["bid"]
gross = (short_price - long_price) / long_price * 100

print("=" * 68)
print(f"  TWO-LEG MICRO — {SYMBOL} — {'REAL' if CONFIRM else 'DRY-RUN'}")
print("=" * 68)
print(f"LONG  {long_name:<8} ask={long_price}")
print(f"SHORT {short_name:<8} bid={short_price}")
print(f"gross={gross:+.4f}%  notional={NOTIONAL:.2f} USDT/leg  leverage=x{LEVERAGE}")

for name, client in clients.items():
    qty = client.amount_for_notional(SYMBOL, NOTIONAL, quotes[name]["ask"])
    effective = qty * quotes[name]["ask"]
    free = client.free_collateral()
    position = client.position(SYMBOL)
    print(f"{name}: qty={qty:g} base  effective≈{effective:.4f} USDT  free={free:.4f}")
    if position is not None:
        die(f"{name}: вже є позиція {SYMBOL}: {position.base_qty:g}")

if not CONFIRM:
    print("\nDRY-RUN OK — ордери не створювались.")
    raise SystemExit(0)

now = int(time.time() * 1000)
plan = TradePlan(
    symbol=SYMBOL,
    long_exchange=long_name,
    short_exchange=short_name,
    notional_usdt=NOTIONAL,
    leverage=LEVERAGE,
    created_at_ms=now,
    expires_at_ms=now + 30_000,
    expected_net_pct=gross,
    entry_ref_spread_pct=gross,
    round_trip_fees_pct=0.22,
    long_ref_price=long_price,
    short_ref_price=short_price,
    entry_executable_spread_pct=gross,
)

init_storage()
position = open_hedge(plan, clients, RiskEngine())
print(f"OPEN RESULT: state={position.get('state')} id={position.get('id')}")
if position.get("state") != "HEDGED":
    die(f"хедж не відкрився: {position.get('state')} {position.get('note', '')}")

time.sleep(2)
closed = close_hedge(position, "micro_probe", clients)
print(
    f"CLOSE RESULT: state={closed.get('state')} "
    f"pnl={float(closed.get('realizedPnl') or 0):+.6f} USDT"
)

residuals = []
for name, client in clients.items():
    live = client.position(SYMBOL)
    if live is not None and abs(live.base_qty) > 0:
        residuals.append(f"{name}:{live.base_qty:g}")
if residuals:
    die("ЗАЛИШКОВІ ПОЗИЦІЇ: " + ", ".join(residuals))
print("✅ Обидві біржі без позицій після тесту.")
