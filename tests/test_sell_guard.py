import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


sys.dont_write_bytecode = True
MODULE = Path(__file__).resolve().parents[1] / "skills/trader-tinvest/scripts/sell_guard.py"
SPEC = importlib.util.spec_from_file_location("sell_guard", MODULE)
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
                         no_existing_debt=True, positions_exclude_borrowed_and_unsettled=True,
                         settled_own_units_include_blocked=True,
                         positions=[{"uid": "test-uid", "kind": "share", "lot_size": 10,
                                     "settled_own_units": "100", "blocked_own_units": "20"}]),
        "orders": [{
            "client_id": "example-only", "account_id": "test-account", "side": "SELL",
            "order_type": "LIMIT", "lots": 8, "limit_price": "100",
            "instrument": {"uid": "test-uid", "kind": "share", "lot_size": 10,
                           "available": True, "eligibility_verified": True,
                           "financing": "own_settled_position", "leveraged": False},
            "quote": dict(times, instrument_uid="test-uid"),
            "position_limit": dict(times, account_id="test-account", instrument_uid="test-uid",
                                   limit_price="100", source="sell_limits", verified=True,
                                   own_position_max_lots=8),
        }],
    }


def second_order(data, lots, uid=None):
    order = copy.deepcopy(data["orders"][0])
    order.update(client_id="second-order", lots=lots)
    if uid is not None:
        order["instrument"]["uid"] = uid
        order["quote"]["instrument_uid"] = uid
        order["position_limit"]["instrument_uid"] = uid
    data["orders"].append(order)
    return order


class SellGuardTests(unittest.TestCase):
    def result(self, data):
        return guard.validate_plan(data, NOW)

    def blocked(self, data, reason=None):
        result = self.result(data)
        self.assertEqual(result["status"], "BLOCK", result)
        self.assertTrue(result["reasons"], result)
        if reason is not None:
            self.assertTrue(any(reason in value for value in result["reasons"]), result)

    def test_exactly_free_units_pass_without_crediting_cash(self):
        self.assertEqual(self.result(fixture()), {
            "status": "PASS", "reasons": [], "required_units": {"test-uid": "80"}})

    def test_blocked_units_and_zero_position_cannot_be_sold(self):
        for total, blocked in (("100", "100"), ("0", "0"), ("100", "21")):
            with self.subTest(total=total, blocked=blocked):
                data = fixture()
                data["snapshot"]["positions"][0].update(
                    settled_own_units=total, blocked_own_units=blocked)
                self.blocked(data, "insufficient free own units")

    def test_missing_or_excess_blocking_does_not_become_zero(self):
        data = fixture()
        del data["snapshot"]["positions"][0]["blocked_own_units"]
        self.blocked(data, "blocked_own_units")
        data = fixture()
        data["snapshot"]["positions"][0]["blocked_own_units"] = "101"
        self.blocked(data, "blocked units exceed")

    def test_snapshot_ownership_settlement_and_blocking_flags_are_explicit(self):
        for flag in ("verified", "no_existing_debt", "positions_exclude_borrowed_and_unsettled",
                     "settled_own_units_include_blocked"):
            for value in (None, False, "true", 1):
                with self.subTest(flag=flag, value=value):
                    data = fixture()
                    data["snapshot"][flag] = value
                    self.blocked(data, "explicitly confirm")

    def test_batch_cannot_sell_same_free_units_twice(self):
        data = fixture()
        data["orders"][0]["lots"] = 4
        data["orders"][0]["position_limit"]["own_position_max_lots"] = 10
        second_order(data, 5)
        self.blocked(data, "insufficient free own units")

    def test_batch_within_both_shared_limits_passes(self):
        data = fixture()
        data["orders"][0]["lots"] = 3
        second_order(data, 5)
        self.assertEqual(self.result(data), {
            "status": "PASS", "reasons": [], "required_units": {"test-uid": "80"}})

    def test_broker_caps_are_not_added_and_smallest_cap_limits_batch(self):
        data = fixture()
        data["snapshot"]["positions"][0]["settled_own_units"] = "1000"
        data["orders"][0]["lots"] = 3
        other = second_order(data, 2)
        other["position_limit"]["own_position_max_lots"] = 4
        self.blocked(data, "aggregate lots exceed")
        data["orders"].reverse()
        self.blocked(data, "aggregate lots exceed")

    def test_distinct_uids_with_same_ticker_remain_separate(self):
        data = fixture()
        data["orders"][0]["instrument"]["ticker"] = "SAME"
        data["snapshot"]["positions"].append({
            "uid": "other-uid", "kind": "share", "lot_size": 10,
            "settled_own_units": "10", "blocked_own_units": "0"})
        other = second_order(data, 1, uid="other-uid")
        self.assertEqual(self.result(data), {
            "status": "PASS", "reasons": [],
            "required_units": {"other-uid": "10", "test-uid": "80"}})
        other["lots"] = 2
        self.blocked(data, "insufficient free own units for other-uid")

    def test_duplicate_snapshot_uid_is_rejected_instead_of_merged(self):
        data = fixture()
        data["snapshot"]["positions"].append(copy.deepcopy(data["snapshot"]["positions"][0]))
        self.blocked(data, "duplicate position UID")

    def test_duplicate_order_id_is_rejected(self):
        data = fixture()
        data["orders"].append(copy.deepcopy(data["orders"][0]))
        self.blocked(data, "duplicate client_id")

    def test_unknown_uid_cannot_use_another_position(self):
        data = fixture()
        data["orders"][0]["instrument"]["uid"] = "unknown"
        self.blocked(data, "no confirmed own position")

    def test_account_identity_must_match_every_record(self):
        for where in ("snapshot", "order", "position_limit"):
            with self.subTest(where=where):
                data = fixture()
                target = (data["snapshot"] if where == "snapshot" else data["orders"][0]
                          if where == "order" else data["orders"][0]["position_limit"])
                target["account_id"] = "another-account"
                self.blocked(data, "account does not match")

    def test_quote_and_limit_must_match_instrument_and_price(self):
        for where in ("quote", "position_limit"):
            data = fixture()
            data["orders"][0][where]["instrument_uid"] = "another-uid"
            self.blocked(data, "instrument does not match")
        data = fixture()
        data["orders"][0]["position_limit"]["limit_price"] = "99"
        self.blocked(data, "price does not match")
        data["orders"][0]["position_limit"]["limit_price"] = "100.00"
        self.assertEqual(self.result(data)["status"], "PASS")

    def test_lot_size_must_match_snapshot_and_not_be_applied_twice(self):
        data = fixture()
        data["orders"][0]["instrument"]["lot_size"] = 1
        self.blocked(data, "lot_size does not match")
        data = fixture()
        data["orders"][0]["lots"] = 1
        self.assertEqual(self.result(data)["required_units"], {"test-uid": "10"})

    def test_fractional_free_units_cannot_fund_a_whole_lot(self):
        data = fixture()
        data["snapshot"]["positions"][0].update(settled_own_units="10", blocked_own_units="0.000000001")
        data["orders"][0]["lots"] = 1
        self.blocked(data, "insufficient free own units")

    def test_large_values_preserve_nanounit_blocking(self):
        data = fixture()
        data["snapshot"]["positions"][0].update(
            lot_size=1, settled_own_units="99999999999999999999.000000001",
            blocked_own_units="0.000000002")
        data["orders"][0].update(lots=99999999999999999999)
        data["orders"][0]["instrument"]["lot_size"] = 1
        data["orders"][0]["position_limit"]["own_position_max_lots"] = 99999999999999999999
        self.blocked(data, "insufficient free own units")

    def test_lots_and_lot_sizes_require_bounded_positive_integers(self):
        for value in (None, True, False, "1", 1.0, 1.5, 0, -1, 10 ** 20, [], {}):
            for where in ("lots", "position_lot_size", "instrument_lot_size"):
                with self.subTest(where=where, value=value):
                    data = fixture()
                    if where == "lots":
                        data["orders"][0]["lots"] = value
                    elif where == "position_lot_size":
                        data["snapshot"]["positions"][0]["lot_size"] = value
                    else:
                        data["orders"][0]["instrument"]["lot_size"] = value
                    self.blocked(data, "integer")

    def test_own_position_cap_required_and_margin_caps_rejected(self):
        for value in (None, True, "8", 8.0, -1, 10 ** 20, [], {}):
            data = fixture()
            data["orders"][0]["position_limit"]["own_position_max_lots"] = value
            self.blocked(data, "own_position_max_lots")
        data = fixture()
        data["orders"][0]["position_limit"]["own_position_max_lots"] = 0
        self.blocked(data, "lots exceed confirmed")
        for source in (None, "sell_margin_limits", "sellMarginLimits", "buy_limits"):
            data = fixture()
            data["orders"][0]["position_limit"]["source"] = source
            self.blocked(data, "sell_limits source")
        data = fixture()
        data["orders"][0]["position_limit"]["verified"] = False
        self.blocked(data, "explicitly confirm verified")

    def test_unsupported_position_types_and_instrument_mismatch_block(self):
        for kind in ("unknown", "future", "option", "spot_currency", None, [], {}):
            data = fixture()
            data["snapshot"]["positions"][0]["kind"] = kind
            self.blocked(data)
        data = fixture()
        data["orders"][0]["instrument"]["kind"] = "bond"
        self.blocked(data, "kind does not match")
        for kind in ("bond", "fund"):
            data = fixture()
            data["snapshot"]["positions"][0]["kind"] = kind
            data["orders"][0]["instrument"]["kind"] = kind
            self.assertEqual(self.result(data)["status"], "PASS")

    def test_unknown_eligibility_borrowed_or_leveraged_exposure_blocks(self):
        for key in ("available", "eligibility_verified"):
            for value in (None, False, "true", 1):
                data = fixture()
                data["orders"][0]["instrument"][key] = value
                self.blocked(data, "instrument must confirm")
        for value in (None, True, 0):
            data = fixture()
            data["orders"][0]["instrument"]["leveraged"] = value
            self.blocked(data, "leveraged or unknown")
        for value in (None, "margin", "borrowed", "fully_paid_cash"):
            data = fixture()
            data["orders"][0]["instrument"]["financing"] = value
            self.blocked(data, "own-position financing")

    def test_buy_market_and_mixed_batches_block(self):
        data = fixture()
        data["orders"][0]["side"] = "BUY"
        self.blocked(data, "only sales")
        data = fixture()
        data["orders"][0]["order_type"] = "MARKET"
        self.blocked(data, "limit price")
        data = fixture()
        data["orders"][0]["lots"] = 1
        second_order(data, 1)["side"] = "BUY"
        self.blocked(data, "only sales")

    def test_invalid_quantities_and_prices_block_without_float_conversion(self):
        for key in ("settled_own_units", "blocked_own_units"):
            for value in (None, 100, 100.0, True, "-1", "NaN", "Infinity", "1e2", "0.0000000001"):
                data = fixture()
                data["snapshot"]["positions"][0][key] = value
                self.blocked(data)
        for value in (None, 100.0, "0", "-1", "NaN", "Infinity", "1e2"):
            data = fixture()
            data["orders"][0]["limit_price"] = value
            self.blocked(data, "limit_price")

    def test_freshness_boundaries_and_timezones(self):
        data = fixture()
        data["snapshot"]["as_of"] = stamp(-60)
        data["orders"][0]["quote"]["as_of"] = stamp(-30)
        data["orders"][0]["position_limit"]["as_of"] = stamp(-30)
        self.assertEqual(self.result(data)["status"], "PASS")
        for where, age in (("snapshot", -61), ("quote", -31), ("position_limit", -31)):
            data = fixture()
            target = data["snapshot"] if where == "snapshot" else data["orders"][0][where]
            target["as_of"] = stamp(age)
            self.blocked(data, "stale or future-dated")
        for where in ("snapshot", "quote", "position_limit"):
            for field, value in (("as_of", stamp(1)), ("expires_at", stamp(0)),
                                 ("as_of", "2026-11-08T00:00:00"), ("as_of", "not-a-date")):
                data = fixture()
                target = data["snapshot"] if where == "snapshot" else data["orders"][0][where]
                target[field] = value
                self.blocked(data)
        self.assertEqual(guard.validate_plan(fixture(), NOW.replace(tzinfo=None))["status"], "BLOCK")

    def test_malformed_objects_and_empty_collections_return_block(self):
        for value in (None, [], "invalid", 1, True):
            self.blocked(value)
        for key in ("snapshot", "orders"):
            for value in (None, [], {}, "invalid", True):
                data = fixture()
                data[key] = value
                self.blocked(data)
        for value in (None, [], {}, [None], [True], ["invalid"]):
            data = fixture()
            data["snapshot"]["positions"] = value
            self.blocked(data)
        for key in ("instrument", "quote", "position_limit"):
            for value in (None, [], "invalid", True):
                data = fixture()
                data["orders"][0][key] = value
                self.blocked(data)
        data = fixture()
        data["orders"] = [None]
        self.blocked(data)

    def test_validation_does_not_mutate_plan(self):
        data = fixture()
        original = copy.deepcopy(data)
        self.result(data)
        self.assertEqual(data, original)


class SellGuardCliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(MODULE), *map(str, args)],
                              capture_output=True, text=True, check=False, timeout=10)

    def test_help_describes_nonexecuting_scope(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No network, credentials, short sales", result.stdout)
        self.assertIn("PASS only checks", result.stdout)

    def test_missing_file_returns_block_json_and_exit_two(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(Path(directory) / "absent.json")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "BLOCK")
        self.assertEqual(result.stderr, "")

    def test_malformed_json_returns_block_json_and_exit_two(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "malformed.json"
            source.write_text('{"orders": [', encoding="utf-8")
            result = self.run_cli(source)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "BLOCK")
        self.assertEqual(result.stderr, "")

    def test_current_valid_input_returns_pass_json_and_exit_zero(self):
        data = fixture()
        now = datetime.now(timezone.utc)
        times = {"as_of": (now - timedelta(seconds=1)).isoformat(),
                 "expires_at": (now + timedelta(seconds=20)).isoformat()}
        data["snapshot"].update(times)
        data["orders"][0]["quote"].update(times)
        data["orders"][0]["position_limit"].update(times)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.json"
            source.write_text(json.dumps(data), encoding="utf-8")
            result = self.run_cli(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "status": "PASS", "reasons": [], "required_units": {"test-uid": "80"}})
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
