"""Read-only production checks; never sets modes/leverage or creates orders."""
import json
from cryptobot import config
from cryptobot.exchanges import AccountPool, build_client

pool = AccountPool.from_env()
for name in config.EXECUTION_ENABLED_EXCHANGES:
    report = {"exchange": name}
    try:
        client = build_client(name, pool.active(name), sandbox=False)
        client.load()
        client.ccxt.options["warnOnFetchOpenOrdersWithoutSymbol"] = False
        report["balance"] = client.usdt_balance()
        report["positions"] = [
            {k: p.get(k) for k in ("symbol", "side", "contracts", "hedged")}
            for p in client.ccxt.fetch_positions()
            if abs(float(p.get("contracts") or 0)) > 0
        ]
        report["openOrders"] = [
            {k: o.get(k) for k in ("symbol", "side", "status")}
            for o in client.ccxt.fetch_open_orders()
        ]
        if client.ccxt.has.get("fetchPositionMode"):
            report["positionMode"] = client.ccxt.fetch_position_mode()
        else:
            report["positionModes"] = list({p.get("hedged") for p in client.ccxt.fetch_positions()})
        report["markets"] = {}
        for symbol in ("XRPUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT", "LTCUSDT", "SOLUSDT"):
            if client.has_market(symbol):
                m = client.market(symbol)
                report["markets"][symbol] = {k: m.get(k) for k in ("active", "swap", "linear", "contractSize", "precision", "limits")}
        report["ok"] = True
    except Exception as exc:
        report["ok"] = False
        report["error"] = type(exc).__name__ + ": " + str(exc)[:250]
    print(json.dumps(report), flush=True)
