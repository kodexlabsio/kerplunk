"""The public-project survey script, on local git repositories."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / '.kodex' / 'scripts' / 'survey.py'


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location('survey', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules['survey'] = module
    spec.loader.exec_module(module)
    return module


def repository(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    for args in (['init', '-q', '-b', 'main'], ['add', '.'], ['commit', '-qm', 'init']):
        subprocess.run(
            ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args], cwd=path, check=True
        )
    return path.as_uri()


def test_the_survey_builds_each_project_and_aggregates(tmp_path: Path) -> None:
    shop = repository(
        tmp_path / 'shop',
        {
            'dbt_project.yml': 'name: shop\nprofile: shop\n',
            'models/stg_orders.sql': "select id as order_id, amount from {{ source('raw', 'orders') }}",
            'models/revenue.sql': "select sum(amount) as revenue from {{ ref('stg_orders') }} where amount > 0",
            'models/gross_revenue.sql': "select sum(amount) as revenue from {{ ref('stg_orders') }}",
            'models/schema.yml': (
                'version: 2\nsources:\n  - name: raw\n    tables:\n      - name: orders\n'
                '        columns: [{name: id}, {name: amount}]\n'
                'models:\n  - name: stg_orders\n    columns:\n      - name: order_id\n'
                '        data_tests: [unique]\n'
            ),
        },
    )
    plain = repository(tmp_path / 'plain', {'README.md': 'not a dbt project\n'})
    listing = tmp_path / 'projects.txt'
    listing.write_text(f'# public projects\n{shop}\n{plain}\n{(tmp_path / "missing").as_uri()}\n')
    survey = load()
    out = tmp_path / 'out'
    assert survey.main([str(listing), '--out', str(out)]) == 0
    records = {r['url']: r for r in map(json.loads, (out / 'projects.jsonl').read_text().splitlines())}
    assert records[plain]['status'] == 'no dbt project'
    assert records[(tmp_path / 'missing').as_uri()]['status'] == 'clone failed'
    built = records[shop]
    assert (built['status'], built['models']) == ('built', 3)
    assert built['columns_with_lineage'] == built['columns'] > 0
    assert built['conflicts'] >= 1 and 'filter' in built['conflict_kinds']
    assert built['entities'] >= 1
    summary = (out / 'summary.md').read_text()
    assert '3 projects listed: 1 built, 1 clone failed, 1 no dbt project.' in summary
    assert '| Projects with at least one conflicting definition | 1 of 1 |' in summary
    # The summary names no project.
    assert 'shop' not in summary.replace('# Konstruct on public dbt projects', '')
    # A second run resumes without building again.
    listing.write_text(f'{shop}\n')
    assert survey.main([str(listing), '--out', str(out)]) == 0
    assert len((out / 'projects.jsonl').read_text().splitlines()) == 3
    assert '1 projects listed: 1 built.' in (out / 'summary.md').read_text()


def test_warning_kinds() -> None:
    survey = load()
    assert survey.kind('dbt model x: SELECT * over relations with unknown columns: a') == 'select-star'
    assert survey.kind('dbt model x: column lineage failed: Unknown column: y') == 'lineage-failed'
    assert survey.kind('something else entirely') == 'other'


def test_a_project_cannot_write_outside_its_database(tmp_path: Path) -> None:
    outside = tmp_path / 'pwned.csv'
    model = '{% do run_query("COPY (SELECT 1 AS x) TO \'' + str(outside) + '\'") %}\nselect 1 as x\n'
    shop = repository(
        tmp_path / 'shop',
        {
            'dbt_project.yml': "name: shop\nprofile: \"{{ env_var('DBT_PROFILE', 'shop') }}\"\n",
            'models/m.sql': model,
        },
    )
    listing = tmp_path / 'projects.txt'
    listing.write_text(f'{shop}\n')
    survey = load()
    out = tmp_path / 'out'
    survey.main([str(listing), '--out', str(out)])
    [record] = map(json.loads, (out / 'projects.jsonl').read_text().splitlines())
    assert record['status'] == 'built'
    assert not outside.exists()


def test_a_commit_can_be_pinned_and_a_slow_project_is_stopped(tmp_path: Path) -> None:
    shop = repository(
        tmp_path / 'shop', {'dbt_project.yml': 'name: shop\nprofile: shop\n', 'models/a.sql': 'select 1 as a'}
    )
    first = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=tmp_path / 'shop', capture_output=True, text=True
    ).stdout.strip()
    (tmp_path / 'shop' / 'models' / 'b.sql').write_text('select 2 as b')
    for args in (['add', '.'], ['commit', '-qm', 'second']):
        subprocess.run(
            ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args],
            cwd=tmp_path / 'shop',
            check=True,
        )
    survey = load()
    listing = tmp_path / 'projects.txt'
    listing.write_text(f'{shop} {first}\n{shop}\n')
    out = tmp_path / 'out'
    survey.main([str(listing), '--out', str(out)])
    pinned, latest = map(json.loads, (out / 'projects.jsonl').read_text().splitlines())
    assert (pinned['commit'], pinned['models']) == (first, 1)
    assert latest['models'] == 2
    listing.write_text(f'{shop} {first}\n')
    slow = tmp_path / 'slow'
    survey.main([str(listing), '--out', str(slow), '--timeout', '0.5'])
    [record] = map(json.loads, (slow / 'projects.jsonl').read_text().splitlines())
    assert record['status'] == 'build timed out'


def test_profile_names_from_env_var_take_their_default(tmp_path: Path) -> None:
    survey = load()
    project = tmp_path / 'dbt_project.yml'
    project.write_text("name: shop\nprofile: \"{{ env_var('DBT_PROFILE', 'analytics') }}\"\n")
    survey.write_profiles([project], tmp_path / 'profiles')
    profiles = (tmp_path / 'profiles' / 'profiles.yml').read_text()
    assert profiles.startswith('analytics:') and 'enable_external_access: false' in profiles


def test_a_project_cannot_read_the_surveyors_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('SURVEY_PROBE_SECRET', 's3cr3t')
    shop = repository(
        tmp_path / 'shop', {'dbt_project.yml': 'name: shop\nprofile: shop\n', 'models/m.sql': 'select 1 as x'}
    )
    listing = tmp_path / 'projects.txt'
    listing.write_text(f'{shop}\n')
    survey = load()
    given: list[dict[str, str]] = []
    real = survey.subprocess.Popen

    def popen(*args: object, **kwargs: object) -> object:
        given.append(dict(kwargs['env']))  # type: ignore[call-overload]
        return real(*args, **kwargs)

    monkeypatch.setattr(survey.subprocess, 'Popen', popen)
    survey.main([str(listing), '--out', str(tmp_path / 'out')])
    [record] = map(json.loads, (tmp_path / 'out' / 'projects.jsonl').read_text().splitlines())
    assert record['status'] == 'built'
    [environment] = given
    assert 'SURVEY_PROBE_SECRET' not in environment and 's3cr3t' not in environment.values()
    assert environment['HOME'].endswith('/home') and environment['GIT_TERMINAL_PROMPT'] == '0'
