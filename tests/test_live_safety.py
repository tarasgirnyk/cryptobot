import json
import sqlite3
import time
import unittest
from unittest.mock import Mock, patch

from cryptobot import config, runtime, storage
from cryptobot.execution import engine, reconcile, state
from cryptobot.exchanges.base import CcxtExchangeClient, ExchangeOpError, Position
from cryptobot.risk import RiskEngine
from test_execution import FakeClient, make_plan, _Base
from test_exchanges import FakeCcxt


class LiveSafetyTests(_Base):
    def clients(self):
        return {n: FakeClient(n) for n in ("Binance", "Bybit")}

    def test_timeout_retains_intent_and_stops(self):
        clients = self.clients()
        clients["Bybit"]._fill = "error"
        pos = engine.open_hedge(make_plan(), clients, RiskEngine())
        self.assertEqual(pos["state"], state.RECOVERY)
        self.assertIn(pos["id"], runtime.live_positions)
        self.assertTrue(runtime.automation_state["killSwitch"])
        with self.assertRaises(engine.ExecutionError):
            engine.open_hedge(make_plan(), clients, RiskEngine())
        self.assertEqual(len(clients["Binance"].orders), 1)

    def test_residual_prevents_false_closed(self):
        clients = self.clients()
        pos = engine.open_hedge(make_plan(), clients, RiskEngine())
        clients["Binance"]._position = Position("TESTUSDT", .01, 100, 0, 100, 0, 1, 0)
        result = engine.close_hedge(pos, "test", clients)
        self.assertEqual(result["state"], state.RECOVERY)
        self.assertIn(pos["id"], runtime.live_positions)
        self.assertEqual(len(runtime.live_closed), 0)

    def test_failed_recovery_stays_tracked(self):
        clients = self.clients()
        clients["Bybit"]._fill = "zero"
        clients["Binance"].position = Mock(side_effect=ExchangeOpError("read failed"))
        pos = engine.open_hedge(make_plan(), clients, RiskEngine())
        self.assertIn(pos["id"], runtime.live_positions)
        self.assertTrue(runtime.automation_state["killSwitch"])

    def test_live_allowlist_blocks_before_orders(self):
        with patch.object(config, "AUTOMATION_MODE", "live"):
            with self.assertRaisesRegex(engine.ExecutionError, "allowlisted"):
                engine.open_hedge(make_plan(), self.clients(), RiskEngine())

    def test_preflight_expiry_blocks_before_orders(self):
        clients = self.clients()
        plan = make_plan()
        with patch.object(type(plan), "expired", side_effect=[False, True]):
            with self.assertRaisesRegex(engine.ExecutionError, "expired"):
                engine.open_hedge(plan, clients, RiskEngine())
        self.assertFalse(clients["Binance"].orders)

    def test_reconcile_scan_error_fails_closed(self):
        c = FakeClient("Binance")
        c.ccxt = Mock()
        c.ccxt.fetch_positions.side_effect = RuntimeError("unavailable")
        self.assertFalse(reconcile.startup_reconcile({"Binance": c}))
        self.assertTrue(runtime.automation_state["killSwitch"])

    def test_reconcile_wrong_direction_rejected(self):
        pos = state.new_position(make_plan(), .5)
        pos.update(state=state.HEDGED, hedgedBaseQty=.5)
        state.store_put(pos)
        clients = self.clients()
        for c in clients.values():
            c._position = Position("TESTUSDT", .5, 100, 0, 100, 0, 50, 0)
        self.assertFalse(reconcile.startup_reconcile(clients))

    def test_outstanding_orders_block_startup(self):
        c = FakeClient("Binance")
        c.ccxt = Mock(options={})
        c.ccxt.fetch_positions.return_value = []
        c.ccxt.fetch_open_orders.return_value = [{"id": "pending"}]
        self.assertFalse(reconcile.startup_reconcile({"Binance": c}))

    def test_daily_loss_survives_new_engine(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE live_closed (closed_at INTEGER,payload TEXT)")
        now = int(time.time() * 1000)
        db.execute("INSERT INTO live_closed VALUES (?,?)", (now,json.dumps({"realizedPnl": -99})))
        with patch.object(storage, "db_connection", db):
            self.assertTrue(RiskEngine().daily_loss_tripped(now))
        db.close()

    def test_common_quantity_is_rounded_for_both_legs(self):
        import math
        clients = self.clients()
        clients["Binance"].amount_for_notional = lambda s,n,p: math.floor(n/p*10+1e-9)/10
        clients["Bybit"].amount_for_notional = lambda s,n,p: math.floor(n/p*4+1e-9)/4
        # .5 and .25 grids cannot share a positive quantity within this budget.
        with self.assertRaises(engine.ExecutionError):
            engine.open_hedge(make_plan(), clients, RiskEngine())
        self.assertFalse(clients["Binance"].orders)

    def test_depth_uses_requested_route_despite_scanner_changes(self):
        from cryptobot.depth import depth_analysis
        candidate = dict(longExchange="Binance",shortExchange="BingX",grossSpreadPct=1)
        with patch("cryptobot.depth.load_depth", return_value={"asks":[[100,100]],"bids":[[101,100]]}) as book:
            result = depth_analysis("XRPUSDT",20,opportunity=candidate)
        self.assertEqual([c.args[0] for c in book.call_args_list],["Binance","BingX"])
        self.assertEqual(result["shortExchange"],"BingX")


class AdapterSafetyTests(unittest.TestCase):
    def test_bingx_hedge_mode_sets_both_sides_and_close_parameters(self):
        ccxt = FakeCcxt(hedged=True)
        c = CcxtExchangeClient("BingX", ccxt)
        c.set_leverage("BTCUSDT", 10)
        self.assertEqual([x[3]["side"] for x in ccxt.calls], ["LONG", "SHORT"])
        c.market_order("BTCUSDT", "sell", .1, "close", True)
        self.assertTrue(ccxt.calls[-1][-1]["hedged"])
        self.assertTrue(ccxt.calls[-1][-1]["reduceOnly"])

    def test_bybit_status_acknowledged(self):
        ccxt = FakeCcxt()
        CcxtExchangeClient("Bybit", ccxt).fetch_order_result("BTCUSDT", "test")
        self.assertEqual(ccxt.calls[-1][-1], {"acknowledged": True})

    def test_below_minimum_is_rejected(self):
        ccxt = FakeCcxt()
        ccxt.markets["BTC/USDT:USDT"] = dict(active=True, swap=True, linear=True,
            contractSize=1, limits={"cost": {"min":100}})
        with self.assertRaises(ExchangeOpError):
            CcxtExchangeClient("Binance", ccxt).validate_open("BTCUSDT", .1, 100)
