import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptobot import config, depth, paper, risk, runtime, scanner, storage


def candidate(**overrides):
    row = {
        "symbol": "TESTUSDT",
        "longExchange": "Binance",
        "shortExchange": "Bybit",
        "grossSpreadPct": 0.70,
        "roundTripFeesPct": 0.22,
        "fundingEffectPct": 0.0,
        "netSpreadPct": 0.48,
        "minTurnover24h": 10_000_000,
        "warning": "",
    }
    row.update(overrides)
    return row


class RiskFilterTests(unittest.TestCase):
    def setUp(self):
        runtime.symbol_cooldowns.clear()

    def test_levels_vwap_respects_quote_notional(self):
        result = depth.levels_vwap([[100, 5], [110, 10]], 800)
        self.assertAlmostEqual(result["filledQuote"], 800)
        self.assertAlmostEqual(result["fillPct"], 100)
        self.assertAlmostEqual(result["vwap"], 800 / (5 + 300 / 110))

    def test_funding_cannot_rescue_unprofitable_price_capture(self):
        row = candidate(grossSpreadPct=0.20, netSpreadPct=1.10)
        self.assertEqual(risk.candidate_rejection_reason(row), "price_capture")

    def test_depth_is_rechecked_after_slippage(self):
        row = candidate(netSpreadPct=0.70)
        weak_depth = {
            "longFillPct": 100,
            "shortFillPct": 100,
            "slippagePct": 0.14,
            "executableSpreadPct": 0.46,
        }
        good_depth = {**weak_depth, "slippagePct": 0.10, "executableSpreadPct": 0.50}
        self.assertEqual(
            risk.candidate_rejection_reason(row, depth=weak_depth),
            "executable_price_capture",
        )
        self.assertIsNone(risk.candidate_rejection_reason(row, depth=good_depth))

    def test_small_order_keeps_depth_and_execution_gates(self):
        row = candidate(netSpreadPct=0.70)
        thin_depth = {
            "longFillPct": 40,
            "shortFillPct": 60,
            "slippagePct": 0.9,
            "executableSpreadPct": -0.2,
        }
        self.assertEqual(
            risk.candidate_rejection_reason(row, depth=thin_depth, notional=5000),
            "depth_fill",
        )
        self.assertEqual(
            risk.candidate_rejection_reason(row, depth=thin_depth, notional=500),
            "depth_fill",
        )

        filled_but_negative = {
            **thin_depth,
            "longFillPct": 100,
            "shortFillPct": 100,
        }
        self.assertEqual(
            risk.candidate_rejection_reason(
                row, depth=filled_but_negative, notional=20
            ),
            "slippage",
        )

    def test_same_route_concentration_is_limited(self):
        opened = [
            {"symbol": "ONEUSDT", "longExchange": "Binance", "shortExchange": "Bybit"},
            {"symbol": "TWOUSDT", "longExchange": "Binance", "shortExchange": "Bybit"},
        ]
        self.assertEqual(
            risk.candidate_rejection_reason(candidate(), opened), "route_limit"
        )


class FundingModelTests(unittest.TestCase):
    def test_settlements_counted_across_hold_window(self):
        now = 1_000_000_000_000
        long_leg = {"exchange": "Binance", "funding": 0.0001, "nextFunding": now + 3_600_000}
        short_leg = {"exchange": "Bybit", "funding": -0.0002, "nextFunding": now + 3_600_000}
        # 8h інтервал, 24h утримання -> по 3 нарахування на ногу.
        effect = scanner.funding.expected_effect(long_leg, short_leg, 24, now_ms=now)
        self.assertAlmostEqual(effect, (-0.0001 * 3 + -0.0002 * 3) * 100)

    def test_imminent_unfavourable_funding_blocks_entry(self):
        now = 1_000_000_000_000
        long_leg = {"exchange": "Binance", "funding": 0.01, "nextFunding": now + 60_000}
        short_leg = {"exchange": "Bybit", "funding": 0.0, "nextFunding": 0}
        self.assertTrue(scanner.funding.blocks_entry(long_leg, short_leg, now))
        # той самий фандінг, але на користь (ми в шорті) -> не блокує
        short_leg2 = {"exchange": "Bybit", "funding": 0.01, "nextFunding": now + 60_000}
        long_leg2 = {"exchange": "Binance", "funding": 0.0, "nextFunding": 0}
        self.assertFalse(scanner.funding.blocks_entry(long_leg2, short_leg2, now))


class MetricsTests(unittest.TestCase):
    def test_readiness_requires_duration_sample_profit_and_health(self):
        now_ms = int(time.time() * 1000)
        old_payload = scanner.cache["payload"]
        scanner.cache["payload"] = {
            "generatedAt": now_ms,
            "exchanges": ["Binance", "Bybit"],
            "errors": [],
        }
        old_enabled = config.ENABLED_EXCHANGES
        config.ENABLED_EXCHANGES = ("Binance", "Bybit")
        try:
            closed = []
            for index in range(50):
                pnl = 1.0 if index < 40 else -0.5
                closed.append(
                    {
                        "id": str(index),
                        "openedAt": now_ms - 8 * 86_400_000 + index * 1000,
                        "closedAt": now_ms - 7 * 86_400_000 + index * 1000,
                        "unrealizedPnl": pnl,
                        "closeReason": "total_stop" if index >= 45 else "spread_converged",
                    }
                )
            metrics = paper.paper_metrics({"open": [], "closed": closed}, now_ms)
            self.assertTrue(metrics["readyForDemo"])
            self.assertEqual(metrics["closedTrades"], 50)
            self.assertAlmostEqual(metrics["realizedPnl"], 35.0)
            self.assertAlmostEqual(metrics["stopRatePct"], 10.0)
        finally:
            scanner.cache["payload"] = old_payload
            config.ENABLED_EXCHANGES = old_enabled


class MultiExchangeTests(unittest.TestCase):
    def test_scanner_selects_best_route_across_enabled_exchanges(self):
        old_enabled = config.ENABLED_EXCHANGES
        loaders = {
            "Binance": {
                "BTCUSDT": {"exchange": "Binance", "bid": 99, "ask": 100, "turnover": 10_000_000, "funding": 0}
            },
            "Bybit": {
                "BTCUSDT": {"exchange": "Bybit", "bid": 100.5, "ask": 101, "turnover": 10_000_000, "funding": 0}
            },
            "BingX": {
                "BTCUSDT": {"exchange": "BingX", "bid": 98.9, "ask": 99, "turnover": 10_000_000, "funding": 0}
            },
            "MEXC": {
                "BTCUSDT": {"exchange": "MEXC", "bid": 100, "ask": 100.2, "turnover": 10_000_000, "funding": 0}
            },
        }
        config.ENABLED_EXCHANGES = tuple(loaders)
        try:
            with (
                mock.patch.object(scanner, "load_binance", return_value=loaders["Binance"]),
                mock.patch.object(scanner, "load_bybit", return_value=loaders["Bybit"]),
                mock.patch.object(scanner, "load_bingx", return_value=loaders["BingX"]),
                mock.patch.object(scanner, "load_mexc", return_value=loaders["MEXC"]),
            ):
                payload = scanner.build_opportunities(0.05)
            best = payload["opportunities"][0]
            self.assertEqual(best["longExchange"], "BingX")
            self.assertEqual(best["shortExchange"], "Bybit")
            self.assertEqual(set(payload["exchanges"]), set(loaders))
        finally:
            config.ENABLED_EXCHANGES = old_enabled

    def test_mexc_depth_contracts_are_converted_to_base_quantity(self):
        old_quotes = dict(scanner.market_quotes)
        scanner.market_quotes.clear()
        scanner.market_quotes["MEXC"] = {
            "BTCUSDT": {
                "nativeSymbol": "BTC_USDT",
                "contractSize": 0.0001,
            }
        }
        try:
            with mock.patch.object(
                depth,
                "fetch_json",
                return_value={"success": True, "data": {"bids": [[100, 2500]], "asks": [[101, 3000]]}},
            ):
                book = depth.load_depth("MEXC", "BTCUSDT")
            self.assertAlmostEqual(book["bids"][0][1], 0.25)
            self.assertAlmostEqual(book["asks"][0][1], 0.30)
        finally:
            scanner.market_quotes.clear()
            scanner.market_quotes.update(old_quotes)

    def test_open_position_is_marked_on_its_original_route(self):
        old_quotes = dict(scanner.market_quotes)
        scanner.market_quotes.clear()
        scanner.market_quotes.update(
            {
                "Binance": {"TESTUSDT": {"bid": 100, "ask": 100.1}},
                "Bybit": {"TESTUSDT": {"bid": 100.1, "ask": 100.2}},
                "BingX": {"TESTUSDT": {"bid": 110, "ask": 111}},
            }
        )
        try:
            marked = paper.mark_paper(
                {
                    "id": "fixed-route",
                    "symbol": "TESTUSDT",
                    "notional": 1000,
                    "longExchange": "Binance",
                    "shortExchange": "Bybit",
                    "entryGrossPct": 1.0,
                    "entryExecutableSpreadPct": 1.0,
                    "roundTripFeesPct": 0.2,
                }
            )
            self.assertAlmostEqual(marked["currentGrossPct"], 0.2)
            self.assertAlmostEqual(marked["unrealizedPnl"], 6.0)
        finally:
            scanner.market_quotes.clear()
            scanner.market_quotes.update(old_quotes)


class StorageTests(unittest.TestCase):
    def test_closed_trades_and_stop_state_survive_restart(self):
        old_data_dir = config.DATA_DIR
        old_connection = storage.db_connection
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config.DATA_DIR = Path(temp_dir)
                storage.db_connection = None
                runtime.paper_positions.clear()
                runtime.paper_closed.clear()
                storage.init_storage()
                closed = {
                    "id": "persisted",
                    "openedAt": 1,
                    "closedAt": 2,
                    "unrealizedPnl": 3.5,
                    "status": "closed",
                    "closeReason": "spread_converged",
                }
                storage.persist_closed_paper(closed)
                storage.set_control_state(paused=True, kill_switch=True)
                storage.db_connection.close()
                storage.db_connection = None
                runtime.paper_closed.clear()
                runtime.automation_state["paused"] = False
                runtime.automation_state["killSwitch"] = False
                storage.init_storage()
                self.assertEqual(runtime.paper_closed[0]["id"], "persisted")
                self.assertTrue(runtime.automation_state["paused"])
                self.assertTrue(runtime.automation_state["killSwitch"])
                storage.db_connection.close()
                storage.db_connection = None
        finally:
            config.DATA_DIR = old_data_dir
            storage.db_connection = old_connection
            runtime.paper_positions.clear()
            runtime.paper_closed.clear()

    def test_live_positions_survive_restart(self):
        old_data_dir = config.DATA_DIR
        old_connection = storage.db_connection
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config.DATA_DIR = Path(temp_dir)
                storage.db_connection = None
                runtime.live_positions.clear()
                runtime.live_closed.clear()
                storage.init_storage()
                storage.persist_live({"id": "live-open", "symbol": "AAAUSDT", "state": "HEDGED"})
                storage.persist_closed_live(
                    {"id": "live-done", "symbol": "BBBUSDT", "state": "CLOSED",
                     "closedAt": 123, "realizedPnl": 1.5}
                )
                storage.db_connection.close()
                storage.db_connection = None
                runtime.live_positions.clear()
                runtime.live_closed.clear()
                storage.init_storage()
                self.assertIn("live-open", runtime.live_positions)
                self.assertEqual(runtime.live_closed[0]["id"], "live-done")
                storage.db_connection.close()
                storage.db_connection = None
        finally:
            config.DATA_DIR = old_data_dir
            storage.db_connection = old_connection
            runtime.live_positions.clear()
            runtime.live_closed.clear()


if __name__ == "__main__":
    unittest.main()
