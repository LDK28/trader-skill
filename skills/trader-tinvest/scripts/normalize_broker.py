#!/usr/bin/env python3
"""Normalize offline T-Invest REST JSON captures into observations, NOT plans.

Input profile: "tinvest-rest-v1"; account_id; captures (nonempty list):
  {capture_id, method, requested_at, received_at, request: {...}, response: {...}}
Supported method labels: GetPositions, GetInstrumentBy, GetMaxLots.
REST/protobuf JSON uses camelCase keys, int64 decimal strings, and Quotation /
MoneyValue {units: string, nano: integer[, currency: string]}. This is NOT an
adapter for unobserved MCP tool output. Never rename arbitrary MCP responses
to this profile or invent missing fields/defaults to make captures pass.

Require one GetPositions, one UID-based GetInstrumentBy per relevant instrument,
and zero or more explicitly priced GetMaxLots. All captures need timezone-aware
original request/receive times. Both must be in the past; age measured from
REQUEST start <=60s (GetMaxLots <=30s). These are collector assertions, not
independent timestamps or broker attestation. No timestamps are refreshed.

GetPositions balance is the UNBLOCKED count of securities, in units, not lots.
Reported total = balance + blocked. This total is NOT evidence of ownership or
settlement. Cash money and blocked arrays remain separate reported amounts;
neither is converted into a verified spendable budget. Futures/options and
negative reported balances are outside this conservative profile.

Output NORMALIZED_OBSERVATIONS (CLI 0) only means successful schema conversion.
ready_for_validators is ALWAYS false. No snapshot, verified flags, trade plan,
settled-own assertion, debt-free assertion or daily baseline is manufactured.
Malformed, incomplete, inconsistent or stale input: BLOCK (CLI 2), no partial
observations. See ../references/broker-normalization.md for the exact contract.
No network, credentials, broker settings, execution or output file writes.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import re


KINDS = {"share": "share", "bond": "bond", "etf": "fund"}
METHODS = frozenset(("GetPositions", "GetInstrumentBy", "GetMaxLots"))
MISSING_CHECKS = (
    "OWNERSHIP_AND_SETTLEMENT", "DEBT_AND_OTHER_OBLIGATIONS",
    "ACCOUNT_ELIGIBILITY_AND_UNLEVERAGED_STATUS", "LIVE_QUOTES_AND_FULL_ORDER_COSTS",
    "DAILY_BASELINE_AND_RECONCILED_CASHFLOWS", "LIVE_CAPTURE_AND_MCP_SCHEMA",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def obj(value, name):
    require(isinstance(value, dict), name + " must be an object")
    return value


def bounded_depth(value):
    """Bound nesting before hashing even unknown JSON fields, without recursion."""
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list)):
            require(depth <= 64, "capture JSON nesting exceeds 64 levels")
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)


def text(value, name):
    require(isinstance(value, str) and bool(value.strip()), name + " is missing")
    return value


def records(value, name, nonempty=False):
    require(isinstance(value, list) and int(nonempty) <= len(value) <= 10000,
            name + " must be an explicit bounded list")
    return value


def int64(value, name, nonnegative=False):
    require(isinstance(value, str) and bool(re.fullmatch(r"-?(?:0|[1-9][0-9]{0,18})", value)),
            name + " must be an int64 decimal string")
    result = int(value)
    require(-(2 ** 63) <= result < 2 ** 63, name + " exceeds int64 range")
    require(not nonnegative or result >= 0, name + " must be nonnegative")
    return result


def quotation(value, name):
    value = obj(value, name)
    units = int64(value.get("units"), name + ".units")
    nano = value.get("nano")
    require(type(nano) is int and -999999999 <= nano <= 999999999,
            name + ".nano must be a bounded integer")
    require(not (units > 0 and nano < 0 or units < 0 and nano > 0),
            name + " has inconsistent units/nano signs")
    with localcontext() as context:
        context.prec = 60
        return Decimal(units) + Decimal(nano) / Decimal(1000000000)


def decimal_text(value):
    return format(value, "f")


def currency(value, name):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value) is not None,
            name + " must be a currency code")
    return value.lower()


def money_list(value, name):
    result = {}
    for record in records(value, name):
        record = obj(record, name + " record")
        code = currency(record.get("currency"), name + ".currency")
        require(code not in result, name + " contains duplicate currency")
        amount = quotation(record, name)
        require(amount >= 0, name + " contains a negative reported balance")
        result[code] = decimal_text(amount)
    return dict(sorted(result.items()))


def timestamp(value, name):
    value = text(value, name)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(name + " must be ISO time") from None
    require(result.tzinfo is not None and result.utcoffset() is not None,
            name + " must include timezone")
    return result


def _normalize(bundle, now):
    require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None,
            "review time must include timezone")
    bundle = obj(bundle, "input")
    bounded_depth(bundle)
    require(bundle.get("profile") == "tinvest-rest-v1", "unsupported input profile; MCP is not inferred")
    account = text(bundle.get("account_id"), "account_id")
    grouped = {method: [] for method in METHODS}
    provenance, seen = [], set()
    for capture in records(bundle.get("captures"), "captures", nonempty=True):
        capture = obj(capture, "capture")
        ident = text(capture.get("capture_id"), "capture_id")
        require(ident not in seen, "duplicate capture_id")
        seen.add(ident)
        method = capture.get("method")
        require(isinstance(method, str) and method in METHODS, "unsupported capture method")
        start = timestamp(capture.get("requested_at"), "requested_at")
        end = timestamp(capture.get("received_at"), "received_at")
        max_age = 30 if method == "GetMaxLots" else 60
        require(start <= end <= now and 0 <= (now - start).total_seconds() <= max_age,
                "capture is stale, future-dated or has reversed timestamps")
        request, response = obj(capture.get("request"), "request"), obj(capture.get("response"), "response")
        if method != "GetInstrumentBy":
            require(request.get("accountId") == account, "capture request account does not match")
        digest = hashlib.sha256(json.dumps({"request": request, "response": response},
                                          sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        provenance.append({"capture_id": ident, "method": method,
                           "requested_at": capture["requested_at"], "received_at": capture["received_at"],
                           "content_sha256": digest})
        grouped[method].append((ident, request, response))
    require(len(grouped["GetPositions"]) == 1, "exactly one GetPositions capture is required")

    instruments = {}
    for ident, request, response in grouped["GetInstrumentBy"]:
        require(request.get("idType") == "INSTRUMENT_ID_TYPE_UID", "instrument lookup must use UID")
        record = obj(response.get("instrument"), "instrument")
        uid = text(record.get("uid"), "instrument UID")
        require(request.get("id") == uid, "instrument request/response UID mismatch")
        require(uid not in instruments, "duplicate instrument UID")
        kind = record.get("instrumentType")
        require(isinstance(kind, str) and kind in KINDS, "unsupported instrument kind")
        lot = record.get("lot")
        require(type(lot) is int and 0 < lot < 2 ** 31, "lot must be a positive int32")
        flags = {}
        for flag in ("buyAvailableFlag", "sellAvailableFlag", "apiTradeAvailableFlag"):
            require(type(record.get(flag)) is bool, "instrument availability flags must be explicit booleans")
            flags[flag] = record[flag]
        instruments[uid] = {"uid": uid, "position_uid": text(record.get("positionUid"), "positionUid"),
                            "kind": KINDS[kind], "raw_kind": kind, "lot_size": lot,
                            "currency": currency(record.get("currency"), "instrument.currency"),
                            "reported_flags": flags, "capture_id": ident}

    positions_id, _, response = grouped["GetPositions"][0]
    require(response.get("accountId") == account, "positions response account does not match")
    require(response.get("limitsLoadingInProgress") is False, "position limits are not explicitly ready")
    for key in ("futures", "options"):
        require(not records(response.get(key), key), "derivative positions are unsupported in this profile")
    reported_money = money_list(response.get("money"), "money")
    reported_blocked_money = money_list(response.get("blocked"), "blocked money")
    positions = {}
    for record in records(response.get("securities"), "securities"):
        record = obj(record, "security")
        uid = text(record.get("instrumentUid"), "security.instrumentUid")
        require(uid not in positions, "duplicate security UID")
        require(uid in instruments, "security metadata is missing")
        instrument = instruments[uid]
        require(record.get("positionUid") == instrument["position_uid"], "position UID mismatch")
        require(record.get("instrumentType") == instrument["raw_kind"], "position instrument kind mismatch")
        require(type(record.get("exchangeBlocked")) is bool, "exchangeBlocked must be explicit boolean")
        balance = int64(record.get("balance"), "security.balance", nonnegative=True)
        blocked = int64(record.get("blocked"), "security.blocked", nonnegative=True)
        positions[uid] = {"uid": uid, "position_uid": instrument["position_uid"],
                          "kind": instrument["kind"], "lot_size": instrument["lot_size"],
                          "currency": instrument["currency"], "reported_unblocked_units": str(balance),
                          "reported_blocked_units": str(blocked), "reported_total_units": str(balance + blocked),
                          "reported_whole_unblocked_lots": balance // instrument["lot_size"],
                          "reported_unblocked_remainder_units": str(balance % instrument["lot_size"]),
                          "exchange_blocked": record["exchangeBlocked"], "capture_id": positions_id}

    limits, limit_keys = [], set()
    for ident, request, response in grouped["GetMaxLots"]:
        uid = text(request.get("instrumentId"), "limits instrumentId")
        require(uid in instruments, "limit instrument metadata is missing; use UID")
        price = quotation(request.get("price"), "GetMaxLots.price")
        require(price > 0, "GetMaxLots requires an explicit positive price")
        code = currency(response.get("currency"), "limits.currency")
        require(code == instruments[uid]["currency"], "limit currency mismatch")
        key = (uid, price)
        require(key not in limit_keys, "duplicate UID/price limit capture")
        limit_keys.add(key)
        buys, sells = obj(response.get("buyLimits"), "buyLimits"), obj(response.get("sellLimits"), "sellLimits")
        buy_cap = int64(buys.get("buyMaxLots"), "buyMaxLots", nonnegative=True)
        sell_cap = int64(sells.get("sellMaxLots"), "sellMaxLots", nonnegative=True)
        own_money = quotation(buys.get("buyMoneyAmount"), "buyMoneyAmount")
        require(own_money >= 0, "buyMoneyAmount must be nonnegative")
        free_lots = positions.get(uid, {}).get("reported_whole_unblocked_lots", 0)
        require(sell_cap <= free_lots, "sellLimits exceeds the reported unblocked position")
        limits.append({"uid": uid, "price": decimal_text(price), "price_units": "GetMaxLots",
                       "currency": code, "reported_own_buy_max_lots": buy_cap,
                       "reported_own_sell_max_lots": sell_cap,
                       "reported_instrument_buy_money": decimal_text(own_money), "capture_id": ident})
    return {"status": "NORMALIZED_OBSERVATIONS", "ready_for_validators": False,
            "account_id": account,
            "observations": {"reported_money": reported_money, "reported_blocked_money": reported_blocked_money,
                             "positions": [positions[uid] for uid in sorted(positions)],
                             "instruments": [instruments[uid] for uid in sorted(instruments)], "limits": limits},
            "provenance": provenance, "missing_checks": list(MISSING_CHECKS)}


def normalize(bundle, now=None):
    """Normalize captured data without creating any verified validator inputs."""
    try:
        return _normalize(bundle, datetime.now(timezone.utc) if now is None else now)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        # Validation messages contain field names, never entire private payloads.
        return {"status": "BLOCK", "ready_for_validators": False,
                "reason": str(exc)}


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
    parser.add_argument("input", help="offline capture bundle; no headers or credentials")
    args = parser.parse_args()
    try:
        with open(args.input, encoding="utf-8") as stream:
            bundle = json.load(stream, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        result = normalize(bundle)
    except (OSError, ValueError, RecursionError):
        result = normalize(None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "NORMALIZED_OBSERVATIONS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
