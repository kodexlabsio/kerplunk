"""Build many public dbt projects with Konstruct and report what it finds, in aggregate.

Each project is cloned (shallow) and built without its warehouse: the survey writes a DuckDB
profile under each profile name the project uses, so ``konstruct build`` can have dbt produce the
manifest (``dbt docs generate`` against an empty DuckDB database, or else ``dbt parse``). That
runs dbt on the project, which renders its Jinja and runs the queries its macros issue, against
that DuckDB database with file and network access turned off. ``--deps`` runs ``dbt deps``
first, which downloads the packages the project names so their macros render. A project runs with
a home directory of its own and none of the surveyor's environment variables but those needed to
run and to reach a proxy, so its Jinja cannot read credentials. Run the survey in a disposable
container with no credentials in its environment. Each project is surveyed in a child process, stopped after
``--timeout``. For each project the survey records the models, how many of their columns have
column lineage, the conflicting definitions ``konstruct conflicts`` finds, the size of the
ontology, the warnings by kind and the build time.

The list is a text file of git URLs, one per line (``#`` starts a comment); an optional second
field pins a commit, tag or branch:

    https://github.com/dbt-labs/jaffle-shop
    https://github.com/example/analytics  v1.4.0

Usage:
    uv run .kodex/scripts/survey.py projects.txt --out survey
    uv run .kodex/scripts/survey.py projects.txt --out survey --deps --limit 20

It writes ``<out>/projects.jsonl`` (one line per project, for checking) and ``<out>/summary.md``,
which has only aggregate figures. Projects already in ``projects.jsonl`` are skipped, so a run
resumes.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from konstruct.core import yamlio  # noqa: E402

# Warning kinds, matched in order against the text after the input's prefix.
KINDS = [
    ('select-star', re.compile(r'SELECT \* over relations with unknown columns')),
    ('lineage-failed', re.compile(r'column lineage failed|could not analyse|not valid SQL', re.IGNORECASE)),
    ('jinja', re.compile(r'render|macro|Jinja', re.IGNORECASE)),
    ('unknown-relation', re.compile(r'not found|does not exist|unknown relation', re.IGNORECASE)),
    ('artefacts', re.compile(r'manifest|catalog|artefact', re.IGNORECASE)),
]
GIT = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}  # a private or deleted repository fails at once
# DuckDB settings for the generated profile: the project's queries cannot touch files or the network.
LOCKED = {
    'enable_external_access': False,
    'autoinstall_known_extensions': False,
    'autoload_known_extensions': False,
}
_ENV_DEFAULT = re.compile(r"""^\{\{\s*env_var\(\s*['"][^'"]+['"]\s*,\s*['"]([^'"]+)['"]\s*\)\s*\}\}$""")


def read_list(path: Path) -> list[tuple[str, str | None]]:
    projects = []
    for line in path.read_text(encoding='utf-8').splitlines():
        fields = line.split('#', 1)[0].split()
        if fields:
            projects.append((fields[0], fields[1] if len(fields) > 1 else None))
    return projects


def kind(warning: str) -> str:
    text = warning.split(': ', 1)[-1]
    return next((name for name, pattern in KINDS if pattern.search(text)), 'other')


def clone(url: str, ref: str | None, into: Path) -> str | None:
    """Fetch ``url`` at ``ref`` (a commit, tag or branch; by default its default branch), one
    commit deep; the commit, or None if it could not be fetched."""
    steps = [
        ['git', 'init', '-q', str(into)],
        ['git', '-C', str(into), 'fetch', '-q', '--depth', '1', url, ref or 'HEAD'],
        ['git', '-C', str(into), '-c', 'advice.detachedHead=false', 'checkout', '-q', 'FETCH_HEAD'],
    ]
    for step in steps:
        if subprocess.run(step, capture_output=True, env=GIT, check=False).returncode != 0:
            return None
    head = subprocess.run(
        ['git', '-C', str(into), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=False
    )
    return head.stdout.strip() or None


def write_profiles(projects: list[Path], into: Path) -> None:
    """A locked-down DuckDB profile under every profile name the projects use, so dbt can parse them."""
    names = set()
    for project_file in projects:
        try:
            data = yamlio.read(project_file) or {}
        except (OSError, yaml.YAMLError):
            continue
        name = str(data.get('profile') or data.get('name') or 'default').strip()
        match = _ENV_DEFAULT.match(name)
        names.add(match[1] if match else name)
    output = {'type': 'duckdb', 'path': str(into / 'survey.duckdb'), 'settings': LOCKED}
    profiles = {name: {'target': 'survey', 'outputs': {'survey': output}} for name in sorted(names)}
    into.mkdir(parents=True, exist_ok=True)
    (into / 'profiles.yml').write_text(yaml.safe_dump(profiles), encoding='utf-8')


def survey(url: str, ref: str | None, work: Path, *, deps: bool) -> dict[str, Any]:
    """Survey one project in this process (see ``survey_isolated``)."""
    from konstruct import Project
    from konstruct.outputs.report import analyse

    record: dict[str, Any] = {'url': url, 'ref': ref}
    checkout = work / 'checkout'
    commit = clone(url, ref, checkout)
    if commit is None:
        return {**record, 'status': 'clone failed'}
    record['commit'] = commit
    projects = [
        p for p in checkout.rglob('dbt_project.yml') if not {'dbt_packages', 'dbt_modules'} & set(p.parts)
    ]
    if not projects:
        return {**record, 'status': 'no dbt project'}
    write_profiles(projects, work / 'profiles')
    os.environ['DBT_PROFILES_DIR'] = str(work / 'profiles')  # this process surveys one project only
    os.environ['GIT_TERMINAL_PROMPT'] = '0'
    if deps:
        for project_file in projects:
            subprocess.run(
                ['dbt', 'deps', '--quiet'], cwd=project_file.parent, capture_output=True, check=False
            )
    project = Project(checkout, out_dir=work / 'konstruct-out')
    started = time.perf_counter()
    try:
        # Read untrusted, so paths outside the checkout are refused; then trusted to run dbt,
        # which the survey does on purpose, with its own profile and no warehouse.
        config = project.config()
        config.trusted = True
        result = project.build(warehouses=False, config=config)
    except Exception as error:  # a crash is a finding too
        return {**record, 'status': 'build failed', 'error': f'{type(error).__name__}: {error}'}
    graph = result.graph
    findings = analyse(graph)
    record.update(
        status='built',
        seconds=round(time.perf_counter() - started, 2),
        models=sum(1 for n in graph.nodes.values() if n.kind == 'model'),
        columns=findings.lineage_total,
        columns_with_lineage=findings.lineage_covered,
        warnings=dict(Counter(kind(w) for w in graph.warnings)),
        conflicts=0,
        conflict_kinds=[],
        entities=0,
        measures=0,
        relationships=0,
        rule_violations=0,
    )
    try:
        conflicts = project.conflicts()
        record['conflicts'] = len(conflicts.conflicts)
        record['conflict_kinds'] = sorted({d.kind for c in conflicts.conflicts for d in c.differences})
    except Exception as error:
        record['conflicts_error'] = f'{type(error).__name__}: {error}'
    try:
        ontology = project.ontology()
        record['entities'] = len(ontology.entities)
        record['measures'] = len(ontology.measures)
        record['relationships'] = len(ontology.relationships)
        record['rule_violations'] = len(ontology.violations)
    except Exception as error:
        record['ontology_error'] = f'{type(error).__name__}: {error}'
    return record


# What a child process keeps from the surveyor's environment: enough to run and to reach package
# hosts through a proxy, and no credentials, which a project's Jinja could read with env_var().
_KEPT = (
    'PATH',
    'LANG',
    'LC_ALL',
    'HTTPS_PROXY',
    'HTTP_PROXY',
    'NO_PROXY',
    'https_proxy',
    'http_proxy',
    'no_proxy',
    'SSL_CERT_FILE',
    'REQUESTS_CA_BUNDLE',
    'VIRTUAL_ENV',
)


def child_environment(work: Path) -> dict[str, str]:
    """The environment a project is surveyed in: a home of its own and no secrets."""
    home = work / 'home'
    home.mkdir(parents=True, exist_ok=True)
    kept = {k: v for k, v in os.environ.items() if k in _KEPT}
    return {
        **kept,
        'HOME': str(home),
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_CONFIG_NOSYSTEM': '1',
        'DBT_SEND_ANONYMOUS_USAGE_STATS': 'false',
    }


def survey_isolated(url: str, ref: str | None, *, deps: bool, timeout: float) -> dict[str, Any]:
    """Survey one project in a child process of its own, stopped with everything it started after
    ``timeout`` seconds, so a project that hangs or crashes is recorded and the survey goes on."""
    work = Path(tempfile.mkdtemp(prefix='konstruct-survey-'))
    command = [
        sys.executable,
        __file__,
        '--one',
        url,
        ref or '',
        '--work',
        str(work),
        *(['--deps'] if deps else []),
    ]
    record: dict[str, Any] = {'url': url, 'ref': ref}
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=child_environment(work),
        )
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            return {**record, 'status': 'build timed out'}
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        lines = [line for line in out.splitlines() if line.startswith('{')]
        if process.returncode != 0 or not lines:
            return {**record, 'status': 'build failed', 'error': (err.strip().splitlines() or [''])[-1][:300]}
        result: dict[str, Any] = json.loads(lines[-1])
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _median(values: list[float]) -> str:
    return f'{statistics.median(values):g}' if values else '-'


def summary(records: list[dict[str, Any]]) -> str:
    built = [r for r in records if r.get('status') == 'built']
    with_models = [r for r in built if r['models']]
    statuses = Counter(r.get('status') for r in records)
    columns = sum(r['columns'] for r in with_models)
    covered = sum(r['columns_with_lineage'] for r in with_models)
    shares = [100 * r['columns_with_lineage'] / r['columns'] for r in with_models if r['columns']]
    conflicted = [r for r in with_models if r['conflicts']]
    warnings: Counter[str] = Counter()
    for r in with_models:
        warnings.update(r['warnings'])
    kinds: Counter[str] = Counter(k for r in conflicted for k in r['conflict_kinds'])
    lines = [
        '# Konstruct on public dbt projects',
        '',
        f'{len(records)} projects listed: '
        + ', '.join(
            f'{count} {status}' for status, count in sorted(statuses.items(), key=lambda s: str(s[0]))
        )
        + f'. Figures below are for the {len(with_models)} built projects with at least one model.',
        '',
        '| Measure | Value |',
        '|---|---|',
        f'| Models, total | {sum(r["models"] for r in with_models)} |',
        f'| Models per project, median | {_median([r["models"] for r in with_models])} |',
        f'| Columns with column lineage, all projects | {covered} of {columns} ({100 * covered / columns:.1f}%) |'
        if columns
        else '| Columns with column lineage, all projects | - |',
        f'| Columns with column lineage per project, median | {_median([round(s, 1) for s in shares])}% |',
        f'| Projects with at least one conflicting definition | {len(conflicted)} of {len(with_models)} |',
        f'| Conflicting definitions per project, median among those | {_median([r["conflicts"] for r in conflicted])} |',
        f'| Projects with at least one entity in the ontology | {sum(1 for r in with_models if r["entities"])} |',
        f'| Entities, measures, relationships per project, median | '
        f'{_median([r["entities"] for r in with_models])}, {_median([r["measures"] for r in with_models])}, '
        f'{_median([r["relationships"] for r in with_models])} |',
        f'| Build time per project, median | {_median([r["seconds"] for r in with_models])} s |',
        '',
        '| Conflict difference | Projects |',
        '|---|---|',
        *(f'| {k} | {n} |' for k, n in kinds.most_common()),
        '',
        '| Warning kind | Count |',
        '|---|---|',
        *(f'| {k} | {n} |' for k, n in warnings.most_common()),
    ]
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n', 1)[0])
    parser.add_argument('projects', type=Path, nargs='?', help='a text file of git URLs, one per line')
    parser.add_argument('--out', type=Path, default=Path('survey'), help='where to write the results')
    parser.add_argument('--deps', action='store_true', help='run `dbt deps` before building')
    parser.add_argument('--limit', type=int, help='survey at most this many projects')
    parser.add_argument('--timeout', type=float, default=900, help='seconds for each project (default 900)')
    parser.add_argument('--one', nargs=2, metavar=('URL', 'REF'), help=argparse.SUPPRESS)
    parser.add_argument('--work', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.one:
        url, ref = args.one
        print(json.dumps(survey(url, ref or None, args.work, deps=args.deps)))
        return 0
    if args.projects is None:
        parser.error('the list of projects is required')
    listed = read_list(args.projects)[: args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    log = args.out / 'projects.jsonl'
    records = (
        [json.loads(line) for line in log.read_text().splitlines() if line.strip()] if log.is_file() else []
    )
    done = {(r['url'], r.get('ref')) for r in records}
    for url, ref in listed:
        if (url, ref) in done:
            continue
        record = survey_isolated(url, ref, deps=args.deps, timeout=args.timeout)
        records.append(record)
        with log.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
        print(f'{url}: {record["status"]}', file=sys.stderr)
    surveyed = [r for r in records if (r['url'], r.get('ref')) in set(listed)]
    (args.out / 'summary.md').write_text(summary(surveyed), encoding='utf-8')
    print((args.out / 'summary.md').read_text(encoding='utf-8'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
