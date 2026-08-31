# CryptoBOT RUNBOOK

Операційний чекліст переходу `paper → demo → live`. Кожен етап має критерії
виходу; не переходь далі, доки вони не виконані.

Скорочення: «двигун» = `cryptobot/execution/`, «звірка» = `startup_reconcile`.

---

## 0. Передумови

- Python 3.11+, `pip install -r requirements.txt` (ставить `ccxt`).
- `.env` з `.env.example`. Ніколи не комітити `.env`.
- Капітал розкладений вручну по біржах — авто-переказу немає.
- API-ключі **без права виводу коштів**. Окремі ключі для demo і для live.
- Мінімум **2 біржі** з ключами (`BINANCE_API_KEYS`, `BYBIT_API_KEYS`,
  `BINGX_API_KEYS`, формат `key:secret`). MEXC — заглушка, автоматом не торгує.

Швидка перевірка збірки:

```bash
python -m unittest discover -s tests -v
python server.py   # → http://127.0.0.1:8765 , /api/health = ok
```

---

## 1. Paper (базовий збір статистики)

```dotenv
AUTOMATION_MODE=paper
```

Крутиться до `/report` (Telegram) або `/api/readiness`:

- ≥ `READINESS_MIN_CLOSED_TRADES` закритих угод;
- ≥ `READINESS_MIN_DAYS` днів спостереження;
- закритий PNL > 0;
- stop rate ≤ `READINESS_MAX_STOP_RATE_PCT`;
- ринкові дані обох бірж справні.

Тут же підганяються пороги (`PAPER_ENTRY_NET_PCT`, `PAPER_MAX_HOLD_HOURS`,
`DEPTH_GATE_MIN_NOTIONAL`, `FUNDING_*`).

**Вихід:** критерії readiness виконані щонайменше 1 тиждень поспіль.

---

## 2. Demo (реальні API, testnet-кошти)

### 2.1 Ключі testnet

- Binance USDⓈ-M Futures Testnet: <https://testnet.binancefuture.com> → API key.
- Bybit Testnet: <https://testnet.bybit.com> → API key (потрібен навіть для
  `load_markets` — ccxt б'є приватний endpoint).
- BingX: демо/VST-акаунт, якщо доступний; інакше BingX у demo пропускається.

```dotenv
AUTOMATION_MODE=demo
EXCHANGE_SANDBOX=true
EXECUTION_ENABLED_EXCHANGES=Binance,Bybit
BINANCE_API_KEYS=key:secret
BYBIT_API_KEYS=key:secret
LIVE_NOTIONAL_USDT=50
MAX_OPEN_POSITIONS=1
DEFAULT_LEVERAGE=10
```

### 2.2 Запуск і що дивитись

```bash
python server.py
```

У логу має бути `[ok] executor готовий: Binance, Bybit (sandbox=True)`.
Якщо `<2` бірж — `[warn] ... поводиться як paper`: перевір ключі.

- `/api/health` → блок `execution.active = true`, `startupReconciled = true`.
- `/api/live` → `open` / `closed`.
- Telegram: `🟢 LIVE HEDGED`, `🔵 LIVE CLOSE`, `⚠️ LIVE RECOVERY`.
- SQLite `audit_events` kinds: `live_open`, `live_open_fills`, `live_hedged`,
  `live_close`, `live_recovery`, `reconcile_ok`, `margin_warn`.

### 2.3 Ручні перевірки

1. Дочекатись 3–5 повних циклів `live_open → live_hedged → live_close`.
2. Звірити `realizedPnl` у `live_closed` з фактичними філами на testnet.
3. `/positions` у Telegram показує live-рядки; кнопка `Закрити` працює
   (callback `lclose:<id>`).
4. `/stop` → всі live-позиції закриваються протягом одного тіку; `/resetstop`
   знімає STOP.
5. Штучно занизити `MARGIN_CRITICAL` (напр. `0.01`) → позиція має закритись із
   `reason=margin`. Повернути значення назад.

**Вихід:** ≥ 3 днів demo без незакритих односторонніх виконань; recovery,
margin-close, `/stop` перевірені.

---

## 3. Chaos-тест (обов'язково перед live)

Ціль: переконатись, що краш під час відкриття не лишає orphan-позицію
непоміченою.

1. У demo дочекатись сигналу і під час `open_hedge` **вбити процес**:
   `Stop-Process -Id <pid> -Force` (Win) / `kill -9 <pid>`.
2. Перезапустити `python server.py`.
3. Очікувано:
   - `/api/health` → `startupReconciled = false`, `killSwitch = true`;
   - Telegram: `🛑 STARTUP RECONCILE: розбіжність стану` зі списком;
   - `audit_events` має `reconcile_mismatch`.
4. Вручну звести позиції на біржах до нуля (або лишити хедж навмисно), потім
   `/resetstop`. Переконатись, що після цього `startup_reconcile` дає `reconcile_ok`.

Автотести цього сценарію: `tests/test_execution.py::ReconcileTests` та
`test_incomplete_state_after_crash_is_flagged`.

---

## 4. Live micro

```dotenv
AUTOMATION_MODE=live
# EXCHANGE_SANDBOX ігнорується — live завжди реальний endpoint
BINANCE_API_KEYS=<real key:secret, без виводу>
BYBIT_API_KEYS=<real key:secret, без виводу>
LIVE_NOTIONAL_USDT=20
MAX_OPEN_POSITIONS=1
LIVE_MAX_NOTIONAL_PER_POS=50
LIVE_MAX_TOTAL_NOTIONAL=50
LIVE_MAX_DAILY_LOSS_USDT=15
API_BEARER_TOKEN=<якщо панель за проксі>
```

- Перший запуск — під наглядом, поруч термінал і Telegram.
- Перевірити `execution.sandbox = false` у `/api/health`.
- Кілька днів, кожну угоду звіряти вручну з біржами.
- Будь-яка `live_recovery`, що не змогла закрити ногу → зупинитись, розібратись.

**Вихід:** ≥ 20 закритих live-угод, жодного необробленого односторонього
виконання, реалізований PNL зійшовся з біржами.

---

## 5. Live scale

Піднімати поступово: `LIVE_NOTIONAL_USDT` 20 → 50 → 100 → 300, паралельно
`LIVE_MAX_*` і `MAX_OPEN_POSITIONS`. Після кожного кроку — тиждень спостереження.
Плаваючі плечі за правилами — окремий етап, не змішувати з масштабуванням.

---

## Аварійні процедури

| Ситуація | Дія |
|---|---|
| Хочу все зупинити | Telegram `/stop` — блокує входи, закриває live-позиції, STOP зберігається після рестарту |
| Після рестарту `startupReconciled=false` | НЕ робити `/resetstop` наосліп; звірити позиції на біржах вручну, довести до узгодженого стану, тоді `/resetstop` |
| `⚠️ MARGIN WARN` | стежити; якщо росте — `/close_<id>` або `/stop` |
| Бан акаунта (`AccountSuspended`/auth) | двигун позначає ключ, але ротація ще не автоматична — підкласти інший ключ у `.env` і перезапустити |
| Ринкові дані впали | входи блокуються автоматично; виходи по відкритих позиціях працюють |
| Зникло `execution.active` | бракує ключів або `load_markets` впав; дивись `audit_events` kind `executor_exchange_failed` |

## Обмеження цієї версії

- Ордери лише `market`, вихід теж `market`.
- Немає авто-переказу маржі між біржами — тільки алерт «поповни X».
- Ротація ключів при бані — інтерфейс є, автоперемикання немає.
- Фандінг-інтервал береться дефолтний per-exchange, не per-symbol.
- MEXC futures автоматом не торгується (немає API).
