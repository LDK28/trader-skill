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


SCRIPT = Path(__file__).resolve().parents[1] / "skills/trader-tinvest/scripts/research_horizons.py"
SPEC = importlib.util.spec_from_file_location("research_horizons", SCRIPT)
horizons = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(horizons)


def fixture(points=None, initial="100"):
    points = points or [("2024-01-31", "100"), ("2024-02-29", "110"),
                        ("2024-03-29", "99"), ("2024-04-01", "121")]
    curve = [{"close_time": day + "T23:00:00+03:00", "equity": value} for day, value in points]
    values = [Decimal(value) for _, value in points]
    return {"status": "SIMULATED", "periods": {"holdout": {
        "initial_cash": initial, "final_equity": points[-1][1], "equity_curve": curve,
        "closed_episodes": [{"net_pnl": "synthetic"}],
        "max_close_drawdown_fraction": horizons.frac(horizons.drawdown(values, Decimal(initial)))}}}


class ResearchHorizonsTests(unittest.TestCase):
    def result(self, data):
        result = horizons.summarize(data)
        self.assertEqual(result["status"], "HORIZONS_SUMMARIZED", result)
        self.assertIs(result["forecast_available"], False)
        return result

    def test_leap_month_end_and_first_close_after_anniversary(self):
        result = self.result(fixture())
        one = result["horizons"]["1_calendar_months"]
        self.assertEqual(one["count"], 2)
        self.assertEqual(one["windows"][0]["end"], "2024-02-29")
        self.assertEqual(one["windows"][1]["end"], "2024-03-29")
        two = result["horizons"]["2_calendar_months"]
        self.assertEqual(two["count"], 1)
        self.assertEqual(two["windows"][0]["anniversary"], "2024-03-31")
        self.assertEqual(two["windows"][0]["end"], "2024-04-01")
        self.assertEqual(two["target_hit_count"], 1)

    def test_non_leap_month_end_is_clamped_without_21_bar_proxy(self):
        data = fixture([("2025-01-31", "100"), ("2025-02-28", "110"), ("2025-03-31", "121")])
        one = self.result(data)["horizons"]["1_calendar_months"]
        self.assertEqual(one["count"], 2)
        self.assertEqual(one["windows"][0]["anniversary"], "2025-02-28")
        self.assertEqual(one["target_hit_count"], 2)

    def test_overlapping_windows_count_and_target_are_historical_only(self):
        start = datetime(2025, 1, 1)
        points = [((start + timedelta(days=i)).date().isoformat(), str(100 + i)) for i in range(91)]
        result = self.result(fixture(points))
        one = result["horizons"]["1_calendar_months"]
        # Jan 1 through Mar 1 inclusive have a complete anniversary by Apr 1.
        self.assertEqual(one["count"], 60)
        self.assertEqual(one["target_hit_count"], 60)
        self.assertEqual(one["target_hit_fraction"], "1.000000000")
        self.assertIs(result["windows_independent"], False)
        self.assertIs(result["window_frequencies_are_future_probabilities"], False)
        self.assertIs(result["strategy_validated"], False)
        self.assertIs(result["ready_for_validators"], False)

    def test_manual_median_losses_extremes_and_window_drawdown(self):
        data = fixture([("2025-01-01", "100"), ("2025-01-15", "120"),
                        ("2025-02-01", "90"), ("2025-02-15", "132")])
        one = self.result(data)["horizons"]["1_calendar_months"]
        self.assertEqual(one["count"], 2)
        self.assertEqual(one["losing_count"], 1)
        self.assertEqual(one["target_hit_count"], 1)
        self.assertEqual(one["median_return_fraction"], "0.000000000")
        self.assertEqual(one["worst_return_fraction"], "-0.100000000")
        self.assertEqual(one["best_return_fraction"], "0.100000000")
        self.assertEqual(one["worst_window_drawdown_fraction"], "0.250000000")
        self.assertEqual(one["worst_window"]["start"], "2025-01-01")

    def test_short_history_has_no_complete_windows_and_no_fake_statistics(self):
        result = self.result(fixture([("2025-01-01", "100"), ("2025-01-20", "105")]))
        for horizon in result["horizons"].values():
            self.assertEqual(horizon["count"], 0)
            self.assertIsNone(horizon["target_hit_fraction"])
            self.assertIsNone(horizon["median_return_fraction"])

    def test_period_uses_initial_cash_without_annualization_or_new_costs(self):
        data = fixture([("2025-01-03", "99"), ("2025-12-30", "120")], initial="100")
        period = self.result(data)["period"]
        self.assertEqual(period["historical_pnl_before_tax"], "20")
        self.assertEqual(period["return_fraction"], "0.200000000")
        self.assertEqual(period["calendar_days_elapsed"], 361)
        self.assertIs(period["annualized"], False)

    def test_timezone_is_explicit_moscow_calendar(self):
        data = fixture([("2025-01-01", "100"), ("2025-02-01", "110")])
        data["periods"]["holdout"]["equity_curve"][0]["close_time"] = "2024-12-31T21:30:00Z"
        result = self.result(data)
        self.assertEqual(result["calendar_timezone"], "Europe/Moscow")
        self.assertEqual(result["horizons"]["1_calendar_months"]["windows"][0]["start"], "2025-01-01")

    def test_decimal_context_does_not_change_results_or_mutate_input(self):
        data = fixture()
        original = copy.deepcopy(data)
        expected = self.result(data)
        with localcontext() as context:
            context.prec = 5
            self.assertEqual(self.result(data), expected)
        self.assertEqual(data, original)

    def test_inconsistent_malformed_and_nonpositive_curves_block(self):
        for value in (None, [], "invalid", {}):
            self.assertEqual(horizons.summarize(value)["status"], "BLOCK")
        for key, value in (("final_equity", "100"), ("initial_cash", "0"),
                           ("max_close_drawdown_fraction", "0.9"), ("closed_episodes", {})):
            data = fixture(); data["periods"]["holdout"][key] = value
            self.assertEqual(horizons.summarize(data)["status"], "BLOCK")
        for key, value in (("equity", "0"), ("equity", "NaN"), ("equity", 100.0),
                           ("close_time", "2024-01-31T23:00:00"), ("close_time", "invalid")):
            data = fixture(); data["periods"]["holdout"]["equity_curve"][0][key] = value
            self.assertEqual(horizons.summarize(data)["status"], "BLOCK")
        data = fixture(); curve = data["periods"]["holdout"]["equity_curve"]
        curve[1]["close_time"] = curve[0]["close_time"]
        self.assertEqual(horizons.summarize(data)["status"], "BLOCK")
        data = fixture(); data["periods"]["holdout"]["equity_curve"].reverse()
        self.assertEqual(horizons.summarize(data)["status"], "BLOCK")

    def test_cli_summary_and_bad_json_have_documented_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.json"
            for content, code in ((json.dumps(fixture()), 0), ('{"status":', 2),
                                  ('{"value":NaN}', 2), ('{"status":1,"status":2}', 2)):
                source.write_text(content)
                result = subprocess.run([sys.executable, str(SCRIPT), str(source)], capture_output=True,
                                        text=True, check=False, timeout=10)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(json.loads(result.stdout)["status"], "HORIZONS_SUMMARIZED" if code == 0 else "BLOCK")


if __name__ == "__main__":
    unittest.main()
