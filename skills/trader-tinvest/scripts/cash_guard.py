#!/usr/bin/env python3
"""Offline review of normalized cash-only BUY proposals; never submits orders.

Input is ONE JSON object, with decimal amounts supplied as strings:
  account_id: string
  snapshot: {account_id, as_of, expires_at, verified: true,
    no_existing_debt: true, cash_excludes_blocked_unsettled_borrowed: true,
    spendable_own_cash: {rub: "10000"}}
  orders: [{client_id, account_id, side: "BUY", order_type: "LIMIT", lots: 1,
    own_funds_max_lots: 1,
    limit_price: "100", instrument: {uid, kind: "share", available: true,
      eligibility_verified: true, financing: "fully_paid_cash", leveraged: false},
    quote: {as_of, expires_at},
    estimate: {as_of, expires_at, verified_for_this_order: true,
      all_costs_included: true, at_limit_price: true, currency: "rub",
      principal: "100", fees: "1", nkd: "0", other: "0",
      worst_case_total: "101"}}]

All times are timezone-aware ISO 8601 strings. Snapshot age limit: 60 seconds;
quote/estimate age limits: 30 seconds. Currencies are lowercase ISO 4217 codes.
Supported kinds: share, bond, fund (unleveraged), spot_currency. The producer
must verify eligibility, debt-free settled own cash, and a conservative estimate
bound to this exact account/instrument/lots/limit price. own_funds_max_lots must
be the confirmed broker buyLimits.buyMaxLots at that same account, instrument,
and limit price, obtained alongside the fresh estimate; never buyMarginLimits.
All true/false flags are
upstream normalization assertions, not broker attestation; record data sources
upstream. This script checks assertions and arithmetic; it cannot verify broker
data or guarantee execution costs. SELL is unsupported and always BLOCKED. This
is not a trading executor, strategy, recommendation, or execution permission.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import re


KINDS = frozenset(("share", "bond", "fund", "spot_currency"))


def check(condition, message):
    if not condition:
        raise ValueError(message)


def amount(value, name, positive=False):
    check(isinstance(value, str), name + " must be a decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(name + " is not a decimal") from None
    check(number.is_finite(), name + " must be finite")
    check(bool(re.fullmatch(r"-?\d{1,20}(?:\.\d{1,9})?", value)),
          name + " must use plain decimal notation with at most 9 decimal places")
    check(number > 0 if positive else number >= 0, name + " has an invalid sign")
    return number


def text(value, name):
    check(isinstance(value, str) and bool(value.strip()), name + " is missing")
    return value


def fresh(record, now, max_age, label):
    check(isinstance(record, dict), label + " is missing")
    parsed = []
    for key in ("as_of", "expires_at"):
        value = text(record.get(key), label + "." + key)
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(label + "." + key + " is not an ISO timestamp") from None
        check(stamp.tzinfo is not None and stamp.utcoffset() is not None,
              label + "." + key + " must include timezone")
        parsed.append(stamp)
    start, expiry = parsed
    check(0 <= (now - start).total_seconds() <= max_age, label + " is stale or future-dated")
    check(expiry > now and expiry > start, label + " has expired")


def _validate_plan(plan, now=None):
    now = now or datetime.now(timezone.utc)
    errors, totals = [], defaultdict(Decimal)
    try:
        check(now.tzinfo is not None, "review time must include timezone")
        check(isinstance(plan, dict), "input must be an object")
        account = text(plan.get("account_id"), "account_id")
        snapshot = plan.get("snapshot")
        fresh(snapshot, now, 60, "snapshot")
        check(snapshot.get("account_id") == account, "snapshot account does not match")
        for flag in ("verified", "no_existing_debt", "cash_excludes_blocked_unsettled_borrowed"):
            check(snapshot.get(flag) is True, "snapshot must explicitly confirm " + flag)
        cash = snapshot.get("spendable_own_cash")
        check(isinstance(cash, dict) and bool(cash), "verified own-cash balances are missing")
        balances = {}
        for currency, value in cash.items():
            check(bool(re.fullmatch(r"[a-z]{3}", currency)), "invalid cash currency code")
            balances[currency] = amount(value, "cash " + currency)
        orders = plan.get("orders")
        check(isinstance(orders, list) and 0 < len(orders) <= 10000,
              "orders must be a nonempty list of at most 10000 proposals")
    except (ValueError, TypeError) as exc:
        return {"status": "BLOCK", "reasons": [str(exc)], "required_cash": {}}

    seen = set()
    for index, order in enumerate(orders, 1):
        try:
            check(isinstance(order, dict), "proposal must be an object")
            client_id = text(order.get("client_id"), "client_id")
            check(client_id not in seen, "duplicate client_id")
            seen.add(client_id)
            check(order.get("account_id") == account, "order account does not match")
            check(order.get("side") == "BUY", "only purchases using existing own cash are supported")
            check(order.get("order_type") == "LIMIT", "a bounded limit price is required")
            check(type(order.get("lots")) is int and order["lots"] > 0, "lots must be a positive integer")
            cap = order.get("own_funds_max_lots")
            check(type(cap) is int and cap >= 0,
                  "confirmed own_funds_max_lots must be a nonnegative integer")
            check(order["lots"] <= cap, "lots exceed confirmed own-funds lot cap")
            amount(order.get("limit_price"), "limit_price", positive=True)
            instrument = order.get("instrument")
            check(isinstance(instrument, dict), "instrument eligibility is missing")
            text(instrument.get("uid"), "instrument UID")
            check(instrument.get("kind") in KINDS, "unsupported or unknown instrument kind")
            check(instrument.get("financing") == "fully_paid_cash", "cash-only financing is not verified")
            for flag in ("available", "eligibility_verified"):
                check(instrument.get(flag) is True, "instrument must confirm " + flag)
            check(instrument.get("leveraged") is False, "leveraged or unknown exposure is blocked")
            fresh(order.get("quote"), now, 30, "quote")
            estimate = order.get("estimate")
            fresh(estimate, now, 30, "estimate")
            for flag in ("verified_for_this_order", "all_costs_included", "at_limit_price"):
                check(estimate.get(flag) is True, "estimate must confirm " + flag)
            currency = estimate.get("currency")
            check(currency in balances, "no verified own cash for estimate currency")
            components = [amount(estimate.get(key), "estimate." + key, positive=key == "principal")
                          for key in ("principal", "fees", "nkd", "other")]
            cost = amount(estimate.get("worst_case_total"), "worst_case_total", positive=True)
            check(cost >= sum(components, Decimal(0)), "cost estimate omits a fee, NKD, or other cost")
            totals[currency] += cost
        except (ValueError, TypeError) as exc:
            errors.append("order " + str(index) + ": " + str(exc))
    for currency, required in totals.items():
        if required > balances[currency]:
            errors.append("insufficient own cash in " + currency + ": required " +
                          str(required) + ", available " + str(balances[currency]))
    return {"status": "BLOCK" if errors else "PASS", "reasons": errors,
            "required_cash": {currency: str(value) for currency, value in sorted(totals.items())}}


def validate_plan(plan, now=None):
    """Return an offline PASS/BLOCK review, with no permission to execute."""
    # 20 integer + 9 fractional digits, 4 components, at most 10000 proposals.
    with localcontext() as context:
        context.prec = 50
        return _validate_plan(plan, now)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="normalized JSON proposal file; no credentials")
    args = parser.parse_args()
    try:
        with open(args.input, encoding="utf-8") as stream:
            result = validate_plan(json.load(stream))
    except (OSError, ValueError) as exc:
        result = {"status": "BLOCK", "reasons": ["input error: " + type(exc).__name__], "required_cash": {}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
