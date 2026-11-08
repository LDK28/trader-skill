#!/usr/bin/env python3
"""Offline own-position check of normalized SELL/LIMIT proposals; no execution.

Input is ONE JSON object. All data below are normalized assertions, not raw
broker responses or evidence of their authenticity. Quantity/price decimals
must be strings; lots and lot_size must be positive JSON integers (not bool).

  account_id: string
  snapshot: {account_id, as_of, expires_at, verified: true,
    no_existing_debt: true, positions_exclude_borrowed_and_unsettled: true,
    settled_own_units_include_blocked: true,
    positions: [{uid, kind: "share", lot_size: 10,
      settled_own_units: "30", blocked_own_units: "10"}]}
  orders: [{client_id, account_id, side: "SELL", order_type: "LIMIT",
    lots: 1, limit_price: "100",
    instrument: {uid, kind: "share", lot_size: 10, available: true,
      eligibility_verified: true, financing: "own_settled_position",
      leveraged: false},
    quote: {instrument_uid, as_of, expires_at},
    position_limit: {account_id, instrument_uid, limit_price: "100",
      as_of, expires_at, source: "sell_limits", verified: true,
      own_position_max_lots: 2}}]

Times are timezone-aware ISO 8601 strings. Snapshot age <=60 seconds;
quote/position_limit ages <=30 seconds. Supported kinds: share, bond, fund
(unleveraged). The position total INCLUDES the blocked quantity; both exclude
borrowed and unsettled units. Missing/ambiguous blocking is not zero. Distinct
UIDs are separate positions; a duplicate UID in the snapshot is invalid.

The producer must reconcile quantities, debt, lot sizes, eligibility, current
reservations and identity with broker data. position_limit must come from
GetMaxLots sell_limits / sellLimits (own position), never sellMarginLimits,
at this account, UID and exact limit price. Quote/limit records must be bound
to the instrument/order as shown. Both limit_price fields use GetMaxLots price
units and the same settlement currency, verified by the producer. For a bond,
do not copy a market quote in points into GetMaxLots: that method expects the
bond price in settlement currency. This is NOT a raw PostOrder input schema;
this guard neither converts prices nor values a bond as price * quantity.

All proposals for a UID share ONE free position and ONE conservative cap: the
minimum confirmed own_position_max_lots across its proposals. Do not add caps.
This checks a whole batch; use all outstanding proposals in that batch and
refresh reservations before another review. PASS does not reserve holdings.

PASS only checks normalized own-position assertions and quantity arithmetic.
It does not check fees, proceeds, profit, daily loss, price tick, execution or
permission to trade. It never credits expected sale proceeds to a BUY budget.
No network, credentials, short sales, borrowed securities or real orders.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import re


KINDS = frozenset(("share", "bond", "fund"))
MAX_INTEGER = 10 ** 20 - 1


def check(condition, message):
    if not condition:
        raise ValueError(message)


def text(value, name):
    check(isinstance(value, str) and bool(value.strip()), name + " is missing")
    return value


def amount(value, name, positive=False):
    check(isinstance(value, str), name + " must be a decimal string")
    # Validate length/notation before Decimal conversion of untrusted input.
    check(bool(re.fullmatch(r"-?\d{1,20}(?:\.\d{1,9})?", value)),
          name + " must use plain decimal notation with at most 9 decimal places")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(name + " is not a decimal") from None
    check(number.is_finite(), name + " must be finite")
    check(number > 0 if positive else number >= 0, name + " has an invalid sign")
    return number


def integer(value, name, allow_zero=False):
    check(type(value) is int and (0 if allow_zero else 1) <= value <= MAX_INTEGER,
          name + " must be a bounded " + ("nonnegative" if allow_zero else "positive") + " integer")
    return value


def fresh(record, now, max_age, name):
    check(isinstance(record, dict), name + " is missing")
    parsed = []
    for key in ("as_of", "expires_at"):
        value = text(record.get(key), name + "." + key)
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(name + "." + key + " is not an ISO timestamp") from None
        check(stamp.tzinfo is not None and stamp.utcoffset() is not None,
              name + "." + key + " must include timezone")
        parsed.append(stamp)
    start, expiry = parsed
    check(0 <= (now - start).total_seconds() <= max_age, name + " is stale or future-dated")
    check(expiry > now and expiry > start, name + " has expired")


def _validate_plan(plan, now):
    required_units, required_lots, caps = defaultdict(int), defaultdict(int), {}
    errors, positions = [], {}
    try:
        check(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None,
              "review time must include timezone")
        check(isinstance(plan, dict), "input must be an object")
        account = text(plan.get("account_id"), "account_id")
        snapshot = plan.get("snapshot")
        fresh(snapshot, now, 60, "snapshot")
        check(snapshot.get("account_id") == account, "snapshot account does not match")
        for flag in ("verified", "no_existing_debt", "positions_exclude_borrowed_and_unsettled",
                     "settled_own_units_include_blocked"):
            check(snapshot.get(flag) is True, "snapshot must explicitly confirm " + flag)
        records = snapshot.get("positions")
        check(isinstance(records, list) and 0 < len(records) <= 10000,
              "positions must be a nonempty list of at most 10000 records")
        for record in records:
            check(isinstance(record, dict), "position must be an object")
            uid = text(record.get("uid"), "position UID")
            check(uid not in positions, "duplicate position UID")
            check(record.get("kind") in KINDS, "unsupported or unknown position kind")
            lot = integer(record.get("lot_size"), "position lot_size")
            total = amount(record.get("settled_own_units"), "settled_own_units")
            blocked = amount(record.get("blocked_own_units"), "blocked_own_units")
            check(blocked <= total, "blocked units exceed settled own units")
            positions[uid] = {"kind": record["kind"], "lot_size": lot, "free": total - blocked}
        orders = plan.get("orders")
        check(isinstance(orders, list) and 0 < len(orders) <= 10000,
              "orders must be a nonempty list of at most 10000 proposals")
    except (ValueError, TypeError) as exc:
        return {"status": "BLOCK", "reasons": [str(exc)], "required_units": {}}

    seen = set()
    for index, order in enumerate(orders, 1):
        try:
            check(isinstance(order, dict), "proposal must be an object")
            client_id = text(order.get("client_id"), "client_id")
            check(client_id not in seen, "duplicate client_id")
            seen.add(client_id)
            check(order.get("account_id") == account, "order account does not match")
            check(order.get("side") == "SELL", "only sales of own positions are supported")
            check(order.get("order_type") == "LIMIT", "a bounded limit price is required")
            lots = integer(order.get("lots"), "lots")
            price = amount(order.get("limit_price"), "limit_price", positive=True)
            instrument = order.get("instrument")
            check(isinstance(instrument, dict), "instrument eligibility is missing")
            uid = text(instrument.get("uid"), "instrument UID")
            check(uid in positions, "no confirmed own position for UID")
            position = positions[uid]
            check(instrument.get("kind") == position["kind"], "instrument kind does not match position")
            lot = integer(instrument.get("lot_size"), "instrument lot_size")
            check(lot == position["lot_size"], "lot_size does not match position")
            for flag in ("available", "eligibility_verified"):
                check(instrument.get(flag) is True, "instrument must confirm " + flag)
            check(instrument.get("financing") == "own_settled_position", "own-position financing is not verified")
            check(instrument.get("leveraged") is False, "leveraged or unknown exposure is blocked")
            quote = order.get("quote")
            fresh(quote, now, 30, "quote")
            check(quote.get("instrument_uid") == uid, "quote instrument does not match")
            limit = order.get("position_limit")
            fresh(limit, now, 30, "position_limit")
            check(limit.get("verified") is True, "position_limit must explicitly confirm verified")
            check(limit.get("source") == "sell_limits", "own-position sell_limits source is required")
            check(limit.get("account_id") == account, "position_limit account does not match")
            check(limit.get("instrument_uid") == uid, "position_limit instrument does not match")
            check(amount(limit.get("limit_price"), "position_limit.limit_price", positive=True) == price,
                  "position_limit price does not match")
            cap = integer(limit.get("own_position_max_lots"), "own_position_max_lots", allow_zero=True)
            check(lots <= cap, "lots exceed confirmed own-position lot cap")
            required_units[uid] += lots * lot
            required_lots[uid] += lots
            caps[uid] = min(caps.get(uid, cap), cap)
        except (ValueError, TypeError) as exc:
            errors.append("order " + str(index) + ": " + str(exc))
    for uid, units in required_units.items():
        if units > positions[uid]["free"]:
            errors.append("insufficient free own units for " + uid + ": required " + str(units) +
                          ", available " + str(positions[uid]["free"]))
        if required_lots[uid] > caps[uid]:
            errors.append("aggregate lots exceed confirmed own-position lot cap for " + uid)
    return {"status": "BLOCK" if errors else "PASS", "reasons": errors,
            "required_units": {uid: str(units) for uid, units in sorted(required_units.items())}}


def validate_plan(plan, now=None):
    """Check one normalized SELL batch; no reservation or permission to execute."""
    with localcontext() as context:
        context.prec = 60
        return _validate_plan(plan, datetime.now(timezone.utc) if now is None else now)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="normalized JSON proposal file; no credentials")
    args = parser.parse_args()
    try:
        with open(args.input, encoding="utf-8") as stream:
            result = validate_plan(json.load(stream))
    except (OSError, ValueError) as exc:
        result = {"status": "BLOCK", "reasons": ["input error: " + type(exc).__name__], "required_units": {}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
