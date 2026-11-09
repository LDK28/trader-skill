#!/usr/bin/env python3
"""Prepare and journal offline T-Invest ticket drafts. Never sends orders."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'trader-tinvest' / 'scripts'))
from trader_common import dec, decimal_str, dumps, require, timestamp
from risk_engine import check_bundle, fingerprint


def prepare(bundle, now=None):
    risk = check_bundle(bundle, now)
    drafts = []
    if risk['status'] == 'PASS_OFFLINE':
        for key in ('buy_plan', 'sell_plan'):
            for order in (bundle.get(key) or {}).get('orders', []):
                instrument = order['instrument']
                ticker, board = instrument.get('ticker'), instrument.get('class_code')
                require(isinstance(ticker, str) and ticker and isinstance(board, str) and board,
                        'verified ticker and class_code mapping required for draft')
                require(instrument.get('identity_verified') is True,
                        'instrument UID to ticker/class_code mapping is not verified')
                payload = {'accountId': bundle['account_id'],
                           'instrumentId': ticker + '_' + board,
                           'quantity': order['lots'],
                           'direction': 'ORDER_DIRECTION_' + order['side'],
                           'orderType': 'ORDER_TYPE_LIMIT',
                           'price': {'value': decimal_str(dec(order['limit_price'], positive=True))},
                           'priceType': 'PRICE_TYPE_CURRENCY',
                           'timeInForce': 'TIME_IN_FORCE_DAY',
                           'confirmMarginTrade': False}
                drafts.append({'client_id': order['client_id'], 'instrument_uid': instrument['uid'],
                               'draft_parameters': payload, 'fingerprint': fingerprint(payload),
                               'status': 'DRAFT_ONLY'})
    return {'schema_version': 1, 'module': 'trader-broker', 'mode': 'offline',
            'status': 'DRAFTS_PREPARED' if drafts else 'BLOCK',
            'risk': risk, 'drafts': drafts, 'execution_enabled': False,
            'notice': 'No request was submitted. Drafts do not reserve cash or securities.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='Complete risk bundle, not a claimed PASS report')
    parser.add_argument('--as-of', help='Offline replay time only')
    parser.add_argument('--journal', type=Path, help='Append draft record to a local JSONL journal')
    args = parser.parse_args(argv)
    try:
        bundle = json.loads(args.input.read_text())
        result = prepare(bundle, timestamp(args.as_of) if args.as_of else None)
        if args.journal:
            args.journal.parent.mkdir(parents=True, exist_ok=True)
            with args.journal.open('a') as out:
                out.write(json.dumps({'recorded_at': datetime.now(timezone.utc).isoformat(),
                                      'status': result['status'],
                                      'bundle_fingerprint': result['risk']['bundle_fingerprint'],
                                      'draft_fingerprints': [d['fingerprint'] for d in result['drafts']]}) + '\n')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = {'status': 'BLOCK', 'errors': [str(exc)], 'drafts': [], 'execution_enabled': False}
    print(dumps(result))
    return 0 if result['status'] == 'DRAFTS_PREPARED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
