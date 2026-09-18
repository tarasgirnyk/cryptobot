"""ЖИВА проба MEXC futures API: чи може акаунт відкривати/закривати позиції через ccxt.

Робить РЕАЛЬНИЙ мікро-ордер (~6 USDT notional за замовч.) і одразу закриває його
reduce_only. Гроші мають лежати в MEXC **Futures** гаманці (не spot).

Запуск (з whitelisted IP):
    PYTHONUTF8=1 python probe_mexc.py
Параметри через env:
    PROBE_NOTIONAL_USDT (6)  PROBE_LEVERAGE (3)  PROBE_SYMBOL (BTC/USDT:USDT)
    PROBE_SIDE (buy)         PROBE_CONFIRM (пусто -> dry-run; "YES" -> шле ордер)
"""
from __future__ import annotations
import os, socket, sys, time, uuid
from pathlib import Path

if os.environ.get("FORCE_IPV4", "1") != "0":
    _g = socket.getaddrinfo
    socket.getaddrinfo = lambda h, p, f=0, t=0, pr=0, fl=0: _g(h, p, socket.AF_INET, t, pr, fl)

import ccxt

ROOT = Path(r"C:\Users\pc1\Documents\CryptoBOT")
SYMBOL   = os.getenv("PROBE_SYMBOL", "BTC/USDT:USDT")
NOTIONAL = float(os.getenv("PROBE_NOTIONAL_USDT", "6"))
LEVERAGE = int(os.getenv("PROBE_LEVERAGE", "3"))
SIDE     = os.getenv("PROBE_SIDE", "buy").lower()
CONFIRM  = os.getenv("PROBE_CONFIRM", "").strip().upper() == "YES"
CLOSE_SIDE = "sell" if SIDE == "buy" else "buy"


def die(msg: str):
    print(f"\n❌ {msg}")
    sys.exit(1)


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


env = load_env(ROOT / ".env")
raw = (env.get("MEXC_API_KEYS") or "").split(",")[0].strip()
if ":" not in raw:
    die("MEXC_API_KEYS немає у .env")
key, secret = (p.strip() for p in raw.split(":", 1))

ex = ccxt.mexc({
    "apiKey": key, "secret": secret, "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

print("=" * 66)
print(f"  MEXC futures probe — {SYMBOL}  notional≈{NOTIONAL} USDT  lev x{LEVERAGE}")
print(f"  режим: {'РЕАЛЬНИЙ ОРДЕР' if CONFIRM else 'DRY-RUN (ордер не надсилається)'}")
print("=" * 66)

# --- ринок ---
try:
    ex.load_markets()
except Exception as e:
    die(f"load_markets: {type(e).__name__}: {str(e)[:200]}")
if SYMBOL not in ex.markets:
    die(f"{SYMBOL} немає в ринках MEXC swap")
mkt = ex.market(SYMBOL)
cs = mkt.get("contractSize") or 1
lim = mkt.get("limits", {})
print(f"contractSize={cs}  amount.limits={lim.get('amount')}  cost.limits={lim.get('cost')}")

# --- баланс futures ---
try:
    bal = ex.fetch_balance()
except Exception as e:
    die(f"fetch_balance: {type(e).__name__}: {str(e)[:200]}")
u = bal.get("USDT") or {}
free = float(u.get("free") or 0)
print(f"MEXC futures USDT: free={free}  total={u.get('total')}")
if free <= 0:
    die("на MEXC futures гаманці 0 USDT — переведи ~10 USDT зі Spot у Futures і повтори")

# --- ціна й обсяг (MEXC: amount у КОНТРАКТАХ, 1 контракт = contractSize бази) ---
px = float(ex.fetch_ticker(SYMBOL)["last"])
csf = float(cs) if cs else 1.0
min_amt = float((lim.get("amount") or {}).get("min") or 1)
raw_contracts = NOTIONAL / (px * csf)
if raw_contracts <= min_amt:
    amount = min_amt
else:
    amount = float(ex.amount_to_precision(SYMBOL, raw_contracts))
    if amount < min_amt:
        amount = min_amt
est_notional = amount * px * csf
print(f"ціна={px}  amount={amount} контрактів (min={min_amt})  "
      f"~notional={est_notional:.2f} USDT  ~margin={est_notional / LEVERAGE:.2f} USDT")
if amount <= 0:
    die("розрахунковий обсяг = 0 (підніми PROBE_NOTIONAL_USDT)")
if est_notional / LEVERAGE > free:
    die(f"замало вільної маржі: треба ~{est_notional / LEVERAGE:.2f}, є {free}")

if not CONFIRM:
    print("\nDRY-RUN завершено (нічого не змінено). Якщо все вище ок — запусти з PROBE_CONFIRM=YES")
    sys.exit(0)

# --- set_leverage (best effort) ---
for ot in (1,):  # 1 = isolated
    for pt in (1, 2):
        try:
            ex.set_leverage(LEVERAGE, SYMBOL, {"openType": ot, "positionType": pt})
            print(f"set_leverage ok (openType={ot}, positionType={pt})")
        except Exception as e:
            print(f"set_leverage warn (openType={ot}, positionType={pt}): {str(e)[:140]}")

# --- OPEN ---
cid_open = "probe-" + uuid.uuid4().hex[:16]
print(f"\n>>> OPEN {SIDE} {amount} {SYMBOL} (market)  cid={cid_open}")
opened = None
try:
    opened = ex.create_order(SYMBOL, "market", SIDE, amount, None,
                             {"clientOrderId": cid_open, "openType": 1})
    print(f"    id={opened.get('id')}  status={opened.get('status')}  "
          f"filled={opened.get('filled')}  avg={opened.get('average') or opened.get('price')}")
except Exception as e:
    die(f"OPEN відхилено: {type(e).__name__}: {str(e)[:300]}\n"
        f"   => MEXC futures ордерний API акаунту НЕДОСТУПНИЙ. Заглушку лишаємо.")

time.sleep(2)
pos_side = None
pos_contracts = amount
try:
    rows = ex.fetch_positions([SYMBOL])
    for r in rows or []:
        c = abs(float(r.get("contracts") or 0))
        if c > 0:
            pos_side = str(r.get("side"))
            pos_contracts = c
            print(f"    позиція: side={pos_side} contracts={c} entry={r.get('entryPrice')} "
                  f"uPnL={r.get('unrealizedPnl')}")
except Exception as e:
    print(f"    fetch_positions warn: {str(e)[:160]}")

# --- CLOSE (обовʼязково) ---
cid_close = "probe-" + uuid.uuid4().hex[:16]
print(f"\n>>> CLOSE {CLOSE_SIDE} {pos_contracts} {SYMBOL} reduce_only  cid={cid_close}")
closed = None
for attempt, params in enumerate((
    {"clientOrderId": cid_close, "reduceOnly": True, "openType": 1},
    {"clientOrderId": cid_close + "b", "reduceOnly": True},
    {"clientOrderId": cid_close + "c"},
), 1):
    try:
        closed = ex.create_order(SYMBOL, "market", CLOSE_SIDE, pos_contracts, None, params)
        print(f"    закрито: id={closed.get('id')} status={closed.get('status')} "
              f"filled={closed.get('filled')} (спроба {attempt}, params={list(params)})")
        break
    except Exception as e:
        print(f"    close спроба {attempt} fail: {str(e)[:200]}")
        time.sleep(1)

time.sleep(2)
flat = False
try:
    rows = ex.fetch_positions([SYMBOL])
    live = [r for r in (rows or []) if abs(float(r.get("contracts") or 0)) > 0]
    flat = not live
    print(f"\nпозиція після закриття: {'ПУСТО ✅' if flat else live}")
except Exception as e:
    print(f"fetch_positions(after) warn: {str(e)[:160]}")

print("\n" + "=" * 66)
if opened and closed and flat:
    print("  РЕЗУЛЬТАТ: ✅ MEXC futures ордерний API ПРАЦЮЄ — можна вмикати в бота.")
elif opened and not flat:
    print("  РЕЗУЛЬТАТ: ⚠️  ОРДЕР ВІДКРИВСЯ, АЛЕ ПОЗИЦІЯ НЕ ЗАКРИЛАСЯ!")
    print("  ЗАКРИЙ ВРУЧНУ в застосунку MEXC: Futures -> BTC/USDT -> Close.")
else:
    print("  РЕЗУЛЬТАТ: ❌ проба не пройшла — деталі вище.")
print("=" * 66)
