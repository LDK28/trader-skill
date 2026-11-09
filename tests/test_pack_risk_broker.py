import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/trader-tinvest/scripts'))
from risk_engine import check_bundle

SPEC = importlib.util.spec_from_file_location('pack_ticket', ROOT / 'skills/trader-broker/scripts/ticket.py')
TICKET = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TICKET)
NOW = datetime(2026, 1, 12, 7, tzinfo=timezone.utc)


def example_bundle():
    times = {'as_of': (NOW - timedelta(seconds=1)).isoformat(),
             'expires_at': (NOW + timedelta(seconds=20)).isoformat()}
    account, uid = 'demo-account', 'demo-uid-A'
    order = {'client_id': 'demo-buy-1', 'account_id': account, 'side': 'BUY',
             'order_type': 'LIMIT', 'lots': 1, 'own_funds_max_lots': 9,
             'limit_price': '100', 'price_unit': 'settlement_currency', 'currency': 'rub',
             'instrument': {'uid': uid, 'ticker': 'DEMO', 'class_code': 'TQBR', 'kind': 'share',
                            'lot_size': 10, 'price_step': '0.01', 'identity_verified': True,
                            'available': True, 'eligibility_verified': True,
                            'financing': 'fully_paid_cash', 'leveraged': False},
             'quote': dict(times, instrument_uid=uid),
             'estimate': dict(times, verified_for_this_order=True, all_costs_included=True,
                              at_limit_price=True, currency='rub', principal='1000', fees='1',
                              nkd='0', other='0', worst_case_total='1001',
                              binding={'account_id': account, 'instrument_uid': uid,
                                       'lots': 1, 'limit_price': '100', 'side': 'BUY'}),
             'own_funds_limit': dict(times, account_id=account, instrument_uid=uid,
                                    limit_price='100', source='buy_limits', verified=True, max_lots=9)}
    return {'schema_version': 1, 'account_id': account, 'execution_enabled': False,
            'data_origin': 'synthetic_fixture',
            'daily_risk': {'account_id': account, 'currency': 'rub', 'loss_limit_fraction': '0.50',
                           'baseline': {'account_id': account, 'currency': 'rub', 'day': '2026-01-12',
                                        'immutable': True, 'day_start_equity': '10000'},
                           'snapshot': dict(times, account_id=account, currency='rub', day='2026-01-12',
                                        current_equity='10000', net_external_cashflow='0',
                                        cashflow_verified=True)},
            'buy_plan': {'account_id': account, 'snapshot': dict(times, account_id=account,
                         verified=True, no_existing_debt=True,
                         cash_excludes_blocked_unsettled_borrowed=True,
                         spendable_own_cash={'rub': '10000'}), 'orders': [order]}}


class PackRiskBrokerTests(unittest.TestCase):
    def blocked(self, payload):
        self.assertEqual(check_bundle(payload, NOW)['status'], 'BLOCK')

    def test_valid_bundle_prepares_only_bounded_drafts(self):
        payload = example_bundle()
        self.assertEqual(check_bundle(payload, NOW)['status'], 'PASS_OFFLINE')
        result = TICKET.prepare(payload, NOW)
        self.assertFalse(result['execution_enabled'])
        draft = result['drafts'][0]['draft_parameters']
        self.assertEqual(draft['quantity'], 1)
        self.assertEqual(draft['instrumentId'], 'DEMO_TQBR')
        self.assertEqual(draft['priceType'], 'PRICE_TYPE_CURRENCY')
        self.assertIs(draft['confirmMarginTrade'], False)
        self.assertNotIn('token', str(result).lower())

    def test_rechecks_bundle_instead_of_trusting_claimed_pass(self):
        p = example_bundle()
        p['status'] = 'PASS'
        p['buy_plan']['orders'][0]['estimate']['binding']['lots'] = 2
        self.assertEqual(TICKET.prepare(p, NOW)['drafts'], [])

    def test_quote_and_cost_and_cap_are_bound_to_identity(self):
        for field in ('quote', 'estimate', 'own_funds_limit'):
            p = example_bundle()
            row = p['buy_plan']['orders'][0]
            if field == 'estimate':
                row[field]['binding']['account_id'] = 'other'
            else:
                row[field]['instrument_uid'] = 'other'
            self.blocked(p)

    def test_aggregate_lot_cap_cannot_be_reused(self):
        p = example_bundle()
        first = p['buy_plan']['orders'][0]
        first['own_funds_max_lots'] = first['own_funds_limit']['max_lots'] = 1
        second = copy.deepcopy(first)
        second['client_id'] = 'another'
        p['buy_plan']['orders'].append(second)
        self.blocked(p)

    def test_understated_principal_is_blocked(self):
        p = example_bundle()
        p['buy_plan']['orders'][0]['estimate'].update(principal='10', worst_case_total='11')
        self.blocked(p)

    def test_raw_points_and_wrong_tick_are_blocked(self):
        p = example_bundle()
        p['buy_plan']['orders'][0]['price_unit'] = 'pt'
        self.blocked(p)
        p = example_bundle()
        p['buy_plan']['orders'][0]['instrument']['price_step'] = '3'
        self.blocked(p)

    def test_freshness_daily_stop_and_baseline_day(self):
        p = example_bundle()
        p['buy_plan']['snapshot']['as_of'] = '2025-01-01T00:00:00Z'
        self.blocked(p)
        p = example_bundle()
        p['daily_risk']['snapshot']['current_equity'] = '5000'
        self.blocked(p)
        p = example_bundle()
        p['daily_risk']['baseline']['day'] = '2026-01-11'
        self.blocked(p)

    def test_optional_size_count_concentration_and_recorded_limits(self):
        cases = [{'max_orders_per_day': 0}, {'blocked_uids': ['demo-uid-A']},
                 {'recorded_client_ids': ['demo-buy-1']},
                 {'amount_currency': 'rub', 'max_order_amount': '1000'},
                 {'max_position_fraction': '0.05', 'equity': '10000',
                  'valuation_currency': 'rub', 'position_values': {'demo-uid-A': '0'}}]
        for limits in cases:
            p = example_bundle()
            p['limits'] = limits
            self.blocked(p)

    def test_no_execution_or_margin_opt_out_and_no_unknown_limits(self):
        p = example_bundle()
        p['execution_enabled'] = True
        self.blocked(p)
        p = example_bundle()
        p['limits'] = {'allow_margin': True}
        self.blocked(p)
        p = example_bundle()
        p['buy_plan']['orders'][0]['instrument']['leveraged'] = True
        self.blocked(p)

    def test_draft_requires_ticker_to_uid_mapping(self):
        p = example_bundle()
        p['buy_plan']['orders'][0]['instrument']['identity_verified'] = False
        with self.assertRaisesRegex(ValueError, 'mapping'):
            TICKET.prepare(p, NOW)

    def test_malformed_values_fail_closed(self):
        for payload in (None, [], {'schema_version': 1, 'account_id': 'demo'},
                        dict(example_bundle(), unrelated_nan=float('nan'))):
            self.blocked(payload)

    def test_daily_observation_must_be_fresh_and_aligned(self):
        for change in ({'as_of': '2025-01-12T06:59:59Z'},
                       {'expires_at': '2025-01-12T07:00:20Z'},
                       {'as_of': '2026-01-12T06:59:50Z'}):
            p = example_bundle()
            p['daily_risk']['snapshot'].update(change)
            self.blocked(p)
        p = example_bundle()
        del p['daily_risk']['snapshot']['as_of']
        self.blocked(p)

    def test_repeated_uid_requires_consistent_metadata_and_reverse_mapping(self):
        for field, value in (('ticker', 'DIFFERENT'), ('class_code', 'TQTF'),
                             ('lot_size', 1), ('price_step', '0.1')):
            p = example_bundle()
            second = copy.deepcopy(p['buy_plan']['orders'][0])
            second['client_id'] = 'second'
            second['instrument'][field] = value
            p['buy_plan']['orders'].append(second)
            self.blocked(p)
        p = example_bundle()
        second = copy.deepcopy(p['buy_plan']['orders'][0])
        second['client_id'] = 'second'
        second['instrument']['uid'] = 'different-uid'
        second['quote']['instrument_uid'] = 'different-uid'
        second['estimate']['binding']['instrument_uid'] = 'different-uid'
        second['own_funds_limit']['instrument_uid'] = 'different-uid'
        p['buy_plan']['orders'].append(second)
        self.blocked(p)

    def test_concentration_uses_daily_equity_and_same_valuation_snapshot(self):
        p = example_bundle()
        p['limits'] = {'max_position_fraction': '0.2', 'equity': '10000',
                       'valuation_currency': 'rub',
                       'valuation_as_of': p['daily_risk']['snapshot']['as_of'],
                       'position_values': {'demo-uid-A': '0'}}
        self.assertEqual(check_bundle(p, NOW)['status'], 'PASS_OFFLINE')
        for change in ({'equity': '1000000000'}, {'valuation_currency': 'usd'},
                       {'valuation_as_of': '2025-01-12T06:59:59Z'}):
            bad = copy.deepcopy(p)
            bad['limits'].update(change)
            self.blocked(bad)


if __name__ == '__main__':
    unittest.main()
