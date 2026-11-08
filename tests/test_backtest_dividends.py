import copy
from decimal import Decimal, localcontext
import unittest

from test_backtest_breakout import backtest, bar, fixture


def with_dividend(data=None, ex_index=3, payment_date="2026-01-08", amount="1"):
    data = fixture() if data is None else data
    data["dataset"]["corporate_actions"] = "cash_dividends"
    data["dividends"] = [{"ex_date": data["bars"][ex_index]["close_time"][:10],
                          "payment_date": payment_date, "amount_per_unit": amount,
                          "source": "synthetic-dividend-source"}]
    return data


class BacktestDividendTests(unittest.TestCase):
    def result(self, data):
        result = backtest.run(data)
        self.assertEqual(result["status"], "SIMULATED", result)
        self.assertFalse(result["strategy_validated"])
        self.assertFalse(result["ready_for_validators"])
        return result

    def development(self, data):
        return self.result(data)["periods"]["development"]

    def test_prior_holdings_create_receivable_and_episode_income(self):
        period = self.development(with_dividend())
        rows = {row["bar_index"]: row for row in period["equity_curve"]}
        self.assertEqual(period["dividend_events"][0]["units"], 76)
        self.assertEqual(Decimal(rows[3]["dividend_receivables"]), 76)
        self.assertEqual(Decimal(rows[3]["equity"]), 1152)
        self.assertEqual(Decimal(period["final_equity"]), 696)
        self.assertEqual(Decimal(period["dividends_cash_received_gross"]), 76)
        self.assertEqual(Decimal(period["dividend_receivables"]), 0)
        episode = period["closed_episodes"][0]
        self.assertEqual(Decimal(episode["trading_pnl_before_dividends"]), -380)
        self.assertEqual(Decimal(episode["dividend_income"]), 76)
        self.assertEqual(Decimal(episode["net_pnl"]), -304)

    def test_ex_day_opening_buyer_is_excluded_but_existing_benchmark_holder_is_not(self):
        period = self.development(with_dividend(ex_index=2))
        self.assertEqual(period["dividend_events"][0]["units"], 0)
        self.assertEqual(Decimal(period["dividends_accrued_gross"]), 0)
        self.assertEqual(Decimal(period["final_equity"]), 620)
        benchmark = period["benchmarks"]["buy_once_hold"]
        self.assertEqual(benchmark["dividend_events"][0]["units"], 83)
        self.assertEqual(Decimal(benchmark["dividends_cash_received_gross"]), 83)

    def test_ex_day_seller_keeps_gross_entitlement(self):
        period = self.development(with_dividend(ex_index=5))
        self.assertEqual(period["fills"][-1]["bar_index"], 5)
        self.assertEqual(period["dividend_events"][0]["units"], 76)
        self.assertEqual(Decimal(period["final_equity"]), 696)

    def test_payment_is_booked_after_open_fills_not_spent_early(self):
        data = fixture()
        data["config"]["settlement_lag_bars"] = 3
        data["bars"][5] = bar(5, 8, 15)
        data["bars"][6] = bar(6, 16, 17)
        data["bars"][7] = bar(7, 18)
        data["bars"][8] = bar(8, 20)
        data["bars"][9] = bar(9, 20)
        period = self.development(with_dividend(data, payment_date="2026-01-07"))
        rows = {row["bar_index"]: row for row in period["equity_curve"]}
        self.assertEqual(Decimal(rows[5]["available_cash"]), 12)
        self.assertEqual(Decimal(rows[6]["available_cash"]), 88)
        self.assertEqual(Decimal(rows[6]["dividend_cash_received"]), 76)
        self.assertFalse([fill for fill in period["fills"] if fill["bar_index"] == 6])
        self.assertEqual([(fill["side"], fill["bar_index"], fill["lots"]) for fill in period["fills"]],
                         [("BUY", 2, 76), ("SELL", 5, 76), ("BUY", 7, 4)])

    def test_unpaid_entitlement_stays_in_final_equity_not_spendable_cash(self):
        period = self.development(with_dividend(payment_date="2026-03-01"))
        final = period["equity_curve"][-1]
        self.assertEqual(Decimal(final["available_cash"]), 620)
        self.assertEqual(Decimal(final["dividend_receivables"]), 76)
        self.assertEqual(Decimal(period["final_equity"]), 696)
        self.assertEqual(Decimal(period["dividends_cash_received_gross"]), 0)

    def test_nontrading_payment_date_uses_first_later_bar_close(self):
        data = with_dividend()
        del data["bars"][7]
        period = self.development(data)
        before, paid = period["equity_curve"][5:7]
        self.assertEqual(before["close_time"][:10], "2026-01-07")
        self.assertEqual(Decimal(before["dividend_cash_received"]), 0)
        self.assertEqual(paid["close_time"][:10], "2026-01-09")
        self.assertEqual(Decimal(paid["dividend_cash_received"]), 76)

    def test_period_reset_does_not_inherit_entitlement_or_payment(self):
        result = self.result(with_dividend(fixture(split=5)))
        development, holdout = result["periods"]["development"], result["periods"]["holdout"]
        self.assertEqual(Decimal(development["dividend_receivables"]), 76)
        self.assertEqual(Decimal(holdout["dividends_accrued_gross"]), 0)
        self.assertEqual(Decimal(holdout["dividends_cash_received_gross"]), 0)
        self.assertEqual(Decimal(holdout["final_equity"]), 1000)

    def test_partial_exit_entitles_only_remaining_units(self):
        data = fixture(split=8)
        data["bars"][1]["volume_lots"] = 3
        data["bars"][2] = bar(2, 13, 9, volume=2)
        data["bars"][3] = bar(3, 8, volume=0)
        data["bars"][4] = bar(4, 7, volume=1)
        data["bars"][5] = bar(5, 6)
        data["bars"][6] = bar(6, 6)
        data["bars"][7] = bar(7, 6)
        period = self.development(with_dividend(data, ex_index=4))
        self.assertEqual(period["dividend_events"][0]["units"], 1)
        self.assertEqual(Decimal(period["dividends_accrued_gross"]), 1)
        self.assertEqual(Decimal(period["closed_episodes"][0]["dividend_income"]), 1)

    def test_grouped_bar_uses_close_trading_date_not_open_calendar_date(self):
        data = with_dividend()
        data["bars"][3]["open_time"] = "2026-01-03T16:00:00+00:00"
        period = self.development(data)
        self.assertEqual(period["dividend_events"][0]["ex_date"], "2026-01-04")
        self.assertEqual(period["dividend_events"][0]["units"], 76)

    def test_invalid_or_ambiguous_events_are_blocked(self):
        for key, value in (("ex_date", "2026-01-30"), ("ex_date", "20260104"),
                           ("payment_date", "2026-01-03"), ("payment_date", "invalid"),
                           ("amount_per_unit", "0"), ("amount_per_unit", "-1"),
                           ("amount_per_unit", 1.0), ("amount_per_unit", "NaN"),
                           ("source", ""), ("source", None)):
            with self.subTest(key=key, value=value):
                data = with_dividend()
                data["dividends"][0][key] = value
                self.assertEqual(backtest.run(data)["status"], "BLOCK")
        for events in (None, [], "invalid", [None]):
            data = with_dividend()
            data["dividends"] = events
            self.assertEqual(backtest.run(data)["status"], "BLOCK")
        data = with_dividend()
        data["dividends"].append(copy.deepcopy(data["dividends"][0]))
        self.assertEqual(backtest.run(data)["status"], "BLOCK")
        data = with_dividend()
        data["dataset"]["corporate_actions"] = "none_declared"
        self.assertEqual(backtest.run(data)["status"], "BLOCK")

    def test_same_day_multiple_bars_are_rejected_for_dividend_mode(self):
        data = with_dividend()
        data["bars"][1].update(open_time="2026-01-01T16:00:00+00:00", close_time="2026-01-01T17:00:00+00:00")
        self.assertEqual(backtest.run(data)["status"], "BLOCK")

    def test_none_declared_is_backwards_compatible_and_inputs_are_not_mutated(self):
        original = fixture()
        before = copy.deepcopy(original)
        baseline = self.result(original)
        original["dividends"] = []
        explicit_empty = self.result(original)
        self.assertEqual(baseline["periods"], explicit_empty["periods"])
        self.assertEqual(baseline["strategy"], "cash-share-breakout-v1")
        self.assertNotIn("dividend_receivables", baseline["periods"]["development"])
        data = with_dividend()
        immutable = copy.deepcopy(data)
        result = self.result(data)
        with localcontext() as context:
            context.prec = 5
            self.assertEqual(self.result(data), result)
        self.assertEqual(data, immutable)
        self.assertEqual(before["dataset"]["corporate_actions"], "none_declared")
        self.assertEqual(result["strategy"], "cash-share-breakout-dividends-v1")


if __name__ == "__main__":
    unittest.main()
