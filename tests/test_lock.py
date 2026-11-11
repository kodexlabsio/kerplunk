"""The lineage lockfile: a consumer pins the meaning of the upstream columns it reads."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import duckdb
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from konstruct import Project
from konstruct.analysis.lock import (
    CANONICAL,
    FILE_NAME,
    LockCheck,
    LockError,
    Lockfile,
    Pin,
    PinnedTerm,
    Rows,
    check,
    check_consumers,
    find,
    lock,
    upstream_reads,
)
from konstruct.core.graph import Graph, Node

# The projects list other repositories outside them, which the build reads only in a trusted project.
pytestmark = pytest.mark.usefixtures('trust_config')

LTV = """create table main.ltv as
with paid as (select customer_id, amount from raw.orders where status <> 'returned')
select customer_id, sum(amount) as total from paid group by customer_id
"""
REPORT = 'create table main.report as select customer_id, total * 2 as doubled from main.ltv'
TERM = """version: 1
terms:
  - name: Lifetime value
    definition: {definition}
    owner: {{name: Priya}}
    status: {status}
    approved: {approved}
    columns: [main.ltv.total]
"""


def git(root: Path, *args: str, author: str = 'Ada') -> str:
    env = {
        **os.environ,
        'GIT_AUTHOR_NAME': author,
        'GIT_AUTHOR_EMAIL': f'{author.lower()}@example.com',
        'GIT_COMMITTER_NAME': author,
        'GIT_COMMITTER_EMAIL': f'{author.lower()}@example.com',
    }
    return subprocess.run(
        ['git', '-C', str(root), '-c', 'commit.gpgsign=false', *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout


def commit(root: Path, message: str, author: str) -> str:
    git(root, 'add', '-A', author=author)
    git(root, 'commit', '-q', '-m', message, author=author)
    return git(root, 'rev-parse', '--short=7', 'HEAD').strip()


def build(root: Path) -> Graph:
    return Project(root).build(warehouses=False, run_dbt=False).graph


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[Path, Path]:
    """finance builds main.ltv in its own git repository; marketing reads it, with finance listed
    in its repositories."""
    finance, marketing = tmp_path / 'finance', tmp_path / 'marketing'
    finance.mkdir()
    marketing.mkdir()
    (finance / 'ltv.sql').write_text(LTV)
    (finance / 'definitions.yml').write_text(
        TERM.format(definition='What a customer has paid.', status='approved', approved='2026-10-30')
    )
    git(finance, 'init', '-q', '-b', 'main')
    commit(finance, 'Add lifetime value', 'Ada')
    (marketing / 'report.sql').write_text(REPORT)
    (marketing / 'konstruct.toml').write_text('dialect = "duckdb"\nrepositories = ["../finance"]\n')
    return finance, marketing


def test_lock_pins_what_the_consumer_reads(repositories: tuple[Path, Path]) -> None:
    _, marketing = repositories
    graph = build(marketing)
    result = lock(graph, marketing)
    assert result.added == ['main.ltv.customer_id', 'main.ltv.total']
    assert str(result).startswith('Wrote konstruct.lock: 2 upstream columns pinned (2 added).')
    text = (marketing / FILE_NAME).read_text()
    pin = Lockfile.loads(text).pin('main.ltv.total')
    assert pin is not None
    assert pin.formula == 'SUM(raw.orders.amount)'
    assert pin.conditions == ['GROUP BY: raw.orders.customer_id', "WHERE: raw.orders.status <> 'returned'"]
    assert pin.sources == ['raw.orders.amount']
    assert pin.repository == 'finance'
    assert pin.used_by == ['report.doubled']
    assert [(t.name, t.status, t.approved, t.owner) for t in pin.terms] == [
        ('Lifetime value', 'approved', '2026-10-30', 'Priya')
    ]
    assert pin.fingerprint.startswith('sha256:') and len(pin.fingerprint) == 71
    # Writing again changes nothing, byte for byte.
    again = lock(graph, marketing)
    assert again.kept == result.added and not again.added
    assert (marketing / FILE_NAME).read_text() == text
    passed = check(graph, marketing)
    assert passed.passed and str(passed) == 'konstruct.lock: 2 pinned upstream columns, none changed.'
    json.dumps(passed.to_dict())
    json.dumps(result.to_dict())


def test_formatting_renamed_ctes_and_passthroughs_are_not_changes(repositories: tuple[Path, Path]) -> None:
    finance, marketing = repositories
    lock(build(marketing), marketing)
    (finance / 'ltv.sql').write_text(
        'CREATE TABLE main.ltv AS\n'
        'WITH settled AS (\n  SELECT amount, customer_id\n  FROM raw.orders\n'
        "  WHERE status != 'returned'\n),\n"
        'again AS (SELECT * FROM settled)\n'
        'SELECT   SUM( amount )   AS total,\n         customer_id\nFROM again\nGROUP BY customer_id\n'
    )
    commit(finance, 'Tidy the lifetime value SQL', 'Ana')
    result = check(build(marketing), marketing)
    assert result.passed, str(result)


def test_a_formula_change_fails_with_what_changed_and_who(repositories: tuple[Path, Path]) -> None:
    finance, marketing = repositories
    lock(build(marketing), marketing)
    (finance / 'ltv.sql').write_text(LTV.replace('sum(amount)', 'sum(amount * 1.1)'))
    sha = commit(finance, 'Include VAT in lifetime value', 'Ana Lima')
    result = check(build(marketing), marketing)
    assert not result.passed
    [change] = result.changes
    assert change.column == 'main.ltv.total' and change.reasons == ['formula']
    assert (change.formula_before, change.formula_after) == ('SUM(orders.amount)', 'SUM(orders.amount * 1.1)')
    assert change.affected == ['report.doubled']
    [by] = change.changed_by
    assert (by.author, by.short, by.subject, by.repository) == (
        'Ana Lima',
        sha,
        'Include VAT in lifetime value',
        '../finance',
    )
    text = str(result)
    assert (
        text.splitlines()[0] == 'konstruct.lock: 1 of 2 pinned upstream columns changed since it was locked.'
    )
    for line in (
        'main.ltv.total  (ltv.total, built by finance, ../finance/ltv.sql)',
        '  formula changed',
        '    locked  SUM(orders.amount)',
        '    now     SUM(orders.amount * 1.1)',
        '    change  added * 1.1',
        f'  changed by         Ana Lima in {sha} on ',
        '                     ltv.total: was SUM(orders.amount)',
        '                                now SUM(orders.amount * 1.1)',
        '  your columns       report.doubled',
        '  accept it          konstruct lock --update main.ltv.total',
        f'  or keep the old    ask finance to revert {sha} (by Ana Lima)',
    ):
        assert any(row.startswith(line) for row in text.splitlines()), (line, text)
    assert '```\nmain.ltv.total' in result.markdown()
    data = result.to_dict()
    assert data['changes'][0]['accept'] == 'konstruct lock --update main.ltv.total'
    assert data['changes'][0]['keep'] == f'ask finance to revert {sha} (by Ana Lima)'
    json.dumps(data)


def test_a_filter_change_fails_and_update_accepts_it(repositories: tuple[Path, Path]) -> None:
    finance, marketing = repositories
    lock(build(marketing), marketing)
    (finance / 'ltv.sql').write_text(LTV.replace("status <> 'returned'", "status = 'completed'"))
    commit(finance, 'Count completed orders only', 'Ana')
    graph = build(marketing)
    result = check(graph, marketing)
    assert [c.column for c in result.changes] == ['main.ltv.customer_id', 'main.ltv.total']
    total = result.changes[1]
    assert total.reasons == ['conditions']
    assert total.conditions_added == ["WHERE: orders.status = 'completed'"]
    assert total.conditions_removed == ["WHERE: orders.status <> 'returned'"]
    assert total.changed_by[0].what == '../finance/ltv.sql' and total.changed_by[0].author == 'Ana'
    assert 'Accept every change: konstruct lock --update' in str(result)
    # Locking again keeps the pins as they were, as a package lockfile keeps its versions.
    kept = lock(graph, marketing)
    assert kept.stale == ['main.ltv.customer_id', 'main.ltv.total']
    assert 'differ from this build and were kept as locked' in str(kept)
    assert not check(graph, marketing).passed
    accepted = lock(graph, marketing, update=['ltv.total'])
    assert accepted.updated == ['main.ltv.total'] and '~ main.ltv.total (change accepted)' in str(accepted)
    assert [c.column for c in check(graph, marketing).changes] == ['main.ltv.customer_id']
    assert lock(graph, marketing, update=True).updated == ['main.ltv.customer_id']
    assert check(graph, marketing).passed
    with pytest.raises(LockError, match=re.escape("'report.doubled' is not pinned")):
        lock(graph, marketing, update=['report.doubled'])


def test_a_removed_column_and_new_reads(repositories: tuple[Path, Path]) -> None:
    finance, marketing = repositories
    lock(build(marketing), marketing)
    (finance / 'ltv.sql').write_text(LTV.replace('sum(amount) as total', 'sum(amount) as lifetime_total'))
    commit(finance, 'Rename total', 'Ana')
    (marketing / 'report.sql').write_text(REPORT.replace('total * 2', 'lifetime_total * 2'))
    result = check(build(marketing), marketing)
    [change] = result.changes
    assert change.reasons == ['removed'] and change.affected == ['report.doubled']
    assert '  removed: no such column in this build' in str(result)
    assert change.changed_by[0].author == 'Ana'
    assert any('read but not pinned: ltv.lifetime_total' in n for n in result.notes)
    relocked = lock(build(marketing), marketing)
    assert (relocked.added, relocked.removed) == (['main.ltv.lifetime_total'], ['main.ltv.total'])


def test_a_type_change(repositories: tuple[Path, Path]) -> None:
    _, marketing = repositories
    graph = build(marketing)
    total = graph.nodes['table.main.ltv#total']
    total.data_type = 'double'
    lock(graph, marketing)
    total.data_type = 'decimal(18, 2)'
    result = check(graph, marketing, history=False)
    [change] = result.changes
    assert change.reasons == ['type'] and (change.type_before, change.type_after) == (
        'DOUBLE',
        'DECIMAL(18, 2)',
    )
    assert '  type               DOUBLE -> DECIMAL(18, 2)' in str(result)
    total.data_type = None
    unknown = check(graph, marketing, history=False)
    assert unknown.passed and unknown.notes == [
        'ltv.total: its type (DOUBLE) is not known in this build, so it was not compared'
    ]


def test_an_approved_definition_change(repositories: tuple[Path, Path]) -> None:
    finance, marketing = repositories
    lock(build(marketing), marketing)
    (finance / 'definitions.yml').write_text(
        TERM.format(
            definition='What a customer has paid, net of VAT.', status='approved', approved='2026-10-30'
        )
    )
    sha = commit(finance, 'Lifetime value is net of VAT', 'Grace')
    result = check(build(marketing), marketing)
    [change] = result.changes
    assert change.reasons == ['definition']
    assert change.terms == [
        "'Lifetime value' definition: 'What a customer has paid.' -> 'What a customer has paid, net of VAT.'"
    ]
    [by] = change.changed_by
    assert (by.what, by.author, by.short) == ("term 'Lifetime value'", 'Grace', sha)
    assert "  definition         'Lifetime value' definition: " in str(result)


def test_a_term_reapproved_or_withdrawn(repositories: tuple[Path, Path]) -> None:
    finance, marketing = repositories
    lock(build(marketing), marketing)
    definition = 'What a customer has paid.'
    (finance / 'definitions.yml').write_text(
        TERM.format(definition=definition, status='approved', approved='2026-11-05')
    )
    reapproved = check(build(marketing), marketing, history=False)
    assert reapproved.passed
    assert reapproved.notes == ["ltv.total: 'Lifetime value' re-approved (2026-10-30 -> 2026-11-05)"]
    (finance / 'definitions.yml').write_text(
        TERM.format(definition=definition, status='draft', approved='null')
    )
    withdrawn = check(build(marketing), marketing, history=False)
    assert withdrawn.changes[0].terms == ["'Lifetime value' status approved -> draft"]
    (finance / 'definitions.yml').unlink()
    unbound = check(build(marketing), marketing, history=False)
    assert unbound.changes[0].terms == ["'Lifetime value' is no longer bound to this column"]


def test_terms_that_were_not_approved_only_note() -> None:
    from konstruct.analysis.lock import term_changes

    notes: list[str] = []
    old = [PinnedTerm('Revenue', 'draft', 'a', definition='Old.'), PinnedTerm('Gone', 'agreed', 'b')]
    new = [PinnedTerm('Revenue', 'draft', 'c', definition='New.'), PinnedTerm('Margin', 'approved', 'd')]
    assert term_changes(old, new, 'x.y', notes) == []
    assert notes == [
        "x.y: 'Revenue' definition: 'Old.' -> 'New.' (not approved when locked)",
        "x.y: 'Gone' is no longer bound to this column (not approved when locked)",
        "x.y: now bound to 'Margin' (approved), which was not when locked",
    ]


def test_uncommitted_changes_are_named(repositories: tuple[Path, Path]) -> None:
    finance, marketing = repositories
    lock(build(marketing), marketing)
    (finance / 'ltv.sql').write_text(LTV.replace("status <> 'returned'", "status <> 'void'"))
    result = check(build(marketing), marketing)
    change = result.changes[-1]
    assert str(change.changed_by[0]) == 'uncommitted changes to ../finance/ltv.sql'
    assert change.keep == 'undo the uncommitted change to ../finance/ltv.sql'


def _dbt_model(package: str, name: str, raw: str, depends: list[str], schema: str) -> dict[str, Any]:
    return {
        'unique_id': f'model.{package}.{name}',
        'resource_type': 'model',
        'name': name,
        'alias': name,
        'package_name': package,
        'database': 'warehouse',
        'schema': schema,
        'relation_name': f'"warehouse"."{schema}"."{name}"',
        'original_file_path': f'models/{name}.sql',
        'fqn': [package, name],
        'raw_code': raw,
        'language': 'sql',
        'depends_on': {'nodes': depends, 'macros': []},
        'config': {'materialized': 'table'},
        'tags': [],
        'columns': {},
    }


def _dbt_source(package: str, source: str, name: str, schema: str, columns: list[str]) -> dict[str, Any]:
    return {
        'unique_id': f'source.{package}.{source}.{name}',
        'resource_type': 'source',
        'name': name,
        'identifier': name,
        'source_name': source,
        'package_name': package,
        'database': 'warehouse',
        'schema': schema,
        'relation_name': f'"warehouse"."{schema}"."{name}"',
        'columns': {c: {'name': c} for c in columns},
    }


def _dbt_repository(
    root: Path, package: str, nodes: list[dict[str, Any]], sources: list[dict[str, Any]]
) -> None:
    (root / 'target').mkdir(parents=True, exist_ok=True)
    (root / 'models').mkdir(exist_ok=True)
    (root / 'dbt_project.yml').write_text(f'name: {package}\n')
    for node in nodes:
        (root / node['original_file_path']).write_text(node['raw_code'])
    manifest = {
        'metadata': {'adapter_type': 'postgres'},
        'nodes': {n['unique_id']: n for n in nodes},
        'sources': {s['unique_id']: s for s in sources},
        'exposures': {},
    }
    (root / 'target' / 'manifest.json').write_text(json.dumps(manifest))


def _finance(root: Path, ltv: str) -> None:
    _dbt_repository(
        root,
        'shop',
        [
            _dbt_model(
                'shop',
                'stg',
                "select id, amount, region from {{ source('raw', 'orders') }}",
                ['source.shop.raw.orders'],
                'analytics',
            ),
            _dbt_model('shop', 'ltv', ltv, ['model.shop.stg'], 'analytics'),
        ],
        [_dbt_source('shop', 'raw', 'orders', 'raw', ['id', 'amount', 'region'])],
    )


def _marketing(root: Path) -> None:
    _dbt_repository(
        root,
        'shop',
        [
            _dbt_model(
                'shop',
                'report',
                "select id, total * 2 as doubled from {{ source('fin', 'ltv') }}",
                ['source.shop.fin.ltv'],
                'marketing',
            )
        ],
        [_dbt_source('shop', 'fin', 'ltv', 'analytics', [])],
    )


def test_dbt_projects_in_two_repositories(tmp_path: Path) -> None:
    """marketing reads finance's ltv model as a dbt source: the pin composes through the source
    down to finance's raw orders, and finance's own build checks marketing's lockfile."""
    finance, marketing = tmp_path / 'finance', tmp_path / 'marketing'
    ltv = "select id, sum(amount) as total from {{ ref('stg') }} group by id"
    _finance(finance, ltv)
    _marketing(marketing)
    (marketing / 'konstruct.toml').write_text('repositories = ["../finance"]\n')
    (finance / 'konstruct.toml').write_text('repositories = ["../marketing"]\n')
    graph = build(marketing)
    assert [graph.label(n) for n in upstream_reads(graph)] == ['fin.ltv.id', 'fin.ltv.total']
    lock(graph, marketing)
    pin = Lockfile.read(marketing / FILE_NAME).pin('warehouse.analytics.ltv.total')
    assert pin is not None
    assert pin.formula == 'SUM(warehouse.raw.orders.amount)'
    assert pin.conditions == ['GROUP BY: warehouse.raw.orders.id']
    assert pin.id == 'source.shop.fin.ltv#total' and pin.used_by == ['report.doubled']
    # Run in finance, the producer, the check names the consumer whose lockfile it is.
    [consumer] = check_consumers(build(finance), finance)
    assert consumer.consumer == 'marketing' and consumer.passed
    assert (
        consumer.summary()
        == '../marketing/konstruct.lock (marketing): 2 pinned upstream columns, none changed.'
    )
    _finance(finance, ltv.replace('sum(amount)', "sum(case when region = 'EU' then amount end)"))
    [broken] = check_consumers(build(finance), finance, history=False)
    assert broken.path == '../marketing/konstruct.lock'
    [change] = broken.changes
    assert change.column == 'warehouse.analytics.ltv.total' and change.affected == ['report.doubled']
    assert change.formula_after == "SUM(CASE WHEN raw.orders.region = 'EU' THEN raw.orders.amount END)"
    assert change.sources_added == ['raw.orders.region']
    assert str(broken).startswith(
        '../marketing/konstruct.lock (marketing): 1 of 2 pinned upstream columns changed'
    )
    # And the consumer's own check finds the same change.
    assert [c.column for c in check(build(marketing), marketing, history=False).changes] == [
        'warehouse.analytics.ltv.total'
    ]


def test_the_jaffle_example(jaffle_built_graph: Path, tmp_path: Path) -> None:
    graph = Graph.read(jaffle_built_graph / 'konstruct-out' / 'graph.json')
    target = tmp_path / FILE_NAME
    with pytest.raises(LockError, match='nothing to lock'):
        lock(graph, jaffle_built_graph, path=target)
    result = lock(graph, jaffle_built_graph, ['customer_dashboard'], path=target)
    assert 'jaffle_shop.main.customers.customer_lifetime_value' in result.added
    lockfile = Lockfile.read(target)
    assert lockfile.reads == ['customer_dashboard']
    clv = lockfile.pin('jaffle_shop.main.customers.customer_lifetime_value')
    assert (
        clv is not None and clv.formula is not None and 'jaffle_shop.main.raw_payments.amount' in clv.formula
    )
    assert 'customer_dashboard (exposure)' in clv.used_by
    assert [(t.name, t.status) for t in clv.terms] == [('Customer lifetime value', 'approved')]
    assert check(graph, jaffle_built_graph, path=target).passed
    # Without a selection, the lockfile's own is used again.
    assert lock(graph, jaffle_built_graph, path=target).kept == result.added
    term = graph.nodes['term.customer_lifetime_value']
    term.description = 'Total paid, net of refunds.'
    payments = graph.nodes['model.jaffle_shop.stg_payments#amount']
    payments.attrs['expression'] = payments.attrs['expression'].replace('/ 100', '/ 1000')
    changed = check(graph, jaffle_built_graph, path=target)
    reasons = {c.label: c.reasons for c in changed.changes}
    assert reasons['customers.customer_lifetime_value'] == ['formula', 'definition']
    clv_change = next(c for c in changed.changes if c.label == 'customers.customer_lifetime_value')
    assert '1000' in str(clv_change.formula_after) and '100' in str(clv_change.formula_before)
    assert 'customer_dashboard (exposure)' in clv_change.affected


def test_selection_by_relation_column_and_term(repositories: tuple[Path, Path]) -> None:
    _, marketing = repositories
    graph = build(marketing)
    target = marketing / 'pins.lock'
    assert lock(graph, marketing, ['ltv'], path=target).added == ['main.ltv.customer_id', 'main.ltv.total']
    assert lock(graph, marketing, ['ltv.total'], path=target).removed == ['main.ltv.customer_id']
    assert lock(graph, marketing, ['Lifetime value'], path=target, write=False).kept == ['main.ltv.total']
    graph.add_node(Node(id='table.main.empty', kind='table', name='empty', relation='main.empty'))
    with pytest.raises(LockError, match=re.escape('nothing to lock: main.empty names no columns')):
        lock(graph, marketing, ['main.empty'], path=target)
    graph.add_node(Node(id='task.d.t', kind='task', name='t'))
    with pytest.raises(
        LockError, match=re.escape("'task.d.t' is a task; lock columns, relations, exposures or terms")
    ):
        lock(graph, marketing, ['task.d.t'], path=target)


def test_lockfile_format_round_trips(tmp_path: Path) -> None:
    pin = Pin(
        column='main.x.y',
        id='table.main.x#y',
        fingerprint='sha256:' + '0' * 64,
        formula='CASE WHEN a = \'"q"\\\n\t\x01\' THEN 1 END',
        rows=[Rows('main.x', ['WHERE: b = 1']), Rows('main.z', ['WHERE: c'], 'LEFT', ['WINDOW'])],
        sources=['raw.t.a'],
        files=['main.p sha256:1'],
        type='INT',
        complete=False,
        verified='confirmed',
        terms=[PinnedTerm('Term', 'approved', 'v1', filters=['b = 1'])],
    )
    # Kept as locked, a pin can rely on conditions main.x no longer has.
    other = Pin('main.a.b', 'table.main.a#b', 'sha256:' + '1' * 64, rows=[Rows('main.x', ['WHERE: b = 2'])])
    lockfile = Lockfile(pins=[pin, other], reads=['x'])
    text = lockfile.dumps()
    assert text.index('main.a.b') < text.index('main.x.y')
    assert 'complete = false' in text and '[[pin.terms]]' in text
    assert f'\nformat = 1\ncanonical = "{CANONICAL}"\n' in text
    assert '\n[[relation]]\nname = "main.x~2"\nconditions = [\n  "WHERE: b = 1",\n]\n' in text
    assert 'relations = [\n  "LEFT JOIN main.z BEFORE WINDOW",\n  "main.x~2",\n]' in text
    loaded = Lockfile.loads(text)
    assert loaded.pins[1] == pin and loaded.reads == ['x'] and loaded.canonical == CANONICAL
    assert loaded.pins[1].conditions == ['LEFT JOIN WHERE BEFORE WINDOW: c', 'WHERE: b = 1']
    assert loaded.dumps() == text
    data = json.loads(json.dumps(lockfile.to_dict()))
    assert data['pins'][1]['conditions'] == ['LEFT JOIN WHERE BEFORE WINDOW: c', 'WHERE: b = 1']
    assert data['format'] == 1
    with pytest.raises(
        LockError, match=re.escape("'relations' names 'main.q', which no [[relation]] defines")
    ):
        Lockfile.loads(text.replace('"main.x~2",\n]', '"main.q",\n]'))
    with pytest.raises(LockError, match=re.escape("[[relation]] 'main.z' is given twice")):
        Lockfile.loads(text + '\n[[relation]]\nname = "main.z"\n')
    with pytest.raises(LockError, match="a pin has a key the format does not have: 'formulas'"):
        Lockfile.loads(text.replace('formula = ', 'formulas = ', 1))
    with pytest.raises(LockError, match="the file has a key the format does not have: 'pins'"):
        Lockfile.loads(text.replace('reads = ', 'pins = 1\nreads = ', 1))
    with pytest.raises(LockError, match="a \\[\\[relation\\]\\] has a key the format does not have: 'x'"):
        Lockfile.loads(text.replace('name = "main.z"', 'name = "main.z"\nx = 1', 1))
    with pytest.raises(LockError, match='names a step other than WINDOW or LIMIT'):
        Lockfile.loads(text.replace('main.z BEFORE WINDOW', 'main.z BEFORE SORT', 1))
    with pytest.raises(LockError, match=re.escape("'main.a.b' is pinned twice")):
        Lockfile.loads(text + text[text.index('[[pin]]') : text.index('[[pin]]', text.index('[[pin]]') + 1)])
    with pytest.raises(LockError, match=re.escape("[[relation]] 'main.q' is not named by any pin")):
        Lockfile.loads(text + '\n[[relation]]\nname = "main.q"\n')
    with pytest.raises(LockError, match="'relation' must be a list of tables"):
        Lockfile.loads('format = 1\ncanonical = "x"\nrelation = 1\n')
    with pytest.raises(LockError, match='not valid TOML'):
        Lockfile.loads('format = ')
    with pytest.raises(LockError, match='unsupported format 2'):
        Lockfile.loads('format = 2')
    with pytest.raises(LockError, match="'canonical' is missing"):
        Lockfile.loads('format = 1')
    with pytest.raises(LockError, match="a pin is malformed: 'column' is missing"):
        Lockfile.loads(
            f'format = 1\ncanonical = "x"\n[[pin]]\nid = "x"\nfingerprint = "{other.fingerprint}"\n'
        )
    with pytest.raises(LockError, match="'pin' must be a list of tables"):
        Lockfile.loads('format = 1\ncanonical = "x"\npin = 1\n')
    with pytest.raises(LockError, match='a pin must be a table'):
        Lockfile.loads('format = 1\ncanonical = "x"\npin = [1]\n')
    with pytest.raises(LockError, match="'terms' must be a list of tables"):
        Lockfile.loads(
            f'format = 1\ncanonical = "x"\n[[pin]]\ncolumn = "a.b"\nid = "x"\n'
            f'fingerprint = "{other.fingerprint}"\nterms = 1\n'
        )
    with pytest.raises(LockError, match="'name' must be a string"):
        Lockfile.loads(text.replace('name = "Term"', 'name = 1'))
    with pytest.raises(LockError, match='no lockfile at'):
        check(Graph(), tmp_path)
    with pytest.raises(LockError, match='run `konstruct lock` first'):
        lock(Graph(), tmp_path, update=['x'])


def test_find_by_warehouse_name_then_id(repositories: tuple[Path, Path]) -> None:
    _, marketing = repositories
    graph = build(marketing)
    found = find(graph, 'main.ltv.total')
    assert found is not None and found.id == 'table.main.ltv#total'
    by_id = find(graph, 'gone.relation.total', 'table.main.ltv#total')
    assert by_id is not None and by_id.id == 'table.main.ltv#total'
    assert find(graph, 'gone.relation.total', 'table.main.ltv') is None


def test_formula_difference() -> None:
    from konstruct.analysis.lock import _difference

    assert _difference('SUM(a)', 'SUM(a * 1.1)') == 'added * 1.1'
    assert _difference('SUM(a * 1.1)', 'SUM(a)') == 'removed * 1.1'
    assert _difference('a + b', 'a - b') == '+ -> -'
    assert _difference('a', 'a') is None
    assert _difference('x', 'COALESCE(y, z, 0)') is None


CONDITIONS = [
    'GROUP BY: raw.orders.customer_id',
    'JOIN ON: raw.customers.id = raw.orders.customer_id',
    'WHERE: raw.orders.amount >= 10',
    "WHERE: raw.orders.channel = 'app' OR raw.orders.channel = 'web'",
    "WHERE: raw.orders.region IN ('a', 'b', 'c')",
    "WHERE: raw.orders.status <> 'returned'",
]


@settings(max_examples=40, deadline=None)
@given(
    cte=st.from_regex(r'[a-z][a-z0-9_]{0,8}', fullmatch=True).filter(
        lambda n: n not in ('as', 'by', 'or', 'on', 'in', 'c', 'o')
    ),
    space=st.sampled_from([' ', '\n', '  ', '\n\t']),
    upper=st.booleans(),
    flip=st.booleans(),
    order=st.permutations([0, 1, 2, 3]),
    nest=st.integers(min_value=0, max_value=2),
    regions=st.lists(st.sampled_from(["'a'", "'b'", "'c'"]), min_size=3, max_size=5).filter(
        lambda r: set(r) == {"'a'", "'b'", "'c'"}
    ),
    zeros=st.sampled_from(['', '0', '00']),
)
def test_fingerprints_ignore_how_the_sql_is_written(
    cte: str,
    space: str,
    upper: bool,
    flip: bool,
    order: list[int],
    nest: int,
    regions: list[str],
    zeros: str,
) -> None:
    """Against the oracle that the SQL means the same, whatever its layout, CTE name, the order
    and bracketing of its conjuncts, of the operands of =, <>, >= and OR and of an IN list (with
    an item given twice), and trailing zeros."""
    conjuncts = [
        "'returned' <> o.status" if flip else "o.status <> 'returned'",
        f'o.region in ({", ".join(regions)})',
        '10 <= o.amount' if flip else 'o.amount >= 10',
        "(o.channel = 'app' or o.channel = 'web')" if flip else "('web' = o.channel or o.channel = 'app')",
    ]
    first, second, third, fourth = (conjuncts[i] for i in order)
    where = [
        f'{first} and {second} and {third} and {fourth}',
        f'({first} and {second}) and ({third} and {fourth})',
        f'{first} and ({second} and ({third} and {fourth}))',
    ][nest]
    join = 'o.customer_id = c.id' if flip else 'c.id = o.customer_id'
    sql = (
        f'create table main.ltv as with {cte} as (select o.customer_id, o.amount from raw.orders o '
        f'join raw.customers c on {join} where {where}) select customer_id,{space}'
        f'sum(amount * 1.1{zeros}) as total from {cte}{space}group by customer_id'
    )
    if upper:
        sql = sql.replace('select', 'SELECT').replace('sum(', 'SUM(').replace('group by', 'GROUP BY')
        sql = sql.replace(' and ', ' AND ').replace(' or ', ' OR ')
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / 'ltv.sql').write_text(sql)
        (root / 'report.sql').write_text(REPORT)
        (root / 'konstruct.toml').write_text('dialect = "duckdb"\n')
        graph = build(root)
        pin = lock(graph, root, ['ltv.total'], write=False).lockfile.pins[0]
    assert (pin.formula, pin.conditions, pin.sources) == (
        'SUM(raw.orders.amount * 1.1)',
        CONDITIONS,
        ['raw.orders.amount'],
    )
    assert pin.fingerprint == FINGERPRINT


# The fingerprint of the formula above, worked out by hand: a change to how fingerprints are made
# changes every committed lockfile, so it must be deliberate.
FINGERPRINT = (
    'sha256:'
    + hashlib.sha256(
        json.dumps(
            {
                'conditions': CONDITIONS,
                'formula': 'SUM(raw.orders.amount * 1.1)',
                'sources': ['raw.orders.amount'],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
)


@pytest.fixture
def chain(tmp_path: Path) -> tuple[Path, Graph]:
    """raw.t -> main.z -> main.a -> main.b, in one repository, pinned by name."""
    (tmp_path / 'z.sql').write_text('create table main.z as select v - 1 as w from raw.t')
    (tmp_path / 'a.sql').write_text('create table main.a as select w + 1 as x from main.z')
    (tmp_path / 'b.sql').write_text('create table main.b as select x * 2 as y from main.a')
    (tmp_path / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    return tmp_path, build(tmp_path)


def test_a_change_beyond_a_gap_in_the_formula(chain: tuple[Path, Graph]) -> None:
    root, graph = chain
    # main.a's formula is not known (as for a Python model), so b.y's formula stops at it.
    del graph.nodes['table.main.a#x'].attrs['expression']
    pin = lock(graph, root, ['b.y']).lockfile.pins[0]
    assert (pin.formula, pin.complete) == ('main.a.x * 2', False)
    assert 'complete = false' in (root / FILE_NAME).read_text()
    graph.nodes['table.main.z#w'].attrs['expression'] = 'raw.t.v - 2'
    result = check(graph, root, history=False)
    [change] = result.changes
    assert change.reasons == ['upstream']
    assert '  a column further upstream changed, beyond what the composed formula shows' in str(result)


def test_a_formula_that_can_no_longer_be_worked_out(chain: tuple[Path, Graph]) -> None:
    root, graph = chain
    lock(graph, root, ['b.y'])
    del graph.nodes['table.main.b#y'].attrs['expression']
    [change] = check(graph, root).changes
    assert (change.reasons, change.formula_before) == (['unknown'], '((t.v - 1) + 1) * 2')
    assert 'the formula could not be worked out in this build' in '\n'.join(change.lines())


def test_verification_lost_is_a_note(chain: tuple[Path, Graph]) -> None:
    root, graph = chain
    edges = [e for e in graph.edges if e.kind == 'derived_from']
    for edge in edges:
        edge.attrs['verified'] = 'confirmed'
    assert lock(graph, root, ['b.y']).lockfile.pins[0].verified == 'confirmed'
    edges[0].attrs['verified'] = 'not exercised'
    result = check(graph, root)
    assert result.passed and result.notes == ['b.y: was verified when locked, and is partly confirmed now']
    assert '\nnote: b.y: was verified' in str(result) and '- b.y: was verified' in result.markdown()


def test_lock_time_comes_from_git(chain: tuple[Path, Graph]) -> None:
    from konstruct.analysis.lock import locked_at

    root, graph = chain
    assert locked_at(root / FILE_NAME) is None
    lock(graph, root, ['b.y'], write=False)
    assert not (root / FILE_NAME).exists()
    assert str(lock(graph, root, ['b.y'], write=False)).startswith('Would write konstruct.lock')
    lock(graph, root, ['b.y'])
    untracked = locked_at(root / FILE_NAME)
    assert untracked is not None and untracked[0].endswith('+00:00') and untracked[1] == ''
    git(root, 'init', '-q', '-b', 'main')
    env = {'GIT_COMMITTER_DATE': '2026-03-04T05:06:07+00:00'}
    os.environ.update(env)
    try:
        commit(root, 'Lock what the report reads', 'Ada')
    finally:
        for key in env:
            os.environ.pop(key)
    locked = locked_at(root / FILE_NAME)
    assert locked is not None and locked[0] == '2026-03-04T05:06:07+00:00'
    assert locked[1] == git(root, 'rev-parse', 'HEAD').strip()
    # A change committed in the same repository is attributed to its commit.
    (root / 'z.sql').write_text('create table main.z as select v - 2 as w from raw.t')
    sha = commit(root, 'Subtract two', 'Ana')
    [change] = check(build(root), root).changes
    assert [(a.author, a.short, a.repository) for a in change.changed_by] == [('Ana', sha, '.')]
    assert change.keep == f'git revert {sha} (by Ana)'
    assert check_consumers(graph, root) == []


# What the review of the first version found


def test_a_dbt_model_read_by_plain_sql_in_another_repository(tmp_path: Path) -> None:
    """The reader's table and the producer's model are one relation: the pin composes through
    to the producer's sources, holds after lock and lock --update, and breaks when it changes."""
    finance, marketing = tmp_path / 'finance', tmp_path / 'marketing'
    ltv = "select id, sum(amount) as total from {{ ref('stg') }} group by id"
    _finance(finance, ltv)
    marketing.mkdir()
    (marketing / 'report.sql').write_text(
        'create table warehouse.marketing.report as '
        'select id, total * 2 as doubled from warehouse.analytics.ltv'
    )
    (marketing / 'konstruct.toml').write_text('dialect = "postgres"\nrepositories = ["../finance"]\n')
    graph = build(marketing)
    lock(graph, marketing)
    pin = Lockfile.read(marketing / FILE_NAME).pin('warehouse.analytics.ltv.total')
    assert pin is not None
    assert (pin.formula, pin.repository, pin.used_by) == (
        'SUM(warehouse.raw.orders.amount)',
        'finance',
        ['report.doubled'],
    )
    assert check(graph, marketing, history=False).passed
    lock(graph, marketing, update=True)
    assert check(graph, marketing, history=False).passed
    _finance(finance, ltv.replace('sum(amount)', 'sum(amount * 2)'))
    [change] = check(build(marketing), marketing, history=False).changes
    assert (change.column, change.reasons) == ('warehouse.analytics.ltv.total', ['formula'])


def test_pinning_one_more_column_changes_no_other_pin(tmp_path: Path) -> None:
    """A long chain: the last column's formula is cut short at the depth limit, which must not
    leak into the pin of a column half-way down."""
    for index in range(40):
        source = 'raw.t' if index == 0 else f'main.z{index - 1:02}'
        (tmp_path / f'z{index:02}.sql').write_text(
            f'create table main.z{index:02} as select v + 1 as v from {source}'
        )
    (tmp_path / 'final.sql').write_text('create table main.a_final as select v from main.z39')
    (tmp_path / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    graph = build(tmp_path)
    alone = lock(graph, tmp_path, ['z20.v'], write=False).lockfile.pins[0]
    lock(graph, tmp_path, ['z20.v'])
    both = lock(graph, tmp_path, ['a_final.v', 'z20.v'])
    assert both.stale == [] and both.lockfile.pin('main.z20.v') == alone


def test_a_filter_on_an_unqualified_column_is_composed(tmp_path: Path) -> None:
    (tmp_path / 'stg.sql').write_text(
        'create table main.stg as select customer_id, amount, status from raw.orders'
    )
    (tmp_path / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) as total from main.stg '
        "where status <> 'void' group by customer_id"
    )
    (tmp_path / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    pin = lock(build(tmp_path), tmp_path, ['ltv.total']).lockfile.pins[0]
    assert pin.conditions == ['GROUP BY: raw.orders.customer_id', "WHERE: raw.orders.status <> 'void'"]
    (tmp_path / 'stg.sql').write_text(
        'create table main.stg as select customer_id, amount, lower(state) as status from raw.orders'
    )
    [change] = check(build(tmp_path), tmp_path, history=False).changes
    assert change.conditions_added == ["WHERE: LOWER(orders.state) <> 'void'"]


def test_a_filter_moved_into_a_model_upstream_is_not_a_change(tmp_path: Path) -> None:
    (tmp_path / 'ltv.sql').write_text(
        'create table main.ltv as select o.customer_id, sum(o.amount) as total from raw.orders o '
        "where o.status <> 'void' group by o.customer_id"
    )
    (tmp_path / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    lock(build(tmp_path), tmp_path, ['ltv.total'])
    (tmp_path / 'stg.sql').write_text(
        "create table main.stg as select customer_id, amount from raw.orders where status <> 'void'"
    )
    (tmp_path / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) as total from main.stg group by customer_id'
    )
    result = check(build(tmp_path), tmp_path, history=False)
    assert result.passed, str(result)


def test_a_filter_on_a_joined_relation_is_part_of_the_pin(tmp_path: Path) -> None:
    (tmp_path / 'cus.sql').write_text('create table main.cus as select id from raw.customers')
    (tmp_path / 'ltv.sql').write_text(
        'create table main.ltv as select o.customer_id, sum(o.amount) as total from raw.orders o '
        'join main.cus c on c.id = o.customer_id group by o.customer_id'
    )
    (tmp_path / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    lock(build(tmp_path), tmp_path, ['ltv.total'])
    (tmp_path / 'cus.sql').write_text('create table main.cus as select id from raw.customers where active')
    [change] = check(build(tmp_path), tmp_path, history=False).changes
    assert change.conditions_added == ['WHERE: customers.active']


DATA = """
create schema raw;
create table raw.orders as select * from (values
    (1, 1, 10, 'paid', 5), (2, 2, 20, 'paid', -1), (3, 1, 30, 'void', 3), (4, 1, 40, 'paid', 0),
    (5, 1, 10, 'paid', 1)
) t(id, customer_id, amount, status, v);
create table raw.customers as select * from (values (1, true, 'a'), (2, false, 'b')) t(id, active, name);
create table raw.payments as select * from (values (1, 1, 5), (2, 1, -2), (3, 9, 7), (4, 1, 10))
    t(id, cid, amount);
"""


def _results(files: dict[str, str], relation: str) -> list[tuple[Any, ...]]:
    """What ``relation`` holds once the files run in DuckDB over :data:`DATA`: the oracle for
    whether two versions of a project mean the same."""
    connection = duckdb.connect()
    connection.execute(DATA)
    pending = list(files.values())
    while pending:
        failed = []
        for sql in pending:
            try:
                connection.execute(sql)
            except duckdb.Error:
                failed.append(sql)
        assert len(failed) < len(pending), failed[0]
        pending = failed
    return sorted(connection.execute(f'select * from {relation}').fetchall(), key=repr)


def _changes(
    tmp_path: Path, before: dict[str, str], after: dict[str, str], column: str
) -> tuple[LockCheck, bool]:
    """Lock ``column`` with the files ``before``, then check it with ``after`` written over
    them; and whether DuckDB finds the column's relation the same in both."""
    lock(build_with(tmp_path, before), tmp_path, [column])
    for name, sql in after.items():
        (tmp_path / name).write_text(sql)
    relation = 'main.' + column.split('.')[0]
    same = _results(before, relation) == _results({**before, **after}, relation)
    return check(build(tmp_path), tmp_path, history=False), same


def test_a_filter_moved_from_a_left_joined_model_to_the_outer_where_breaks_the_pin(tmp_path: Path) -> None:
    """Filtering the joined customers keeps every order; filtering the joined rows drops the
    orders of customers that are not active."""
    ltv = (
        'create table main.ltv as select o.customer_id, sum(o.amount) as total from raw.orders o '
        'left join main.cus c on c.id = o.customer_id {where}group by o.customer_id'
    )
    before = {
        'cus.sql': 'create table main.cus as select id, name from raw.customers where active',
        'ltv.sql': ltv.format(where=''),
    }
    after = {
        'cus.sql': 'create table main.cus as select id, name, active from raw.customers',
        'ltv.sql': ltv.format(where='where c.active '),
    }
    pin = lock(build_with(tmp_path / 'pin', before), tmp_path / 'pin', ['ltv.total']).lockfile.pins[0]
    assert pin.conditions == [
        'GROUP BY: raw.orders.customer_id',
        'LEFT JOIN ON: raw.customers.id = raw.orders.customer_id',
        'LEFT JOIN WHERE: raw.customers.active',
        'LEFT JOIN: main.cus',
    ]
    assert [r.reference for r in pin.rows] == ['LEFT JOIN main.cus', 'main.ltv']
    result, same = _changes(tmp_path / 'check', before, after, 'ltv.total')
    assert not same
    [change] = result.changes
    assert change.reasons == ['conditions']
    assert (change.conditions_added, change.conditions_removed) == (
        ['WHERE: customers.active'],
        ['LEFT JOIN WHERE: customers.active'],
    )


def build_with(root: Path, files: dict[str, str]) -> Graph:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    for name, sql in files.items():
        (root / name).write_text(sql)
    return build(root)


@pytest.mark.parametrize(
    'joined',
    [
        'left join raw.customers c on c.id = o.customer_id and c.active',
        'left join (select k.id from raw.customers k where k.active) c on c.id = o.customer_id',
    ],
)
def test_a_filter_on_the_joined_side_of_a_left_join_is_part_of_its_on(tmp_path: Path, joined: str) -> None:
    """Whether the left-joined customers are filtered in a CTE, a subquery or the join's ON, the
    orders and the customers that join them are the same."""
    ltv = (
        'create table main.ltv as {cte}select o.customer_id, count(c.id) as matched from raw.orders o '
        '{joined} group by o.customer_id'
    )
    cte = 'with c as (select k.id from raw.customers k where k.active) '
    before = {'ltv.sql': ltv.format(cte=cte, joined='left join c on c.id = o.customer_id')}
    result, same = _changes(tmp_path, before, {'ltv.sql': ltv.format(cte='', joined=joined)}, 'ltv.matched')
    assert same and result.passed, str(result)


def test_an_inner_join_on_the_joined_side_of_a_left_join_is_not_a_left_join(tmp_path: Path) -> None:
    """Customers joined to their payments, then left joined, are not customers left joined and
    then their payments: a customer with no payment matches in the second only."""
    ltv = 'create table main.ltv as select o.id, count({key}) as matched from raw.orders o {joins} group by o.id'
    nested = 'left join (select c.id from raw.customers c join raw.payments p on p.cid = c.id) x on x.id = o.customer_id'
    chained = 'left join raw.customers c on c.id = o.customer_id left join raw.payments p on p.cid = c.id'
    result, same = _changes(
        tmp_path,
        {'ltv.sql': ltv.format(key='x.id', joins=nested)},
        {'ltv.sql': ltv.format(key='c.id', joins=chained)},
        'ltv.matched',
    )
    assert not same
    [change] = result.changes
    assert change.conditions_added == ['LEFT JOIN ON: customers.id = payments.cid']
    assert change.conditions_removed == ['JOIN ON: customers.id = payments.cid']


def test_a_filter_under_a_full_join_is_not_part_of_its_on(tmp_path: Path) -> None:
    """In the ON of a FULL JOIN, a filter on one side keeps that side's rows it removes, unmatched."""
    ltv = 'create table main.ltv as select o.id, p.amount from raw.orders o full join {p} on p.cid = o.id'
    result, same = _changes(
        tmp_path,
        {'ltv.sql': ltv.format(p='(select q.cid, q.amount from raw.payments q where q.amount > 0) p')},
        {'ltv.sql': ltv.format(p='raw.payments p') + ' and p.amount > 0'},
        'ltv.amount',
    )
    assert not same
    [change] = result.changes
    assert change.conditions_removed == [
        'FULL JOIN ON: orders.id = payments.cid',
        'FULL JOIN WHERE: payments.amount > 0',
    ]


def test_a_condition_moved_to_another_left_joins_on_breaks_the_pin(tmp_path: Path) -> None:
    """``a.flag`` in the ON of the join of b decides which rows of b join; in the ON of the join
    of c, which rows of c."""
    ltv = (
        'create table main.ltv as select o.id, c.name from raw.orders o '
        'left join raw.customers c on c.id = o.customer_id{first} '
        'left join raw.payments p on p.cid = o.customer_id{second}'
    )
    flag = " and o.status = 'paid'"
    result, same = _changes(
        tmp_path,
        {'ltv.sql': ltv.format(first=flag, second='')},
        {'ltv.sql': ltv.format(first='', second=flag)},
        'ltv.name',
    )
    assert not same and not result.passed


WINDOWED = {
    'inside': 'create table main.r as with x as (select id, v, amount from raw.orders where v > 0) '
    'select id, {window} as w from x',
    'same scope': 'create table main.r as select id, {window} as w from raw.orders where v > 0',
    'outside': 'create table main.r as select id, w from (select id, v, {window} as w from raw.orders) x '
    'where v > 0',
}


@pytest.mark.parametrize('window', ['row_number() over (order by v)', 'sum(amount) over (order by id)'])
def test_a_filter_moved_across_a_window_function_breaks_the_pin(tmp_path: Path, window: str) -> None:
    """A filter before a window function changes the rows it numbers or sums; after it, only the
    rows shown. Within the rows the window sees, where the filter is written does not matter."""
    inside, scope, outside = (WINDOWED[k].format(window=window) for k in ('inside', 'same scope', 'outside'))
    result, same = _changes(tmp_path / 'across', {'r.sql': inside}, {'r.sql': outside}, 'r.w')
    assert not same
    [change] = result.changes
    assert change.conditions_added == ['WHERE: orders.v > 0']
    assert change.conditions_removed == ['WHERE BEFORE WINDOW: orders.v > 0']
    result, same = _changes(tmp_path / 'within', {'r.sql': inside}, {'r.sql': scope}, 'r.w')
    assert same and result.passed, str(result)


def test_a_filter_moved_from_a_model_to_after_a_window_downstream_breaks_the_pin(tmp_path: Path) -> None:
    window = 'row_number() over (order by v)'
    numbered = f'create table main.r as select id, {window} as w from main.stg'
    later = (
        f'create table main.r as select id, w from (select id, v, {window} as w from main.stg) x where v > 0'
    )
    result, same = _changes(
        tmp_path,
        {'stg.sql': 'create table main.stg as select id, v from raw.orders where v > 0', 'r.sql': numbered},
        {'stg.sql': 'create table main.stg as select id, v from raw.orders', 'r.sql': later},
        'r.w',
    )
    assert not same
    [change] = result.changes
    assert change.conditions_removed == ['WHERE BEFORE WINDOW: orders.v > 0']
    assert change.conditions_added == ['WHERE: orders.v > 0']


@pytest.mark.parametrize(
    ('before', 'after'),
    [
        ('select customer_id, amount from raw.orders', 'select distinct customer_id, amount from raw.orders'),
        (
            'select customer_id, amount from raw.orders',
            'select customer_id, amount from raw.orders order by amount limit 2',
        ),
        (
            'select customer_id, amount from raw.orders union all select cid, amount from raw.payments',
            'select customer_id, amount from raw.orders union select cid, amount from raw.payments',
        ),
        (
            'select customer_id, sum(amount) as amount from raw.orders group by all',
            'select customer_id, status, sum(amount) as amount from raw.orders group by all',
        ),
    ],
    ids=['distinct', 'limit', 'union', 'group by all'],
)
def test_what_decides_which_rows_there_are_is_part_of_the_pin(
    tmp_path: Path, before: str, after: str
) -> None:
    result, same = _changes(
        tmp_path,
        {'ltv.sql': f'create table main.ltv as {before}'},
        {'ltv.sql': f'create table main.ltv as {after}'},
        'ltv.amount',
    )
    assert not same
    [change] = result.changes
    assert 'conditions' in change.reasons


@pytest.mark.parametrize(
    ('report', 'pinned'),
    [
        ('select *, total * 2 as t2 from main.ltv', ['main.ltv.customer_id', 'main.ltv.total']),
        ('select * exclude (customer_id) from main.ltv', ['main.ltv.total']),
    ],
)
def test_a_star_beside_other_items_or_leaving_columns_out(
    repositories: tuple[Path, Path], report: str, pinned: list[str]
) -> None:
    _, marketing = repositories
    (marketing / 'report.sql').write_text(f'create table main.report as {report}')
    assert lock(build(marketing), marketing).added == pinned


def test_a_column_with_no_formula_is_pinned_by_the_file_that_builds_it(tmp_path: Path) -> None:
    """A Python model's columns have no expression: the pin is incomplete, and an edit to the
    model's file breaks it."""
    finance, marketing = tmp_path / 'finance', tmp_path / 'marketing'
    model = _dbt_model('shop', 'scores', '', ['source.shop.raw.orders'], 'analytics')
    code = 'def model(dbt, session):\n    return dbt.source("raw", "orders").groupby("id").sum()\n'
    model.update(
        language='python',
        raw_code=code,
        original_file_path='models/scores.py',
        columns={'id': {'name': 'id'}, 'score': {'name': 'score'}},
    )
    _dbt_repository(finance, 'shop', [model], [_dbt_source('shop', 'raw', 'orders', 'raw', ['id'])])
    marketing.mkdir()
    (marketing / 'report.sql').write_text(
        'create table warehouse.marketing.report as select score from warehouse.analytics.scores'
    )
    (marketing / 'konstruct.toml').write_text('dialect = "postgres"\nrepositories = ["../finance"]\n')
    pin = lock(build(marketing), marketing, ['warehouse.analytics.scores.score']).lockfile.pins[0]
    digest = hashlib.sha256(code.encode()).hexdigest()
    assert (pin.complete, pin.files) == (False, [f'warehouse.analytics.scores sha256:{digest}'])
    model['raw_code'] = code.replace('sum()', 'mean()')
    _dbt_repository(finance, 'shop', [model], [_dbt_source('shop', 'raw', 'orders', 'raw', ['id'])])
    [change] = check(build(marketing), marketing, history=False).changes
    assert change.reasons == ['files']
    assert '  the file that builds warehouse.analytics.scores changed' in str(change.lines())


@pytest.mark.parametrize('large', [False, True])
def test_an_edit_to_a_seed_breaks_the_pins_that_read_it(tmp_path: Path, large: bool) -> None:
    """A seed's rows are data in the producer's repository: its file's checksum is part of every
    pin that reads it, joins on it or filters by it, though the formula stays complete. dbt
    records a large seed's checksum as its path, so then the file is hashed."""
    finance, marketing = tmp_path / 'finance', tmp_path / 'marketing'
    sales = _dbt_model(
        'shop',
        'sales',
        "select o.id, r.region_name, o.amount from {{ source('raw', 'orders') }} o "
        "join {{ ref('regions') }} r on r.code = o.country",
        ['source.shop.raw.orders', 'seed.shop.regions'],
        'analytics',
    )

    def producer(csv: str) -> None:
        seed = _dbt_model('shop', 'regions', '', [], 'analytics')
        digest = hashlib.sha256(csv.encode()).hexdigest()
        seed.update(
            unique_id='seed.shop.regions',
            resource_type='seed',
            original_file_path='seeds/regions.csv',
            checksum={'name': 'path', 'checksum': 'seeds/regions.csv'}
            if large
            else {'name': 'sha256', 'checksum': digest},
            config={'materialized': 'seed'},
        )
        (finance / 'seeds').mkdir(parents=True, exist_ok=True)
        _dbt_repository(
            finance,
            'shop',
            [sales],
            [_dbt_source('shop', 'raw', 'orders', 'raw', ['id', 'amount', 'country'])],
        )
        manifest = json.loads((finance / 'target' / 'manifest.json').read_text())
        manifest['nodes'][seed['unique_id']] = seed
        (finance / 'target' / 'manifest.json').write_text(json.dumps(manifest))
        (finance / 'seeds' / 'regions.csv').write_text(csv)

    producer('code,region_name\nFR,EU\nUS,NA\n')
    marketing.mkdir()
    (marketing / 'report.sql').write_text(
        'create table warehouse.marketing.report as select region_name, amount from warehouse.analytics.sales'
    )
    (marketing / 'konstruct.toml').write_text('dialect = "postgres"\nrepositories = ["../finance"]\n')
    pins = lock(build(marketing), marketing).lockfile.pins
    assert [(p.column, p.complete) for p in pins] == [
        ('warehouse.analytics.sales.amount', True),
        ('warehouse.analytics.sales.region_name', True),
    ]
    digest = hashlib.sha256(b'code,region_name\nFR,EU\nUS,NA\n').hexdigest()
    assert all(p.files == [f'warehouse.analytics.regions sha256:{digest}'] for p in pins)
    assert check(build(marketing), marketing, history=False).passed
    producer('code,region_name\nFR,EMEA\nUS,AMER\n')
    result = check(build(marketing), marketing, history=False)
    assert [(c.column, c.reasons, c.files) for c in result.changes] == [
        ('warehouse.analytics.sales.amount', ['files'], ['warehouse.analytics.regions']),
        ('warehouse.analytics.sales.region_name', ['files'], ['warehouse.analytics.regions']),
    ]


def test_a_column_the_producer_renamed_is_removed(repositories: tuple[Path, Path]) -> None:
    finance, marketing = repositories
    lock(build(marketing), marketing)
    (finance / 'ltv.sql').write_text(LTV.replace('sum(amount) as total', 'sum(amount) as lifetime_total'))
    commit(finance, 'Rename total', 'Ana')
    result = check(build(marketing), marketing)
    [change] = result.changes
    assert change.reasons == ['removed'] and change.affected == ['report.doubled']
    assert '  removed: no longer built by finance' in str(result)
    assert change.changed_by[0].author == 'Ana'
    assert lock(build(marketing), marketing).missing == ['main.ltv.total']


def test_a_producer_checks_only_the_pins_it_builds(tmp_path: Path) -> None:
    """dash reads finance and marketing; finance also lists raw, which dash does not. Run in
    finance, the check judges the pins on finance's columns only, composed as far as dash saw
    them, and notes the rest."""
    raw, finance, marketing, dash = (tmp_path / n for n in ('raw', 'finance', 'marketing', 'dash'))
    for root in (raw, finance, marketing, dash):
        root.mkdir()
        (root / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    (raw / 'orders.sql').write_text(
        'create table main.orders as select customer_id, amount / 100 as amount from landing.orders'
    )
    (finance / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) as total from main.orders group by 1'
    )
    (finance / 'konstruct.toml').write_text('dialect = "duckdb"\nrepositories = ["../raw", "../dash"]\n')
    (marketing / 'clicks.sql').write_text('create table main.clicks as select id, n from web.events')
    (dash / 'dash.sql').write_text(
        'create table main.dash as select l.total, c.n from main.ltv l join main.clicks c on c.id = l.customer_id'
    )
    (dash / 'konstruct.toml').write_text(
        'dialect = "duckdb"\nrepositories = ["../finance", "../marketing"]\n'
    )
    lock(build(dash), dash)
    [result] = check_consumers(build(finance), finance, history=False)
    assert result.passed, str(result)
    assert result.notes == [
        '2 pinned columns are not built in this build, so they were not checked: '
        'main.clicks.id, main.clicks.n'
    ]
    (finance / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) * 2 as total from main.orders group by 1'
    )
    [broken] = check_consumers(build(finance), finance, history=False)
    assert [(c.column, c.reasons) for c in broken.changes] == [('main.ltv.total', ['formula'])]
    assert broken.changes[0].formula_after == 'SUM(orders.amount) * 2'


def test_a_consumer_reading_every_column(repositories: tuple[Path, Path]) -> None:
    _, marketing = repositories
    (marketing / 'report.sql').write_text('create table main.report as select * from main.ltv')
    result = lock(build(marketing), marketing)
    assert result.added == ['main.ltv.customer_id', 'main.ltv.total']
    assert result.lockfile.pins[1].used_by == ['report (reads every column)']


@pytest.mark.parametrize(
    'sql',
    [
        'create table main.report as select * from main.ltv where customer_id > 0',
        'create table main.report as select l.* from main.ltv l join raw.other o on o.id = l.customer_id',
        'create table main.report as select l.*, o.x from main.ltv l join raw.other o on o.id = l.customer_id',
    ],
)
def test_a_consumer_reading_every_column_and_filtering_or_joining_on_one(
    repositories: tuple[Path, Path], sql: str
) -> None:
    """A key the reader filters or joins on says nothing about which columns its star reads."""
    _, marketing = repositories
    (marketing / 'report.sql').write_text(sql)
    result = lock(build(marketing), marketing)
    assert {'main.ltv.customer_id', 'main.ltv.total'} <= set(result.added)
    pin = result.lockfile.pin('main.ltv.total')
    assert pin is not None and pin.used_by == ['report (reads every column)']


def test_lock_and_check_agree_on_what_changed(repositories: tuple[Path, Path]) -> None:
    """A re-approval and an edit to a term that was not approved only note: locking again keeps
    nothing stale, and brings what they changed up to date."""
    finance, marketing = repositories
    (finance / 'definitions.yml').write_text(
        TERM.format(definition='Paid.', status='approved', approved='2026-10-30')
        + '  - {name: Customer, definition: Who paid., status: draft, columns: [main.ltv.customer_id]}\n'
    )
    lock(build(marketing), marketing)
    (finance / 'definitions.yml').write_text(
        TERM.format(definition='Paid.', status='approved', approved='2026-11-05')
        + '  - {name: Customer, definition: Who bought., status: draft, columns: [main.ltv.customer_id]}\n'
    )
    graph = build(marketing)
    before = check(graph, marketing, history=False)
    assert before.passed and len(before.notes) == 2
    relocked = lock(graph, marketing)
    assert relocked.stale == [] and relocked.updated == []
    pin = relocked.lockfile.pin('main.ltv.total')
    assert pin is not None and pin.terms[0].approved == '2026-11-05'
    after = check(graph, marketing, history=False)
    assert after.passed and after.notes == []


def test_a_new_canonical_form_fails_once(repositories: tuple[Path, Path]) -> None:
    finance, marketing = repositories
    graph = build(marketing)
    lock(graph, marketing)
    text = (marketing / FILE_NAME).read_text()
    assert '\nformat = 1\n' in text and f'\ncanonical = "{CANONICAL}"\n' in text
    (marketing / FILE_NAME).write_text(text.replace(CANONICAL, 'konstruct-0/sqlglot-1.0'))
    same = check(graph, marketing, history=False)
    assert same.passed and 'konstruct lock' in same.notes[0]
    (finance / 'ltv.sql').write_text(LTV.replace('sum(amount)', 'sum(amount * 2)'))
    graph = build(marketing)
    result = check(graph, marketing, history=False)
    assert not result.passed and result.changes == []
    assert result.summary() == (
        'konstruct.lock: written with canonical form konstruct-0/sqlglot-1.0, which this Konstruct '
        f'does not write ({CANONICAL}); re-lock after review.'
    )
    with pytest.raises(LockError, match='re-lock after review'):
        lock(graph, marketing)
    assert lock(graph, marketing, update=True).updated == ['main.ltv.total']
    assert check(graph, marketing, history=False).passed


def test_a_new_canonical_form_folds_only_what_it_prints(repositories: tuple[Path, Path]) -> None:
    """Only a formula or a condition can read differently in another canonical form: a removed
    column, a new type and a changed definition fail as they would in the same form."""
    finance, marketing = repositories
    graph = build(marketing)
    graph.nodes['table.main.ltv#customer_id'].data_type = 'integer'
    lock(graph, marketing)
    text = (marketing / FILE_NAME).read_text()
    (marketing / FILE_NAME).write_text(text.replace(CANONICAL, 'konstruct-0/sqlglot-1.0'))
    (finance / 'ltv.sql').write_text(LTV.replace('sum(amount) as total', 'sum(amount) as lifetime_total'))
    renamed = check(build(marketing), marketing, history=False)
    assert [(c.column, c.reasons) for c in renamed.changes] == [('main.ltv.total', ['removed'])]
    assert renamed.relock == 0 and not renamed.passed
    # Nor does locking again refuse: a removed column reads the same in either form.
    relocked = lock(build(marketing), marketing)
    assert relocked.missing == ['main.ltv.total'] and relocked.lockfile.canonical == CANONICAL
    (marketing / FILE_NAME).write_text(text.replace(CANONICAL, 'konstruct-0/sqlglot-1.0'))
    (finance / 'ltv.sql').write_text(LTV.replace('sum(amount)', 'sum(amount * 2)'))
    (finance / 'definitions.yml').write_text(
        TERM.format(definition='Paid, net of VAT.', status='approved', approved='2026-10-30')
    )
    graph = build(marketing)
    graph.nodes['table.main.ltv#customer_id'].data_type = 'bigint'
    both = check(graph, marketing, history=False)
    assert [(c.column, c.reasons) for c in both.changes] == [
        ('main.ltv.customer_id', ['type']),
        ('main.ltv.total', ['definition']),
    ]
    assert both.changes[1].formula_after is None
    assert both.relock == 1
    assert str(both).startswith(
        'konstruct.lock: written with canonical form konstruct-0/sqlglot-1.0, which this Konstruct '
        f'does not write ({CANONICAL}); re-lock after review. 2 of 2 pinned upstream columns changed '
        'since they were locked.'
    )


def test_a_pin_compares_its_formula_as_well_as_its_fingerprint(repositories: tuple[Path, Path]) -> None:
    _, marketing = repositories
    graph = build(marketing)
    lock(graph, marketing)
    text = (marketing / FILE_NAME).read_text()
    (marketing / FILE_NAME).write_text(
        text.replace('"SUM(raw.orders.amount)"', '"SUM(raw.orders.amount) * 2"')
    )
    [change] = check(graph, marketing, history=False).changes
    assert change.reasons == ['formula']


def test_names_that_collide_are_shown_in_full(tmp_path: Path) -> None:
    (tmp_path / 'other.sql').write_text('create table main.other as select amount from raw.orders')
    (tmp_path / 'ltv.sql').write_text('create table main.ltv as select sum(amount) as total from raw.orders')
    (tmp_path / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    lock(build(tmp_path), tmp_path, ['ltv.total'])
    (tmp_path / 'ltv.sql').write_text('create table main.ltv as select sum(amount) as total from main.orders')
    result = check(build(tmp_path), tmp_path, history=False)
    [change] = result.changes
    assert (change.formula_before, change.formula_after) == (
        'SUM(raw.orders.amount)',
        'SUM(main.orders.amount)',
    )
    assert result.summary() == 'konstruct.lock: 1 of 1 pinned upstream column changed since it was locked.'
    kept = lock(build(tmp_path), tmp_path)
    assert '1 pinned column differs from this build and was kept as locked' in str(kept)


def test_malformed_pins_are_refused() -> None:
    pin = f'format = 1\ncanonical = "{CANONICAL}"\n[[pin]]\ncolumn = "a.b"\nid = "x"\n'
    with pytest.raises(LockError, match="a pin is malformed: 'fingerprint' is missing"):
        Lockfile.loads(pin)
    good = pin + f'fingerprint = "sha256:{"0" * 64}"\n'
    assert Lockfile.loads(good).pins[0].complete
    with pytest.raises(LockError, match="'complete' must be true or false"):
        Lockfile.loads(good + 'complete = "false"\n')
    with pytest.raises(LockError, match="'fingerprint' must be sha256: and 64 hex digits"):
        Lockfile.loads(pin + 'fingerprint = "sha256:0"\n')
    with pytest.raises(LockError, match="'sources' must be a list of strings"):
        Lockfile.loads(good + 'sources = "raw.t.a"\n')
    with pytest.raises(LockError, match='unsupported format'):
        Lockfile.loads('format = "1"\n')


def test_an_uncommitted_change_to_a_file_with_an_unusual_name(repositories: tuple[Path, Path]) -> None:
    finance, marketing = repositories
    (finance / 'ltv.sql').rename(finance / 'lïfe time.sql')
    commit(finance, 'Rename the file', 'Ada')
    lock(build(marketing), marketing)
    (finance / 'lïfe time.sql').write_text(LTV.replace("status <> 'returned'", "status <> 'void'"))
    change = check(build(marketing), marketing).changes[-1]
    assert str(change.changed_by[0]) == 'uncommitted changes to ../finance/lïfe time.sql'


def test_without_history_no_commit_is_named(repositories: tuple[Path, Path]) -> None:
    finance, marketing = repositories
    lock(build(marketing), marketing)
    (finance / 'ltv.sql').write_text(LTV.replace('sum(amount)', 'sum(amount * 1.1)'))
    commit(finance, 'A private subject', 'Ana Lima')
    graph = build(marketing)
    assert 'A private subject' in check(graph, marketing).markdown()
    private = check(graph, marketing, history=False).markdown()
    assert 'A private subject' not in private and 'Ana Lima' not in private


def test_a_negative_constant_is_a_constant() -> None:
    """``-1 = a`` is ``a = -1``, as ``1 = a`` is ``a = 1``."""
    import sqlglot

    from konstruct.analysis.lock import _canonical

    assert _canonical(sqlglot.parse_one('-1 = a')).sql() == 'a = -1'
    assert _canonical(sqlglot.parse_one('-1 < a')).sql() == 'a > -1'


# What the fourth review found


CUS = 'create table main.cus as select id from raw.customers{where}'


@pytest.mark.parametrize(
    'read',
    [
        'where o.customer_id in (select c.id from main.cus c)',
        'where o.customer_id not in (select c.id from main.cus c)',
        'cross join main.cus c',
        ', main.cus c',
        'join main.cus c on true',
    ],
)
def test_a_relation_read_with_no_column_of_it_is_pinned(tmp_path: Path, read: str) -> None:
    """Read in a subquery, cross joined or joined on TRUE, a relation's rows decide which rows
    there are, though no column of it reaches the pinned one."""
    ltv = f'create table main.ltv as select o.id, o.amount from raw.orders o {read}'
    result, same = _changes(
        tmp_path,
        {'cus.sql': CUS.format(where=' where active'), 'ltv.sql': ltv},
        {'cus.sql': CUS.format(where='')},
        'ltv.amount',
    )
    assert not same
    [change] = result.changes
    assert change.conditions_removed == ['WHERE: customers.active']


def test_which_side_of_a_left_join_is_kept_is_part_of_the_pin(tmp_path: Path) -> None:
    """``p LEFT JOIN c`` keeps every payment; ``c LEFT JOIN p`` every customer. ``c RIGHT JOIN p``
    is ``p LEFT JOIN c``."""
    ltv = 'create table main.ltv as select p.id, p.amount from {joins} on c.id = p.cid'
    kept = ltv.format(joins='raw.payments p left join raw.customers c')
    result, same = _changes(
        tmp_path / 'other way',
        {'ltv.sql': kept},
        {'ltv.sql': ltv.format(joins='raw.customers c left join raw.payments p')},
        'ltv.amount',
    )
    assert not same
    [change] = result.changes
    assert change.conditions_added == ['LEFT JOIN: raw.payments']
    assert change.conditions_removed == ['LEFT JOIN: raw.customers']
    result, same = _changes(
        tmp_path / 'right',
        {'ltv.sql': kept},
        {'ltv.sql': ltv.format(joins='raw.customers c right join raw.payments p')},
        'ltv.amount',
    )
    assert same and result.passed, str(result)


def test_the_filters_of_a_unions_branches_stay_apart(tmp_path: Path) -> None:
    union = (
        'create table main.u as select id, amount from raw.orders where amount > {a} '
        'union all select id, amount from raw.payments where amount > {b}'
    )
    result, same = _changes(
        tmp_path, {'u.sql': union.format(a=15, b=0)}, {'u.sql': union.format(a=0, b=15)}, 'u.amount'
    )
    assert not same
    [change] = result.changes
    assert change.conditions_added == ['WHERE: orders.amount > 0', 'WHERE: payments.amount > 15']


def test_a_column_two_select_stars_pass_on_is_followed(tmp_path: Path) -> None:
    """``select * from {{ ref() }}`` twice over: the column is the one the first star reads, with
    every filter on the way."""
    stg = 'create table main.stg as select * from raw.orders{where}'
    result, same = _changes(
        tmp_path,
        {
            'stg.sql': stg.format(where=''),
            'mid.sql': 'create table main.mid as select * from main.stg',
            'rep.sql': 'create table main.rep as select amount from main.mid',
        },
        {'stg.sql': stg.format(where=" where status = 'paid'")},
        'mid.amount',
    )
    assert not same
    [change] = result.changes
    assert change.conditions_added == ["WHERE: orders.status = 'paid'"]


def test_an_incremental_models_key_and_strategy_are_pinned(tmp_path: Path) -> None:
    """A run of an incremental model keeps or replaces earlier rows by its key and strategy."""
    model = _dbt_model(
        'shop',
        'ltv',
        "select id, amount from {{ source('raw', 'orders') }}",
        ['source.shop.raw.orders'],
        'analytics',
    )
    source = [_dbt_source('shop', 'raw', 'orders', 'raw', ['id', 'amount'])]

    def write(strategy: str) -> None:
        model['config'] = {
            'materialized': 'incremental',
            'unique_key': 'id',
            'incremental_strategy': strategy,
        }
        _dbt_repository(tmp_path, 'shop', [model], source)

    write('merge')
    (tmp_path / 'konstruct.toml').write_text('dialect = "postgres"\n')
    pin = lock(build(tmp_path), tmp_path, ['ltv.amount']).lockfile.pins[0]
    assert {'MATERIALIZED: incremental', 'UNIQUE KEY: id', 'INCREMENTAL STRATEGY: merge'} <= set(
        pin.conditions
    )
    write('append')
    [change] = check(build(tmp_path), tmp_path, history=False).changes
    assert change.conditions_added == ['INCREMENTAL STRATEGY: append']


@pytest.mark.parametrize(
    ('ltv', 'changed'),
    [
        ('select o.id, o.amount from raw.orders o cross join (select * from range(2) t(d)) r', ('2', '3')),
        (
            'select o.id, o.amount, g.generate_series as n from raw.orders o '
            'left join generate_series(1, 2) g on g.generate_series = o.id',
            ('(1, 2)', '(2, 3)'),
        ),
        (
            'select o.id, o.amount * v.w as amount from raw.orders o '
            "join (values ('paid', 1), ('void', 0)) v(status, w) on v.status = o.status",
            ("'paid', 1", "'paid', 2"),
        ),
    ],
    ids=['range', 'generate_series', 'values'],
)
def test_rows_from_a_table_function_or_values_are_pinned(
    tmp_path: Path, ltv: str, changed: tuple[str, str]
) -> None:
    """Rows a model makes itself, which no source column describes, are pinned by the SQL that
    makes them."""
    sql = f'create table main.ltv as {ltv}'
    result, same = _changes(tmp_path, {'ltv.sql': sql}, {'ltv.sql': sql.replace(*changed)}, 'ltv.amount')
    assert not same
    [change] = result.changes
    assert change.reasons == ['conditions'] and change.conditions_added[0].startswith('ROWS FROM: ')


def test_a_measure_formula_is_part_of_a_terms_meaning() -> None:
    """An Ossie metric is defined by its formula, so a new formula is a new version of the term;
    a term with none keeps the version it had before formulas counted."""
    from konstruct.analysis.lock import pinned_term, term_changes

    measure = Node('term.revenue', 'term', 'Revenue', attrs={'kind': 'measure', 'formula': 'SUM(amount)'})
    measure.attrs['status'] = 'approved'
    before = pinned_term(measure)
    assert before.formula == 'SUM(amount)'
    measure.attrs['formula'] = 'SUM(amount) - SUM(refunded)'
    after = pinned_term(measure)
    assert before.version != after.version
    assert term_changes([before], [after], 'x', []) == [
        "'Revenue' formula: SUM(amount) -> SUM(amount) - SUM(refunded)"
    ]
    assert PinnedTerm.from_dict(after.to_dict()) == after
    # A formula is what a measure computes, so a draft measure's new formula is a change too; its
    # other changes are notes.
    measure.attrs.update(status='draft')
    draft = pinned_term(measure)
    measure.attrs.update(formula='AVG(amount)', grain='day')
    notes: list[str] = []
    assert term_changes([draft], [pinned_term(measure)], 'x', notes) == [
        "'Revenue' formula: SUM(amount) - SUM(refunded) -> AVG(amount)"
    ]
    assert notes == ["x: 'Revenue' grain: none -> day (not approved when locked)"]
    plain = Node('term.ltv', 'term', 'LTV', description='Paid.', attrs={'status': 'approved'})
    meaning = dict.fromkeys(('grain', 'columns', 'filters', 'unit', 'currency', 'exceptions'))
    payload = json.dumps(meaning | {'definition': 'Paid.'}, sort_keys=True).encode()
    assert pinned_term(plain).version == hashlib.sha256(payload).hexdigest()[:12]
    assert 'formula' not in pinned_term(plain).to_dict()
