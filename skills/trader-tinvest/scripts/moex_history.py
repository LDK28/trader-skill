#!/usr/bin/env python3
"""Prepare an unverified research backtest from MOEX ISS TQBR daily data.

Usage: moex_history.py STUDY_JSON OUTPUT_DIR [--replay CAPTURES_JSON]
Default: public HTTPS GET to iss.moex.com, no authentication or broker access.
Replay: offline conversion of previously captured exact response strings.
Only a declared educational_demo purpose is supported. MOEX public ISS terms
do not establish rights to production/profit-oriented use or redistribution.

Study: purpose:"educational_demo", instrument, board:"TQBR", from, till,
holdout_start_date, currency:"rub", historical_lot_size_assumption (integer),
historical_tick_assumption (decimal string), cash_tick, config (backtest
config), corporate_actions:"none_declared"|"cash_dividends", constant_lot_and_tick:true,
bar_basis:"moex_trading_day". Metadata are explicit research assumptions;
current lot/tick are never substituted for historical values.
cash_dividends preserves explicit dividends for engine validation. Optional
hourly_repairs dates request hourly captures and explicit reconstruction;
the reconstructed six fields must still match history, and raw is retained.

Fetch candles AND history to an empty final page, retain raw response hashes
and request/receive times. Parse numeric tokens with Decimal. ISS equities
candle volume is SHARES, converted to lots using the declared historical lot.
Reconcile all OHLC, volume and turnover by grouping calendar candles into the
matching history trading-day rows (TRADINGSESSION=3). Additional candles must
be weekend sessions; unexplained differences block conversion. No data are
dropped to make the comparison pass. Bucket times are interpreted as Moscow
labels, not actual trade execution timestamps. Historical events, lot/tick,
rights of use and independence remain unverified. See moex-history.md.

Writes source_captures.json, provenance.json, data_quality.json and
backtest_input.json locally.
Keep output outside published source, e.g. .local/study. No real operations.
"""
import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import hashlib
from http.client import HTTPException
import json
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo("Europe/Moscow")
FIELDS = ("open", "high", "low", "close", "volume", "value")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(_):
    raise ValueError("nonfinite JSON number")


def read_json(raw, decimals=False):
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant,
                      **({"parse_float": Decimal} if decimals else {}))


def spec(study):
    require(isinstance(study, dict), "study must be an object")
    require(study.get("purpose") == "educational_demo", "only educational_demo is supported")
    require(study.get("board") == "TQBR" and study.get("currency") == "rub", "only TQBR/RUB is supported")
    secid = study.get("instrument")
    require(isinstance(secid, str) and re.fullmatch(r"[A-Z0-9._-]{1,20}", secid) is not None, "invalid SECID")
    first, last = date.fromisoformat(study["from"]), date.fromisoformat(study["till"])
    require(first <= last and (last - first).days <= 3660, "invalid or excessive study range")
    require(type(study.get("historical_lot_size_assumption")) is int and
            0 < study["historical_lot_size_assumption"] < 2 ** 31, "historical lot assumption is required")
    for key in ("historical_tick_assumption", "cash_tick"):
        require(isinstance(study.get(key), str) and re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,9})?", study[key]) is not None
                and Decimal(study[key]) > 0, "positive decimal tick assumption is required")
    require(study.get("corporate_actions") in ("none_declared", "cash_dividends") and study.get("constant_lot_and_tick") is True,
            "explicit corporate-action/lot assumptions required")
    if study.get("corporate_actions") == "cash_dividends":
        require(isinstance(study.get("dividends"), list) and bool(study["dividends"]), "dividend events required")
    require(study.get("bar_basis") == "moex_trading_day", "explicit trading-day basis required")
    require(isinstance(study.get("config"), dict), "backtest config required")
    date.fromisoformat(study["holdout_start_date"])
    repairs = study.get("hourly_repairs", [])
    require(isinstance(repairs, list) and len(repairs) <= 30 and len(set(repairs)) == len(repairs), "invalid repair dates")
    require(all(first <= date.fromisoformat(day) <= last for day in repairs), "repair date outside study")
    return first, last


def series_names(study):
    return ("candles", "history", *("hourly:" + day for day in study.get("hourly_repairs", [])))


def source_url(study, series, offset):
    hourly = series.startswith("hourly:")
    path = ("https://iss.moex.com/iss/" + ("history/" if series == "history" else "") +
            "engines/stock/markets/shares/boards/TQBR/securities/" + study["instrument"] +
            ("/candles.json" if series == "candles" or hourly else ".json"))
    query = {"from": series.split(":", 1)[1] if hourly else study["from"],
             "till": series.split(":", 1)[1] if hourly else study["till"],
             "start": offset, "iss.meta": "off", "iss.only": "candles" if hourly else series}
    if series == "candles" or hourly:
        query["interval"] = 60 if hourly else 24
    return path + "?" + urlencode(query)


def table(raw, name):
    payload = read_json(raw, decimals=True)
    require(isinstance(payload, dict), "source payload must be an object")
    block = payload.get(name)
    require(isinstance(block, dict) and isinstance(block.get("columns"), list) and isinstance(block.get("data"), list),
            "missing source table")
    columns = block["columns"]
    require(all(isinstance(c, str) for c in columns) and len(set(columns)) == len(columns), "duplicate/invalid source columns")
    result = []
    for row in block["data"]:
        require(isinstance(row, list) and len(row) == len(columns), "invalid source row")
        result.append(dict(zip(columns, row)))
    return result


def fetch(study):
    spec(study)
    captures = []
    for series in series_names(study):
        offset = 0
        for _ in range(50):
            url = source_url(study, series, offset)
            requested = datetime.now(timezone.utc).isoformat()
            with urlopen(Request(url, headers={"User-Agent": "trader-tinvest-educational-research/1.0"}), timeout=25) as response:
                raw_bytes = response.read(10000001)
            require(len(raw_bytes) <= 10000000, "oversized source response")
            raw = raw_bytes.decode("utf-8")
            captures.append({"series": series, "url": url, "requested_at": requested,
                             "received_at": datetime.now(timezone.utc).isoformat(),
                             "sha256": hashlib.sha256(raw_bytes).hexdigest(), "raw": raw})
            rows = table(raw, "candles" if series.startswith("hourly:") else series)
            if not rows:
                break
            offset += len(rows)
            require(offset <= 10000, "too many history rows")
        else:
            raise ValueError("pagination did not terminate")
    return captures


def amount(value):
    require(type(value) in (int, Decimal), "source numeric token required")
    result = Decimal(value)
    require(result.is_finite() and result >= 0, "nonnegative finite source number required")
    return result


def stamp(value):
    require(isinstance(value, str), "capture timestamp required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None and result.utcoffset() is not None, "capture timezone required")
    return result


def prepare(study, captures):
    """Reconcile captured source tables; raises ValueError on any mismatch."""
    with localcontext() as context:
        context.prec = 80
        return _prepare(study, captures)


def _prepare(study, captures):
    first, last = spec(study)
    require(isinstance(captures, list) and 2 <= len(captures) <= 100, "bounded captures required")
    collected, provenance, earliest = {}, [], []
    for series in series_names(study):
        pages = [capture for capture in captures if isinstance(capture, dict) and capture.get("series") == series]
        require(bool(pages), "missing series")
        rows, offset, finished = [], 0, False
        for page in pages:
            require(not finished and page.get("url") == source_url(study, series, offset), "page URL/offset mismatch")
            start, end = stamp(page.get("requested_at")), stamp(page.get("received_at"))
            require(start <= end and last < start.astimezone(MOSCOW).date(), "unfinished day or reversed capture time")
            raw = page.get("raw")
            require(isinstance(raw, str) and hashlib.sha256(raw.encode("utf-8")).hexdigest() == page.get("sha256"),
                    "source hash mismatch")
            page_rows = table(raw, "candles" if series.startswith("hourly:") else series)
            require(len(rows) + len(page_rows) <= 10000, "too many source rows")
            rows.extend(page_rows)
            offset += len(page_rows)
            finished = not page_rows
            earliest.append(start)
            provenance.append({key: page[key] for key in ("series", "url", "requested_at", "received_at", "sha256")})
        require(finished and bool(rows), "missing empty final page or empty history")
        collected[series] = rows
    require(len(provenance) == len(captures), "unsupported capture series")

    source_repairs = []
    for day in study.get("hourly_repairs", []):
        hours = collected["hourly:" + day]
        previous_end = None
        for hour in hours:
            begin, end = (datetime.strptime(hour[key], "%Y-%m-%d %H:%M:%S") for key in ("begin", "end"))
            require(begin.date().isoformat() == day and end.date().isoformat() == day and begin <= end and
                    (previous_end is None or begin > previous_end), "invalid hourly repair chronology")
            previous_end = end
            values = {key: amount(hour[key]) for key in FIELDS}
            require(values["low"] > 0 and values["low"] <= min(values["open"], values["close"]) and
                    values["high"] >= max(values["open"], values["close"]), "invalid hourly repair OHLC")
            require(values["volume"] > 0 and values["volume"] == values["volume"].to_integral_value() and
                    values["low"] * values["volume"] <= values["value"] <= values["high"] * values["volume"],
                    "invalid hourly repair volume/turnover")
        merged = {"open": amount(hours[0]["open"]), "close": amount(hours[-1]["close"]),
                  "high": max(amount(h["high"]) for h in hours), "low": min(amount(h["low"]) for h in hours),
                  "volume": sum((amount(h["volume"]) for h in hours), Decimal(0)),
                  "value": sum((amount(h["value"]) for h in hours), Decimal(0)),
                  "begin": day + " 00:00:00", "end": hours[-1]["end"]}
        indexes = [i for i, row in enumerate(collected["candles"]) if row["begin"][:10] == day]
        require(len(indexes) == 1, "repair must replace exactly one existing daily candle")
        old = collected["candles"][indexes[0]]
        source_repairs.append({"date": day, "hourly_rows": len(hours), "changed_fields":
                               [key for key in FIELDS if amount(old[key]) != merged[key]]})
        collected["candles"][indexes[0]] = merged

    candles, prev = [], None
    for row in collected["candles"]:
        begin = datetime.strptime(row["begin"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MOSCOW)
        end = datetime.strptime(row["end"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MOSCOW)
        day = begin.date()
        require(first <= day <= last and begin < end and end.date() == day and (prev is None or day > prev),
                "unordered, duplicate or out-of-range calendar candle")
        require(end < min(earliest), "candle is not complete at capture")
        prev = day
        values = {key: amount(row[key]) for key in FIELDS}
        require(values["low"] > 0 and values["low"] <= min(values["open"], values["close"]) and
                values["high"] >= max(values["open"], values["close"]), "invalid candle OHLC")
        require(values["volume"] > 0 and values["volume"] == values["volume"].to_integral_value(), "invalid share volume")
        require(values["low"] * values["volume"] <= values["value"] <= values["high"] * values["volume"],
                "turnover/volume price-unit mismatch")
        candles.append(dict(values, day=day, begin=begin, end=end))

    bars, dates, groups, cursor, previous_day = [], [], [], 0, None
    lot, tick = study["historical_lot_size_assumption"], Decimal(study["historical_tick_assumption"])
    for row in collected["history"]:
        day = date.fromisoformat(row["TRADEDATE"])
        require(row.get("BOARDID") == "TQBR" and row.get("SECID") == study["instrument"] and
                row.get("TRADINGSESSION") == 3 and row.get("CURRENCYID") in ("SUR", "RUB"), "history identity/session/currency mismatch")
        require(first <= day <= last and (previous_day is None or day > previous_day), "unordered or duplicate trading day")
        previous_day = day
        group = []
        while cursor < len(candles) and candles[cursor]["day"] <= day:
            group.append(candles[cursor]); cursor += 1
        require(bool(group) and group[-1]["day"] == day, "missing trading-day candle")
        require(all(c["day"].weekday() >= 5 for c in group[:-1]), "unexplained non-weekend source grouping")
        merged = {"open": group[0]["open"], "close": group[-1]["close"],
                  "high": max(c["high"] for c in group), "low": min(c["low"] for c in group),
                  "volume": sum((c["volume"] for c in group), Decimal(0)),
                  "value": sum((c["value"] for c in group), Decimal(0))}
        require(all(merged[key] == amount(row[key.upper()]) for key in FIELDS), "candles/history OHLCV/turnover mismatch")
        require(merged["volume"] % lot == 0, "share volume is not a whole number of declared lots")
        require(all(merged[key] % tick == 0 for key in ("open", "high", "low", "close")), "OHLC inconsistent with declared tick")
        bars.append({"open_time": group[0]["begin"].isoformat(), "close_time": group[-1]["end"].isoformat(),
                     **{key: format(merged[key], "f") for key in ("open", "high", "low", "close")},
                     "volume_lots": int(merged["volume"] / lot), "is_complete": True})
        dates.append(day.isoformat())
        if len(group) > 1:
            groups.append({"trade_date": day.isoformat(), "calendar_dates": [c["day"].isoformat() for c in group]})
    require(cursor == len(candles), "unmatched calendar candles")
    require(study["holdout_start_date"] in dates, "holdout trading date is missing")
    split = dates.index(study["holdout_start_date"])
    payload = {"instrument": {"uid": "moex:TQBR:" + study["instrument"], "kind": "share", "currency": "rub",
                               "lot_size": lot, "price_tick": study["historical_tick_assumption"], "cash_tick": study["cash_tick"]},
               "dataset": {"kind": "historical_unverified", "source": "MOEX ISS TQBR reconciled trading-day OHLCV",
                           "as_of": max(stamp(p["received_at"]) for p in provenance).isoformat(),
                           "corporate_actions": study["corporate_actions"], "constant_lot_and_tick": True},
               "config": study["config"], "holdout_start": bars[split]["open_time"], "bars": bars}
    if study["corporate_actions"] == "cash_dividends":
        payload["dividends"] = study["dividends"]
    grouped_dates = {day for group in groups for day in group["calendar_dates"]}
    repaired_direct_days = sum(repair["date"] not in grouped_dates for repair in source_repairs)
    quality = {"status": "RECONSTRUCTED_UNVERIFIED" if source_repairs else "RECONCILED_UNVERIFIED", "ready_for_validators": False,
               "calendar_candles": len(candles), "trading_days": len(bars),
               "direct_matching_days": len(bars) - len(groups) - repaired_direct_days, "weekend_groups": groups,
               "unexplained_mismatches": 0, "trading_dates": dates,
               "source_repairs": source_repairs,
               "source_timezone_interpretation": "Europe/Moscow day-bucket labels, not actual fill times",
               "unverified": ["historical_lot_tick_continuity", "complete_corporate_action_register",
                              "independent_provider_and_official_session_calendar", "production_data_rights"]}
    return {"backtest_input": payload, "data_quality": quality, "provenance": provenance}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("study", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--replay", type=Path, help="saved source_captures.json; no HTTP")
    args = parser.parse_args()
    try:
        study = read_json(args.study.read_text(encoding="utf-8"))
        captures = read_json(args.replay.read_text(encoding="utf-8")) if args.replay else fetch(study)
        result = prepare(study, captures)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "source_captures.json").write_text(json.dumps(captures, ensure_ascii=False, indent=2) + "\n")
        for name, value in result.items():
            (args.output_dir / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": "PREPARED_UNVERIFIED", "ready_for_validators": False,
                          "trading_days": result["data_quality"]["trading_days"]}))
        return 0
    except (ValueError, TypeError, KeyError, ArithmeticError, OSError, HTTPException, RecursionError):
        print(json.dumps({"status": "BLOCK", "ready_for_validators": False,
                          "reason": "Invalid, inconsistent, incomplete or unavailable MOEX research inputs"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
