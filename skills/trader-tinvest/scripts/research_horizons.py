#!/usr/bin/env python3
"""Summarize a computed backtest holdout; never estimates future returns.

Input is a SIMULATED backtest result with periods.holdout containing
initial_cash, final_equity, equity_curve[{close_time,equity}], closed_episodes,
and max_close_drawdown_fraction. Money/equity values are decimal strings.

Each curve CLOSE starts a historical window. Its end is the first available
close on/after the same calendar day 1 or 2 months later (clamped to month-end).
Dates are consistently interpreted in Europe/Moscow. Incomplete end windows
are excluded. Windows inherit the already simulated portfolio, costs and
cashflows: there are no new trades, resets, parameters or engine imports.
The period return uses initial_cash, not a reset or an annualized month rate.

The historical fraction of overlapping windows reaching >=10% is NOT an
independent-trial frequency or future probability. No annual forecast,
validation, trading permission, network, credentials or file writes.
CLI: research_horizons.py RESULT_JSON; summary exit 0, invalid input exit 2.
"""
import argparse
from bisect import bisect_left
import calendar
from datetime import date, datetime
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo("Europe/Moscow")
TARGET = Decimal("0.10")
DISPLAY_TICK = Decimal("0.000000001")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def amount(value, name):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,60}(?:\.[0-9]{1,30})?", value) is not None,
            name + " must be a bounded positive decimal string")
    result = Decimal(value)
    require(result > 0, name + " must be positive")
    return result


def frac(value):
    return format(value.quantize(DISPLAY_TICK), "f")


def add_months(day, count):
    month_index = day.year * 12 + day.month - 1 + count
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def drawdown(values, initial):
    peak, worst = initial, Decimal(0)
    for value in values:
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak)
    return worst


def windows(dates, values, months):
    rows, returns, drawdowns = [], [], []
    for index, day in enumerate(dates):
        try:
            target = add_months(day, months)
        except ValueError:  # Anniversary beyond the representable final year cannot fit.
            continue
        end = bisect_left(dates, target, lo=index + 1)
        if end == len(dates):
            continue
        change = values[end] / values[index] - 1
        dd = drawdown(values[index:end + 1], values[index])
        returns.append(change); drawdowns.append(dd)
        rows.append({"start": day.isoformat(), "anniversary": target.isoformat(),
                     "end": dates[end].isoformat(), "start_equity": format(values[index], "f"),
                     "end_equity": format(values[end], "f"), "return_fraction": frac(change),
                     "max_close_drawdown_fraction": frac(dd), "target_hit": change >= TARGET})
    count = len(rows)
    if not count:
        return {"count": 0, "target_hit_count": 0, "target_hit_fraction": None,
                "losing_count": 0, "losing_fraction": None, "median_return_fraction": None,
                "worst_return_fraction": None, "best_return_fraction": None,
                "worst_window_drawdown_fraction": None, "best_window": None, "worst_window": None,
                "windows": []}
    ordered = sorted(returns)
    middle = count // 2
    median = ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    hits, losses = sum(value >= TARGET for value in returns), sum(value < 0 for value in returns)
    return {"count": count, "target_hit_count": hits, "target_hit_fraction": frac(Decimal(hits) / count),
            "losing_count": losses, "losing_fraction": frac(Decimal(losses) / count),
            "median_return_fraction": frac(median), "worst_return_fraction": frac(min(returns)),
            "best_return_fraction": frac(max(returns)), "worst_window_drawdown_fraction": frac(max(drawdowns)),
            "best_window": rows[max(range(count), key=returns.__getitem__)],
            "worst_window": rows[min(range(count), key=returns.__getitem__)], "windows": rows}


def summarize(payload):
    """Report historical holdout outcomes; observations never become a forecast."""
    try:
        with localcontext() as context:
            context.prec = 120
            require(isinstance(payload, dict) and payload.get("status") == "SIMULATED", "SIMULATED result required")
            periods = payload.get("periods")
            require(isinstance(periods, dict) and isinstance(periods.get("holdout"), dict), "holdout required")
            period = periods["holdout"]
            initial, final = amount(period.get("initial_cash"), "initial_cash"), amount(period.get("final_equity"), "final_equity")
            curve = period.get("equity_curve")
            require(isinstance(curve, list) and 2 <= len(curve) <= 10000, "bounded daily curve required")
            dates, values, stamps = [], [], []
            for row in curve:
                require(isinstance(row, dict) and isinstance(row.get("close_time"), str), "close_time required")
                stamp = datetime.fromisoformat(row["close_time"].replace("Z", "+00:00"))
                require(stamp.tzinfo is not None and stamp.utcoffset() is not None, "timezone required")
                day = stamp.astimezone(MOSCOW).date()
                require(not stamps or stamp > stamps[-1] and day > dates[-1], "dates must be strictly increasing and unique")
                stamps.append(stamp); dates.append(day); values.append(amount(row.get("equity"), "curve equity"))
            require(final == values[-1], "final equity does not match curve")
            dd = drawdown(values, initial)
            declared_dd = period.get("max_close_drawdown_fraction")
            require(isinstance(declared_dd, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]{1,30})?", declared_dd) is not None,
                    "declared drawdown required")
            require(Decimal(declared_dd) < 1 and frac(Decimal(declared_dd)) == frac(dd), "drawdown does not match curve")
            closed = period.get("closed_episodes")
            require(isinstance(closed, list) and len(closed) <= 10000, "closed episode list required")
            source_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                                    allow_nan=False).encode("utf-8")).hexdigest()
            return {"status": "HORIZONS_SUMMARIZED", "strategy_validated": False,
                    "ready_for_validators": False, "forecast_available": False, "windows_independent": False,
                    "window_frequencies_are_future_probabilities": False, "calendar_timezone": "Europe/Moscow",
                    "target_return_fraction": "0.10", "source_result_sha256": source_hash,
                    "period": {"first_close": stamps[0].isoformat(), "last_close": stamps[-1].isoformat(),
                               "calendar_days_elapsed": (dates[-1] - dates[0]).days,
                               "initial_cash": format(initial, "f"), "final_equity": format(final, "f"),
                               "historical_pnl_before_tax": format(final - initial, "f"),
                               "return_fraction": frac(final / initial - 1), "annualized": False,
                               "max_close_drawdown_fraction": frac(dd), "closed_episodes": len(closed)},
                    "horizons": {str(months) + "_calendar_months": windows(dates, values, months) for months in (1, 2)}}
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError, OverflowError):
        return {"status": "BLOCK", "strategy_validated": False, "ready_for_validators": False,
                "forecast_available": False, "reason": "Invalid or inconsistent simulated holdout curve"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(_):
    raise ValueError("nonfinite JSON number")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                             parse_constant=invalid_constant)
        result = summarize(payload)
    except (OSError, ValueError, RecursionError):
        result = summarize(None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "HORIZONS_SUMMARIZED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
