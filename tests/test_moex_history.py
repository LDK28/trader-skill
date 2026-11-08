import copy
from decimal import Decimal, localcontext
import hashlib
from http.client import IncompleteRead
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1] / "skills/trader-tinvest/scripts"
SPEC = importlib.util.spec_from_file_location("moex_history", SCRIPTS / "moex_history.py")
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)


def exact_json(value):
    """Serialize artificial Decimal tokens as JSON numbers, without float."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":" + exact_json(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(exact_json(v) for v in value) + "]"
    return json.dumps(value)


def study():
    return {
        "purpose": "educational_demo", "instrument": "FAKE", "board": "TQBR",
        "from": "2025-02-25", "till": "2025-03-04", "holdout_start_date": "2025-03-03",
        "currency": "rub", "historical_lot_size_assumption": 10,
        "historical_tick_assumption": "0.5", "cash_tick": "0.01",
        "corporate_actions": "none_declared", "constant_lot_and_tick": True,
        "bar_basis": "moex_trading_day",
        "config": {"initial_cash": "1000", "entry_lookback": 1, "exit_lookback": 1,
                   "max_hold_bars": 5, "commission_bps": "30", "half_spread_bps": "5",
                   "slippage_bps": "5", "max_participation": "0.1", "settlement_lag_bars": 1},
    }


def source_rows():
    values = [
        ("2025-02-25", "10", "11", "9.5", "10.5", 100, "1010"),
        ("2025-02-26", "10.5", "11.5", "10", "11", 120, "1300"),
        ("2025-02-27", "11", "11.5", "10.5", "11", 140, "1540"),
        ("2025-02-28", "11", "12", "10", "10.5", 160, "1760"),
        ("2025-03-01", "10.5", "12", "10", "11", 20, "220"),
        ("2025-03-02", "11", "12.5", "10.5", "12", 30, "345"),
        ("2025-03-03", "12", "13", "11", "12", 200, "2400"),
        ("2025-03-04", "12", "14", "11.5", "13", 180, "2340"),
    ]
    candles = []
    for day, opening, high, low, close, volume, turnover in values:
        candles.append({"open": Decimal(opening), "high": Decimal(high), "low": Decimal(low),
                        "close": Decimal(close), "volume": volume, "value": Decimal(turnover),
                        "begin": day + " 00:00:00", "end": day + " 23:59:59"})
    trading = []
    for index in (0, 1, 2, 3, 6, 7):
        c = candles[index]
        record = {"BOARDID": "TQBR", "SECID": "FAKE", "TRADINGSESSION": 3,
                  "CURRENCYID": "SUR", "TRADEDATE": c["begin"][:10],
                  **{key.upper(): c[key] for key in history.FIELDS}}
        if index == 6:
            record.update(OPEN=Decimal("10.5"), HIGH=Decimal("13"), LOW=Decimal("10"),
                          CLOSE=Decimal("12"), VOLUME=250, VALUE=Decimal("2965"))
        trading.append(record)
    return candles, trading


def raw_table(name, rows):
    columns = (list(rows[0]) if rows else
               (list(source_rows()[0][0]) if name == "candles" else list(source_rows()[1][0])))
    return exact_json({name: {"columns": columns, "data": [[row[key] for key in columns] for row in rows]}})


def captures(spec=None, candle_rows=None, trading_rows=None, page_size=None):
    spec = spec or study()
    original_candles, original_trading = source_rows()
    rows_by_series = {"candles": original_candles if candle_rows is None else candle_rows,
                      "history": original_trading if trading_rows is None else trading_rows}
    result = []
    for series, rows in rows_by_series.items():
        chunks = [rows] if page_size is None else [rows[i:i + page_size] for i in range(0, len(rows), page_size)]
        chunks.append([])
        offset = 0
        for chunk in chunks:
            raw = raw_table(series, chunk)
            result.append({"series": series, "url": history.source_url(spec, series, offset),
                           "requested_at": "2025-03-10T10:00:00+00:00",
                           "received_at": "2025-03-10T10:00:01+00:00",
                           "raw": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest()})
            offset += len(chunk)
    return result


class MoexHistoryTests(unittest.TestCase):
    def blocked(self, spec, pages):
        with self.assertRaises((ValueError, TypeError, KeyError, ArithmeticError)):
            history.prepare(spec, pages)

    def test_share_volume_converts_to_lots_once_and_moscow_bucket_times_are_preserved(self):
        result = history.prepare(study(), captures())
        bars = result["backtest_input"]["bars"]
        self.assertEqual(bars[0]["volume_lots"], 10)
        self.assertEqual(bars[0]["open"], "10")
        self.assertEqual(bars[0]["close"], "10.5")
        self.assertEqual(bars[0]["open_time"], "2025-02-25T00:00:00+03:00")
        self.assertTrue(bars[0]["is_complete"])

    def test_weekend_group_retains_all_prices_volume_and_turnover(self):
        result = history.prepare(study(), captures())
        quality = result["data_quality"]
        self.assertEqual((quality["calendar_candles"], quality["trading_days"], quality["direct_matching_days"]), (8, 6, 5))
        self.assertEqual(quality["unexplained_mismatches"], 0)
        self.assertEqual(quality["weekend_groups"], [{"trade_date": "2025-03-03",
                         "calendar_dates": ["2025-03-01", "2025-03-02", "2025-03-03"]}])
        merged = result["backtest_input"]["bars"][4]
        self.assertEqual({key: merged[key] for key in ("open", "high", "low", "close", "volume_lots")},
                         {"open": "10.5", "high": "13", "low": "10", "close": "12", "volume_lots": 25})
        self.assertEqual(merged["open_time"], "2025-03-01T00:00:00+03:00")
        self.assertEqual(result["backtest_input"]["holdout_start"], merged["open_time"])

    def test_decimal_tokens_are_exact_under_small_caller_context(self):
        spec = study()
        spec["historical_tick_assumption"] = "0.000000001"
        candles, trading = source_rows()
        exact = Decimal("10.123456789")
        for key in ("open", "high", "low", "close"):
            candles[0][key] = exact
            trading[0][key.upper()] = exact
        candles[0]["value"] = trading[0]["VALUE"] = Decimal("1012.3456789")
        with localcontext() as context:
            context.prec = 5
            result = history.prepare(spec, captures(spec, candles, trading))
        self.assertEqual(result["backtest_input"]["bars"][0]["open"], "10.123456789")

    def test_output_is_unverified_source_scoped_and_provenance_does_not_mutate_input(self):
        spec, pages = study(), captures()
        original = copy.deepcopy((spec, pages))
        result = history.prepare(spec, pages)
        self.assertEqual((spec, pages), original)
        self.assertEqual(result["data_quality"]["status"], "RECONCILED_UNVERIFIED")
        self.assertIs(result["data_quality"]["ready_for_validators"], False)
        self.assertEqual(result["backtest_input"]["dataset"]["kind"], "historical_unverified")
        self.assertEqual(result["backtest_input"]["instrument"]["uid"], "moex:TQBR:FAKE")
        self.assertIn("historical_lot_tick_continuity", result["data_quality"]["unverified"])
        self.assertEqual(result["provenance"][0]["sha256"], pages[0]["sha256"])
        self.assertEqual(result["provenance"][0]["requested_at"], pages[0]["requested_at"])
        self.assertNotIn("raw", result["provenance"][0])

    def test_multiple_pages_reconcile_in_offset_order_until_empty_page(self):
        pages = captures(page_size=3)
        result = history.prepare(study(), pages)
        self.assertEqual(result["data_quality"]["trading_days"], 6)
        self.assertEqual(len(result["provenance"]), 7)
        self.assertIn("start=3", pages[1]["url"])

    def test_missing_terminal_repeated_pages_bad_offsets_and_extra_series_block(self):
        for mutate in (lambda p: p.pop(1), lambda p: p.append(copy.deepcopy(p[0])),
                       lambda p: p[1].update(url=p[0]["url"]),
                       lambda p: p.append(dict(p[0], series="other"))):
            pages = captures()
            mutate(pages)
            self.blocked(study(), pages)

    def test_hash_and_fixed_source_url_are_bound_to_capture(self):
        for field, value in (("sha256", "0" * 64), ("raw", "{}"),
                             ("url", "https://example.invalid/candles")):
            pages = captures()
            pages[0][field] = value
            self.blocked(study(), pages)

    def test_duplicate_reversed_and_out_of_range_calendar_dates_block(self):
        for mode in ("duplicate", "reverse", "outside"):
            candles, trading = source_rows()
            if mode == "duplicate":
                candles[1] = copy.deepcopy(candles[0])
            elif mode == "reverse":
                candles[0], candles[1] = candles[1], candles[0]
            else:
                candles[0]["begin"], candles[0]["end"] = "2025-02-24 00:00:00", "2025-02-24 23:59:59"
            self.blocked(study(), captures(candle_rows=candles, trading_rows=trading))

    def test_history_identity_session_currency_and_duplicate_dates_block(self):
        for key, value in (("BOARDID", "OTHER"), ("SECID", "OTHER"),
                           ("TRADINGSESSION", 1), ("CURRENCYID", "USD"), ("TRADEDATE", "2025-02-24")):
            candles, trading = source_rows()
            trading[0][key] = value
            self.blocked(study(), captures(candle_rows=candles, trading_rows=trading))
        candles, trading = source_rows()
        trading.insert(1, copy.deepcopy(trading[0]))
        self.blocked(study(), captures(candle_rows=candles, trading_rows=trading))

    def test_any_ohlcv_or_turnover_disagreement_blocks_instead_of_selecting_one_source(self):
        for key in ("OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "VALUE"):
            candles, trading = source_rows()
            trading[4][key] += 1
            self.blocked(study(), captures(candle_rows=candles, trading_rows=trading))

    def test_missing_unmatched_and_unexplained_weekday_grouping_block(self):
        candles, trading = source_rows()
        self.blocked(study(), captures(candle_rows=candles[:-1], trading_rows=trading))
        self.blocked(study(), captures(candle_rows=candles, trading_rows=trading[:-1]))
        # Omitting Thursday's history row must not silently merge it into Friday.
        missing = trading[:2] + trading[3:]
        self.blocked(study(), captures(candle_rows=candles, trading_rows=missing))

    def test_volume_turnover_and_declared_lot_remainders_are_checked(self):
        for key, value in (("volume", True), ("volume", Decimal("100.5")), ("volume", -1),
                           ("volume", 0), ("value", Decimal("9999999")), ("low", Decimal("0"))):
            candles, trading = source_rows()
            candles[0][key] = value
            self.blocked(study(), captures(candle_rows=candles, trading_rows=trading))
        spec = study()
        spec["historical_lot_size_assumption"] = 3
        self.blocked(spec, captures(spec))

    def test_historical_assumptions_and_profile_scope_must_be_explicit(self):
        for key, value in (("purpose", "production"), ("board", "TQTF"), ("currency", "usd"),
                           ("historical_lot_size_assumption", True), ("historical_lot_size_assumption", None),
                           ("historical_tick_assumption", 0.5), ("historical_tick_assumption", "0"),
                           ("corporate_actions", "unknown"), ("constant_lot_and_tick", False),
                           ("bar_basis", "calendar_day"), ("holdout_start_date", "2025-03-02")):
            spec = study()
            spec[key] = value
            self.blocked(spec, captures(spec))

    def test_captures_require_timezone_order_and_a_completed_requested_window(self):
        for field, value in (("requested_at", "2025-03-10T10:00:02Z"),
                             ("received_at", "2025-03-10T10:00:01"),
                             ("requested_at", "2025-03-04T10:00:00Z")):
            pages = captures()
            pages[0][field] = value
            self.blocked(study(), pages)


class MoexHistoryCliTests(unittest.TestCase):
    def test_truncated_http_response_returns_block_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            study_file = root / "study.json"
            study_file.write_text(json.dumps(study()), encoding="utf-8")
            with patch.object(sys, "argv", ["moex_history.py", str(study_file), str(root / "out")]), \
                    patch.object(history, "urlopen") as request, patch("sys.stdout", new_callable=io.StringIO) as output:
                request.return_value.__enter__.return_value.read.side_effect = IncompleteRead(b"{", 20)
                result = history.main()
            self.assertEqual(result, 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "BLOCK")
            self.assertFalse((root / "out").exists())

    def run_cli(self, spec, pages):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            study_file, capture_file, output = root / "study.json", root / "captures.json", root / "output"
            study_file.write_text(json.dumps(spec), encoding="utf-8")
            capture_file.write_text(json.dumps(pages), encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPTS / "moex_history.py"), str(study_file),
                                     str(output), "--replay", str(capture_file)], capture_output=True,
                                    text=True, timeout=10, check=False)
            files = {p.name: json.loads(p.read_text()) for p in output.glob("*.json")} if output.exists() else {}
        return result, files

    def test_replay_writes_only_unverified_research_outputs_without_http(self):
        result, files = self.run_cli(study(), captures())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"status": "PREPARED_UNVERIFIED",
                                                   "ready_for_validators": False, "trading_days": 6})
        self.assertEqual(set(files), {"source_captures.json", "backtest_input.json", "data_quality.json", "provenance.json"})
        self.assertEqual(files["backtest_input.json"]["instrument"]["lot_size"], 10)
        self.assertEqual(result.stderr, "")

    def test_failure_does_not_echo_source_payload_or_create_partial_outputs(self):
        pages = captures()
        pages[0]["sha256"] = "private-payload-do-not-echo"
        result, files = self.run_cli(study(), pages)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "BLOCK")
        self.assertNotIn("private-payload-do-not-echo", result.stdout + result.stderr)
        self.assertFalse(files)
        self.assertEqual(result.stderr, "")

    def test_nonobject_source_json_returns_block_without_traceback(self):
        pages = captures()
        pages[0]["raw"] = "[]"
        pages[0]["sha256"] = hashlib.sha256(b"[]").hexdigest()
        result, files = self.run_cli(study(), pages)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "BLOCK")
        self.assertEqual(result.stderr, "")
        self.assertFalse(files)


if __name__ == "__main__":
    unittest.main()
