import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
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


normalizer = load("normalize_broker")
NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


def stamp(seconds):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def quote(units, nano=0):
    return {"units": units, "nano": nano}


def fixture():
    def capture(ident, method, request, response):
        return {"capture_id": ident, "method": method, "requested_at": stamp(-2),
                "received_at": stamp(-1), "request": request, "response": response}

    return {
        "profile": "tinvest-rest-v1", "account_id": "synthetic-account",
        "captures": [
            capture("positions", "GetPositions", {"accountId": "synthetic-account"}, {
                "accountId": "synthetic-account", "limitsLoadingInProgress": False,
                "money": [dict(quote("1000", 1), currency="rub")],
                "blocked": [dict(quote("100"), currency="rub")],
                "futures": [], "options": [],
                "securities": [{"instrumentUid": "synthetic-uid", "positionUid": "synthetic-position",
                                "instrumentType": "share", "balance": "83", "blocked": "20",
                                "exchangeBlocked": False}],
            }),
            capture("instrument", "GetInstrumentBy",
                    {"idType": "INSTRUMENT_ID_TYPE_UID", "id": "synthetic-uid"}, {
                        "instrument": {"uid": "synthetic-uid", "positionUid": "synthetic-position",
                                       "instrumentType": "share", "lot": 10, "currency": "rub",
                                       "buyAvailableFlag": True, "sellAvailableFlag": True,
                                       "apiTradeAvailableFlag": True},
                    }),
            capture("limits", "GetMaxLots", {"accountId": "synthetic-account",
                                              "instrumentId": "synthetic-uid", "price": quote("100")}, {
                "currency": "rub", "buyLimits": {"buyMaxLots": "3", "buyMoneyAmount": quote("300", 1)},
                "sellLimits": {"sellMaxLots": "8"},
                "buyMarginLimits": {"buyMaxLots": "999", "buyMoneyAmount": quote("99999")},
                "sellMarginLimits": {"sellMaxLots": "999"},
            }),
        ],
    }


class NormalizeBrokerTests(unittest.TestCase):
    def result(self, data, now=NOW):
        result = normalizer.normalize(data, now)
        self.assertIs(result["ready_for_validators"], False)
        return result

    def blocked(self, data):
        result = self.result(data)
        self.assertEqual(result["status"], "BLOCK", result)
        self.assertNotIn("observations", result)
        self.assertTrue(result.get("reason"), result)

    def test_reported_securities_are_units_and_balance_is_already_unblocked(self):
        result = self.result(fixture())
        self.assertEqual(result["status"], "NORMALIZED_OBSERVATIONS")
        position = result["observations"]["positions"][0]
        self.assertEqual(position["reported_unblocked_units"], "83")
        self.assertEqual(position["reported_blocked_units"], "20")
        self.assertEqual(position["reported_total_units"], "103")
        self.assertEqual(position["reported_whole_unblocked_lots"], 8)
        self.assertEqual(position["reported_unblocked_remainder_units"], "3")

    def test_cash_arrays_remain_separate_not_a_spendable_budget(self):
        observations = self.result(fixture())["observations"]
        self.assertEqual(observations["reported_money"], {"rub": "1000.000000001"})
        self.assertEqual(observations["reported_blocked_money"], {"rub": "100"})
        self.assertNotIn("spendable_own_cash", observations)

    def test_only_own_limits_are_reported_and_not_margin_values(self):
        limit = self.result(fixture())["observations"]["limits"][0]
        self.assertEqual(limit["reported_own_buy_max_lots"], 3)
        self.assertEqual(limit["reported_own_sell_max_lots"], 8)
        self.assertEqual(limit["reported_instrument_buy_money"], "300.000000001")
        for key in ("buyLimits", "sellLimits"):
            data = fixture()
            del data["captures"][2]["response"][key]
            self.blocked(data)

    def test_missing_risk_evidence_is_not_manufactured(self):
        result = self.result(fixture())
        prohibited = {"snapshot", "orders", "verified", "no_existing_debt", "settled_own_units",
                      "positions_exclude_borrowed_and_unsettled", "cashflow_verified", "immutable",
                      "leveraged", "spendable_own_cash"}

        def check_keys(value):
            if isinstance(value, dict):
                self.assertFalse(prohibited.intersection(value), value)
                for item in value.values():
                    check_keys(item)
            elif isinstance(value, list):
                for item in value:
                    check_keys(item)

        check_keys(result)
        self.assertIn("OWNERSHIP_AND_SETTLEMENT", result["missing_checks"])
        self.assertIn("DEBT_AND_OTHER_OBLIGATIONS", result["missing_checks"])
        for value in (result, result["observations"]):
            self.assertEqual(load("cash_guard").validate_plan(value, NOW)["status"], "BLOCK")
            self.assertEqual(load("sell_guard").validate_plan(value, NOW)["status"], "BLOCK")
            self.assertEqual(load("daily_loss").evaluate(value)["status"], "INSUFFICIENT_DATA")

    def test_fund_mapping_and_blocking_flags_do_not_prove_eligibility(self):
        data = fixture()
        data["captures"][0]["response"]["securities"][0].update(
            instrumentType="etf", exchangeBlocked=True)
        instrument = data["captures"][1]["response"]["instrument"]
        instrument.update(instrumentType="etf", sellAvailableFlag=False, apiTradeAvailableFlag=False)
        result = self.result(data)
        self.assertEqual(result["status"], "NORMALIZED_OBSERVATIONS")
        self.assertEqual(result["observations"]["instruments"][0]["kind"], "fund")
        self.assertIs(result["observations"]["positions"][0]["exchange_blocked"], True)
        self.assertNotIn("eligibility_verified", result["observations"]["instruments"][0])

    def test_money_precision_is_exact_even_under_small_callers_decimal_context(self):
        data = fixture()
        data["captures"][0]["response"]["money"][0].update(
            units="9223372036854775807", nano=999999999)
        with localcontext() as context:
            context.prec = 5
            result = self.result(data)
        self.assertEqual(result["observations"]["reported_money"]["rub"],
                         "9223372036854775807.999999999")
        self.assertEqual(normalizer.quotation(quote("0", -1), "test"), Decimal("-0.000000001"))
        self.assertEqual(normalizer.quotation(quote("-2", -1), "test"), Decimal("-2.000000001"))

    def test_int64_strings_and_nano_ranges_and_signs_are_strict(self):
        bad = (quote(1), quote(True), quote("01"), quote("1e2"), quote("1.0"),
               quote("9223372036854775808"), quote("-9223372036854775809"),
               quote("1", -1), quote("-1", 1), quote("0", 1000000000),
               quote("0", -1000000000), quote("0", True), quote("0", 0.0),
               {"units": "1"}, {"nano": 0})
        for value in bad:
            with self.subTest(value=value):
                data = fixture()
                data["captures"][0]["response"]["money"] = [dict(value, currency="rub")]
                self.blocked(data)

    def test_quantity_strings_are_not_lots_and_negative_balances_block(self):
        for key in ("balance", "blocked"):
            for value in (83, 83.0, True, "83.0", "-1", "9223372036854775808"):
                with self.subTest(key=key, value=value):
                    data = fixture()
                    data["captures"][0]["response"]["securities"][0][key] = value
                    self.blocked(data)
        for key in ("money", "blocked"):
            data = fixture()
            data["captures"][0]["response"][key][0].update(units="0", nano=-1)
            self.blocked(data)

    def test_account_uid_position_uid_kind_and_currency_bindings(self):
        for index in (0, 2):
            data = fixture()
            data["captures"][index]["request"]["accountId"] = "other-account"
            self.blocked(data)
        mutations = ((0, "response", "accountId", "other-account"),
                     (1, "request", "id", "other-uid"),
                     (1, "request", "idType", "INSTRUMENT_ID_TYPE_TICKER"),
                     (2, "request", "instrumentId", "other-uid"),
                     (2, "response", "currency", "usd"))
        for index, where, key, value in mutations:
            data = fixture()
            data["captures"][index][where][key] = value
            self.blocked(data)
        for key, value in (("instrumentUid", "other-uid"), ("positionUid", "other-position"),
                           ("instrumentType", "bond")):
            data = fixture()
            data["captures"][0]["response"]["securities"][0][key] = value
            self.blocked(data)

    def test_instrument_lot_type_and_explicit_flags_are_required(self):
        for value in (None, True, "10", 10.0, 0, -1, 2 ** 31):
            data = fixture()
            data["captures"][1]["response"]["instrument"]["lot"] = value
            self.blocked(data)
        for key in ("buyAvailableFlag", "sellAvailableFlag", "apiTradeAvailableFlag"):
            for value in (None, 0, "true"):
                data = fixture()
                data["captures"][1]["response"]["instrument"][key] = value
                self.blocked(data)
        for key, value in (("instrumentType", "future"), ("currency", "rub/usd")):
            data = fixture()
            data["captures"][1]["response"]["instrument"][key] = value
            self.blocked(data)

    def test_missing_metadata_and_repeated_source_records_are_not_merged(self):
        data = fixture()
        del data["captures"][1]
        self.blocked(data)
        for index in (0, 1, 2):
            data = fixture()
            other = copy.deepcopy(data["captures"][index])
            other["capture_id"] = "second-capture"
            data["captures"].append(other)
            self.blocked(data)
        data = fixture()
        data["captures"][2]["capture_id"] = "positions"
        self.blocked(data)
        data = fixture()
        securities = data["captures"][0]["response"]["securities"]
        securities.append(copy.deepcopy(securities[0]))
        self.blocked(data)
        data = fixture()
        money = data["captures"][0]["response"]["money"]
        money.append(dict(quote("1"), currency="RUB"))
        self.blocked(data)

    def test_loading_derivatives_and_implicit_missing_defaults_block(self):
        for value in (True, None, 0):
            data = fixture()
            data["captures"][0]["response"]["limitsLoadingInProgress"] = value
            self.blocked(data)
        for key in ("futures", "options"):
            data = fixture()
            data["captures"][0]["response"][key] = [{"synthetic": True}]
            self.blocked(data)
        for key in ("money", "blocked", "securities", "futures", "options", "limitsLoadingInProgress"):
            data = fixture()
            del data["captures"][0]["response"][key]
            self.blocked(data)
        data = fixture()
        del data["captures"][0]["response"]["securities"][0]["exchangeBlocked"]
        self.blocked(data)

    def test_limits_require_explicit_price_own_caps_and_consistent_position(self):
        for value in (None, quote("0"), quote("-1"), {"value": "100"}):
            data = fixture()
            data["captures"][2]["request"]["price"] = value
            self.blocked(data)
        for key, value in (("buyMaxLots", "-1"), ("buyMaxLots", 3)):
            data = fixture()
            data["captures"][2]["response"]["buyLimits"][key] = value
            self.blocked(data)
        data = fixture()
        data["captures"][2]["response"]["sellLimits"]["sellMaxLots"] = "9"
        self.blocked(data)
        data = fixture()
        data["captures"][2]["response"]["buyLimits"]["buyMoneyAmount"] = quote("0", -1)
        self.blocked(data)

    def test_caps_for_different_prices_are_distinct_and_never_summed(self):
        data = fixture()
        other = copy.deepcopy(data["captures"][2])
        other["capture_id"] = "second-price"
        other["request"]["price"] = quote("101")
        data["captures"].append(other)
        limits = self.result(data)["observations"]["limits"]
        self.assertEqual([limit["price"] for limit in limits], ["100", "101"])
        self.assertEqual([limit["reported_own_sell_max_lots"] for limit in limits], [8, 8])

    def test_zero_limits_and_empty_securities_do_not_manufacture_positions(self):
        data = fixture()
        data["captures"][0]["response"]["securities"] = []
        data["captures"][2]["response"]["sellLimits"]["sellMaxLots"] = "0"
        result = self.result(data)
        self.assertEqual(result["status"], "NORMALIZED_OBSERVATIONS")
        self.assertEqual(result["observations"]["positions"], [])
        data["captures"].pop()
        self.assertEqual(self.result(data)["observations"]["limits"], [])

    def test_freshness_is_original_request_start_with_exact_boundaries(self):
        data = fixture()
        for capture in data["captures"]:
            capture["requested_at"] = stamp(-30 if capture["method"] == "GetMaxLots" else -60)
        self.assertEqual(self.result(data)["status"], "NORMALIZED_OBSERVATIONS")
        for index, age in ((0, -61), (1, -61), (2, -31)):
            data = fixture()
            data["captures"][index]["requested_at"] = stamp(age)
            data["captures"][index]["received_at"] = stamp(-1)
            self.blocked(data)
        self.assertEqual(self.result(fixture(), NOW + timedelta(seconds=30))["status"], "BLOCK")

    def test_future_reversed_naive_and_invalid_capture_times_block(self):
        for key, value in (("requested_at", stamp(1)), ("received_at", stamp(1)),
                           ("received_at", stamp(-3)), ("requested_at", "2026-11-08T00:00:00"),
                           ("received_at", "invalid")):
            data = fixture()
            data["captures"][0][key] = value
            self.blocked(data)
        self.assertEqual(self.result(fixture(), NOW.replace(tzinfo=None))["status"], "BLOCK")

    def test_provenance_preserves_times_hashes_and_input(self):
        data = fixture()
        original = copy.deepcopy(data)
        result = self.result(data)
        source = data["captures"][0]
        expected_hash = hashlib.sha256(json.dumps(
            {"request": source["request"], "response": source["response"]}, sort_keys=True,
            separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        self.assertEqual(result["provenance"][0]["content_sha256"], expected_hash)
        self.assertEqual(result["provenance"][0]["requested_at"], stamp(-2))
        self.assertEqual(result["provenance"][0]["received_at"], stamp(-1))
        later = self.result(data, NOW + timedelta(seconds=1))
        self.assertEqual(later["provenance"], result["provenance"])
        self.assertEqual(data, original)

    def test_unknown_profiles_methods_and_malformed_objects_block(self):
        for value in (None, [], "invalid", True):
            self.blocked(value)
        for key, value in (("profile", "tinvest-mcp"), ("profile", None), ("captures", []),
                           ("captures", [None]), ("captures", {})):
            data = fixture()
            data[key] = value
            self.blocked(data)
        for key, value in (("method", "PostOrder"), ("method", []), ("request", []),
                           ("response", None), ("capture_id", "")):
            data = fixture()
            data["captures"][0][key] = value
            self.blocked(data)

    def test_deep_unknown_response_field_returns_block(self):
        data = fixture()
        nested = 0
        for _ in range(sys.getrecursionlimit() + 100):
            nested = [nested]
        data["captures"][0]["response"]["unknown"] = nested
        self.blocked(data)


class NormalizeBrokerCliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "normalize_broker.py"), *map(str, args)],
                              capture_output=True, text=True, check=False, timeout=10)

    def run_text(self, text):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic-captures.json"
            source.write_text(text, encoding="utf-8")
            return self.run_cli(source)

    def test_help_states_observations_are_not_trade_plans(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ready_for_validators is ALWAYS false", result.stdout)
        self.assertIn("NOT an", result.stdout)

    def test_deep_json_returns_block_without_traceback(self):
        result = self.run_text("[" * 2000 + "0" + "]" * 2000)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "BLOCK")
        self.assertEqual(result.stderr, "")

    def test_invalid_files_duplicate_keys_and_nonfinite_json_fail_closed(self):
        for text in ('{"profile":', '{"profile":"tinvest-rest-v1","profile":"tinvest-rest-v1"}',
                     '{"unused":NaN}'):
            result = self.run_text(text)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "BLOCK")
            self.assertEqual(result.stderr, "")
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(Path(directory) / "missing.json")
        self.assertEqual(result.returncode, 2)
        self.assertIs(json.loads(result.stdout)["ready_for_validators"], False)

    def test_failure_diagnostic_does_not_echo_private_payload(self):
        data = fixture()
        data["account_id"] = "synthetic-private-marker"
        result = self.run_text(json.dumps(data))
        self.assertEqual(result.returncode, 2, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output.get("reason"))
        self.assertNotIn("synthetic-private-marker", result.stdout)

    def test_current_valid_capture_is_successful_conversion_only(self):
        data = fixture()
        now = datetime.now(timezone.utc)
        for capture in data["captures"]:
            capture["requested_at"] = (now - timedelta(seconds=2)).isoformat()
            capture["received_at"] = (now - timedelta(seconds=1)).isoformat()
        result = self.run_text(json.dumps(data))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "NORMALIZED_OBSERVATIONS")
        self.assertIs(output["ready_for_validators"], False)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
