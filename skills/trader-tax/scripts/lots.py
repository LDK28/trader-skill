#!/usr/bin/env python3
"""Offline FIFO/RUB transaction ledger. No account, network, or order access.

    python3 lots.py report INPUT.json
    python3 lots.py rates

All quantities and amounts are plain decimal strings. A valid ledger is returned
even when the optional tax estimate is unsupported. Dates, source references,
completeness and policy verification are caller assertions, not attestations.
"""
import argparse
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


RATE_SOURCE = "https://www.nalog.gov.ru/rn20/taxation/taxes/ndfl/"
RATE_CHECKED_ON = "2026-11-09"
RATE_PROFILE = "ru-resident-passive-2025-2026-reference-v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, name, positive=False):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,9})?", value),
            name + " must be a nonnegative plain decimal string (20+9 digits maximum)")
    result = Decimal(value)
    require(result > 0 if positive else result >= 0, name + " has an invalid sign")
    return result


def decimal_str(value):
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def text(value, name):
    require(isinstance(value, str) and 0 < len(value.strip()) <= 1000, name + " is required")
    return value


def iso_day(value, name):
    require(isinstance(value, str), name + " must be an ISO date")
    result = date.fromisoformat(value)
    require(result.isoformat() == value, name + " must be YYYY-MM-DD")
    return result


def timestamp(value):
    text(value, "occurred_at")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None and result.utcoffset() is not None,
            "occurred_at must include a timezone")
    return result


def public_url(value, name):
    text(value, name)
    parsed = urlsplit(value)
    require(parsed.scheme == "https" and bool(parsed.hostname) and
            parsed.username is None and parsed.password is None, name + " must be an HTTPS source URL")
    return value


def rates_reference():
    return {"profile_id": RATE_PROFILE, "source_url": RATE_SOURCE,
            "checked_on": RATE_CHECKED_ON, "supported_tax_years": [2025, 2026],
            "residency": "ru_tax_resident", "scope": "verified_aggregate_passive_base",
            "brackets": [{"up_to_rub": "2400000", "rate": "0.13"},
                         {"up_to_rub": None, "rate": "0.15"}],
            "automatic_personal_adoption": False, "full_tax_engine": False}


def estimate_tax(policy, year):
    unsupported = {"status": "TAX_ESTIMATE_UNSUPPORTED", "amount_rub": None,
                   "reason": "Explicit verified year/residency/aggregate taxable-base policy is required"}
    if policy is None:
        return unsupported
    try:
        require(isinstance(policy, dict), "policy must be an object")
        require(policy.get("profile_id") == RATE_PROFILE and policy.get("tax_year") == year and
                year in (2025, 2026), "unsupported policy or year")
        require(policy.get("residency") == "ru_tax_resident", "unsupported residency")
        require(policy.get("scope") == "verified_aggregate_passive_base", "unsupported policy scope")
        for flag in ("verified", "residency_verified", "all_relevant_income_accounted",
                     "taxable_base_verified", "basket_and_loss_rules_verified"):
            require(policy.get(flag) is True, "policy must explicitly confirm " + flag)
        require(policy.get("source_url") == RATE_SOURCE, "rate source mismatch")
        require(policy.get("source_checked_on") == RATE_CHECKED_ON, "rate check date mismatch")
        text(policy.get("taxable_base_source_ref"), "taxable_base_source_ref")
        base = number(policy.get("taxable_base_rub"), "taxable_base_rub")
        with localcontext() as context:
            context.prec = 80
            tax = min(base, Decimal("2400000")) * Decimal("0.13") + max(
                Decimal(0), base - Decimal("2400000")) * Decimal("0.15")
        return {"status": "ILLUSTRATIVE_TAX_ESTIMATE", "amount_rub": decimal_str(tax),
                "verified_input_taxable_base_rub": decimal_str(base), "tax_year": year,
                "profile_id": RATE_PROFILE, "source_url": RATE_SOURCE,
                "source_checked_on": RATE_CHECKED_ON,
                "base_derived_automatically_from_ledger": False,
                "withholding_credits_deductions_or_final_rounding_applied": False,
                "full_tax_engine": False}
    except (ValueError, TypeError, KeyError):
        return unsupported


def _evaluate(payload):
    require(isinstance(payload, dict) and type(payload.get("schema_version")) is int and
            payload["schema_version"] == 1, "schema_version 1 is required")
    year = payload.get("tax_year")
    require(type(year) is int and 1900 <= year <= 2100, "tax_year must be an integer")
    require(payload.get("accounting_currency") == "RUB" and payload.get("basis_method") == "FIFO",
            "only FIFO in RUB is supported")
    require(payload.get("data_kind") in ("synthetic", "recorded_transactions"), "explicit data_kind is required")
    scope = text(payload.get("ledger_scope_id"), "ledger_scope_id")
    require(payload.get("history_complete") is True, "complete included-instrument history must be explicitly confirmed")
    transactions = payload.get("transactions")
    require(isinstance(transactions, list) and len(transactions) <= 20000, "bounded transaction list is required")
    lots, instrument_baskets, seen = defaultdict(list), {}, set()
    sales, incomes, annual = [], [], defaultdict(lambda: {"proceeds": Decimal(0), "basis": Decimal(0),
                                                          "fees": Decimal(0), "gain": Decimal(0)})
    previous = None
    for transaction in transactions:
        require(isinstance(transaction, dict), "transaction must be an object")
        identifier = text(transaction.get("id"), "transaction id")
        require(identifier not in seen, "duplicate transaction id")
        seen.add(identifier)
        instant = timestamp(transaction.get("occurred_at"))
        require(previous is None or instant > previous, "transactions must have unique strictly increasing occurred_at")
        previous = instant
        day = iso_day(transaction.get("tax_date"), "tax_date")
        require(day.year <= year, "transactions after requested tax year must be excluded explicitly")
        source = text(transaction.get("source_ref"), "source_ref")
        kind = transaction.get("kind")
        require(kind in ("BUY", "SELL", "DIVIDEND", "COUPON"), "unsupported transaction kind; do not silently reverse history")
        uid = text(transaction.get("instrument_uid"), "instrument_uid")
        basket = text(transaction.get("tax_basket"), "tax_basket")
        currency = transaction.get("currency")
        require(isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency), "currency must be an uppercase code")
        gross = number(transaction.get("gross_amount"), "gross_amount", positive=True)
        fees = number(transaction.get("fees"), "fees")
        fx = transaction.get("fx_to_rub")
        rate = Decimal(1)
        fx_evidence = None
        if currency != "RUB" or fx is not None:
            require(isinstance(fx, dict), "per-transaction FX is required")
            rate = number(fx.get("rate"), "fx_to_rub.rate", positive=True)
            require(iso_day(fx.get("date"), "fx date") == day, "FX date must match the explicitly supplied tax_date")
            public_url(fx.get("source_url"), "FX source_url")
            require(currency != "RUB" or rate == 1, "RUB conversion rate must be 1")
            fx_evidence = dict(fx)
        gross_rub, fees_rub = gross * rate, fees * rate
        if kind in ("DIVIDEND", "COUPON"):
            require("quantity" not in transaction, "income events do not consume security quantity")
            if day.year == year:
                incomes.append({"transaction_id": identifier, "tax_date": day.isoformat(), "kind": kind,
                                "instrument_uid": uid, "tax_basket": basket,
                                "gross_rub": decimal_str(gross_rub), "fees_rub": decimal_str(fees_rub),
                                "currency": currency, "fx_to_rub": fx_evidence, "source_ref": source,
                                "tax_treatment": "NOT_AUTOMATICALLY_DETERMINED"})
            continue
        quantity = number(transaction.get("quantity"), "quantity", positive=True)
        key = uid
        require(key not in instrument_baskets or instrument_baskets[key] == basket,
                "instrument tax basket changed; explicit reviewed transformation is required")
        instrument_baskets[key] = basket
        if kind == "BUY":
            lots[key].append({"acquisition_id": identifier, "occurred_at": transaction["occurred_at"],
                              "tax_date": day.isoformat(), "quantity": quantity, "basis": gross_rub + fees_rub,
                              "tax_basket": basket, "currency": currency, "fx_to_rub": fx_evidence,
                              "source_ref": source})
            continue
        require(sum((lot["quantity"] for lot in lots[key]), Decimal(0)) >= quantity,
                "insufficient documented FIFO basis for sale " + identifier)
        remaining, basis, allocations = quantity, Decimal(0), []
        for lot in lots[key]:
            if remaining == 0:
                break
            take = min(remaining, lot["quantity"])
            if not take:
                continue
            require(iso_day(lot["tax_date"], "acquisition tax_date") <= day,
                    "sale tax_date precedes its documented acquisition tax_date")
            allocated = lot["basis"] if take == lot["quantity"] else lot["basis"] * take / lot["quantity"]
            lot["quantity"] -= take
            lot["basis"] -= allocated
            basis += allocated
            remaining -= take
            allocations.append({"acquisition_id": lot["acquisition_id"], "quantity": decimal_str(take),
                                "basis_rub": decimal_str(allocated), "acquisition_tax_date": lot["tax_date"]})
        gain = gross_rub - fees_rub - basis
        if day.year == year:
            sales.append({"transaction_id": identifier, "instrument_uid": uid, "tax_basket": basket,
                          "tax_year": day.year, "tax_date": day.isoformat(), "quantity": decimal_str(quantity),
                          "gross_proceeds_rub": decimal_str(gross_rub), "sale_fees_rub": decimal_str(fees_rub),
                          "fifo_basis_rub": decimal_str(basis), "realized_gain_rub": decimal_str(gain),
                          "fifo_allocations": allocations, "currency": currency,
                          "fx_to_rub": fx_evidence, "source_ref": source})
            values = annual[(day.year, basket)]
            values["proceeds"] += gross_rub
            values["fees"] += fees_rub
            values["basis"] += basis
            values["gain"] += gain
    remaining_lots = []
    for uid in sorted(lots):
        for lot in lots[uid]:
            if lot["quantity"]:
                row = dict(lot, instrument_uid=uid)
                row["quantity"], row["basis_rub"] = decimal_str(row.pop("quantity")), decimal_str(row.pop("basis"))
                remaining_lots.append(row)
    baskets = [{"tax_year": yr, "tax_basket": basket, "gross_proceeds_rub": decimal_str(values["proceeds"]),
                "sale_fees_rub": decimal_str(values["fees"]), "fifo_basis_rub": decimal_str(values["basis"]),
                "realized_gain_rub": decimal_str(values["gain"]), "is_legal_taxable_base": False}
               for (yr, basket), values in sorted(annual.items())]
    return {"status": "LEDGER_CALCULATED", "data_kind": payload["data_kind"], "ledger_scope_id": scope,
            "tax_year": year,
            "accounting_currency": "RUB", "basis_method": "FIFO", "sales": sales,
            "basket_year_totals": baskets, "remaining_lots": remaining_lots,
            "separate_income_events": incomes, "cross_basket_loss_netting_applied": False,
            "tax_estimate": estimate_tax(payload.get("estimate_policy"), year),
            "full_tax_engine": False, "orders_created": False}


def evaluate(payload):
    try:
        with localcontext() as context:
            context.prec = 100
            return _evaluate(payload)
    except (ValueError, TypeError, KeyError, ArithmeticError, OverflowError):
        return {"status": "BLOCK", "reason": "Missing, unsupported or inconsistent FIFO transaction inputs",
                "ledger_complete": False, "orders_created": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="calculate FIFO ledger from normalized recorded transactions")
    report.add_argument("input", type=Path)
    commands.add_parser("rates", help="show limited FTS reference; does not adopt personal tax policy")
    args = parser.parse_args()
    if args.command == "rates":
        result = rates_reference()
    else:
        try:
            result = evaluate(json.loads(args.input.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            result = {"status": "BLOCK", "reason": "Unable to read normalized transaction JSON"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("status") == "BLOCK" else 0


if __name__ == "__main__":
    raise SystemExit(main())
