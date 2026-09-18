import unittest

from cryptobot.exchanges import (
    AccountPool,
    CcxtExchangeClient,
    ExchangeOpError,
    MexcExchangeClient,
    NotSupported,
    OrderRejected,
    build_client,
)
from cryptobot.exchanges.accounts import _parse_pairs
from cryptobot.exchanges.base import _base_of


class FakeCcxt:
    """Мінімальний двійник ccxt-біржі для офлайн-тестів."""

    def __init__(self, **overrides):
        self.markets = {"BTC/USDT:USDT": {}, "1000PEPE/USDT:USDT": {}}
        self.calls = []
        self._overrides = overrides

    def load_markets(self):
        self.calls.append(("load_markets",))

    def market(self, symbol):
        return self.markets[symbol]

    def amount_to_precision(self, symbol, amount):
        return round(float(amount), 3)

    def set_leverage(self, leverage, symbol, params=None):
        self.calls.append(("set_leverage", leverage, symbol, params))
        if "set_leverage_error" in self._overrides:
            raise self._overrides["set_leverage_error"]

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls.append(("create_order", symbol, type_, side, amount, params))
        return self._overrides.get(
            "order",
            {
                "id": "OID1",
                "clientOrderId": (params or {}).get("clientOrderId"),
                "status": "closed",
                "filled": amount,
                "average": 100.5,
                "side": side,
            },
        )

    def fetch_order(self, order_id, symbol):
        return self._overrides.get(
            "fetch_order",
            {"id": order_id, "status": "filled", "filled": 1.0, "average": 50.0, "side": "buy"},
        )

    def fetch_positions(self, symbols):
        return self._overrides.get("positions", [])

    def fetch_balance(self):
        return self._overrides.get("balance", {"USDT": {"free": 123.4}})

    def cancel_all_orders(self, symbol):
        self.calls.append(("cancel_all_orders", symbol))

    def fetch_time(self):
        return 1_700_000_000_000


class BaseOfTests(unittest.TestCase):
    def test_strips_usdt_and_keeps_multiplier_prefix(self):
        self.assertEqual(_base_of("BTCUSDT"), "BTC")
        self.assertEqual(_base_of("1000PEPEUSDT"), "1000PEPE")
        with self.assertRaises(NotSupported):
            _base_of("BTCUSD")


class CcxtClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCcxt()
        self.client = CcxtExchangeClient("Binance", self.fake)
        self.client.load()

    def test_unified_and_has_market(self):
        self.assertEqual(self.client.unified("BTCUSDT"), "BTC/USDT:USDT")
        self.assertTrue(self.client.has_market("1000PEPEUSDT"))
        self.assertFalse(self.client.has_market("DOGEUSDT"))

    def test_amount_for_notional_uses_precision(self):
        self.assertEqual(self.client.amount_for_notional("BTCUSDT", 1000, 30000), 0.033)

    def test_market_order_parses_result_and_passes_client_id(self):
        result = self.client.market_order("BTCUSDT", "buy", 0.5, "pos-A", reduce_only=True)
        self.assertEqual(result.status, "closed")
        self.assertTrue(result.is_filled)
        self.assertEqual(result.filled_base, 0.5)
        self.assertEqual(result.avg_price, 100.5)
        self.assertEqual(result.client_id, "pos-A")
        _, _, _, _, _, params = self.fake.calls[-1]
        self.assertEqual(params["clientOrderId"], "pos-A")
        self.assertTrue(params["reduceOnly"])

    def test_fetch_order_result_maps_filled_to_closed(self):
        result = self.client.fetch_order_result("BTCUSDT", "OID1")
        self.assertEqual(result.status, "closed")
        self.assertEqual(result.filled_base, 1.0)

    def test_set_leverage_swallows_not_modified(self):
        fake = FakeCcxt(set_leverage_error=Exception("leverage not modified"))
        client = CcxtExchangeClient("Binance", fake)
        client.set_leverage("BTCUSDT", 10)  # не має кидати

    def test_set_leverage_translates_real_error(self):
        fake = FakeCcxt(set_leverage_error=Exception("boom"))
        client = CcxtExchangeClient("Binance", fake)
        with self.assertRaises(ExchangeOpError):
            client.set_leverage("BTCUSDT", 10)

    def test_position_signs_quantity_by_side(self):
        fake = FakeCcxt(
            positions=[
                {
                    "side": "short",
                    "contracts": 3,
                    "contractSize": 1,
                    "entryPrice": 100,
                    "liquidationPrice": 130,
                    "markPrice": 101,
                    "marginRatio": 0.2,
                    "notional": 300,
                    "unrealizedPnl": -3,
                }
            ]
        )
        client = CcxtExchangeClient("Binance", fake)
        pos = client.position("BTCUSDT")
        self.assertEqual(pos.base_qty, -3)
        self.assertEqual(pos.liq_price, 130)

    def test_position_none_when_flat(self):
        fake = FakeCcxt(positions=[{"side": "long", "contracts": 0, "contractSize": 1}])
        self.assertIsNone(CcxtExchangeClient("Binance", fake).position("BTCUSDT"))

    def test_free_collateral(self):
        self.assertEqual(self.client.free_collateral(), 123.4)


class MexcClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCcxt()
        self.fake.markets["BTC/USDT:USDT"] = {
            "contractSize": 0.0001,
            "limits": {"amount": {"min": 1}},
        }
        self.client = MexcExchangeClient(self.fake)

    def test_notional_is_rounded_to_contracts_and_returned_as_base(self):
        # 20 USDT / (50,000 * 0.0001) = 4 contracts = 0.0004 BTC.
        self.assertAlmostEqual(
            self.client.amount_for_notional("BTCUSDT", 20, 50_000), 0.0004
        )

    def test_market_order_converts_base_to_contracts_and_fill_back_to_base(self):
        self.client.set_leverage("BTCUSDT", 3)
        result = self.client.market_order("BTCUSDT", "buy", 0.0004, "mexc-A")
        _, _, _, _, amount, params = self.fake.calls[-1]
        self.assertEqual(amount, 4)
        self.assertEqual(params["openType"], 1)
        self.assertEqual(params["leverage"], 3)
        self.assertAlmostEqual(result.filled_base, 0.0004)

    def test_reduce_only_is_forwarded(self):
        self.client.set_leverage("BTCUSDT", 3)
        self.client.market_order("BTCUSDT", "sell", 0.0002, "mexc-C", True)
        params = self.fake.calls[-1][-1]
        self.assertTrue(params["reduceOnly"])

    def test_set_leverage_configures_long_and_short(self):
        self.client.set_leverage("BTCUSDT", 3)
        calls = [row for row in self.fake.calls if row[0] == "set_leverage"]
        self.assertEqual([row[3]["positionType"] for row in calls], [1, 2])
        self.assertTrue(all(row[3]["openType"] == 1 for row in calls))

    def test_below_one_contract_is_rejected(self):
        self.client.set_leverage("BTCUSDT", 3)
        with self.assertRaises(OrderRejected):
            self.client.market_order("BTCUSDT", "buy", 0.00001, "tiny")

    def test_order_requires_leverage_first(self):
        with self.assertRaises(OrderRejected):
            self.client.market_order("BTCUSDT", "buy", 0.0002, "no-lev")


class AccountPoolTests(unittest.TestCase):
    def test_parse_pairs(self):
        accounts = _parse_pairs("Binance", " k1:s1 , bad , k2:s2 ")
        self.assertEqual([a.key for a in accounts], ["k1", "k2"])
        self.assertEqual(accounts[0].secret, "s1")

    def test_from_env_and_access(self):
        env = {"BINANCE_API_KEYS": "k1:s1", "BYBIT_API_KEYS": ""}
        pool = AccountPool.from_env(getenv=lambda name, default="": env.get(name, default))
        self.assertTrue(pool.has("Binance"))
        self.assertFalse(pool.has("Bybit"))
        self.assertEqual(pool.active("Binance").key, "k1")
        with self.assertRaises(KeyError):
            pool.active("Bybit")

    def test_single_key_ban_cannot_rotate(self):
        pool = AccountPool.from_env(getenv=lambda *_: "k1:s1")
        pool.report_ban("Binance", "suspended")
        self.assertFalse(pool.rotate("Binance"))

    def test_two_keys_rotate_on_ban(self):
        pool = AccountPool.from_env(getenv=lambda *_: "k1:s1,k2:s2")
        self.assertEqual(pool.active("Binance").key, "k1")
        pool.report_ban("Binance", "suspended")
        self.assertEqual(pool.active("Binance").key, "k2")


class FactoryTests(unittest.TestCase):
    def test_unknown_exchange_raises(self):
        with self.assertRaises(ValueError):
            build_client("Kraken", None)

    def test_missing_account_raises(self):
        with self.assertRaises(ValueError):
            build_client("Binance", None)
        with self.assertRaises(ValueError):
            build_client("MEXC", None)


if __name__ == "__main__":
    unittest.main()
