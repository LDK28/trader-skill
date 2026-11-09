"""Shared offline input/CLI helpers. No network, credentials or execution."""
import argparse
from datetime import datetime
from decimal import Decimal, localcontext
import importlib.util
import json
from pathlib import Path
import re
import sys


def require(condition, message):
    if not condition:
        raise ValueError(message)


def dec(value, label='value', positive=False, nonnegative=False):
    require(isinstance(value, str) and len(value) <= 64 and
            bool(re.fullmatch(r'-?\d{1,30}(?:\.\d{1,18})?', value)),
            label + ' must be a finite plain decimal string')
    result = Decimal(value)
    require(result.is_finite(), label + ' must be finite')
    if positive:
        require(result > 0, label + ' must be positive')
    elif nonnegative:
        require(result >= 0, label + ' must be nonnegative')
    return result


def decimal_str(value):
    require(isinstance(value, Decimal) and value.is_finite(), 'finite Decimal required')
    text = format(value, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return '0' if text in ('-0', '') else text


def timestamp(value, label='timestamp'):
    require(isinstance(value, str), label + ' is required')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(label + ' must be ISO 8601') from None
    require(result.tzinfo is not None and result.utcoffset() is not None,
            label + ' must include timezone')
    return result


def load_companion(name):
    require(bool(re.fullmatch(r'[a-z_]+', name)), 'invalid companion module name')
    path = Path(__file__).resolve().parent / (name + '.py')
    require(path.is_file(), 'companion module is not installed: ' + name)
    key = '_trader_tinvest_' + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2,
                      default=lambda x: decimal_str(x) if isinstance(x, Decimal) else str(x))


def cli_main(handler, description, argv=None):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('input', type=Path, help='Offline JSON input; no credentials')
    parser.add_argument('--output', type=Path, help='Optional local JSON result file')
    args = parser.parse_args(argv)
    try:
        def invalid_constant(_):
            raise ValueError('non-finite JSON numbers are not accepted')
        payload = json.loads(args.input.read_text(), parse_constant=invalid_constant)
        with localcontext() as context:
            context.prec = 80
            result = handler(payload)
        require(isinstance(result, dict), 'module result must be an object')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = {'status': 'INVALID_INPUT', 'errors': [str(exc)],
                  'mode': 'offline', 'execution_enabled': False}
    rendered = dumps(result) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    else:
        print(rendered, end='')
    return 2 if result.get('status') in (
        'BLOCK', 'INVALID_INPUT', 'INCOMPLETE', 'INSUFFICIENT_DATA') else 0
