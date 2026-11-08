import copy
import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "skills/trader-tinvest/scripts/daily_loss.py"
SPEC = importlib.util.spec_from_file_location("daily_loss", MODULE_PATH)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def example():
    return {"account_id": "test-account", "currency": "rub", "loss_limit_fraction": "0.50",
            "baseline": {"account_id": "test-account", "currency": "rub", "day": "2026-11-08",
                         "immutable": True, "day_start_equity": "10000"},
            "snapshot": {"account_id": "test-account", "currency": "rub", "day": "2026-11-08",
                         "current_equity": "10000", "net_external_cashflow": "0", "cashflow_verified": True}}


class DailyLossTests(unittest.TestCase):
    def test_stop_at_exactly_half(self):
        payload = example()
        payload["snapshot"]["current_equity"] = "5000"
        result = GATE.evaluate(payload)
        self.assertEqual(result["status"], "STOP_NEW_PROPOSALS")
        self.assertEqual(result["daily_loss_fraction"], "0.5")

    def test_below_and_above_half(self):
        for current, expected in (("5000.01", True), ("4999.99", False)):
            with self.subTest(current=current):
                payload = example()
                payload["snapshot"]["current_equity"] = current
                self.assertEqual(GATE.evaluate(payload)["allow_new_proposals"], expected)

    def test_deposit_is_not_profit(self):
        payload = example()
        payload["snapshot"].update(current_equity="15000", net_external_cashflow="5000")
        result = GATE.evaluate(payload)
        self.assertEqual(result["cashflow_adjusted_pnl"], "0")
        self.assertEqual(result["reference_capital"], "15000")

    def test_withdrawal_is_not_loss(self):
        payload = example()
        payload["snapshot"].update(current_equity="5000", net_external_cashflow="-5000")
        self.assertEqual(GATE.evaluate(payload)["daily_loss"], "0")
        self.assertTrue(GATE.evaluate(payload)["allow_new_proposals"])

    def test_cashflows_cannot_hide_existing_loss(self):
        payload = example()
        payload["snapshot"].update(current_equity="5000", net_external_cashflow="5000")
        self.assertEqual(GATE.evaluate(payload)["status"], "STOP_NEW_PROPOSALS")

    def test_first_day_deposit_can_supply_reference(self):
        payload = example()
        payload["baseline"]["day_start_equity"] = "0"
        payload["snapshot"]["net_external_cashflow"] = "10000"
        self.assertTrue(GATE.evaluate(payload)["allow_new_proposals"])

    def test_nonpositive_reference_blocks(self):
        for flow in ("-10000", "-12000"):
            payload = example()
            payload["snapshot"]["net_external_cashflow"] = flow
            self.assertEqual(GATE.evaluate(payload)["status"], "INSUFFICIENT_DATA")

    def test_bad_numeric_values_block(self):
        for bad in (5000.0, 5000, "NaN", "Infinity", "1e20", None, True):
            with self.subTest(value=bad):
                payload = example()
                payload["snapshot"]["current_equity"] = bad
                self.assertFalse(GATE.evaluate(payload)["allow_new_proposals"])

    def test_identity_date_and_verification_are_required(self):
        mutations = (("account_id", "other"), ("currency", "usd"), ("day", "2026-11-09"),
                     ("day", "invalid"), ("cashflow_verified", False))
        for field, value in mutations:
            payload = example()
            payload["snapshot"][field] = value
            self.assertEqual(GATE.evaluate(payload)["status"], "INSUFFICIENT_DATA")
        payload = example()
        payload["baseline"]["immutable"] = False
        self.assertFalse(GATE.evaluate(payload)["allow_new_proposals"])

    def test_missing_invalid_or_out_of_range_limit(self):
        for limit in (None, "0", "-0.5", "1.01", 0.5):
            payload = example()
            payload["loss_limit_fraction"] = limit
            self.assertEqual(GATE.evaluate(payload)["status"], "INSUFFICIENT_DATA")

    def test_evaluation_does_not_modify_baseline(self):
        payload = example()
        prior = copy.deepcopy(payload)
        GATE.evaluate(payload)
        self.assertEqual(payload, prior)


if __name__ == "__main__":
    unittest.main()
