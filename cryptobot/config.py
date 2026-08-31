"""Усі параметри читаються з середовища тут, щоб решта коду не чіпала ``os.getenv``.

Значення беруться під час імпорту. Тести підміняють атрибути цього модуля
(``config.DATA_DIR``, ``config.ENABLED_EXCHANGES`` тощо) напряму.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptobot.util import number, read_secret


# Корінь проєкту (містить index.html) — на рівень вище за пакет.
ROOT = Path(__file__).resolve().parent.parent

HOST = os.getenv("CRYPTBOT_HOST", "127.0.0.1")
PORT = int(os.getenv("CRYPTBOT_PORT", "8765"))
CACHE_TTL = max(1, int(os.getenv("SCAN_INTERVAL_SEC", "5")))

# --- Публічні ринкові endpoints -------------------------------------------------
BINANCE_BOOK = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
BINANCE_STATS = "https://fapi.binance.com/fapi/v1/ticker/24hr"
BINANCE_FUNDING = "https://fapi.binance.com/fapi/v1/premiumIndex"
BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers?category=linear"
BINANCE_DEPTH = "https://fapi.binance.com/fapi/v1/depth"
BYBIT_DEPTH = "https://api.bybit.com/v5/market/orderbook"
BINGX_TICKERS = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
BINGX_PREMIUM = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"
BINGX_CONTRACTS = "https://open-api.bingx.com/openApi/swap/v2/quote/contracts"
BINGX_DEPTH = "https://open-api.bingx.com/openApi/swap/v2/quote/depth"
MEXC_TICKERS = "https://contract.mexc.com/api/v1/contract/ticker"
MEXC_CONTRACTS = "https://contract.mexc.com/api/v1/contract/detail"
MEXC_DEPTH = "https://contract.mexc.com/api/v1/contract/depth"

DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))

# --- Режим автоматизації ------------------------------------------------------
# observe — лише сигнали; paper — віртуальні входи; demo/live — реальні API
# (виконавець додається окремим етапом, поки поводяться як paper з попередженням).
AUTOMATION_MODE = os.getenv("AUTOMATION_MODE", "observe").lower()
VALID_MODES = ("observe", "paper", "demo", "live")
AUTO_OPEN_MODES = ("paper", "demo", "live")

SUPPORTED_EXCHANGES = ("Binance", "Bybit", "BingX", "MEXC")
ENABLED_EXCHANGES = tuple(
    name
    for name in SUPPORTED_EXCHANGES
    if name in {
        value.strip()
        for value in os.getenv("ENABLED_EXCHANGES", "Binance,Bybit").split(",")
        if value.strip()
    }
)

# --- Пороги paper / risk -----------------------------------------------------
PAPER_ENTRY_NET_PCT = float(os.getenv("PAPER_ENTRY_NET_PCT", "0.30"))
PAPER_EXIT_GROSS_PCT = float(os.getenv("PAPER_EXIT_GROSS_PCT", "0.10"))
# Мінімальний ціновий прибуток, очікуваний якщо спред дійде до виходу.
# Фандінг свідомо не враховується — він не зароблений до розрахунку.
PAPER_MIN_PRICE_CAPTURE_PCT = float(os.getenv("PAPER_MIN_PRICE_CAPTURE_PCT", "0.15"))
PAPER_TOTAL_STOP_PCT = float(os.getenv("PAPER_TOTAL_STOP_PCT", "-2.00"))
PAPER_NOTIONAL_USDT = float(os.getenv("PAPER_NOTIONAL_USDT", "1000"))
PAPER_MAX_HOLD_HOURS = float(os.getenv("PAPER_MAX_HOLD_HOURS", "24"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
MAX_SAME_ROUTE_POSITIONS = int(os.getenv("MAX_SAME_ROUTE_POSITIONS", "2"))
MIN_TURNOVER_USDT = float(os.getenv("MIN_TURNOVER_USDT", "1000000"))
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "0.15"))
MAX_PRICE_DEVIATION_PCT = float(os.getenv("MAX_PRICE_DEVIATION_PCT", "10"))
SYMBOL_COOLDOWN_SEC = int(os.getenv("SYMBOL_COOLDOWN_SEC", "1800"))

# Нижче цього номіналу перевірки стакана (заповнення/прослизання) не
# застосовуються — на $500–1000 глибина рідко є вузьким місцем.
DEPTH_GATE_MIN_NOTIONAL = float(os.getenv("DEPTH_GATE_MIN_NOTIONAL", "2000"))

# --- Модель фандінгу -------------------------------------------------------
# Дефолтний інтервал розрахунку фандінгу (год) поки не тягнемо точний per-symbol.
FUNDING_INTERVAL_HOURS = {
    "Binance": float(os.getenv("FUNDING_INTERVAL_BINANCE_H", "8")),
    "Bybit": float(os.getenv("FUNDING_INTERVAL_BYBIT_H", "8")),
    "BingX": float(os.getenv("FUNDING_INTERVAL_BINGX_H", "8")),
    "MEXC": float(os.getenv("FUNDING_INTERVAL_MEXC_H", "8")),
}
# За скільки хвилин до несприятливого нарахування блокувати новий вхід.
FUNDING_ENTRY_BUFFER_MIN = float(os.getenv("FUNDING_ENTRY_BUFFER_MIN", "10"))

# --- Моніторинг ринку -------------------------------------------------------
MARKET_STALE_SEC = max(CACHE_TTL * 3, int(os.getenv("MARKET_STALE_SEC", "30")))
MARKET_ALERT_FAILURES = max(1, int(os.getenv("MARKET_ALERT_FAILURES", "3")))
TELEGRAM_REPORT_INTERVAL_SEC = max(
    3600, int(os.getenv("TELEGRAM_REPORT_INTERVAL_SEC", "86400"))
)

# --- Критерії готовності до demo -------------------------------------------
READINESS_MIN_CLOSED_TRADES = max(1, int(os.getenv("READINESS_MIN_CLOSED_TRADES", "50")))
READINESS_MIN_DAYS = max(1, int(os.getenv("READINESS_MIN_DAYS", "7")))
READINESS_MAX_STOP_RATE_PCT = float(os.getenv("READINESS_MAX_STOP_RATE_PCT", "20"))

# --- Виконання (demo/live, Milestone B+) ----------------------------------
EXECUTION_ENABLED_EXCHANGES = tuple(
    name
    for name in SUPPORTED_EXCHANGES
    if name in {
        value.strip()
        for value in os.getenv(
            "EXECUTION_ENABLED_EXCHANGES", "Binance,Bybit,BingX"
        ).split(",")
        if value.strip()
    }
)
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "10"))
LIVE_NOTIONAL_USDT = float(os.getenv("LIVE_NOTIONAL_USDT", "50"))
# demo -> завжди sandbox; live -> завжди реальний. Змінна керує лише demo/тестами.
EXCHANGE_SANDBOX = os.getenv("EXCHANGE_SANDBOX", "true").strip().lower() != "false"


def use_sandbox() -> bool:
    """True для demo (та коли явно ввімкнено); False для live."""
    if AUTOMATION_MODE == "live":
        return False
    if AUTOMATION_MODE == "demo":
        return True
    return EXCHANGE_SANDBOX


# --- Execution engine (Milestone C) --------------------------------------
SIGNAL_TTL_SEC = int(os.getenv("SIGNAL_TTL_SEC", "15"))
ORDER_TIMEOUT_SEC = float(os.getenv("ORDER_TIMEOUT_SEC", "10"))
# Допустиме відхилення заповнення ноги у відсотках від цільового обсягу.
FILL_TOLERANCE_PCT = float(os.getenv("FILL_TOLERANCE_PCT", "2"))
# Скільки вільної маржі тримати понад початкову (у відсотках від init margin).
MARGIN_BUFFER_PCT = float(os.getenv("MARGIN_BUFFER_PCT", "25"))
MARGIN_WARN = float(os.getenv("MARGIN_WARN", "0.55"))
MARGIN_CRITICAL = float(os.getenv("MARGIN_CRITICAL", "0.80"))
MARGIN_LIQ_DISTANCE_CRIT_PCT = float(os.getenv("MARGIN_LIQ_DISTANCE_CRIT_PCT", "5"))
MARGIN_MONITOR_INTERVAL_SEC = int(os.getenv("MARGIN_MONITOR_INTERVAL_SEC", "10"))
LIVE_MAX_NOTIONAL_PER_POS = float(os.getenv("LIVE_MAX_NOTIONAL_PER_POS", "500"))
LIVE_MAX_TOTAL_NOTIONAL = float(os.getenv("LIVE_MAX_TOTAL_NOTIONAL", "1500"))
LIVE_MAX_DAILY_LOSS_USDT = float(os.getenv("LIVE_MAX_DAILY_LOSS_USDT", "50"))


# Bearer-токен для мутуючих /api-маршрутів. Порожньо -> без авторизації
# (панель на 127.0.0.1). Задається при винесенні за reverse proxy.
API_BEARER_TOKEN = read_secret("API_BEARER_TOKEN")

# --- Telegram -------------------------------------------------------------
TELEGRAM_BOT_TOKEN = read_secret("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_ALLOWED_USER_IDS = {
    value.strip()
    for value in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if value.strip()
}

# Комісія одного ордера у відсотках (taker). Використовується автоматикою.
AUTOMATION_FEE_PCT = number(os.getenv("AUTOMATION_FEE_PCT", "0.055"), 0.055)


def is_auto_open_mode() -> bool:
    """True, якщо режим передбачає автоматичне відкриття позицій."""
    return AUTOMATION_MODE in AUTO_OPEN_MODES
