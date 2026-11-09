"""Cross-module and source-distribution tests using synthetic data only."""
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('pack_source_distribution', ROOT / 'scripts/package_skills.py')
PACK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACK)

EXAMPLES = (
    ('trader-copycat', 'holdings.py', 'references/example.json', (), 'ANALYZED', 'targets'),
    ('trader-news', 'scripts/news.py', 'references/news.example.json', (), 'OK', 'items'),
    ('trader-budget', 'size.py', 'references/example.json', (), 'ANALYZED', 'proposals'),
    ('trader-pelosi', 'scripts/disclosures.py', 'examples/disclosures.synthetic.json', ('index',),
     'DISCLOSURES_REVIEWED', 'events'),
    ('trader-analyst', 'scripts/facts.py', 'references/reports.example.json', (), 'OK', 'companies'),
    ('trader-screener', 'scripts/screen.py', 'references/screen.example.json', (), 'OK', 'shortlist'),
    ('trader-risk', 'scripts/check.py', 'references/example.json', (), 'PASS_OFFLINE', 'checks'),
    ('trader-broker', 'scripts/ticket.py', 'references/example.json', (), 'DRAFTS_PREPARED', 'drafts'),
    ('trader-tax', 'scripts/lots.py', 'examples/transactions.synthetic.json', ('report',),
     'LEDGER_CALCULATED', 'sales'),
    ('trader-alerts', 'scripts/alerts.py', 'references/alerts.example.json', (), 'OK', 'events'),
    ('trader-rebalance', 'rebalance.py', 'references/example.json', (), 'ANALYZED', 'drift'),
)

# Run the real CLI and sibling imports, but make any socket connection a failure.
OFFLINE_BOOTSTRAP = '''
import runpy, socket, sys
def forbidden(*args, **kwargs):
    raise RuntimeError("Network access is forbidden in synthetic integration tests")
socket.create_connection = forbidden
socket.socket.connect = forbidden
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def cli(script, arguments, cwd):
    env = {'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONNOUSERSITE': '1', 'LANG': 'C.UTF-8'}
    return subprocess.run([sys.executable, '-c', OFFLINE_BOOTSTRAP, str(script), *map(str, arguments)],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=15)


class PackIntegrationTests(unittest.TestCase):
    def examples(self, root, cwd):
        self.assertEqual(len(EXAMPLES), 11)
        self.assertEqual({row[0] for row in EXAMPLES}, set(PACK.SKILLS) - {'trader-tinvest'})
        for skill, script, example, prefix, expected_status, result_field in EXAMPLES:
            with self.subTest(skill=skill, root=root):
                folder = root / 'skills' / skill
                args = [*prefix, folder / example]
                if skill in ('trader-risk', 'trader-broker'):
                    args.extend(('--as-of', '2026-01-12T07:00:00Z'))
                result = cli(folder / script, args, cwd)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data['status'], expected_status, data)
                self.assertTrue(data[result_field], data)
                if 'execution_enabled' in data:
                    self.assertIs(data['execution_enabled'], False)
                else:
                    self.assertIs(data['orders_created'], False)
                if skill == 'trader-budget':
                    self.assertLessEqual(Decimal(data['estimated_spend']), Decimal(data['own_cash']))
                if skill == 'trader-copycat':
                    self.assertEqual(sum(Decimal(row['weight']) for row in data['targets']), Decimal(1))
                if skill == 'trader-risk':
                    self.assertEqual(data['checks']['buy_plan']['status'], 'PASS')

    def test_all_eleven_synthetic_clis_run_without_network_or_repository_cwd(self):
        with tempfile.TemporaryDirectory() as folder:
            self.examples(ROOT, Path(folder))

    def test_whole_source_zip_has_twelve_skills_and_runs_after_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder)
            output = temporary / 'skills.zip'
            count = PACK.build(output, ROOT)
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertEqual(len(names), len(set(names)))
                self.assertEqual(count, len(names))
                for name in names:
                    parts = PurePosixPath(name).parts
                    self.assertEqual(parts[0], 'trader-skill-ru')
                    self.assertNotIn('..', parts)
                    self.assertFalse(PurePosixPath(name).is_absolute())
                skill_manifests = {PurePosixPath(name).parts[2] for name in names
                                   if len(PurePosixPath(name).parts) == 4
                                   and PurePosixPath(name).parts[1] == 'skills'
                                   and PurePosixPath(name).parts[-1] == 'SKILL.md'}
                self.assertEqual(skill_manifests, set(PACK.SKILLS))
                self.assertEqual(len(skill_manifests), 12)
                self.assertIn('trader-skill-ru/scripts/package_skills.py', names)
                archive.extractall(temporary / 'extracted')
            extracted = temporary / 'extracted/trader-skill-ru'
            unrelated = temporary / 'unrelated-working-directory'
            unrelated.mkdir()
            self.examples(extracted, unrelated)

    def test_packaging_excludes_private_local_and_cache_artifacts_in_all_source_trees(self):
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder)
            source = temporary / 'source'
            for path in PACK.source_files(ROOT):
                destination = source / path.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            sentinel = 'synthetic-private-' + uuid.uuid4().hex
            local_paths = (
                '.local/broker/raw.json', 'work/live-capture.json', 'outputs/live-snapshot.json',
                'policy.local.json', '.env', '.git/config',
                'skills/trader-tinvest/references/policy.local.json',
                'skills/trader-tinvest/.local/raw.json',
                'skills/trader-news/scripts/__pycache__/news.cpython-39.pyc',
                'tests/.local/private.py', 'tests/__pycache__/private.pyc',
                'docs/.local/private.md', 'third_party/.local/private',
                'scripts/.local/private.py',
            )
            for relative in local_paths:
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(sentinel)
            output = temporary / 'source-only.zip'
            PACK.build(output, source)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                for relative in local_paths:
                    self.assertNotIn('trader-skill-ru/' + relative, names)
                self.assertFalse(any(sentinel.encode() in archive.read(name) for name in names))

    def test_packaging_rejects_source_symlink_without_opening_external_private_file(self):
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder)
            source = temporary / 'source'
            for path in PACK.source_files(ROOT):
                destination = source / path.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            external = temporary / 'private-outside-source.json'
            external.write_text('synthetic private content')
            link = source / 'skills/trader-budget/references/unsafe.json'
            link.symlink_to(external)
            output = temporary / 'must-not-exist.zip'
            with self.assertRaisesRegex(ValueError, 'symlink'):
                PACK.build(output, source)
            self.assertFalse(output.exists())

    def test_copycat_thirds_feed_budget_only_after_explicit_current_cost_join(self):
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder)
            copied = json.loads((ROOT / 'skills/trader-copycat/references/example.json').read_text())
            copied.pop('prior', None)
            copied['top_n'] = 3
            copied['current']['holdings'] = [
                {'isin': 'RU000SYNTH0' + str(i), 'name': 'Synthetic ' + uid, 'units': '1', 'value': '100'}
                for i, uid in enumerate(('A', 'B', 'C'), 1)]
            copied['mapping'] = [
                {'isin': 'RU000SYNTH0' + str(i), 'uid': 'synthetic-' + uid, 'class_code': 'SYNTH',
                 'currency': 'rub', 'lot_size': 1, 'source': 'synthetic identity mapping'}
                for i, uid in enumerate(('A', 'B', 'C'), 1)]
            input_path = temporary / 'public-synthetic.json'
            input_path.write_text(json.dumps(copied))
            run = cli(ROOT / 'skills/trader-copycat/holdings.py', [input_path], temporary)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            copied_result = json.loads(run.stdout)
            targets = copied_result['targets']
            self.assertEqual(len(targets), 3)
            self.assertEqual(sum(Decimal(row['weight']) for row in targets), Decimal(1))
            self.assertTrue(all(len(row['weight'].partition('.')[2]) <= 18 for row in targets))
            # Do not reuse historical disclosed values as current per-lot purchase costs.
            current_costs = [
                {'uid': 'synthetic-' + uid, 'class_code': 'SYNTH', 'currency': 'rub', 'lot_size': 1,
                 'kind': 'share', 'leveraged': False, 'financing': 'fully_paid_cash',
                 'cost_source': 'fresh synthetic cost model at 2026-10-02T10:00:00Z, not a broker estimate',
                 'principal_per_lot': str(i * 100), 'fee_per_lot': str(i),
                 'nkd_per_lot': '0', 'other_per_lot': '0', 'fixed_fee': '0'}
                for i, uid in enumerate(('A', 'B', 'C'), 1)]
            budget_input = {'account_id': 'synthetic-account', 'currency': 'rub',
                            'as_of': '2026-10-02T10:00:00Z', 'own_cash': '12000',
                            'targets': targets, 'instruments': current_costs, 'rules': {}}
            budget_path = temporary / 'budget-with-explicit-cost-join.json'
            budget_path.write_text(json.dumps(budget_input))
            run = cli(ROOT / 'skills/trader-budget/size.py', [budget_path], temporary)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(result['status'], 'ANALYZED')
            self.assertIs(result['execution_enabled'], False)
            self.assertEqual({row['uid'] for row in result['proposals']}, {row['uid'] for row in targets})
            models = {row['uid']: row for row in current_costs}
            for row in result['proposals']:
                model = models[row['uid']]
                exact = row['lots'] * (Decimal(model['principal_per_lot']) + Decimal(model['fee_per_lot']))
                self.assertEqual(Decimal(row['estimated_cash_cost']), exact)
                self.assertNotIn('verified_for_this_order', row)
                self.assertNotIn('own_funds_max_lots', row)
            self.assertLessEqual(Decimal(result['estimated_spend']), Decimal('12000'))


if __name__ == '__main__':
    unittest.main()
