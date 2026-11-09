#!/usr/bin/env python3
"""Build a source-only skills ZIP. No credentials or broker captures included."""
import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ('trader-tinvest', 'trader-copycat', 'trader-news', 'trader-budget',
          'trader-pelosi', 'trader-analyst', 'trader-screener', 'trader-risk',
          'trader-broker', 'trader-tax', 'trader-alerts', 'trader-rebalance')


def ignored(path):
    return (any(part in ('__pycache__', '.local') for part in path.parts)
            or path.name.endswith(('.local.json', '.pyc', '.pyo')))


def source_files(root=ROOT):
    root = Path(root)
    paths = []
    for filename in ('plugin.json', '.codex-plugin/plugin.json', 'README.md', 'AGENTS.md',
                     '.gitignore', 'NOTICE.md'):
        path = root / filename
        if not path.is_file() or path.is_symlink():
            raise ValueError('Required package source is missing: ' + filename)
        paths.append(path)
    for name in SKILLS:
        folder = root / 'skills' / name
        if not (folder / 'SKILL.md').is_file():
            raise ValueError('Required skill is missing: ' + name)
        for path in folder.rglob('*'):
            relative = path.relative_to(root)
            if path.is_symlink():
                raise ValueError('Source symlinks are not packaged')
            if not path.is_file() or ignored(relative):
                continue
            if path.suffix not in ('.md', '.py', '.json'):
                raise ValueError('Unsupported skill resource: ' + str(relative))
            paths.append(path)
    for directory, suffixes in (('scripts', ('.py',)), ('tests', ('.py',)), ('third_party', ('.md', '')),
                                ('docs', ('.md',))):
        for path in (root / directory).rglob('*'):
            if path.is_symlink():
                raise ValueError('Source symlinks are not packaged')
            if path.is_file() and path.suffix in suffixes and not ignored(path.relative_to(root)):
                paths.append(path)
    return sorted(set(paths))


def build(output, root=ROOT):
    root, output = Path(root), Path(output)
    paths = source_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, str(Path('trader-skill-ru') / path.relative_to(root)))
    return len(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    count = build(args.output)
    print('Packaged', count, 'source files into', args.output)


if __name__ == '__main__':
    main()
