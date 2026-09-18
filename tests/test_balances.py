import unittest
from unittest import mock

from cryptobot import balances


class _Pool:
    def has(self, name):
        return name in {"Binance", "MEXC"}

    def active(self, name):
        return object()


class _Client:
    def __init__(self, name):
        self.name = name

    def load(self):
        pass

    def usdt_balance(self):
        if self.name == "MEXC":
            raise RuntimeError("auth failed")
        return {"free": 12.5, "used": 2.5, "total": 15.0, "currency": "USDT"}


class BalanceSnapshotTests(unittest.TestCase):
    def setUp(self):
        balances._cache_at = 0

    @mock.patch("cryptobot.balances.build_client", side_effect=lambda name, *_a, **_k: _Client(name))
    @mock.patch("cryptobot.balances.AccountPool.from_env", return_value=_Pool())
    def test_reports_connected_missing_and_failed_exchanges(self, _pool, build):
        payload = balances.balance_snapshot(force=True)
        rows = {row["exchange"]: row for row in payload["exchanges"]}
        self.assertTrue(rows["Binance"]["connected"])
        self.assertEqual(rows["Binance"]["free"], 12.5)
        self.assertFalse(rows["Bybit"]["connected"])
        self.assertEqual(rows["Bybit"]["error"], "API-ключ не заданий")
        self.assertFalse(rows["MEXC"]["connected"])
        self.assertIn("auth failed", rows["MEXC"]["error"])
        self.assertTrue(all(call.kwargs["sandbox"] is False for call in build.call_args_list))


if __name__ == "__main__":
    unittest.main()
