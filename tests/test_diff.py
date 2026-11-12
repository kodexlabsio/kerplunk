"""konstruct diff on real git repositories, and the dbt Jinja renderer it relies on."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import duckdb
import pytest

from konstruct import Project
from konstruct.analysis.diff import Context, DiffError, default_base
from konstruct.core.graph import Graph
from konstruct.engine.build import build
from konstruct.engine.config import load_config
from konstruct.inputs.jinja import Renderer, render_model
from konstruct.interfaces.cli import main

# Renderer


def manifest() -> dict[str, object]:
    return {
        'metadata': {'adapter_type': 'duckdb', 'project_name': 'shop'},
        'nodes': {
            'model.shop.stg': {
                'resource_type': 'model',
                'name': 'stg',
                'package_name': 'shop',
                'relation_name': '"db"."main"."stg"',
            }
        },
        'sources': {
            'source.shop.app.events': {
                'source_name': 'app',
                'name': 'events',
                'relation_name': '"db"."raw"."events"',
            }
        },
    }


@pytest.fixture
def renderer(tmp_path: Path) -> Renderer:
    (tmp_path / 'macros').mkdir()
    (tmp_path / 'macros' / 'money.sql').write_text(
        "{% macro cents_to_dollars(col) %}{{ return(adapter.dispatch('cents_to_dollars')(col)) }}{% endmacro %}\n"
        '{% macro default__cents_to_dollars(col) %}({{ col }} / 100){% endmacro %}\n'
        '{% macro duckdb__cents_to_dollars(col) %}({{ col }} / 100.0){% endmacro %}\n'
    )
    package = tmp_path / 'dbt_packages' / 'dbt_utils' / 'macros'
    package.mkdir(parents=True)
    (package / 'keys.sql').write_text(
        '{% macro surrogate_key(cols) %}md5({{ cols | join(" || \'-\' || ") }}){% endmacro %}'
    )
    return Renderer(tmp_path, manifest())


def test_renderer_expands_loops_macros_dispatch_and_packages(renderer: Renderer) -> None:
    sql = """
    {% set methods = ['card', 'cash'] %}
    {{ config(materialized='table') }}
    select
      {% for m in methods %}sum(case when method = '{{ m }}' then amount end) as {{ m }}_amount,{% endfor %}
      {{ cents_to_dollars('amount') }} as dollars,
      {{ dbt_utils.surrogate_key(['a', 'b']) }} as id,
      {{ var('vat', 0.2) }} as vat
    from {{ ref('stg') }} join {{ source('app', 'events') }} using (id)
    {% if is_incremental() %} where updated > (select max(updated) from {{ this }}) {% endif %}
    """
    rendered = ' '.join(renderer.render(sql).split())
    assert "sum(case when method = 'card' then amount end) as card_amount" in rendered
    assert "sum(case when method = 'cash' then amount end) as cash_amount" in rendered
    assert '(amount / 100.0) as dollars' in rendered
    assert "md5(a || '-' || b) as id" in rendered
    assert '0.2 as vat' in rendered
    assert 'from "db"."main"."stg" join "db"."raw"."events"' in rendered
    assert 'updated' not in rendered


def test_dbt_builtin_macros(renderer: Renderer) -> None:
    rendered = renderer.render(
        "select {{ dbt.date_trunc('day', 'ordered_at') }} as d, {{ dbt.hash(dbt.concat(['a', \"'-'\", 'b'])) }} as h,"
        " {{ dbt.safe_cast('x', dbt.type_int()) }} as i, {{ dbt.datediff('a', 'b', 'week') }} as w"
    )
    assert rendered == (
        "select date_trunc('day', ordered_at) as d, md5(cast(a || '-' || b as varchar)) as h,"
        " try_cast(x as integer) as i, date_diff('week', a, b) as w"
    )
    spine = renderer.render(
        "{{ dbt.date_spine('day', \"cast('2026-01-01' as date)\", \"cast('2026-02-01' as date)\") }}"
    )
    assert spine.startswith('select cast(spine as date) as date_day from generate_series(')


def test_unknown_names_keep_their_arguments_and_errors_fall_back(renderer: Renderer) -> None:
    assert renderer.render("select {{ some_macro('amount', 'day') }}{{ adapter.unknown.thing }} 1") == (
        "select konstruct_macro(amount, 'day') 1"
    )
    assert renderer.render('{{ nothing() }}') == ''
    assert render_model('{% if %}', renderer, lambda raw: 'fallback') == 'fallback'
    assert render_model('x', None, lambda raw: raw.upper()) == 'X'


# diff on a SQL project


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        [
            'git',
            '-C',
            str(root),
            '-c',
            'user.name=t',
            '-c',
            'user.email=t@example.com',
            '-c',
            'commit.gpgsign=false',
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


BASE_LTV = """
create table main.ltv as
with o as (select customer_id, amount, status from raw.orders)
select customer_id, sum(amount) as total, count(*) as orders
from o where status <> 'void' group by customer_id
"""

BASE_REPORT = 'create table main.report as select customer_id, total from main.ltv where total > 100'

DAG = """
from airflow.sdk import DAG
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.providers.standard.operators.empty import EmptyOperator

with DAG("nightly"):
    ltv = SQLExecuteQueryOperator(task_id="ltv", sql="sql/ltv.sql")
    report = SQLExecuteQueryOperator(task_id="report", sql="sql/report.sql")
    ltv >> report
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    (root / 'sql').mkdir(parents=True)
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute('create table raw.orders (id int, customer_id int, amount int, status varchar)')
    con.close()
    (root / 'sql' / 'ltv.sql').write_text(BASE_LTV)
    (root / 'sql' / 'report.sql').write_text(BASE_REPORT)
    (root / 'dag.py').write_text(DAG)
    (root / '.gitignore').write_text('konstruct-out/\n*.duckdb\n')
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'base')
    Project(root).build()
    return root


def test_reformatting_and_renaming_ctes_is_not_a_change(repo: Path) -> None:
    (repo / 'sql' / 'ltv.sql').write_text(
        'CREATE TABLE main.ltv AS WITH base_orders AS (SELECT customer_id, amount, status FROM raw.orders)\n'
        'SELECT base_orders.customer_id, SUM(base_orders.amount) AS total, COUNT(*) AS orders\n'
        "FROM base_orders WHERE base_orders.status != 'void' GROUP BY base_orders.customer_id"
    )
    result = Project(repo).diff(base='HEAD')
    assert result.summary() == 'No change to models, SQL or DAGs.'


def test_changed_removed_and_added_columns(repo: Path) -> None:
    (repo / 'sql' / 'ltv.sql').write_text(
        BASE_LTV.replace(
            'sum(amount) as total, count(*) as orders', 'sum(amount) * 1.2 as total, max(amount) as biggest'
        )
    )
    result = Project(repo).diff(base='HEAD')
    [relation] = [r for r in result.relations if r.changed]
    assert relation.label == 'ltv' and relation.status == 'modified'
    changes = {c.column: c for c in relation.columns}
    assert set(changes) == {'total', 'orders', 'biggest'}
    assert changes['total'].change == 'changed'
    assert changes['total'].before == 'SUM(shop.raw.orders.amount)'
    assert changes['total'].after == 'SUM(shop.raw.orders.amount) * 1.2'
    assert changes['total'].impact is not None and changes['total'].impact['total'] >= 1
    assert changes['orders'].change == 'removed' and changes['orders'].breaks == []
    assert changes['biggest'].change == 'added'


def test_a_removed_column_still_read_downstream_is_breaking(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / 'sql' / 'ltv.sql').write_text(BASE_LTV.replace('sum(amount) as total', 'sum(amount) as revenue'))
    result = Project(repo).diff(base='HEAD')
    [(label, change)] = result.breaking
    assert (label, change.column, change.breaks) == ('ltv', 'total', ['report'])
    assert result.summary().startswith('1 breaking change')
    assert main(['--no-color', 'diff', '-C', str(repo), '--base', 'HEAD', '--fail-on-breaking']) == 1
    captured = capsys.readouterr()
    assert 'BREAKS report, which still reads it' in captured.out
    assert 'older than' not in captured.err
    # For CI: the markdown on stdout and the result as JSON beside it, so a script can tell a
    # breaking change (status 1, breaking listed) from a failure (status 1, no file).
    report = repo / 'diff.json'
    args = ['-q', 'diff', '-C', str(repo), '--base', 'HEAD', '--format', 'markdown', '--fail-on-breaking']
    assert main([*args, '--json-file', str(report)]) == 1
    assert json.loads(report.read_text())['breaking'][0]['column'] == 'total'
    assert '**Breaking changes**' in capsys.readouterr().out
    # Fixing the consumer in the same change makes it safe.
    (repo / 'sql' / 'report.sql').write_text(BASE_REPORT.replace('total', 'revenue'))
    assert Project(repo).diff(base='HEAD').breaking == []


def test_changed_conditions_are_row_changes(repo: Path) -> None:
    (repo / 'sql' / 'ltv.sql').write_text(
        BASE_LTV.replace("status <> 'void'", "status not in ('void', 'test')")
    )
    [relation] = [r for r in Project(repo).diff(base='HEAD').relations if r.changed]
    assert relation.columns == []
    assert relation.conditions_removed == ["WHERE: shop.raw.orders.status <> 'void'"]
    assert relation.conditions_added == ["WHERE: NOT shop.raw.orders.status IN ('void', 'test')"]
    assert relation.rows_impact is not None


def test_dag_changes(repo: Path) -> None:
    (repo / 'dag.py').write_text(
        DAG.replace('    ltv >> report\n', '    extra = EmptyOperator(task_id="extra")\n    ltv >> extra\n')
    )
    result = Project(repo).diff(base='HEAD')
    [dag] = result.dags
    assert dag.dag == 'nightly'
    assert dag.tasks_added == ['extra']
    assert dag.dependencies_added == ['ltv >> extra']
    assert dag.dependencies_removed == ['ltv >> report']


def test_new_and_deleted_files_and_formats(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / 'sql' / 'new.sql').write_text('create table main.fresh as select customer_id from raw.orders')
    (repo / 'notes.txt').write_text('hello')
    git(repo, 'rm', '-q', 'sql/report.sql')
    result = Project(repo).diff(base='HEAD')
    statuses = {r.label: r.status for r in result.relations}
    assert statuses == {'fresh': 'added', 'report': 'removed'}
    assert result.other == ['notes.txt']
    for fmt in ('text', 'markdown', 'json'):
        assert main(['--no-color', '-q', 'diff', '-C', str(repo), '--base', 'HEAD', '--format', fmt]) == 0
    out = capsys.readouterr().out
    assert main(['-q', 'diff', '-C', str(repo), '--base', 'HEAD', '--format', 'selector']) == 0
    assert capsys.readouterr().out.strip() == Project(repo).diff(base='HEAD').selection.selector
    assert '### Konstruct:' in out
    assert json.loads(out[out.index('{') :])['counts']['added'] == 1


def test_comparing_two_commits_and_the_default_base(repo: Path) -> None:
    git(repo, 'checkout', '-qb', 'feature')
    (repo / 'sql' / 'ltv.sql').write_text(BASE_LTV.replace('count(*)', 'count(distinct status)'))
    git(repo, 'commit', '-qam', 'change')
    git(repo, 'branch', '-q', '-f', 'main', 'HEAD~1')
    assert default_base(repo) == git(repo, 'rev-parse', 'HEAD~1').strip()
    assert Project(repo).diff().counts()['changed'] == 1
    result = Project(repo).diff(base='HEAD~1', head='HEAD')
    assert result.head == 'HEAD'
    assert [c.column for r in result.relations for c in r.columns] == ['orders']
    git(repo, 'branch', '-q', '-f', 'main', 'HEAD')
    assert default_base(repo) == 'HEAD'


def test_comparing_with_a_date(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = git(repo, 'rev-parse', 'HEAD').strip()
    (repo / 'sql' / 'ltv.sql').write_text(BASE_LTV.replace('count(*)', 'count(distinct status)'))
    # Made on the date itself: a date alone means its start, whatever the time of day now.
    monkeypatch.setenv('GIT_COMMITTER_DATE', '2030-01-01T00:30:00')
    monkeypatch.setenv('GIT_AUTHOR_DATE', '2030-01-01T00:30:00')
    git(repo, 'commit', '-qam', 'change')
    result = Project(repo).diff(base='2030-01-01', head='HEAD')
    assert (result.base, result.base_commit, result.head_commit) == ('2030-01-01', base, None)
    assert result.shown_base == f'2030-01-01 ({base[:7]})'
    assert result.to_dict()['base'] == '2030-01-01'
    assert [c.column for r in result.relations for c in r.columns] == ['orders']
    assert Project(repo).diff(base='2030-01-01 01:00', head='HEAD').summary() == (
        'No change to models, SQL or DAGs.'
    )
    with pytest.raises(DiffError, match='no commit on this branch before 1999-01-01'):
        Project(repo).diff(base='1999-01-01')
    with pytest.raises(DiffError, match='2030-13-45 is not a valid date'):
        Project(repo).diff(base='2030-13-45')
    git(repo, 'tag', '1999-01-01', base)
    assert Project(repo).diff(base='1999-01-01', head='HEAD').base_commit is None


@pytest.mark.parametrize('side', ['base', 'head'])
def test_a_ref_that_git_would_read_as_an_option_is_refused(repo: Path, side: str) -> None:
    # The MCP server's diff and gate tools take refs from an agent; `--output` would have git
    # write a file wherever the ref said.
    written = repo.parent / 'written'
    ref = f'--output={written}'
    with pytest.raises(DiffError, match='is not a commit, branch, tag or date'):
        Project(repo).diff(**{side: ref})
    with pytest.raises(DiffError, match='is not a commit, branch, tag or date'):
        Project(repo).gate(base=ref)
    assert main(['-q', 'diff', '-C', str(repo), f'--{side}={ref}']) == 1
    assert not written.exists()
    assert not list(repo.parent.glob('written*'))


def test_a_ref_is_resolved_to_a_commit_before_git_is_given_it_elsewhere(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(repo, 'branch', '-q', 'reviewed')
    commit = git(repo, 'rev-parse', 'HEAD').strip()
    (repo / 'sql' / 'ltv.sql').write_text(BASE_LTV.replace('count(*)', 'count(distinct status)'))
    calls: list[list[str]] = []
    run = subprocess.run

    def recording(command: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(command, list) and command[:1] == ['git']:
            calls.append([str(part) for part in command])
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', recording)
    result = Project(repo).diff(base='reviewed')
    assert result.base == 'reviewed' and result.base_commit is None
    naming = [call for call in calls if any('reviewed' in part for part in call)]
    assert naming == [
        ['git', '-C', str(repo), 'rev-parse', '--verify', '--quiet', '--end-of-options', 'reviewed^{commit}']
    ]
    assert any(commit in call for call in calls if 'diff' in call)
    with pytest.raises(DiffError, match="'no-such-branch' is not a commit, branch, tag or date"):
        Project(repo).diff(base='no-such-branch')


def test_outside_git(tmp_path: Path) -> None:
    (tmp_path / 'x.sql').write_text('create table b as select a from t')
    project = Project(tmp_path)
    project.build()
    with pytest.raises(DiffError):
        project.diff()


# diff on a dbt project, with Jinja that only rendering can resolve


@pytest.mark.dbt
def test_dbt_project_diff(built_jaffle: Path, tmp_path: Path) -> None:
    root = tmp_path / 'shop'
    shutil.copytree(built_jaffle, root)
    (root / '.gitignore').write_text('target/\nlogs/\nkonstruct-out/\n*.duckdb\n')
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'base')
    from .conftest import run_dbt

    run_dbt(root, 'docs', 'generate')
    Project(root).build(run_dbt=False)
    payments = root / 'models' / 'staging' / 'stg_payments.sql'
    payments.write_text(payments.read_text().replace('payment_method,', 'payment_method as method,'))
    orders = root / 'models' / 'orders.sql'
    orders.write_text(
        orders.read_text().replace('from orders\n', "from orders\n    where orders.status != 'returned'\n", 1)
    )
    result = Project(root).diff(base='HEAD')
    assert [(label, c.column, c.breaks) for label, c in result.breaking] == [
        ('stg_payments', 'payment_method', ['orders'])
    ]
    [orders_change] = [r for r in result.relations if r.label == 'orders']
    assert orders_change.note is None
    assert orders_change.conditions_added == ["WHERE: jaffle_shop.main.stg_orders.status <> 'returned'"]


@pytest.mark.dbt
def test_column_aware_selection_is_sound(built_jaffle: Path, tmp_path: Path) -> None:
    """Build the project with dbt before and after a change and compare every table: each model
    whose data changed must be in konstruct's selection, which must still be smaller than
    state:modified+ when a change touches nothing downstream."""
    from .conftest import run_dbt

    root = tmp_path / 'shop'
    shutil.copytree(built_jaffle, root)
    (root / '.gitignore').write_text('target/\nlogs/\nkonstruct-out/\n*.duckdb\n')
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'base')
    run_dbt(root, 'build')

    def snapshot() -> dict[str, list[str]]:
        con = duckdb.connect(str(root / 'jaffle_shop.duckdb'), read_only=True)
        try:
            names = [
                r[0]
                for r in con.execute(
                    "select table_name from information_schema.tables where table_schema = 'main'"
                ).fetchall()
            ]
            return {
                name: sorted(map(repr, con.execute(f'select * from main."{name}"').fetchall()))
                for name in names
            }
        finally:
            con.close()

    before = snapshot()
    payments = root / 'models' / 'staging' / 'stg_payments.sql'
    payments.write_text(
        payments.read_text().replace('amount / 100 as amount', 'amount / 100.0 * 1.2 as amount')
    )
    customers = root / 'models' / 'staging' / 'stg_customers.sql'
    customers.write_text(customers.read_text().replace('    select\n', '    SELECT\n'))
    run_dbt(root, 'build')
    after = snapshot()
    changed_tables = {name for name in after if before.get(name) != after[name]}

    run_dbt(root, 'docs', 'generate')
    project = Project(root)
    project.build(run_dbt=False)
    selection = project.diff(base='HEAD').selection
    assert changed_tables == {'stg_payments', 'orders', 'customers'}
    assert changed_tables <= set(selection.models)
    assert 'stg_customers' not in selection.models
    assert 'stg_customers' in selection.modified_plus
    assert len(selection.models) < len(selection.modified_plus)
    assert 'not_null_orders_amount' in selection.tests


def test_dbt_yaml_changes() -> None:
    from konstruct.analysis.diff import compare_yaml

    before = """
version: 2
sources:
  - name: app
    tables: [{name: events}]
models:
  - name: orders
    description: One row per order
    data_tests: [{dbt_utils.expression_is_true: {arguments: {expression: "amount >= 0"}}}]
    columns:
      - name: order_id
        data_tests: [unique, not_null]
      - name: amount
        description: Order value
        tests: [not_null]
exposures:
  - name: dashboard
    depends_on: ["ref('orders')", "ref('customers')"]
"""
    after = """
version: 2
sources:
  - name: app
    tables: [{name: events}, {name: sessions}]
models:
  - name: orders
    description: One row per order, excluding returns
    columns:
      - name: order_id
        data_tests: [unique]
      - name: amount
        description: Order value
        tests: [not_null, {accepted_range: {min_value: 0}}]
exposures:
  - name: dashboard
    depends_on: ["ref('customers')"]
"""
    change = compare_yaml('models/schema.yml', before, after)
    assert change.tests_removed == ['orders.order_id: not_null', 'orders: dbt_utils.expression_is_true']
    assert change.tests_added == ['orders.amount: accepted_range']
    assert change.sources_added == ['app.sessions']
    assert change.descriptions == ['description changed: orders']
    assert change.exposures == [
        "exposure dashboard now depends on ref('customers') (was ref('customers'), ref('orders'))"
    ]
    unreadable = compare_yaml('x.yml', 'not: [valid', 'models: []')
    assert unreadable.changed is False
    assert unreadable.note is not None and unreadable.note.startswith('could not compare the before version')
    # A test given its own name is named the same way whichever form declares it.
    named = 'models: [{name: o, columns: [{name: id, data_tests: [%s]}]}]'
    long_form = named % '{name: id_is_unique, test_name: unique}'
    short_form = named % '{unique: {name: id_is_unique}}'
    assert compare_yaml('x.yml', None, long_form).tests_added == ['o.id: unique (id_is_unique)']
    assert compare_yaml('x.yml', None, short_form).tests_added == ['o.id: unique (id_is_unique)']
    assert not compare_yaml('x.yml', long_form, short_form).changed


def test_the_summary_counts_yaml_files_that_could_not_be_compared() -> None:
    from konstruct.analysis.diff import Diff, compare_yaml

    unreadable = compare_yaml('a.yml', 'models: [', 'models: []')
    tested = compare_yaml('b.yml', 'models: []', 'models:\n  - name: m\n    data_tests: [unique]\n')
    described = compare_yaml('c.yml', 'models: []', 'models:\n  - name: m\n    description: M\n')
    result = Diff(base='HEAD', head='working tree', files={})
    result.yaml = [unreadable]
    assert result.summary() == '1 YAML file could not be compared.'
    result.yaml = [unreadable, compare_yaml('d.yml', 'x: "\\ud800"', None)]
    assert result.summary() == '2 YAML files could not be compared.'
    result.yaml = [tested, unreadable]
    assert result.summary() == '1 dbt test added, 1 YAML file could not be compared.'
    result.yaml = [described]
    assert result.summary() == 'No change to models, SQL or DAGs.'


def test_yaml_changes_appear_in_the_diff(repo: Path) -> None:
    (repo / 'schema.yml').write_text(
        'version: 2\nmodels:\n  - name: ltv\n    columns:\n      - name: total\n        tests: [not_null]\n'
    )
    git(repo, 'add', 'schema.yml')
    git(repo, 'commit', '-qm', 'tests')
    (repo / 'schema.yml').write_text(
        'version: 2\nmodels:\n  - name: ltv\n    columns:\n      - name: total\n'
    )
    result = Project(repo).diff(base='HEAD')
    assert result.summary() == '1 dbt test removed.'
    assert 'test removed: ltv.total: not_null' in str(result)
    assert '- test removed: ltv.total: not_null' in result.markdown()


def test_a_character_json_escapes_as_a_surrogate_pair_is_read_in_a_diff(repo: Path) -> None:
    """``json.dumps`` and Jinja's ``tojson`` write a character past the Basic Multilingual Plane
    as an escaped surrogate pair, which is the character, not two lone halves."""
    description = json.dumps('Orders \N{PACKAGE}')
    assert description == '"Orders \\ud83d\\udce6"'
    base = f'version: 2\nmodels:\n  - name: ltv\n    description: {description}\n    columns:\n'
    (repo / 'schema.yml').write_text(base + '      - name: total\n        tests: [not_null]\n')
    git(repo, 'add', 'schema.yml')
    git(repo, 'commit', '-qm', 'tests')
    (repo / 'schema.yml').write_text(base + '      - name: total\n        tests: [not_null, unique]\n')
    result = Project(repo).diff(base='HEAD')
    assert result.summary() == '1 dbt test added.'
    (change,) = result.yaml
    assert (change.tests_added, change.tests_removed, change.note) == (['ltv.total: unique'], [], None)


def test_a_yaml_version_that_cannot_be_read_is_noted_and_not_compared(repo: Path) -> None:
    (repo / 'schema.yml').write_text(
        'version: 2\nmodels:\n  - name: ltv\n    columns:\n      - name: total\n        tests: [not_null]\n'
    )
    git(repo, 'add', 'schema.yml')
    git(repo, 'commit', '-qm', 'tests')
    (repo / 'schema.yml').write_text('version: 2\nmodels:\n  - name: ltv\n    description: "\\ud800"\n')
    result = Project(repo).diff(base='HEAD')
    assert result.summary() == '1 YAML file could not be compared.'
    assert result.counts()['tests_removed'] == 0
    note = (
        'could not compare the after version, which is not valid YAML: '
        'a lone surrogate is not allowed (line 4)'
    )
    assert [y['note'] for y in result.to_dict()['yaml']] == [note]
    assert note in str(result) and f'- {note}' in result.markdown()


def _guessed_columns_project(root: Path) -> Graph:
    """Without a warehouse, raw.src and raw.other have only the columns m1 and m2 read."""
    (root / 'm1.sql').write_text('create table main.m1 as select id, amount from raw.src')
    (root / 'm2.sql').write_text('create table main.m2 as select a_id, region from raw.other')
    return build(load_config(root)).graph


def test_diff_takes_the_columns_the_build_only_guessed_as_the_build_did(tmp_path: Path) -> None:
    # Neither relation is known to lack label, so a star over their join could take it from either.
    graph = _guessed_columns_project(tmp_path)
    sql = 'with q as (select * from raw.src a join raw.other o on a.id = o.a_id) select label from q'
    [statement] = Context(graph, tmp_path).lineage(sql, None, None, None)
    assert {(s.relation, s.column) for s in statement.columns['label']} == {
        ('raw.src', 'label'),
        ('raw.other', 'label'),
    }


PAIRS = (
    'create table main.pairs as select {o}.id, sum({p}.amount) as total from raw.orders {o} '
    'join raw.orders {p} on {on} group by {o}.id'
)


def test_a_self_join_turned_the_other_way_is_a_row_change(repo: Path) -> None:
    (repo / 'sql' / 'pairs.sql').write_text(PAIRS.format(o='o', p='p', on='p.customer_id = o.id'))
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'pairs')
    (repo / 'sql' / 'pairs.sql').write_text(PAIRS.format(o='a', p='b', on='b.customer_id = a.id'))
    assert not [r for r in Project(repo).diff(base='HEAD').relations if r.changed]
    (repo / 'sql' / 'pairs.sql').write_text(PAIRS.format(o='o', p='p', on='o.customer_id = p.id'))
    [relation] = [r for r in Project(repo).diff(base='HEAD').relations if r.changed]
    assert relation.conditions_removed == [
        'JOIN ON: shop.raw."orders#2".customer_id = shop.raw."orders#1".id'
    ]
    assert relation.conditions_added == ['JOIN ON: shop.raw."orders#1".customer_id = shop.raw."orders#2".id']


@pytest.mark.parametrize('on', ['p.customer_id = o.id', 'o.id = p.customer_id'])
def test_the_sides_of_an_equality_written_the_other_way_round_are_no_change(repo: Path, on: str) -> None:
    (repo / 'sql' / 'pairs.sql').write_text(
        PAIRS.format(o='o', p='p', on='p.customer_id = o.id and o.id > 0')
    )
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'pairs')
    (repo / 'sql' / 'pairs.sql').write_text(PAIRS.format(o='o', p='p', on=f'0 < o.id and {on}'))
    assert not [r for r in Project(repo).diff(base='HEAD').relations if r.changed]


def test_a_description_that_is_not_text_is_read_as_its_text() -> None:
    from konstruct.analysis.diff import _yaml_facts

    facts = _yaml_facts('models:\n  - name: a\n    description: 5\n  - name: b\n    description: [x]\n')
    assert facts['descriptions'] == {'a': '5', 'b': "['x']"}
    assert _yaml_facts('models:\n  - name: c\n    description: 0\n')['descriptions'] == {'c': ''}


def test_a_column_description_that_is_not_text_is_read_as_its_text() -> None:
    from konstruct.analysis.diff import _yaml_facts

    facts = _yaml_facts(
        'models:\n  - name: m\n    columns: [{name: c, description: [1]}, {name: d, description: 7}]\n'
    )
    assert facts['descriptions'] == {'m': '', 'm.c': '[1]', 'm.d': '7'}
