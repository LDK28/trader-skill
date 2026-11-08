import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import unittest


sys.dont_write_bytecode = True
MODULE = Path(__file__).resolve().parents[1] / "skills/trader-tinvest/scripts/cash_guard.py"
SPEC = importlib.util.spec_from_file_location("cash_guard", MODULE)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
NOW = datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)


def stamp(seconds):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def fixture():
    times = {"as_of": stamp(-1), "expires_at": stamp(20)}
    return {
        "account_id": "test-account",
        "snapshot": dict(times, account_id="test-account", verified=True,
                         no_existing_debt=True,
                         cash_excludes_blocked_unsettled_borrowed=True,
                         spendable_own_cash={"rub": "10000"}),
        "orders": [{
            "client_id": "example-only", "account_id": "test-account", "side": "BUY",
            "order_type": "LIMIT", "lots": 1, "own_funds_max_lots": 1, "limit_price": "1000",
            "instrument": {"uid": "example-uid", "kind": "share", "available": True,
                           "eligibility_verified": True, "financing": "fully_paid_cash",
                           "leveraged": False},
            "quote": dict(times),
            "estimate": dict(times, verified_for_this_order=True, all_costs_included=True,
                             at_limit_price=True, currency="rub", principal="9990",
                             fees="10", nkd="0", other="0", worst_case_total="10000")
        }]
    }


class CashGuardTests(unittest.TestCase):
    def result(self, data):
        return guard.validate_plan(data, NOW)

    def blocked(self, data, reason):
        result = self.result(data)
        self.assertEqual(result["status"], "BLOCK", result)
        self.assertTrue(any(reason in value for value in result["reasons"]), result)

    def test_exact_limit_cash_purchase_passes(self):
        result = self.result(fixture())
        self.assertEqual(result, {"status": "PASS", "reasons": [], "required_cash": {"rub": "10000"}})

    def test_batch_cannot_reuse_same_cash(self):
        data = fixture()
        for key, value in {"principal": "5990", "fees": "10", "worst_case_total": "6000"}.items():
            data["orders"][0]["estimate"][key] = value
        other = copy.deepcopy(data["orders"][0])
        other["client_id"] = "second"
        data["orders"].append(other)
        self.blocked(data, "insufficient own cash")

    def test_fees_over_budget_are_not_ignored(self):
        data = fixture()
        data["orders"][0]["estimate"].update(principal="10000", fees="1", worst_case_total="10001")
        self.blocked(data, "insufficient own cash")

    def test_missing_cost_or_nkd_cannot_be_assumed_zero(self):
        data = fixture()
        del data["orders"][0]["estimate"]["nkd"]
        self.blocked(data, "nkd must be a decimal string")
        data = fixture()
        data["orders"][0]["estimate"].update(principal="9990", fees="11")
        self.blocked(data, "omits a fee")

    def test_currencies_are_never_combined(self):
        data = fixture()
        data["snapshot"]["spendable_own_cash"]["usd"] = "100000"
        data["snapshot"]["spendable_own_cash"]["rub"] = "9999"
        self.blocked(data, "insufficient own cash in rub")
        data["orders"][0]["estimate"]["currency"] = "eur"
        self.blocked(data, "no verified own cash")

    def test_duplicate_client_id_is_blocked(self):
        data = fixture()
        data["orders"].append(copy.deepcopy(data["orders"][0]))
        self.blocked(data, "duplicate client_id")

    def test_zero_own_funds_lot_cap_blocks_buy_even_with_cash(self):
        data = fixture()
        data["orders"][0]["own_funds_max_lots"] = 0
        self.blocked(data, "lots exceed confirmed own-funds lot cap")

    def test_requested_lots_cannot_exceed_own_funds_cap(self):
        data = fixture()
        data["orders"][0].update(lots=3, own_funds_max_lots=2)
        data["orders"][0]["estimate"].update(principal="3000", fees="10", worst_case_total="3010")
        self.blocked(data, "lots exceed confirmed own-funds lot cap")

    def test_missing_or_invalid_own_funds_lot_cap_blocks(self):
        data = fixture()
        del data["orders"][0]["own_funds_max_lots"]
        self.blocked(data, "own_funds_max_lots must be a nonnegative integer")
        for value in (None, "2", 2.0, True, -1, [], {}):
            with self.subTest(value=value):
                data = fixture()
                data["orders"][0]["own_funds_max_lots"] = value
                self.blocked(data, "own_funds_max_lots must be a nonnegative integer")

    def test_malformed_nested_input_returns_block(self):
        for where, key, value in (("instrument", "kind", []),
                                  ("estimate", "currency", []),
                                  ("instrument", "kind", {}),
                                  ("estimate", "currency", {})):
            with self.subTest(where=where, key=key, value=value):
                data = fixture()
                data["orders"][0][where][key] = value
                self.assertEqual(self.result(data)["status"], "BLOCK")
        for key in ("instrument", "estimate", "quote"):
            with self.subTest(key=key):
                data = fixture()
                data["orders"][0][key] = []
                self.assertEqual(self.result(data)["status"], "BLOCK")

    def test_identity_must_match(self):
        for where in ("snapshot", "order"):
            data = fixture()
            target = data["snapshot"] if where == "snapshot" else data["orders"][0]
            target["account_id"] = "different-account"
            self.blocked(data, "account does not match")

    def test_stale_future_and_expired_records_block(self):
        for key, offset in (("snapshot", -61), ("quote", -31), ("estimate", -31)):
            data = fixture()
            record = data["snapshot"] if key == "snapshot" else data["orders"][0][key]
            record["as_of"] = stamp(offset)
            self.blocked(data, "stale or future-dated")
        data = fixture()
        data["orders"][0]["quote"]["as_of"] = stamp(1)
        self.blocked(data, "future-dated")
        data = fixture()
        data["snapshot"]["expires_at"] = stamp(0)
        self.blocked(data, "has expired")

    def test_unknown_leveraged_and_futures_instruments_block(self):
        for kind in ("unknown", "futures", "option", "structured_note"):
            data = fixture()
            data["orders"][0]["instrument"]["kind"] = kind
            self.blocked(data, "unsupported or unknown")
        for value in (True, None):
            data = fixture()
            data["orders"][0]["instrument"]["leveraged"] = value
            self.blocked(data, "leveraged or unknown")
        data = fixture()
        data["orders"][0]["instrument"]["financing"] = "margin"
        self.blocked(data, "cash-only financing")

    def test_sell_market_and_fractional_lots_block(self):
        for key, value, reason in (("side", "SELL", "only purchases"),
                                   ("order_type", "MARKET", "limit price"),
                                   ("lots", 1.5, "positive integer"),
                                   ("lots", True, "positive integer"),
                                   ("lots", 0, "positive integer")):
            data = fixture()
            data["orders"][0][key] = value
            self.blocked(data, reason)

    def test_float_nonfinite_and_negative_amounts_block(self):
        for value, reason in ((10000.0, "decimal string"), ("NaN", "finite"),
                              ("Infinity", "finite"), ("-1", "invalid sign")):
            data = fixture()
            data["snapshot"]["spendable_own_cash"]["rub"] = value
            self.blocked(data, reason)

    def test_eligibility_cash_and_estimate_must_be_explicit(self):
        for flag in ("verified", "no_existing_debt", "cash_excludes_blocked_unsettled_borrowed"):
            data = fixture()
            data["snapshot"][flag] = False
            self.blocked(data, "explicitly confirm")
        for flag in ("verified_for_this_order", "all_costs_included", "at_limit_price"):
            data = fixture()
            del data["orders"][0]["estimate"][flag]
            self.blocked(data, "estimate must confirm")
        data = fixture()
        del data["orders"][0]["instrument"]["eligibility_verified"]
        self.blocked(data, "instrument must confirm")

    def test_decimal_arithmetic_preserves_nano_precision(self):
        data = fixture()
        data["snapshot"]["spendable_own_cash"]["rub"] = "0.300000001"
        data["orders"][0]["estimate"].update(principal="0.1", fees="0.200000001",
                                             worst_case_total="0.300000001")
        self.assertEqual(self.result(data)["status"], "PASS")

    def test_large_amounts_cannot_round_away_nano_overspend(self):
        data = fixture()
        data["snapshot"]["spendable_own_cash"]["rub"] = "99999999999999999999.000000001"
        data["orders"][0]["estimate"].update(principal="99999999999999999999",
                                             fees="0.000000002",
                                             worst_case_total="99999999999999999999.000000002")
        self.blocked(data, "insufficient own cash")


if __name__ == "__main__":
    unittest.main()
