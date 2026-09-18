"""Read-only перевірка конекту до всіх бірж із .env (LIVE endpoints).

Нічого не торгує: лише час сервера, load_markets і fetch_balance (баланс).
Запуск із папки проєкту:  python check_connect.py
"""
from __future__ import annotations
import os, sys, time, socket
from pathlib import Path

# Багато бірж (BingX) не приймають IPv6 у whitelist. За замовчуванням форсуємо IPv4.
# Вимкнути: FORCE_IPV4=0 python check_connect.py
if os.environ.get("FORCE_IPV4", "1") != "0":
    _orig_getaddrinfo = socket.getaddrinfo
    def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    socket.getaddrinfo = _ipv4_only

try:
    import ccxt
except ImportError:
    sys.exit("ccxt не встановлено. Спершу: pip install -r requirements.txt")

HERE = Path(__file__).resolve().parent

def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        sys.exit(f"Не знайдено {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env

env = load_env(HERE / ".env")

def parse_pair(raw: str):
    raw = (raw or "").split(",")[0].strip()
    if ":" not in raw:
        return None, None
    k, s = raw.split(":", 1)
    return k.strip(), s.strip()

EXCH = {
    "Binance": ("binanceusdm", "BINANCE_API_KEYS"),
    "Bybit":   ("bybit",       "BYBIT_API_KEYS"),
    "BingX":   ("bingx",       "BINGX_API_KEYS"),
    "MEXC":    ("mexc",        "MEXC_API_KEYS"),
}

def classify(exc: Exception) -> str:
    m = str(exc).lower()
    if isinstance(exc, ccxt.AuthenticationError):
        return "AUTH ❌ — невірний ключ/секрет або підпис"
    if isinstance(exc, ccxt.PermissionDenied):
        return "PERMISSION/IP ❌ — IP не в whitelist або бракує прав ключа"
    if isinstance(exc, ccxt.AccountSuspended):
        return "ACCOUNT SUSPENDED ❌ — акаунт заблоковано"
    if any(x in m for x in ["451", "403", "restricted", "eligibility", "geo", "unavailable from"]):
        return "GEO/IP BLOCK ❌ — біржа блокує цю IP (не ключ)"
    if isinstance(exc, (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable)):
        return "NETWORK ❌ — мережа/таймаут/блок IP"
    return f"{type(exc).__name__} ❌"

print("=" * 62)
print("  CryptoBOT — перевірка конекту до бірж (LIVE, read-only)")
print("=" * 62)

ok_count = 0
for disp, (cid, envkey) in EXCH.items():
    print(f"\n[{disp}]")
    key, secret = parse_pair(env.get(envkey, ""))
    if not (key and secret):
        print("  ключі: НЕМАЄ у .env — пропуск")
        continue
    print(f"  ключ: ...{key[-6:]}  (є)")
    ex = getattr(ccxt, cid)({
        "apiKey": key, "secret": secret, "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    try:
        drift = int(ex.fetch_time()) - int(time.time() * 1000)
        print(f"  публічний час: ok (дрейф {drift} ms)")
    except Exception as e:
        print(f"  публічний час: {classify(e)}\n    {str(e)[:200]}")
        continue
    try:
        ex.load_markets()
        print(f"  ринки: ok ({len(ex.markets)} шт.)")
    except Exception as e:
        print(f"  ринки: {classify(e)}\n    {str(e)[:200]}")
    try:
        bal = ex.fetch_balance()
        usdt = bal.get("USDT") or {}
        print(f"  АВТЕНТИФІКАЦІЯ: ✅ OK — USDT free={usdt.get('free')} total={usdt.get('total')}")
        ok_count += 1
    except Exception as e:
        print(f"  АВТЕНТИФІКАЦІЯ: {classify(e)}\n    {str(e)[:200]}")

print("\n" + "=" * 62)
print(f"  Автентифікація пройшла на {ok_count} біржах.")
print("  Нагадування: MEXC ф'ючерси бот НЕ торгує (заглушка).")
print("  Для торгівлі потрібно мінімум 2 біржі з Binance/Bybit/BingX.")
print("=" * 62)
