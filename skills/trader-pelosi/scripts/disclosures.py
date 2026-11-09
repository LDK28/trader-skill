#!/usr/bin/env python3
"""Offline Russian public issuer disclosure tracker; compatibility skill name.

    python3 disclosures.py index INPUT.json [--issuer QUERY] [--person QUERY]
    python3 disclosures.py new INPUT.json --state LOCAL_STATE.json [--write-state]

No web requests or PDF parser. Input is an explicitly normalized public index
with source documents, publication timestamps, event dates and optional ranges.
State tracks emitted content revisions, not presumed transactions of officials.
"""
import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


MODE = "ru_public_issuer"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value, name, maximum=2000):
    require(isinstance(value, str) and 0 < len(value.strip()) <= maximum, name + " is required")
    return value.strip()


def timestamp(value):
    result = datetime.fromisoformat(text(value, "timestamp").replace("Z", "+00:00"))
    require(result.tzinfo is not None and result.utcoffset() is not None, "timestamp must include timezone")
    return result


def iso_day(value):
    result = date.fromisoformat(value)
    require(result.isoformat() == value, "date must be YYYY-MM-DD")
    return result


def source_url(value):
    value = text(value, "source_url")
    parsed = urlsplit(value)
    require(parsed.scheme == "https" and bool(parsed.hostname) and
            parsed.username is None and parsed.password is None, "public HTTPS source URL is required")
    return value


def range_value(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,9})?", value),
            "range bounds must be nonnegative decimal strings")
    return Decimal(value)


def normalize_event(event, captured):
    require(isinstance(event, dict), "event must be an object")
    require(event.get("public") is True, "only explicitly public disclosures are supported")
    identifier, url = text(event.get("id"), "event id", 256), source_url(event.get("source_url"))
    issuer = event.get("issuer")
    require(isinstance(issuer, dict), "issuer identity required")
    issuer = {"id": text(issuer.get("id"), "issuer id", 256), "name": text(issuer.get("name"), "issuer name")}
    published = timestamp(event.get("published_at"))
    require(published <= captured, "publication is later than captured_at")
    require("event_date" in event, "event_date must be a date or explicit null")
    event_day = None if event["event_date"] is None else iso_day(event["event_date"])
    persons = event.get("persons", [])
    require(isinstance(persons, list) and len(persons) <= 100, "bounded persons list required")
    persons_out = []
    for person in persons:
        require(isinstance(person, dict), "person must be an object")
        persons_out.append({"name": text(person.get("name"), "public person name"),
                            "role": text(person.get("role"), "disclosed role")})
    persons_out.sort(key=lambda person: (person["name"], person["role"]))
    documents = event.get("documents", [])
    require(isinstance(documents, list) and len(documents) <= 100, "bounded documents list required")
    docs_out, document_ids = [], set()
    for document in documents:
        require(isinstance(document, dict), "document must be an object")
        doc_id = text(document.get("id"), "document id", 256)
        require(doc_id not in document_ids, "duplicate document id")
        document_ids.add(doc_id)
        doc = {"id": doc_id, "source_url": source_url(document.get("source_url"))}
        if "sha256" in document:
            require(isinstance(document["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", document["sha256"]), "invalid document sha256")
            doc["sha256"] = document["sha256"]
        docs_out.append(doc)
    docs_out.sort(key=lambda doc: doc["id"])
    amount_range = event.get("amount_range")
    if amount_range is not None:
        require(isinstance(amount_range, dict), "amount_range must be an object")
        currency = amount_range.get("currency")
        require(isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency), "range currency required")
        lower, upper = amount_range.get("lower"), amount_range.get("upper")
        require(lower is not None or upper is not None, "range must have at least one known bound")
        lo = None if lower is None else range_value(lower)
        hi = None if upper is None else range_value(upper)
        require(lo is None or hi is None or lo <= hi, "range bounds reversed")
        amount_range = {"currency": currency, "lower": lower, "upper": upper}
    details = event.get("details", {})
    require(isinstance(details, dict) and len(json.dumps(details, ensure_ascii=False, allow_nan=False)) <= 20000,
            "bounded normalized details required")
    row = {"id": identifier, "source_url": url, "issuer": issuer,
           "published_at": published.isoformat(), "event_date": None if event_day is None else event_day.isoformat(),
           "event_type": text(event.get("event_type"), "event_type", 256),
           "title": text(event.get("title"), "title"), "public": True, "persons": persons_out,
           "documents": docs_out, "amount_range": amount_range, "details": deepcopy(details)}
    canonical = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    row["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    row["publication_lag_calendar_days"] = None if event_day is None else (
        published.date() - event_day).days
    row["is_proven_trade"] = False
    return row


def _review(payload, state, issuer, person, since, new_only):
    require(isinstance(payload, dict) and type(payload.get("schema_version")) is int and
            payload["schema_version"] == 1 and payload.get("mode") == MODE,
            "schema_version 1 and explicit ru_public_issuer mode required")
    require(payload.get("data_kind") in ("synthetic", "normalized_public_disclosures"), "explicit data_kind required")
    captured = timestamp(payload.get("captured_at"))
    source_id = text(payload.get("source_id"), "namespaced source_id", 256)
    events = payload.get("events")
    require(isinstance(events, list) and len(events) <= 20000, "bounded event index required")
    source_index_url = source_url(payload.get("source_index_url"))
    if state is None:
        state = {"schema_version": 1, "mode": MODE, "seen": {}, "last_check_at": None}
    require(isinstance(state, dict) and type(state.get("schema_version")) is int and
            state["schema_version"] == 1 and state.get("mode") == MODE,
            "state schema/mode mismatch")
    seen = state.get("seen")
    require(isinstance(seen, dict) and len(seen) <= 100000, "bounded state seen map required")
    for key, value in seen.items():
        text(key, "seen key")
        require(isinstance(value, dict) and isinstance(value.get("fingerprint"), str) and
                re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"]), "invalid seen fingerprint")
    last_check = state.get("last_check_at")
    require(last_check is None or timestamp(last_check) <= captured, "state cannot be rolled back to an older capture")
    cutoff = None if since is None else iso_day(since)
    next_state, unique, result = deepcopy(state), {}, []
    for event in events:
        row = normalize_event(event, captured)
        key = json.dumps([MODE, source_id, row["id"]], ensure_ascii=False, separators=(",", ":"))
        if key in unique:
            require(unique[key] == row["fingerprint"], "conflicting duplicate event id")
            continue
        unique[key] = row["fingerprint"]
        if issuer and issuer.casefold() not in (row["issuer"]["id"] + " " + row["issuer"]["name"]).casefold():
            continue
        if person and not any(person.casefold() in value["name"].casefold() for value in row["persons"]):
            continue
        if cutoff and timestamp(row["published_at"]).astimezone(timezone.utc).date() < cutoff:
            continue
        old = seen.get(key)
        if new_only and old and old["fingerprint"] == row["fingerprint"]:
            continue
        row["change"] = "UPDATED" if old and old["fingerprint"] != row["fingerprint"] else "NEW" if not old else "SEEN"
        if row["change"] == "UPDATED":
            row["previous_fingerprint"] = old["fingerprint"]
        result.append(row)
        next_state["seen"][key] = {"fingerprint": row["fingerprint"], "published_at": row["published_at"],
                                   "source_url": row["source_url"]}
    result.sort(key=lambda row: (timestamp(row["published_at"]), row["id"]), reverse=True)
    next_state["last_check_at"] = captured.isoformat()
    return {"status": "DISCLOSURES_REVIEWED", "mode": MODE, "data_kind": payload["data_kind"],
            "captured_at": captured.isoformat(), "source_index_url": source_index_url,
            "coverage": "supplied_public_index_only", "events": result, "count": len(result),
            "next_state": next_state, "independently_verified_documents": False,
            "rf_congress_trade_registry_claimed": False, "orders_created": False}


def review(payload, state=None, issuer=None, person=None, since=None, new_only=True):
    try:
        require(issuer is None or isinstance(issuer, str), "issuer filter must be text")
        require(person is None or isinstance(person, str), "person filter must be text")
        return _review(payload, state, issuer, person, since, new_only)
    except (ValueError, TypeError, KeyError, ArithmeticError, OverflowError, RecursionError):
        return {"status": "BLOCK", "reason": "Missing, nonpublic, unsupported or inconsistent disclosure inputs",
                "state_updated": False, "orders_created": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("index", "new"))
    parser.add_argument("input", type=Path)
    parser.add_argument("--issuer")
    parser.add_argument("--person")
    parser.add_argument("--since", help="publication date lower bound, YYYY-MM-DD in UTC")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--write-state", action="store_true", help="explicitly persist emitted revision fingerprints")
    args = parser.parse_args()
    if args.write_state and (args.state is None or args.command != "new"):
        parser.error("--write-state requires new and --state")
    if args.write_state and args.state.resolve() == args.input.resolve():
        parser.error("state output must not overwrite the source index")
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        state = json.loads(args.state.read_text(encoding="utf-8")) if args.state and args.state.exists() else None
        result = review(payload, state, args.issuer, args.person, args.since, args.command == "new")
        if args.write_state and result["status"] != "BLOCK":
            args.state.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.state.with_name(args.state.name + ".tmp")
            temporary.write_text(json.dumps(result["next_state"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(args.state)
            result["state_updated"] = True
    except (OSError, ValueError):
        result = {"status": "BLOCK", "reason": "Unable to read inputs or persist state", "state_updated": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "BLOCK" else 0


if __name__ == "__main__":
    raise SystemExit(main())
