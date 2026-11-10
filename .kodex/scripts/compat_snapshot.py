"""Add a release to the compatibility corpus in tests/compat/<tag>/.

Builds the corpus projects with one Konstruct release and keeps what that release wrote: each
project's graph.json, and the --json output of the commands below run against it.
tests/test_compat.py then checks that the current code still reads every one of those graphs and
answers as the release did.

The projects are built from this checkout's example and test fixtures, whichever release builds
them, so each release's snapshot describes the same inputs. Paths are written as <root>, and what
changes from one run to the next (the build's time and duration) is left out. A release whose
outputs are the same as the newest snapshot's, apart from its version, adds no folder:
tests/compat/releases.json records each release with the folder that holds its outputs.

Usage (from the repository root, after the release is tagged):
    uv run python .kodex/scripts/compat_snapshot.py --ref v0.12.1
    uv run python .kodex/scripts/compat_snapshot.py                 # this checkout, as it is
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / 'examples' / 'jaffle_shop'
FIXTURES = REPO / 'tests' / 'fixtures'
CORPUS = REPO / 'tests' / 'compat'

# Commands every project answers, by the name their output is kept under.
COMMANDS: dict[str, list[str]] = {
    'impact_stg_orders': ['impact', 'stg_orders'],
    'impact_stg_payments_amount': ['impact', 'stg_payments.amount'],
    'trace_customer_ltv_lifetime_value': ['trace', 'customer_ltv.lifetime_value'],
    'trace_orders_amount_depth_1': ['trace', 'orders.amount', '--depth', '1'],
    'path_raw_payments_customer_dashboard': ['path', 'raw_payments', 'customer_dashboard'],
    'search_lifetime_value': ['search', 'lifetime', 'value', '-n', '5'],
    'describe_orders': ['describe', 'orders'],
    'describe_build_customer_ltv': ['describe', 'jaffle_shop_daily.build_customer_ltv'],
    'list': ['list'],
    'list_columns': ['list', 'columns'],
    'query_payment_method': ['query', 'what breaks if I change payment_method'],
    'define_lifetime_value': ['define', 'lifetime', 'value'],
    'ontology': ['ontology'],
    'explain_customers_customer_lifetime_value': ['explain', 'customers.customer_lifetime_value'],
    'conflicts': ['conflicts', '--no-write'],
}
# And those only one project can answer.
EXTRA: dict[str, dict[str, list[str]]] = {
    'orchestration': {
        'trace_daily_totals_total': ['trace', 'daily_totals.total'],
        'impact_customer_ltv_task': ['impact', 'reporting/customer_ltv'],
        'describe_archive_orders': ['describe', 'archive_orders'],
        'describe_dependencies': ['describe', 'dependencies'],
    },
}


def run(command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def environment(bin_dir: Path) -> dict[str, str]:
    """The release's own executables (konstruct and dbt) first on PATH."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(('KONSTRUCT_', 'VIRTUAL_ENV'))}
    env['PATH'] = f'{bin_dir}{os.pathsep}{env.get("PATH", "")}'
    env['DBT_SEND_ANONYMOUS_USAGE_STATS'] = 'false'
    env['NO_COLOR'] = '1'
    return env


def prepare(work: Path) -> dict[str, Path]:
    """The corpus projects, with the jaffle_shop warehouse built and no dbt artefacts, so each
    release runs dbt itself."""
    beside = Path(sys.executable).parent / 'dbt'
    dbt = str(beside) if beside.is_file() else shutil.which('dbt')
    if not dbt:
        sys.exit('dbt is not installed; run this script with `uv run`')
    jaffle = work / 'jaffle_shop'
    shutil.copytree(
        EXAMPLE, jaffle, ignore=shutil.ignore_patterns('target', 'logs', 'konstruct-out', '*.duckdb*')
    )
    env = {**environment(Path(dbt).parent), 'DBT_PROFILES_DIR': str(jaffle)}
    subprocess.run([dbt, 'build', '--quiet'], cwd=jaffle, env=env, check=True)
    import duckdb

    con = duckdb.connect(str(jaffle / 'jaffle_shop.duckdb'))
    try:
        con.execute((jaffle / 'dags' / 'sql' / 'customer_ltv.sql').read_text(encoding='utf-8'))
    finally:
        con.close()
    for leftover in ('target', 'logs'):
        shutil.rmtree(jaffle / leftover, ignore_errors=True)

    # jaffle_shop with Dagster definitions and the Airflow fixtures beside its own DAG.
    orchestration = work / 'orchestration'
    shutil.copytree(jaffle, orchestration)
    (orchestration / 'orchestration').mkdir()
    shutil.copy(FIXTURES / 'dagster' / 'definitions.py', orchestration / 'orchestration' / 'definitions.py')
    for dag in sorted((FIXTURES / 'airflow').glob('*.py')):
        shutil.copy(dag, orchestration / 'dags' / dag.name)
    return {'jaffle_shop': jaffle, 'orchestration': orchestration}


def normalise(text: str, root: Path) -> str:
    for form in {str(root.resolve()), str(root)}:
        text = text.replace(form, '<root>')
    return text


def keep(text: str, root: Path, path: Path) -> None:
    """Write a JSON document indented, with paths normalised and run-specific values left out."""
    data = json.loads(normalise(text, root))
    if isinstance(data, dict) and 'seconds' in data:
        data['seconds'] = 0
    if isinstance(data, dict) and isinstance(data.get('meta'), dict):
        for key in ('generated_at', 'build_seconds'):
            data['meta'].pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')


def snapshot(bin_dir: Path, work: Path) -> tuple[str, Path]:
    """Build every corpus project with the konstruct in ``bin_dir``; returns the release's tag and
    the folder its outputs are in."""
    env = environment(bin_dir)
    konstruct = str(bin_dir / 'konstruct')
    version = run([konstruct, '--version'], work, env).stdout.split()[-1]
    target = work / 'snapshot'
    for name, source in prepare(work / 'projects').items():
        root = work / 'run' / name
        shutil.copytree(source, root)
        # The corpus projects are this repository's own, so each release reads them trusted, as
        # CI does; releases before trust ignore the variable.
        project_env = {**env, 'DBT_PROFILES_DIR': str(root), 'KONSTRUCT_TRUST_CONFIG': '1'}
        built = run([konstruct, 'build', str(root), '--json'], root, project_env)
        if built.returncode != 0:
            sys.exit(f'konstruct {version} could not build {name}:\n{built.stderr}')
        folder = target / name
        keep(built.stdout, root, folder / 'build.json')
        keep((root / 'konstruct-out' / 'graph.json').read_text(encoding='utf-8'), root, folder / 'graph.json')
        index: dict[str, dict[str, object]] = {}
        for key, args in {**COMMANDS, **EXTRA.get(name, {})}.items():
            result = run([konstruct, *args, '-C', str(root), '--json', '--no-build'], root, project_env)
            index[key] = {'args': args, 'exit': result.returncode}
            if result.stdout.strip():
                keep(result.stdout, root, folder / f'{key}.json')
        (folder / 'commands.json').write_text(json.dumps(index, indent=1) + '\n', encoding='utf-8')
        print(f'konstruct {version}: {name}, {len(index)} commands')
    return f'v{version}', target


def _tag_order(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.removeprefix('v').split('.'))


def comparable(folder: Path) -> dict[str, object]:
    """Every output in ``folder`` by path, without the version of Konstruct that wrote it."""
    found: dict[str, object] = {}
    for path in sorted(folder.rglob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('meta'), dict):
            data['meta'].pop('konstruct_version', None)
        found[str(path.relative_to(folder))] = data
    return found


def record(tag: str, folder: Path, corpus: Path) -> str:
    """Add the release ``tag``, whose outputs are in ``folder``, to the corpus; returns the folder
    of the corpus that holds them."""
    index = corpus / 'releases.json'
    releases: dict[str, str] = json.loads(index.read_text(encoding='utf-8')) if index.is_file() else {}
    releases.pop(tag, None)
    earlier = [t for t in releases if _tag_order(t) < _tag_order(tag)]
    newest = releases[max(earlier, key=_tag_order)] if earlier else None
    if newest and comparable(corpus / newest) == comparable(folder):
        releases[tag] = newest
    else:
        if (corpus / tag).exists():
            shutil.rmtree(corpus / tag)
        shutil.copytree(folder, corpus / tag)
        releases[tag] = tag
    ordered = dict(sorted(releases.items(), key=lambda item: _tag_order(item[0])))
    index.write_text(json.dumps(ordered, indent=1) + '\n', encoding='utf-8')
    return releases[tag]


def released(ref: str, work: Path) -> Path:
    """The bin directory of a virtual environment with the release ``ref`` installed from a git
    worktree of this repository."""
    source = work / 'source'
    subprocess.run(['git', 'worktree', 'add', '--detach', str(source), ref], cwd=REPO, check=True)
    subprocess.run(['uv', 'sync', '--frozen', '--quiet', '--project', str(source)], cwd=source, check=True)
    return source / '.venv' / 'bin'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--ref', help='a released tag, such as v0.12.1 (default: this checkout)')
    parser.add_argument(
        '--out', type=Path, default=CORPUS, help='the corpus directory (default: tests/compat)'
    )
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix='konstruct-compat-') as temporary:
        work = Path(temporary)
        try:
            bin_dir = released(args.ref, work) if args.ref else Path(sys.executable).parent
            tag, folder = snapshot(bin_dir, work)
            kept = record(tag, folder, args.out.resolve())
        finally:
            if args.ref:
                subprocess.run(
                    ['git', 'worktree', 'remove', '--force', str(work / 'source')], cwd=REPO, check=False
                )
    where = 'a new folder' if kept == tag else f'the same outputs as {kept}'
    print(f'{tag}: {where} in {args.out}; add it to git and run `uv run pytest tests/test_compat.py`')
    return 0


if __name__ == '__main__':
    sys.exit(main())
