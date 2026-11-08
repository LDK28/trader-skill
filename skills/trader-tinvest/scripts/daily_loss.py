"""Offline daily loss gate; never submits orders or liquidates positions.

Input JSON: account_id, currency, loss_limit_fraction (decimal strings for numbers),
baseline={account_id,currency,day,immutable:true,day_start_equity}, and
snapshot={account_id,currency,day,current_equity,net_external_cashflow,
cashflow_verified:true}. Dates are YYYY-MM-DD in the chosen review timezone.
Positive external cashflow is a deposit; negative is a withdrawal.

Reference capital = day_start_equity + net_external_cashflow. The gate compares
max(0, reference-current_equity) / reference with the specified loss threshold.
This denominator convention is an exposure-control metric, not an investment
return. Immutable/verified flags are caller assertions, not independent proof.
Keep the original daily baseline; never reset it to hide a loss. This gate cannot
guarantee a maximum actual loss or establish that an account has no borrowing.
"""
import argparse
from datetime import date
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re


def number(value, field):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(
        r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value
    ):
        raise ValueError(field + " must be a finite decimal string")
    return Decimal(value)


def evaluate(payload):
    """Return an analytical gate result; invalid or incomplete data blocks ideas."""
    try:
        if not isinstance(payload, dict):
            raise ValueError("input must be an object")
        baseline, snapshot = payload.get("baseline"), payload.get("snapshot")
        if not isinstance(baseline, dict) or not isinstance(snapshot, dict):
            raise ValueError("baseline and snapshot are required objects")
        account, currency = payload.get("account_id"), payload.get("currency")
        if not isinstance(account, str) or not account:
            raise ValueError("account_id is required")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency):
            raise ValueError("one currency code is required")
        for record in (baseline, snapshot):
            if record.get("account_id") != account or str(record.get("currency", "")).lower() != currency.lower():
                raise ValueError("account or currency mismatch")
            day = record.get("day")
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError("valid review day is required")
        if baseline["day"] != snapshot["day"]:
            raise ValueError("baseline and snapshot must belong to the same day")
        if baseline.get("immutable") is not True or snapshot.get("cashflow_verified") is not True:
            raise ValueError("verified cashflow and an immutable daily baseline are required")
        start = number(baseline.get("day_start_equity"), "day_start_equity")
        current = number(snapshot.get("current_equity"), "current_equity")
        flow = number(snapshot.get("net_external_cashflow"), "net_external_cashflow")
        limit = number(payload.get("loss_limit_fraction"), "loss_limit_fraction")
        if start < 0 or not Decimal(0) < limit <= Decimal(1):
            raise ValueError("nonnegative baseline and a loss fraction in (0,1] are required")
        with localcontext() as ctx:
            ctx.prec = 160
            reference = start + flow
            if reference <= 0:
                raise ValueError("cashflow-adjusted reference capital must be positive")
            pnl = current - reference
            loss = max(Decimal(0), -pnl)
            stopped = loss >= reference * limit
            ratio = loss / reference
            return {"status": "STOP_NEW_PROPOSALS" if stopped else "ALLOW_NEW_PROPOSALS",
                    "allow_new_proposals": not stopped, "currency": currency.lower(),
                    "day": baseline["day"], "reference_capital": str(reference),
                    "cashflow_adjusted_pnl": str(pnl), "daily_loss": str(loss),
                    "daily_loss_fraction": str(ratio), "loss_limit_fraction": str(limit)}
    except (ValueError, TypeError, KeyError):
        return {"status": "INSUFFICIENT_DATA", "allow_new_proposals": False,
                "reason": "Missing, invalid, unverified, or inconsistent daily loss inputs"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Normalized daily-risk JSON file")
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text())
    except (OSError, ValueError):
        payload = None
    result = evaluate(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["allow_new_proposals"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
