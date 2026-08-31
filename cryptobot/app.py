"""HTTP-сервер, статика панелі та точка входу ``main()``."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from cryptobot import config, runtime
from cryptobot.automation import automation_loop
from cryptobot.depth import depth_analysis
from cryptobot.paper import close_paper, market_health, open_paper, paper_metrics, paper_snapshot
from cryptobot.risk import candidate_rejection_reason
from cryptobot.scanner import history_lock, market_payload, opportunity_for, spread_history
from cryptobot.storage import init_storage
from cryptobot.telegram import telegram_poll_loop
from cryptobot.util import number


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        clean = urllib.parse.urlparse(path).path.lstrip("/") or "index.html"
        return str(config.ROOT / clean)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/health":
            payload = _last_payload()
            market = market_health(payload)
            return self.send_json(
                {
                    "status": "ok" if market["status"] == "ok" else "degraded",
                    "marketData": market["status"],
                    "marketAgeSec": market["ageSec"],
                    "marketErrors": market["errors"],
                    "exchanges": market["exchanges"],
                    "generatedAt": payload.get("generatedAt"),
                    "automationMode": config.AUTOMATION_MODE,
                    "paused": runtime.automation_state["paused"],
                    "killSwitch": runtime.automation_state["killSwitch"],
                    "startupReconciled": runtime.automation_state["startupReconciled"],
                    "lastAutomationRunAt": runtime.automation_state["lastRunAt"],
                    "lastMarketSuccessAt": runtime.automation_state["lastMarketSuccessAt"],
                    "automationError": runtime.automation_state["lastError"],
                    "telegramConnected": bool(
                        config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID
                    ),
                    "telegramError": runtime.automation_state["telegramError"],
                    "execution": _execution_health(),
                }
            )
        if parsed.path in {"/api/metrics", "/api/readiness"}:
            return self.send_json(paper_metrics())
        if parsed.path == "/api/live":
            return self.send_json(_live_snapshot())
        if parsed.path == "/api/history":
            symbol = params.get("symbol", [""])[0].upper()
            with history_lock:
                return self.send_json({"symbol": symbol, "points": list(spread_history[symbol])})
        if parsed.path == "/api/depth":
            symbol = params.get("symbol", [""])[0].upper()
            notional = max(10.0, min(number(params.get("notional", ["1000"])[0]), 100000.0))
            try:
                return self.send_json(depth_analysis(symbol, notional))
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 502)
        if parsed.path == "/api/paper":
            return self.send_json(paper_snapshot())
        if parsed.path != "/api/opportunities":
            return super().do_GET()

        fee_pct = max(0.0, min(number(params.get("fee", ["0.055"])[0]), 1.0))
        self.send_json(market_payload(fee_pct))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if config.API_BEARER_TOKEN:
            if self.headers.get("Authorization", "") != f"Bearer {config.API_BEARER_TOKEN}":
                return self.send_json({"error": "unauthorized"}, 401)
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "Некоректний JSON"}, 400)
        if parsed.path == "/api/paper/open":
            symbol = str(data.get("symbol", "")).upper()
            notional = max(10.0, min(number(data.get("notional"), 1000), 100000.0))
            try:
                opened_now = paper_snapshot()["open"]
                if len(opened_now) >= config.MAX_OPEN_POSITIONS:
                    raise ValueError("Досягнуто ліміт відкритих paper-позицій")
                candidate = opportunity_for(symbol)
                if not candidate:
                    raise ValueError("Символ відсутній у поточному скані")
                rejection = candidate_rejection_reason(candidate, opened_now)
                if rejection:
                    raise ValueError(f"Risk-фільтр: {rejection}")
                depth = depth_analysis(symbol, notional)
                rejection = candidate_rejection_reason(
                    candidate, opened_now, depth=depth, notional=notional
                )
                if rejection:
                    raise ValueError(f"Risk-фільтр: {rejection}")
                return self.send_json(open_paper(symbol, notional, depth), 201)
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 409)
        if parsed.path == "/api/paper/close":
            position_id = str(data.get("id", ""))
            try:
                return self.send_json(close_paper(position_id))
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 404)
        return self.send_json({"error": "Маршрут не знайдено"}, 404)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def _last_payload():
    from cryptobot.scanner import cache

    return cache.get("payload") or {}


def _live_snapshot():
    if config.AUTOMATION_MODE not in ("demo", "live"):
        return {"open": [], "closed": [], "mode": config.AUTOMATION_MODE}
    from cryptobot.execution import state

    return {
        "open": [state.mark_live(row) for row in state.open_live_positions()],
        "closed": list(runtime.live_closed),
        "mode": config.AUTOMATION_MODE,
    }


def _execution_health():
    if config.AUTOMATION_MODE not in ("demo", "live"):
        return {"mode": config.AUTOMATION_MODE, "active": False}
    try:
        from cryptobot.execution import clients as registry

        built = registry.get_clients()
        return {
            "mode": config.AUTOMATION_MODE,
            "active": registry.enabled(),
            "exchanges": sorted(built),
            "sandbox": config.use_sandbox(),
            "openPositions": len(_live_snapshot()["open"]),
        }
    except Exception as exc:  # noqa: BLE001
        return {"mode": config.AUTOMATION_MODE, "active": False, "error": str(exc)[:200]}


def main():
    if config.AUTOMATION_MODE not in config.VALID_MODES:
        print(f"[warn] невідомий AUTOMATION_MODE={config.AUTOMATION_MODE!r}, працюю як observe")
    init_storage()
    if config.AUTOMATION_MODE in ("demo", "live"):
        from cryptobot.execution import driver

        driver.startup()
    threading.Thread(target=automation_loop, name="automation", daemon=True).start()
    threading.Thread(target=telegram_poll_loop, name="telegram", daemon=True).start()
    print(f"CryptoBOT MVP: http://{config.HOST}:{config.PORT}")
    ThreadingHTTPServer((config.HOST, config.PORT), Handler).serve_forever()
