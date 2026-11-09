"""Synthetic offline portfolio-module tests; no credentials, network or trades."""
import copy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def module(folder, filename):
    spec = importlib.util.spec_from_file_location('test_' + folder.replace('-', '_'),
                                                ROOT / 'skills' / folder / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


budget = module('trader-budget', 'size.py')
rebalance = module('trader-rebalance', 'rebalance.py')
copycat = module('trader-copycat', 'holdings.py')


def metadata(uid, principal='100', lot=1):
    return {'uid': uid, 'class_code': 'SYNTH', 'currency': 'rub', 'lot_size': lot,
            'kind': 'share', 'leveraged': False, 'financing': 'fully_paid_cash',
            'cost_source': 'synthetic', 'principal_per_lot': principal, 'fee_per_lot': '0',
            'nkd_per_lot': '0', 'other_per_lot': '0', 'fixed_fee': '0'}


def allocation():
    return {'account_id': 'synthetic', 'currency': 'rub', 'as_of': '2026-10-01T10:00:00Z',
            'own_cash': '10000', 'targets': [{'uid': 'A', 'weight': '0.8'},
                                            {'uid': 'B', 'weight': '0.1'},
                                            {'uid': 'C', 'weight': '0.1'}],
            'instruments': [metadata(uid) for uid in ('A', 'B', 'C')],
            'rules': {'cash_buffer_fraction': '0', 'max_position_fraction': '0.5'}}


def rebalance_input():
    return {'account_id': 'synthetic', 'currency': 'rub', 'as_of': '2026-10-01T10:00:00Z',
            'own_cash': '1000', 'targets': [{'uid': 'A', 'weight': '0.5'}, {'uid': 'B', 'weight': '0.5'}],
            'instruments': [metadata('A', '1000', 10), metadata('B', '1000', 10)],
            'rules': {'cash_buffer_fraction': '0'}, 'band_fraction': '0.05',
            'positions': [{'uid': 'A', 'currency': 'rub', 'units': '80', 'free_units': '20',
                           'value': '8000', 'sell_max_lots': 2},
                          {'uid': 'B', 'currency': 'rub', 'units': '10', 'free_units': '10',
                           'value': '1000', 'sell_max_lots': 1}]}


def public_input():
    current = {'entity': 'Synthetic fund', 'source_url': 'https://example.invalid/current',
               'source_kind': 'synthetic_fixture', 'as_of': '2026-09-30',
               'published_at': '2026-10-01T10:00:00Z', 'currency': 'rub', 'complete': True,
               'quantity_basis': 'common adjusted units', 'coverage_scope': 'all synthetic holdings',
               'holdings': [{'isin': 'RU000SYNTH01', 'name': 'Synthetic A', 'units': '6', 'value': '600'},
                            {'isin': 'RU000SYNTH02', 'name': 'Synthetic B', 'units': '3', 'value': '300'},
                            {'isin': 'RU000SYNTH03', 'name': 'Synthetic C', 'units': '1', 'value': '100'}]}
    prior = copy.deepcopy(current)
    prior.update(as_of='2026-06-30', published_at='2026-07-01T10:00:00Z',
                 source_url='https://example.invalid/prior')
    prior['holdings'] = [{'isin': 'RU000SYNTH01', 'name': 'Synthetic A', 'units': '5', 'value': '500'},
                         {'isin': 'RU000SYNTH02', 'name': 'Synthetic B', 'units': '4', 'value': '400'},
                         {'isin': 'RU000SYNTH04', 'name': 'Synthetic D', 'units': '1', 'value': '100'}]
    return {'current': current, 'prior': prior, 'mapping': [
        {'isin': 'RU000SYNTH0' + str(i), 'uid': uid, 'class_code': 'SYNTH', 'currency': 'rub',
         'lot_size': 1, 'source': 'synthetic mapping'} for i, uid in enumerate(('A', 'B', 'C'), 1)],
        'top_n': 2, 'min_weight_fraction': '0', 'exclude_isins': []}


class BudgetTests(unittest.TestCase):
    def test_capped_excess_is_redistributed_by_weights(self):
        result = budget.analyze(allocation())
        costs = {row['uid']: row['estimated_cash_cost'] for row in result['proposals']}
        self.assertEqual(costs, {'A': Decimal('5000'), 'B': Decimal('2500'), 'C': Decimal('2500')})
        self.assertEqual(result['remaining_own_cash'], 0)

    def test_buffer_and_unreachable_caps_leave_explicit_cash(self):
        data = allocation()
        data['targets'] = data['targets'][:2]
        data['rules'].update(cash_buffer_fraction='0.1', max_position_fraction='0.25')
        result = budget.analyze(data)
        self.assertEqual(result['cash_buffer'], Decimal('1000'))
        self.assertEqual(result['estimated_spend'], Decimal('5000'))
        self.assertEqual(result['rounding_or_cap_idle'], Decimal('4000'))

    def test_lots_include_every_cost_and_fixed_charge(self):
        data = allocation()
        data.update(own_cash='1000', targets=[{'uid': 'A', 'weight': '1'}], rules={})
        data['instruments'][0].update(lot_size=10, principal_per_lot='100', fee_per_lot='1',
                                      nkd_per_lot='2', other_per_lot='1', fixed_fee='5')
        result = budget.analyze(data)
        self.assertEqual(result['proposals'][0]['lots'], 9)
        self.assertEqual(result['proposals'][0]['units'], '90')
        self.assertEqual(result['estimated_spend'], Decimal('941'))
        self.assertEqual(result['remaining_own_cash'], Decimal('59'))

    def test_unaffordable_target_is_explicitly_dropped(self):
        data = allocation()
        data['instruments'][0]['principal_per_lot'] = '20000'
        result = budget.analyze(data)
        self.assertIn('A', [row['uid'] for row in result['dropped']])
        self.assertTrue(all(row['uid'] != 'A' for row in result['proposals']))
        self.assertLessEqual(result['estimated_spend'], Decimal(data['own_cash']))

    def test_minimum_order_never_emits_tiny_partial_order(self):
        data = allocation()
        data.update(own_cash='400', rules={'min_order': '250'})
        result = budget.analyze(data)
        self.assertTrue(result['proposals'])
        self.assertTrue(all(row['estimated_cash_cost'] >= Decimal('250') for row in result['proposals']))
        self.assertLessEqual(result['estimated_spend'], Decimal('400'))

    def test_missing_cost_float_duplicate_and_borrowed_kinds_rejected(self):
        variants = []
        data = allocation(); del data['instruments'][0]['nkd_per_lot']; variants.append(data)
        data = allocation(); data['own_cash'] = 10000.0; variants.append(data)
        data = allocation(); data['targets'].append(copy.deepcopy(data['targets'][0])); variants.append(data)
        data = allocation(); data['instruments'][0]['currency'] = 'usd'; variants.append(data)
        data = allocation(); data['instruments'][0]['kind'] = 'future'; variants.append(data)
        data = allocation(); data['instruments'][0]['leveraged'] = True; variants.append(data)
        for data in variants:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    budget.analyze(data)

    def test_target_mapping_must_match_current_instrument_metadata(self):
        for key, value in (('class_code', 'OTHER'), ('currency', 'usd'), ('lot_size', 100)):
            data = allocation(); data['targets'][0][key] = value
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, 'target and metadata disagree'):
                    budget.analyze(data)


class RebalanceTests(unittest.TestCase):
    def test_sell_is_limited_to_free_owned_lots_and_buy_ignores_proceeds(self):
        result = rebalance.analyze(rebalance_input())
        sells = [row for row in result['proposals'] if row['side'] == 'SELL']
        buys = [row for row in result['proposals'] if row['side'] == 'BUY']
        self.assertEqual(sells[0]['lots'], 2)
        self.assertEqual(sells[0]['units'], '20')
        self.assertEqual(buys[0]['lots'], 1)
        self.assertEqual(result['buy_cost_from_current_cash'], Decimal('1000'))
        self.assertFalse(result['future_sell_proceeds_credited'])

    def test_broker_sell_cap_is_respected(self):
        data = rebalance_input()
        data['positions'][0]['sell_max_lots'] = 1
        result = rebalance.analyze(data)
        self.assertEqual([row['lots'] for row in result['proposals'] if row['side'] == 'SELL'], [1])

    def test_missing_sell_cap_never_silently_grants_sale(self):
        data = rebalance_input()
        del data['positions'][0]['sell_max_lots']
        result = rebalance.analyze(data)
        self.assertFalse(any(row['side'] == 'SELL' for row in result['proposals']))
        self.assertIn('sell_max_lots', result['notes'][0]['reason'])

    def test_zero_cash_sale_proposals_do_not_fund_buys(self):
        data = rebalance_input(); data['own_cash'] = '0'
        result = rebalance.analyze(data)
        self.assertTrue(any(row['side'] == 'SELL' for row in result['proposals']))
        self.assertFalse(any(row['side'] == 'BUY' for row in result['proposals']))

    def test_band_boundary_and_untracked_opt_in(self):
        data = rebalance_input(); data['band_fraction'] = '0.4'
        self.assertEqual(rebalance.analyze(data)['proposals'], [])
        data = rebalance_input(); data['targets'] = [{'uid': 'B', 'weight': '1'}]
        self.assertFalse(any(row['uid'] == 'A' for row in rebalance.analyze(data)['proposals']))
        data['sell_untracked'] = True
        self.assertTrue(any(row['uid'] == 'A' and row['side'] == 'SELL'
                            for row in rebalance.analyze(data)['proposals']))

    def test_reserve_can_prevent_new_buys_and_invalid_free_units_fail(self):
        data = rebalance_input(); data['rules']['cash_buffer_fraction'] = '0.1'
        self.assertFalse(any(row['side'] == 'BUY' for row in rebalance.analyze(data)['proposals']))
        data = rebalance_input(); data['positions'][0]['free_units'] = '81'
        with self.assertRaises(ValueError):
            rebalance.analyze(data)

    def test_drifted_target_does_not_silently_change_trading_class(self):
        data = rebalance_input(); data['targets'][0]['class_code'] = 'OTHER'
        with self.assertRaisesRegex(ValueError, 'target and metadata disagree'):
            rebalance.analyze(data)


class CopycatTests(unittest.TestCase):
    def test_top_n_renormalizes_and_diff_classifies_unit_changes(self):
        result = copycat.analyze(public_input())
        self.assertEqual(sum(row['weight'] for row in result['targets']), Decimal(1))
        self.assertEqual([row['uid'] for row in result['targets']], ['A', 'B'])
        self.assertEqual(result['selected_positions_value'], Decimal('900'))
        changes = {row['isin']: row['status'] for row in result['comparison']['changes']}
        self.assertEqual(changes, {'RU000SYNTH01': 'ADDED', 'RU000SYNTH02': 'TRIMMED',
                                   'RU000SYNTH03': 'NEW', 'RU000SYNTH04': 'SOLD'})

    def test_aggregation_is_by_isin_not_name(self):
        data = public_input()
        data['current']['holdings'][0].update(units='2', value='200')
        data['current']['holdings'].append({'isin': 'RU000SYNTH01', 'name': 'Alias A',
                                             'units': '4', 'value': '400'})
        result = copycat.analyze(data)
        self.assertEqual(result['selected'][0]['units'], Decimal('6'))
        self.assertEqual(result['selected'][0]['value'], Decimal('600'))

    def test_missing_major_mapping_cannot_substitute_minor_holding(self):
        data = public_input(); data['mapping'] = data['mapping'][1:]
        result = copycat.analyze(data)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertEqual(result['targets'], [])
        self.assertEqual(result['missing_mapping'], ['RU000SYNTH01'])
        self.assertEqual([row['isin'] for row in result['selected']], ['RU000SYNTH01', 'RU000SYNTH02'])

    def test_incomplete_or_incomparable_reports_never_claim_sales(self):
        variants = []
        for key, value in (('complete', False), ('quantity_basis', 'different'),
                           ('coverage_scope', 'only equities'), ('entity', 'other fund'),
                           ('currency', 'usd')):
            data = public_input(); data['prior'][key] = value; variants.append(data)
        data = public_input(); data['prior']['as_of'] = data['current']['as_of']
        data['prior']['published_at'] = data['current']['published_at']; variants.append(data)
        for data in variants:
            with self.subTest(data=data):
                comparison = copycat.analyze(data)['comparison']
                self.assertFalse(comparison['comparable'])
                self.assertEqual(comparison['changes'], [])

    def test_exclusions_minimum_and_ambiguous_mapping(self):
        data = public_input(); data['exclude_isins'] = ['RU000SYNTH01']; data['min_weight_fraction'] = '0.2'
        result = copycat.analyze(data)
        self.assertEqual(result['targets'][0]['uid'], 'B')
        self.assertEqual(result['targets'][0]['weight'], Decimal(1))
        data = public_input(); data['mapping'].append(copy.deepcopy(data['mapping'][0]))
        with self.assertRaises(ValueError):
            copycat.analyze(data)

    def test_public_provenance_and_decimal_inputs_required(self):
        data = public_input(); del data['current']['source_url']
        with self.assertRaises(ValueError):
            copycat.analyze(data)
        data = public_input(); data['current']['holdings'][0]['value'] = 600.0
        with self.assertRaises(ValueError):
            copycat.analyze(data)


class PortfolioCliTests(unittest.TestCase):
    def test_synthetic_examples_run_and_emit_no_execution_flags(self):
        for folder, script in (('trader-budget', 'size.py'), ('trader-rebalance', 'rebalance.py'),
                               ('trader-copycat', 'holdings.py')):
            with self.subTest(folder=folder):
                path = ROOT / 'skills' / folder
                run = subprocess.run([sys.executable, str(path / script), str(path / 'references/example.json')],
                                     capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                data = json.loads(run.stdout)
                self.assertEqual(data['mode'], 'offline')
                self.assertIs(data['execution_enabled'], False)


if __name__ == '__main__':
    unittest.main()
