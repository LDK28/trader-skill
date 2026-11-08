import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1] / "skills/trader-tinvest/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backtest = load("backtest_breakout")


def bar(index, opening, close=None, high=None, low=None, volume=10000):
    opening = Decimal(str(opening))
    close = opening if close is None else Decimal(str(close))
    start = datetime(2026, 1, 1, 7, tzinfo=timezone.utc) + timedelta(days=index)
    return {"open_time": start.isoformat(), "close_time": (start + timedelta(hours=8)).isoformat(),
            "open": str(opening), "close": str(close),
            "high": str(max(opening, close) + 1 if high is None else high),
            "low": str(min(opening, close) - 1 if low is None else low),
            "volume_lots": volume, "is_complete": True}


def fixture(prices=None, split=10):
    prices = prices or [10, 12, 13, 14, 9, 8, 8, 8, 8, 8, 8, 8, 8, 8]
    bars = [bar(i, price) for i, price in enumerate(prices)]
    return {
        "instrument": {"uid": "synthetic-share", "kind": "share", "currency": "rub",
                       "lot_size": 1, "price_tick": "0.01", "cash_tick": "0.01"},
        "dataset": {"kind": "synthetic", "source": "artificial-test-not-market-data",
                    "as_of": "2026-02-01T00:00:00Z", "corporate_actions": "none_declared",
                    "constant_lot_and_tick": True},
        "config": {"initial_cash": "1000", "entry_lookback": 1, "exit_lookback": 1,
                   "max_hold_bars": 100, "commission_bps": "0", "half_spread_bps": "0",
                   "slippage_bps": "0", "max_participation": "1", "settlement_lag_bars": 1},
        "holdout_start": bars[split]["open_time"], "bars": bars,
    }


class BacktestBreakoutTests(unittest.TestCase):
    def result(self, data):
        result = backtest.run(data)
        self.assertEqual(result["status"], "SIMULATED", result)
        self.assertIs(result["strategy_validated"], False)
        self.assertIs(result["ready_for_validators"], False)
        return result

    def development(self, data):
        return self.result(data)["periods"]["development"]

    def blocked(self, data):
        self.assertEqual(backtest.run(data)["status"], "BLOCK")

    def test_prior_channel_and_next_open_fills_have_manual_loss(self):
        period = self.development(fixture())
        self.assertEqual([(x["side"], x["signal_index"], x["bar_index"], x["lots"])
                          for x in period["fills"]], [("BUY", 1, 2, 76), ("SELL", 4, 5, 76)])
        self.assertEqual(Decimal(period["final_equity"]), Decimal("620"))
        self.assertEqual(Decimal(period["net_pnl_before_tax"]), Decimal("-380"))
        self.assertEqual(Decimal(period["closed_episodes"][0]["net_pnl"]), Decimal("-380"))

    def test_commission_impact_and_adverse_tick_rounding_match_manual_pnl(self):
        data = fixture(split=4)
        data["bars"][2] = bar(2, 10)
        data["instrument"].update(lot_size=10, price_tick="0.05")
        data["config"].update(commission_bps="100", half_spread_bps="50", slippage_bps="50")
        period = self.development(data)
        buy, sell = period["fills"]
        self.assertEqual((buy["lots"], buy["units"]), (9, 90))
        self.assertEqual(Decimal(buy["price"]), Decimal("10.10"))
        self.assertEqual(Decimal(sell["price"]), Decimal("13.85"))
        self.assertEqual(Decimal(buy["commission"]), Decimal("9.09"))
        self.assertEqual(Decimal(sell["commission"]), Decimal("12.47"))
        self.assertEqual(Decimal(period["net_pnl_before_tax"]), Decimal("315.94"))
        self.assertEqual(Decimal(period["execution_shortfall"]), Decimal("22.50"))
        self.assertEqual(Decimal(period["gross_pnl_same_fills"]), Decimal("360"))

    def test_affordable_lots_include_commission_and_cash_never_goes_negative(self):
        data = fixture(split=4)
        data["bars"][2] = bar(2, 10)
        data["config"].update(initial_cash="101", commission_bps="100")
        data["instrument"]["lot_size"] = 10
        period = self.development(data)
        self.assertEqual(period["fills"][0]["lots"], 1)
        self.assertEqual(Decimal(period["equity_curve"][1]["available_cash"]), 0)
        data["config"]["initial_cash"] = "100.99"
        period = self.development(data)
        self.assertFalse(period["fills"])
        self.assertTrue(all(Decimal(row["available_cash"]) >= 0 for row in period["equity_curve"]))

    def test_signal_does_not_fill_on_same_close_or_use_future_data(self):
        data = fixture()
        original = self.development(data)
        data["bars"][3] = bar(3, 100, volume=0)
        changed = self.development(data)
        first = original["fills"][0]
        self.assertEqual(first, changed["fills"][0])
        self.assertGreater(first["bar_index"], first["signal_index"])
        self.assertEqual(Decimal(first["reference_open"]), 13)

    def test_previous_volume_limits_entry_and_current_full_volume_does_not(self):
        data = fixture()
        data["bars"][1]["volume_lots"] = 5
        data["bars"][2]["volume_lots"] = 0
        first = self.development(data)["fills"][0]
        data["bars"][2]["volume_lots"] = 999999
        self.assertEqual(first, self.development(data)["fills"][0])
        self.assertEqual(first["lots"], 5)
        data["bars"][1]["volume_lots"] = 0
        period = self.development(data)
        self.assertFalse(period["fills"])
        self.assertTrue(any(row["bar_index"] == 2 and row["side"] == "BUY"
                            for row in period["skipped_fills"]))

    def test_partial_buy_is_not_topped_up_and_partial_sells_persist(self):
        data = fixture(split=8)
        data["config"]["commission_bps"] = "100"
        data["bars"][1]["volume_lots"] = 3
        data["bars"][2] = bar(2, 13, 9, volume=2)
        data["bars"][3] = bar(3, 8, volume=0)
        data["bars"][4] = bar(4, 7, volume=1)
        data["bars"][5] = bar(5, 6)
        data["bars"][6] = bar(6, 6)
        data["bars"][7] = bar(7, 6)
        period = self.development(data)
        self.assertEqual([(x["side"], x["bar_index"], x["lots"]) for x in period["fills"]],
                         [("BUY", 2, 3), ("SELL", 3, 2), ("SELL", 5, 1)])
        self.assertEqual(len(period["closed_episodes"]), 1)
        self.assertEqual(Decimal(period["commissions"]), Decimal("0.61"))
        self.assertEqual(Decimal(period["final_equity"]), Decimal("982.39"))
        self.assertTrue(all(row["position_lots"] >= 0 for row in period["equity_curve"]))

    def test_max_hold_counts_entry_bar_and_exits_next_open(self):
        data = fixture(list(range(10, 24)))
        data["bars"][1] = bar(1, 12)
        data["config"]["max_hold_bars"] = 1
        fills = self.development(data)["fills"]
        self.assertEqual([(x["side"], x["signal_index"], x["bar_index"], x["reason"])
                          for x in fills[:2]], [("BUY", 1, 2, "breakout"), ("SELL", 2, 3, "max_hold")])

    def test_unsettled_proceeds_count_once_in_equity_but_are_not_spendable(self):
        data = fixture()
        data["config"]["settlement_lag_bars"] = 3
        data["bars"][5] = bar(5, 8, 15)
        data["bars"][6] = bar(6, 16)
        data["bars"][7] = bar(7, 18)
        data["bars"][8] = bar(8, 20)
        data["bars"][9] = bar(9, 20)
        period = self.development(data)
        rows = {row["bar_index"]: row for row in period["equity_curve"]}
        for index in (5, 6, 7):
            self.assertEqual(Decimal(rows[index]["available_cash"]), 12)
            self.assertEqual(Decimal(rows[index]["unsettled_proceeds"]), 608)
            self.assertEqual(Decimal(rows[index]["equity"]), 620)
        self.assertEqual(Decimal(rows[8]["unsettled_proceeds"]), 0)
        self.assertEqual([(x["side"], x["bar_index"]) for x in period["fills"]],
                         [("BUY", 2), ("SELL", 5), ("BUY", 8)])
        self.assertIn(6, [row["bar_index"] for row in period["skipped_fills"]])

    def test_split_resets_cash_position_and_pending_exit(self):
        data = fixture(split=5)
        result = self.result(data)
        self.assertEqual(result["periods"]["development"]["pending_signal"]["side"], "SELL")
        holdout = result["periods"]["holdout"]
        self.assertFalse(holdout["fills"])
        self.assertEqual(Decimal(holdout["equity_curve"][0]["equity"]), 1000)
        self.assertEqual(holdout["equity_curve"][0]["position_lots"], 0)
        data["bars"][2] = bar(2, 100)
        data["bars"][3] = bar(3, 200)
        self.assertEqual(self.result(data)["periods"]["holdout"], holdout)

    def test_last_signals_are_not_filled_and_no_forced_final_sale(self):
        data = fixture([10, 10, 12] + [12] * 10 + [20], split=3)
        result = self.result(data)
        development, holdout = result["periods"]["development"], result["periods"]["holdout"]
        self.assertFalse(development["fills"])
        self.assertEqual(development["pending_signal"], {"side": "BUY", "reason": "breakout", "signal_index": 2})
        self.assertFalse(holdout["fills"])
        self.assertEqual(holdout["pending_signal"]["signal_index"], 13)
        period = self.development(fixture(split=4))
        self.assertEqual(len(period["fills"]), 1)
        self.assertEqual(period["open_position_lots"], 76)
        self.assertEqual(Decimal(period["final_equity"]), 1076)
        self.assertFalse(period["closed_episodes"])

    def test_cash_and_buy_once_hold_benchmarks_have_comparable_periods(self):
        data = fixture(split=4)
        period = self.development(data)
        benchmark = period["benchmarks"]["buy_once_hold"]
        self.assertEqual(Decimal(period["benchmarks"]["cash_final_equity"]), 1000)
        self.assertEqual([(x["side"], x["bar_index"], x["lots"]) for x in benchmark["fills"]], [("BUY", 1, 83)])
        self.assertEqual(Decimal(benchmark["final_equity"]), 1166)
        self.assertEqual((benchmark["start_index"], benchmark["end_index_exclusive"]),
                         (period["start_index"], period["end_index_exclusive"]))

    def test_coarse_fee_rounding_cannot_create_negative_sale_proceeds(self):
        data = fixture([1] * 8, split=4)
        data["bars"] = [bar(i, "0.01", high="0.02", low="0.01", volume=100) for i in range(8)]
        data["bars"][1] = bar(1, "0.03", high="0.04", low="0.02", volume=100)
        data["bars"][2]["volume_lots"] = 1
        data["holdout_start"] = data["bars"][4]["open_time"]
        data["config"].update(initial_cash="2", commission_bps="100")
        data["instrument"]["cash_tick"] = "1"
        period = self.development(data)
        self.assertFalse([fill for fill in period["fills"] if fill["side"] == "SELL"])
        self.assertEqual(period["pending_signal"]["side"], "SELL")
        self.assertTrue(all(Decimal(row["unsettled_proceeds"]) >= 0 and Decimal(row["available_cash"]) >= 0
                            for row in period["equity_curve"]))

    def test_complete_ohlc_price_tick_and_bar_times_are_checked(self):
        for key, value in (("is_complete", False), ("is_complete", 1), ("high", "11"),
                           ("low", "13"), ("open", "12.001"), ("volume_lots", 1.5),
                           ("volume_lots", True), ("volume_lots", -1), ("close_time", "invalid")):
            with self.subTest(key=key, value=value):
                data = fixture()
                data["bars"][1][key] = value
                self.blocked(data)
        for key, value in (("open_time", "2026-01-02T07:00:00"),
                           ("close_time", "2026-03-01T00:00:00Z"),
                           ("open_time", "2026-01-01T07:00:00Z")):
            data = fixture()
            data["bars"][1][key] = value
            self.blocked(data)
        data = fixture()
        data["bars"][1], data["bars"][2] = data["bars"][2], data["bars"][1]
        self.blocked(data)

    def test_counts_costs_split_and_scope_are_strict(self):
        for key, value in (("entry_lookback", True), ("exit_lookback", 0), ("max_hold_bars", "3"),
                           ("settlement_lag_bars", 0), ("initial_cash", 1000.0),
                           ("commission_bps", "NaN"), ("slippage_bps", "-1"),
                           ("half_spread_bps", "1001"), ("max_participation", "1.01")):
            data = fixture()
            data["config"][key] = value
            self.blocked(data)
        for key, value in (("kind", "bond"), ("currency", "rub/usd"), ("lot_size", True), ("cash_tick", "0")):
            data = fixture()
            data["instrument"][key] = value
            self.blocked(data)
        for key, value in (("kind", "verified"), ("corporate_actions", "splits"), ("constant_lot_and_tick", False)):
            data = fixture()
            data["dataset"][key] = value
            self.blocked(data)
        for index in (1, 2, 13):
            data = fixture()
            data["holdout_start"] = data["bars"][index]["open_time"]
            self.blocked(data)

    def test_invalid_objects_and_missing_fields_fail_closed(self):
        for value in (None, [], True, "input"):
            self.blocked(value)
        for key in ("instrument", "dataset", "config", "bars"):
            for value in (None, [], "invalid"):
                data = fixture()
                data[key] = value
                self.blocked(data)
        for key in ("open", "volume_lots", "is_complete"):
            data = fixture()
            del data["bars"][1][key]
            self.blocked(data)

    def test_reproducibility_small_decimal_context_and_no_false_validation(self):
        data = fixture()
        original = copy.deepcopy(data)
        result = self.result(data)
        with localcontext() as context:
            context.prec = 5
            repeated = self.result(data)
        self.assertEqual(result, repeated)
        self.assertEqual(data, original)
        self.assertIs(result["holdout_independence_verified"], False)
        self.assertEqual(result["data_kind"], "synthetic")
        self.assertEqual(len(result["parameters_sha256"]), 64)
        for name in ("cash_guard", "sell_guard"):
            self.assertEqual(load(name).validate_plan(result)["status"], "BLOCK")
        self.assertEqual(load("daily_loss").evaluate(result)["status"], "INSUFFICIENT_DATA")


class BacktestBreakoutCliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "backtest_breakout.py"), *map(str, args)],
                              capture_output=True, text=True, check=False, timeout=10)

    def run_text(self, text):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic-backtest.json"
            source.write_text(text, encoding="utf-8")
            return self.run_cli(source)

    def test_help_defines_simulation_not_execution(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NEXT open", result.stdout)
        self.assertIn("No network, credentials", result.stdout)

    def test_valid_fixture_is_a_successful_unvalidated_simulation(self):
        result = self.run_text(json.dumps(fixture()))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "SIMULATED")
        self.assertIs(output["strategy_validated"], False)
        self.assertIs(output["ready_for_validators"], False)

    def test_missing_malformed_duplicate_and_nonfinite_json_are_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(Path(directory) / "missing.json")
        self.assertEqual(result.returncode, 2)
        for value in ('{"config":', '{"bars":[],"bars":[]}', '{"value":NaN}', '[' * 2000 + '0' + ']' * 2000):
            result = self.run_text(value)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "BLOCK")
            self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
