import time
import unittest

from cryptobot import config, runtime
from cryptobot.exchanges.base import ExchangeOpError, OrderResult, Position
from cryptobot.execution import engine, margin, reconcile, state
from cryptobot.execution.plan import TradePlan
from cryptobot.risk import HardLimitHit, RiskEngine


def make_candidate(**over):
    row = {
        "symbol": "TESTUSDT",
        "longExchange": "Binance",
        "shortExchange": "Bybit",
        "grossSpreadPct": 0.9,
        "roundTripFeesPct": 0.22,
        "netSpreadPct": 0.6,
        "longAsk": 100.0,
        "shortBid": 100.9,
    }
    row.update(over)
    return row


def make_plan(**over):
    depth = {"slippagePct": 0.05, "executableSpreadPct": 0.8}
    kw = dict(notional=50, leverage=10, ttl_sec=15)
    kw.update(over)
    return TradePlan.from_candidate(make_candidate(), depth, **kw)


class FakeClient:
    def __init__(self, name, *, fill="full", price=100.0, free=1e9, position=None, market=True):
        self.name = name
        self._fill = fill  # "full" | "zero" | "error" | float ratio
        self.price = price
        self._free = free
        self._position = position
        self._market = market
        self.orders = []

    def has_market(self, symbol):
        return self._market

    def set_leverage(self, symbol, leverage):
        pass

    def amount_for_notional(self, symbol, notional, price):
        return round(notional / (price or self.price), 6)

    def free_collateral(self):
        return self._free

    def market_order(self, symbol, side, qty, client_id, reduce_only=False):
        self.orders.append(dict(side=side, qty=qty, client_id=client_id, reduce_only=reduce_only))
        if self._fill == "error":
            raise ExchangeOpError("boom")
        if self._fill == "zero":
            filled = 0.0
        elif isinstance(self._fill, (int, float)):
            filled = qty * self._fill
        else:
            filled = qty
        return OrderResult(
            id="O" + client_id,
            client_id=client_id,
            symbol=symbol,
            side=side,
            status="closed" if filled > 0 else "rejected",
            filled_base=filled,
            avg_price=self.price,
            raw={},
        )

    def fetch_order_result(self, symbol, order_id):
        return OrderResult(order_id, None, symbol, "", "closed", 0.0, self.price, {})

    def position(self, symbol):
        return self._position


class _Base(unittest.TestCase):
    def setUp(self):
        runtime.live_positions.clear()
        runtime.live_closed.clear()
        runtime.automation_state["killSwitch"] = False
        runtime.automation_state["paused"] = False
        runtime.automation_state["startupReconciled"] = True

    tearDown = setUp


class PlanTests(_Base):
    def test_from_candidate_and_expiry(self):
        plan = make_plan(ttl_sec=15)
        self.assertEqual(plan.symbol, "TESTUSDT")
        self.assertAlmostEqual(plan.expected_net_pct, 0.55)
        self.assertFalse(plan.expired())
        self.assertTrue(make_plan(ttl_sec=-1).expired())


class RiskEngineTests(_Base):
    def test_limits(self):
        re = RiskEngine()
        plan = make_plan(notional=50)
        re.check(plan, [])  # ok
        with self.assertRaises(HardLimitHit):
            re.check(make_plan(notional=config.LIVE_MAX_NOTIONAL_PER_POS + 1), [])
        big = [{"notional": config.LIVE_MAX_TOTAL_NOTIONAL}]
        with self.assertRaises(HardLimitHit):
            re.check(plan, big)
        many = [{"notional": 1}] * config.MAX_OPEN_POSITIONS
        with self.assertRaises(HardLimitHit):
            re.check(plan, many)

    def test_daily_loss_trips_and_rolls(self):
        re = RiskEngine()
        now = time.time() * 1000
        re.register_close(-(config.LIVE_MAX_DAILY_LOSS_USDT + 5), now)
        self.assertTrue(re.daily_loss_tripped(now))
        next_day = now + 24 * 3_600_000
        self.assertFalse(re.daily_loss_tripped(next_day))


class OpenHedgeTests(_Base):
    def _clients(self, **kw):
        return {
            "Binance": FakeClient("Binance", **kw.get("long", {})),
            "Bybit": FakeClient("Bybit", **kw.get("short", {})),
        }

    def test_happy_path_hedged(self):
        clients = self._clients()
        pos = engine.open_hedge(make_plan(), clients, RiskEngine())
        self.assertEqual(pos["state"], state.HEDGED)
        # target = min(50/100.0, 50/100.9) обмежується дорожчою ногою
        self.assertAlmostEqual(pos["hedgedBaseQty"], round(50 / 100.9, 6))
        self.assertEqual(len(runtime.live_positions), 1)

    def test_one_legged_fill_recovers(self):
        clients = self._clients(short={"fill": "zero"})
        pos = engine.open_hedge(make_plan(), clients, RiskEngine())
        self.assertIn(pos["state"], (state.RECOVERY, state.FAILED))
        self.assertNotIn(pos["id"], runtime.live_positions)
        self.assertTrue(any(o["reduce_only"] for o in clients["Binance"].orders))

    def test_partial_mismatch_rebalances(self):
        clients = self._clients(short={"fill": 0.8})
        pos = engine.open_hedge(make_plan(), clients, RiskEngine())
        self.assertEqual(pos["state"], state.HEDGED)
        self.assertLessEqual(pos["hedgedBaseQty"], 0.5)

    def test_expired_plan_rejected(self):
        with self.assertRaises(engine.ExecutionError):
            engine.open_hedge(make_plan(ttl_sec=-1), self._clients(), RiskEngine())

    def test_insufficient_margin_rejected(self):
        clients = self._clients(long={"free": 0.0})
        with self.assertRaises(engine.ExecutionError):
            engine.open_hedge(make_plan(), clients, RiskEngine())


class CloseHedgeTests(_Base):
    def test_close_computes_realized_and_archives(self):
        clients = {"Binance": FakeClient("Binance"), "Bybit": FakeClient("Bybit")}
        pos = engine.open_hedge(make_plan(), clients, RiskEngine())
        clients["Binance"].price = 101.0  # long продає дорожче -> +
        clients["Bybit"].price = 99.0     # short відкуповує дешевше -> +
        closed = engine.close_hedge(pos, "manual", clients)
        self.assertEqual(closed["state"], state.CLOSED)
        self.assertGreater(closed["realizedPnl"], 0)
        self.assertEqual(runtime.live_closed[0]["id"], pos["id"])


class ExitReasonTests(_Base):
    def test_thresholds(self):
        now = time.time() * 1000
        self.assertEqual(
            state.exit_reason({"unrealizedPnlPct": config.PAPER_TOTAL_STOP_PCT - 1}, now),
            "total_stop",
        )
        self.assertEqual(
            state.exit_reason(
                {"unrealizedPnlPct": 0, "currentGrossPct": config.PAPER_EXIT_GROSS_PCT - 0.01}, now
            ),
            "spread_converged",
        )
        old = now - (config.PAPER_MAX_HOLD_HOURS + 1) * 3_600_000
        self.assertEqual(
            state.exit_reason({"unrealizedPnlPct": 0, "currentGrossPct": 5, "openedAt": old}, now),
            "max_hold",
        )
        self.assertIsNone(
            state.exit_reason({"unrealizedPnlPct": 0, "currentGrossPct": 5, "openedAt": now}, now)
        )


class ReconcileTests(_Base):
    def _track(self, qty=0.5):
        pos = state.new_position(make_plan(), qty)
        pos["state"] = state.HEDGED
        pos["hedgedBaseQty"] = qty
        runtime.live_positions[pos["id"]] = pos
        return pos

    def test_clean_when_positions_match(self):
        self._track(0.5)
        clients = {
            "Binance": FakeClient("Binance", position=Position("TESTUSDT", 0.5, 100, 0, 100, 0, 50, 0)),
            "Bybit": FakeClient("Bybit", position=Position("TESTUSDT", -0.5, 100, 0, 100, 0, 50, 0)),
        }
        self.assertTrue(reconcile.startup_reconcile(clients))
        self.assertTrue(runtime.automation_state["startupReconciled"])

    def test_mismatch_sets_kill_switch(self):
        self._track(0.5)
        clients = {
            "Binance": FakeClient("Binance", position=None),
            "Bybit": FakeClient("Bybit", position=Position("TESTUSDT", -0.5, 100, 0, 100, 0, 50, 0)),
        }
        self.assertFalse(reconcile.startup_reconcile(clients))
        self.assertTrue(runtime.automation_state["killSwitch"])
        self.assertFalse(runtime.automation_state["startupReconciled"])

    def test_incomplete_state_after_crash_is_flagged(self):
        # позиція, що застрягла в LEG_PENDING (краш під час open_hedge)
        pos = state.new_position(make_plan(), 0.5)
        pos["state"] = state.LEG_PENDING
        runtime.live_positions[pos["id"]] = pos
        clients = {"Binance": FakeClient("Binance"), "Bybit": FakeClient("Bybit")}
        self.assertFalse(reconcile.startup_reconcile(clients))
        self.assertTrue(runtime.automation_state["killSwitch"])


class MarginTests(_Base):
    def test_critical_ratio_triggers_close(self):
        pos = state.new_position(make_plan(), 0.5)
        pos["state"] = state.HEDGED
        runtime.live_positions[pos["id"]] = pos
        danger = Position("TESTUSDT", 0.5, 100, 90, 100, 0.95, 50, -5)
        clients = {
            "Binance": FakeClient("Binance", position=danger),
            "Bybit": FakeClient("Bybit", position=danger),
        }
        calls = []
        margin.check_once(clients, lambda p, r, c: calls.append((p["id"], r)))
        self.assertEqual(calls[0], (pos["id"], "margin"))


if __name__ == "__main__":
    unittest.main()
