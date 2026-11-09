#!/usr/bin/env python3
"""Offline combined T-Invest risk review. Optional --as-of is replay only."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'trader-tinvest' / 'scripts'))
from trader_common import dumps, timestamp
from risk_engine import check_bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--as-of', help='Explicit offline replay timestamp, never a live check')
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.input.read_text())
        result = check_bundle(payload, timestamp(args.as_of) if args.as_of else None)
    except (OSError, ValueError, TypeError) as exc:
        result = {'status': 'BLOCK', 'errors': [str(exc)], 'execution_enabled': False}
    print(dumps(result))
    return 0 if result['status'] == 'PASS_OFFLINE' else 2


if __name__ == '__main__':
    raise SystemExit(main())
