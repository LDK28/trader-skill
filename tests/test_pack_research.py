"""Synthetic, offline behavior tests for the four research skill ports."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(skill, filename):
    path = ROOT / 'skills' / skill / 'scripts' / filename
    spec = importlib.util.spec_from_file_location('pack_' + skill.replace('-', '_'), path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


NEWS = module('trader-news', 'news.py')
FACTS = module('trader-analyst', 'facts.py')
SCREEN = module('trader-screener', 'screen.py')
ALERTS = module('trader-alerts', 'alerts.py')


def example(skill, name):
    return json.loads((ROOT / 'skills' / skill / 'references' / name).read_text())


class NewsTests(unittest.TestCase):
    def test_mcp_uid_link_and_updated_timestamp(self):
        payload = example('trader-news', 'news.example.json')
        result = NEWS.evaluate(payload)
        item = next(row for row in result['items'] if row['id'] == 'synthetic-news-1')
        self.assertEqual(item['timestamp_kind'], 'source_update')
        self.assertEqual(item['instruments'][0]['uid'], 'demo-share')
        self.assertIn('earnings', item['tags'])
        self.assertFalse(item['content_verified'])

    def test_state_prevents_repeated_publications(self):
        payload = example('trader-news', 'news.example.json')
        first = NEWS.evaluate(payload)
        payload['state'] = first['state']
        self.assertEqual(NEWS.evaluate(payload)['items'], [])
        payload['show_seen'] = True
        self.assertEqual(len(NEWS.evaluate(payload)['items']), 2)

    def test_url_dedup_merges_uid_references_and_retains_latest_date(self):
        payload = {'as_of': '2026-11-09T12:00:00Z', 'items': [
            {'title': 'Новость', 'source': 'A', 'published_at': '2026-11-09T11:00:00Z',
             'url': 'https://example.invalid/a?utm_source=x', 'instruments': [{'uid': 'x', 'class_code': 'TQBR'}]},
            {'title': 'Новость', 'source': 'B', 'published_at': '2026-11-09T10:00:00Z',
             'url': 'https://example.invalid/a#fragment', 'instruments': [{'uid': 'y', 'class_code': 'TQBR'}]}]}
        result = NEWS.evaluate(payload)
        self.assertEqual(len(result['items']), 1)
        item = result['items'][0]
        self.assertEqual(item['published_at'], '2026-11-09T11:00:00Z')
        self.assertEqual({r['uid'] for r in item['instruments']}, {'x', 'y'})

    def test_window_future_missing_source_and_ambiguous_ticker(self):
        payload = {'as_of': '2026-11-09T12:00:00Z', 'hours': 1,
                   'watchlist': [{'uid': 'x', 'class_code': 'TQBR', 'ticker': 'SAME'}, {'uid': 'y', 'class_code': 'SPB', 'ticker': 'SAME'}],
                   'items': [{'title': 'Future', 'source': 'X', 'published_at': '2026-11-10T10:00:00Z'},
                             {'title': 'Old', 'source': 'X', 'published_at': '2026-11-01T10:00:00Z'},
                             {'title': 'No source', 'published_at': '2026-11-09T11:30:00Z'},
                             {'title': 'Current', 'source': 'X', 'published_at': '2026-11-09T11:30:00Z', 'instruments': [{'ticker': 'SAME'}]}]}
        result = NEWS.evaluate(payload)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['instruments'], [])
        self.assertEqual(len(result['items'][0]['unresolved_references']), 1)
        self.assertEqual(len(result['rejected']), 2)

    def test_local_rss_file_and_atom_are_supported(self):
        rss = '<rss><channel><item><title>RSS</title><link>https://example.invalid/rss</link><pubDate>Mon, 09 Nov 2026 12:00:00 +0300</pubDate></item></channel></rss>'
        atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>a1</id><title>Atom</title><updated>2026-11-09T10:00:00Z</updated><link href="https://example.invalid/atom"/></entry></feed>'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'feed.xml'
            path.write_text(rss)
            result = NEWS.evaluate({'as_of': '2026-11-09T12:00:00Z', 'feeds': [{'source': 'RSS', 'path': str(path)}, {'source': 'Atom', 'xml': atom}]})
        self.assertEqual(len(result['items']), 2)
        self.assertEqual({r['published_at'] for r in result['items']}, {'2026-11-09T09:00:00Z', '2026-11-09T10:00:00Z'})

    def test_entity_xml_and_naive_time_are_rejected(self):
        with self.assertRaises(ValueError):
            NEWS.parse_feed('<!DOCTYPE x [<!ENTITY y "bad">]><rss/>', 'X')
        with self.assertRaises(ValueError):
            NEWS.date_value('2026-11-09T10:00:00')


class AnalystTests(unittest.TestCase):
    def test_yoy_margins_eps_and_missing_are_not_zero(self):
        result = FACTS.evaluate(example('trader-analyst', 'reports.example.json'))
        metrics = result['companies'][0]['metrics']
        self.assertEqual(metrics['revenue_growth_yoy']['value'], '20')
        self.assertEqual(metrics['net_margin']['value'], '25')
        self.assertEqual(metrics['eps_diluted_growth_yoy']['value'], '50')
        self.assertNotIn('gross_margin', metrics)

    def test_ifrs_rsbu_and_scope_are_separate(self):
        payload = example('trader-analyst', 'reports.example.json')
        separate = copy.deepcopy(payload['companies'][0]['reports'][-1])
        separate.update(standard='RSBU', scope='standalone')
        separate['metrics']['net_income']['value'] = '90'
        payload['companies'][0]['reports'].append(separate)
        rows = FACTS.evaluate(payload)['companies']
        self.assertEqual(len(rows), 2)
        for row in rows:
            if row['standard'] == 'IFRS':
                self.assertEqual(row['metrics']['net_margin']['value'], '25')
            else:
                self.assertEqual(row['metrics']['net_margin']['value'], '75')
                self.assertNotIn('net_income_growth_yoy', row['metrics'])

    def test_future_restatement_is_excluded_at_cutoff(self):
        payload = example('trader-analyst', 'reports.example.json')
        restated = copy.deepcopy(payload['companies'][0]['reports'][-1])
        restated['published_at'] = '2026-12-01T09:00:00Z'
        restated['metrics']['revenue']['value'] = '999'
        payload['companies'][0]['reports'].append(restated)
        result = FACTS.evaluate(payload)
        self.assertEqual(result['excluded_future_reports'], 1)
        self.assertEqual(result['companies'][0]['metrics']['revenue']['value'], '120')

    def test_latest_known_restatement_replaces_same_period(self):
        payload = example('trader-analyst', 'reports.example.json')
        restated = copy.deepcopy(payload['companies'][0]['reports'][-1])
        restated.update(published_at='2026-09-01T09:00:00Z', restated=True)
        restated['metrics']['revenue']['value'] = '150'
        payload['companies'][0]['reports'].append(restated)
        result = FACTS.evaluate(payload)
        self.assertEqual(len(result['reports']), 2)
        self.assertEqual(result['companies'][0]['metrics']['revenue_growth_yoy']['value'], '50')

    def test_negative_yoy_base_does_not_invent_growth(self):
        payload = example('trader-analyst', 'reports.example.json')
        payload['companies'][0]['reports'][0]['metrics']['net_income']['value'] = '-20'
        result = FACTS.evaluate(payload)['companies'][0]
        self.assertNotIn('net_income_growth_yoy', result['metrics'])
        self.assertTrue(any('nonpositive' in note for note in result['notes']))

    def test_currency_units_and_publication_are_enforced(self):
        for field, value in (('currency', 'usd'), ('unit', 'percent'), ('value', 120.0)):
            payload = example('trader-analyst', 'reports.example.json')
            payload['companies'][0]['reports'][-1]['metrics']['revenue'][field] = value
            with self.assertRaises(ValueError):
                FACTS.evaluate(payload)
        payload = example('trader-analyst', 'reports.example.json')
        payload['companies'][0]['reports'][-1]['published_at'] = '2026-01-01T09:00:00Z'
        with self.assertRaises(ValueError):
            FACTS.evaluate(payload)

    def test_ttm_requires_four_discrete_contiguous_quarters(self):
        reports = []
        for index, (start, end) in enumerate((('2025-01-01', '2025-03-31'), ('2025-04-01', '2025-06-30'), ('2025-07-01', '2025-09-30'), ('2025-10-01', '2025-12-31'))):
            reports.append({'standard': 'IFRS', 'scope': 'consolidated', 'currency': 'rub', 'period_type': 'quarter', 'flow_basis': 'discrete',
                            'period_start': start, 'period_end': end, 'published_at': '2026-02-01T00:00:00Z', 'source_url': 'https://example.invalid/q' + str(index),
                            'metrics': {'revenue': {'value': str(10 * (index + 1)), 'unit': 'money', 'currency': 'rub'}}})
        payload = {'as_of': '2026-11-09T00:00:00Z', 'companies': [{'uid': 'x', 'class_code': 'TQBR', 'reports': reports}]}
        self.assertEqual(FACTS.evaluate(payload)['companies'][0]['metrics']['revenue_ttm']['value'], '100')
        payload['companies'][0]['reports'][1]['flow_basis'] = 'year_to_date'
        self.assertNotIn('revenue_ttm', next(r for r in FACTS.evaluate(payload)['companies'] if r['flow_basis'] == 'discrete')['metrics'])


class ScreenerTests(unittest.TestCase):
    def test_pass_and_weighted_relative_ranking(self):
        payload = example('trader-screener', 'screen.example.json')
        second = copy.deepcopy(payload['companies'][0])
        second['uid'] = second['availability']['uid'] = 'second'
        second['metrics']['revenue_growth_yoy']['value'] = '10'
        payload['companies'].append(second)
        result = SCREEN.evaluate(payload)
        self.assertEqual(result['passed_count'], 2)
        self.assertEqual([r['uid'] for r in result['shortlist']], ['demo-share', 'second'])
        self.assertEqual([r['score'] for r in result['shortlist']], ['1', '0'])
        self.assertNotIn('weight', result['shortlist'][0])

    def test_missing_unit_currency_lookahead_and_availability_fail(self):
        mutations = [('missing', None), ('unit', 'ratio'), ('future', '2027-01-01T00:00:00Z'), ('availability', False)]
        for kind, value in mutations:
            with self.subTest(kind=kind):
                payload = example('trader-screener', 'screen.example.json')
                row = payload['companies'][0]
                if kind == 'missing':
                    del row['metrics']['net_margin']
                elif kind == 'unit':
                    row['metrics']['net_margin']['unit'] = value
                elif kind == 'future':
                    row['metrics']['net_margin']['published_at'] = value
                else:
                    row['availability']['account_available'] = value
                result = SCREEN.evaluate(payload)
                self.assertEqual(result['passed_count'], 0)
                self.assertTrue(result['results'][0]['reasons'])

    def test_account_identity_and_accounting_basis_cannot_mix(self):
        for field, value in (('uid', 'other'), ('class_code', 'other')):
            payload = example('trader-screener', 'screen.example.json')
            payload['companies'][0]['availability'][field] = value
            self.assertEqual(SCREEN.evaluate(payload)['passed_count'], 0)
        payload = example('trader-screener', 'screen.example.json')
        payload['companies'][0]['standard'] = 'RSBU'
        self.assertEqual(SCREEN.evaluate(payload)['passed_count'], 0)

    def test_money_rules_require_same_currency(self):
        payload = example('trader-screener', 'screen.example.json')
        payload['companies'][0]['metrics']['capital'] = {'value': '100', 'unit': 'money', 'currency': 'usd'}
        payload['spec']['rules'] = [{'metric': 'capital', 'unit': 'money', 'currency': 'rub', 'op': '>=', 'value': '10'}]
        self.assertEqual(SCREEN.evaluate(payload)['passed_count'], 0)

    def test_between_membership_ties_and_missing_rank(self):
        payload = example('trader-screener', 'screen.example.json')
        payload['companies'][0]['metrics']['sector'] = {'value': 'demo', 'unit': 'text'}
        payload['spec']['rules'] = [{'metric': 'net_margin', 'unit': 'percent', 'op': 'between', 'value': ['20', '30']}, {'metric': 'sector', 'unit': 'text', 'op': 'in', 'value': ['demo']}]
        self.assertEqual(SCREEN.evaluate(payload)['passed_count'], 1)
        del payload['companies'][0]['metrics']['revenue_growth_yoy']
        self.assertEqual(SCREEN.evaluate(payload)['passed_count'], 0)


class AlertTests(unittest.TestCase):
    def test_crossing_and_session_are_not_repeated(self):
        payload = example('trader-alerts', 'alerts.example.json')
        first = ALERTS.evaluate(payload)
        self.assertEqual(len(first['events']), 2)
        self.assertEqual(first['events'][0]['event'], 'crossing')
        self.assertEqual(first['market_status']['DEMO']['status'], 'OPEN')
        payload['state'] = first['state']
        self.assertEqual(ALERTS.evaluate(payload)['events'], [])

    def test_one_shot_and_rearm(self):
        for rearm, expected in ((True, 2), (False, 1)):
            payload = example('trader-alerts', 'alerts.example.json')
            payload['rules'] = [dict(payload['rules'][0], rearm=rearm, cooldown_seconds=0)]
            payload['quotes'] += [dict(payload['quotes'][0], price='99', as_of='2026-11-09T10:01:00Z'), dict(payload['quotes'][0], price='102', as_of='2026-11-09T10:02:00Z')]
            payload['as_of'] = '2026-11-09T10:02:00Z'
            self.assertEqual(len(ALERTS.evaluate(payload)['events']), expected)

    def test_cooldown_drops_crossing_then_allows_later_new_crossing(self):
        payload = example('trader-alerts', 'alerts.example.json')
        payload['rules'] = [dict(payload['rules'][0], cooldown_seconds=180)]
        base = payload['quotes'][0]
        payload['quotes'] += [dict(base, price='99', as_of='2026-11-09T10:01:00Z'), dict(base, price='102', as_of='2026-11-09T10:02:00Z'),
                              dict(base, price='99', as_of='2026-11-09T10:03:00Z'), dict(base, price='103', as_of='2026-11-09T10:04:00Z')]
        payload['as_of'] = '2026-11-09T10:04:00Z'
        self.assertEqual(len(ALERTS.evaluate(payload)['events']), 2)

    def test_stale_future_and_out_of_order_quotes_do_not_trigger(self):
        payload = example('trader-alerts', 'alerts.example.json')
        payload['rules'] = [payload['rules'][0]]
        payload['quotes'] = [dict(payload['quotes'][0], price='101', as_of='2026-11-09T08:00:00Z'), dict(payload['quotes'][0], price='101', as_of='2026-11-09T11:00:00Z')]
        self.assertEqual(ALERTS.evaluate(payload)['events'], [])
        clean = example('trader-alerts', 'alerts.example.json')
        first = ALERTS.evaluate(clean)
        clean['state'] = first['state']
        clean['quotes'] = [clean['quotes'][0]]
        self.assertEqual(ALERTS.evaluate(clean)['events'], [])

    def test_schedule_unknown_never_implies_open_or_closed(self):
        payload = example('trader-alerts', 'alerts.example.json')
        payload['exchange_schedules'] = {}
        payload['rules'][0].update(market_hours_only=True, exchange='DEMO')
        result = ALERTS.evaluate(payload)
        self.assertEqual(result['events'], [])
        self.assertTrue(any(row['reason'] == 'schedule_unknown' for row in result['skipped']))
        schedule = {'coverage_start': '2026-11-09T00:00:00Z', 'coverage_end': '2026-11-10T00:00:00Z', 'intervals': []}
        self.assertEqual(ALERTS.clock(schedule, ALERTS.timestamp('2026-11-09T10:00:00Z'))['status'], 'CLOSED')
        self.assertEqual(ALERTS.clock(schedule, ALERTS.timestamp('2026-11-10T10:00:00Z'))['status'], 'UNKNOWN')

    def test_changed_rule_conflicting_quotes_and_currency(self):
        payload = example('trader-alerts', 'alerts.example.json')
        first = ALERTS.evaluate(payload)
        payload['state'] = first['state']
        payload['rules'][0]['price'] = '110'
        with self.assertRaises(ValueError):
            ALERTS.evaluate(payload)
        payload = example('trader-alerts', 'alerts.example.json')
        payload['quotes'].append(dict(payload['quotes'][-1], price='102'))
        with self.assertRaises(ValueError):
            ALERTS.evaluate(payload)
        payload = example('trader-alerts', 'alerts.example.json')
        payload['rules'] = [dict(payload['rules'][0], currency='usd')]
        self.assertEqual(ALERTS.evaluate(payload)['events'], [])

    def test_percent_reference_and_initial_condition_are_explicit(self):
        payload = example('trader-alerts', 'alerts.example.json')
        payload['rules'] = [{'id': 'pct', 'uid': 'demo-share', 'class_code': 'TQBR', 'currency': 'rub', 'type': 'pct_move', 'pct': '2', 'reference_price': '100', 'direction': 'up'}]
        payload['quotes'] = [dict(payload['quotes'][-1], price='103')]
        event = ALERTS.evaluate(payload)['events'][0]
        self.assertEqual(event['event'], 'condition_observed')
        payload['rules'][0]['fire_on_initial'] = False
        self.assertEqual(ALERTS.evaluate(payload)['events'], [])


class CliSmokeTests(unittest.TestCase):
    def test_nonobject_inputs_and_nested_metadata_are_rejected_cleanly(self):
        for implementation in (NEWS, FACTS, SCREEN, ALERTS):
            with self.assertRaises(ValueError):
                implementation.evaluate([])
        payload = example('trader-screener', 'screen.example.json')
        payload['companies'][0]['metrics'] = None
        self.assertEqual(SCREEN.evaluate(payload)['passed_count'], 0)
        payload = example('trader-alerts', 'alerts.example.json')
        payload['exchange_schedules']['DEMO']['intervals'] = [None]
        with self.assertRaises(ValueError):
            ALERTS.evaluate(payload)

    def test_all_four_synthetic_clis(self):
        for skill, script, filename in (('trader-news', 'news.py', 'news.example.json'), ('trader-analyst', 'facts.py', 'reports.example.json'),
                                        ('trader-screener', 'screen.py', 'screen.example.json'), ('trader-alerts', 'alerts.py', 'alerts.example.json')):
            with self.subTest(skill=skill):
                result = subprocess.run([sys.executable, str(ROOT / 'skills' / skill / 'scripts' / script), str(ROOT / 'skills' / skill / 'references' / filename)], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertEqual(json.loads(result.stdout)['status'], 'OK')


if __name__ == '__main__':
    unittest.main()
