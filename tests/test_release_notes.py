"""Release notes and the definition effect: what changed in each formula, filter and term over a
range of commits, who approved it, and what the change does on today's data."""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from konstruct import Project, QueryError
from konstruct.analysis.effect import Effect, compare
from konstruct.analysis.release_notes import Change
from konstruct.interfaces.cli import main
from tests.test_history import commit, git

TERM = """terms:
  - name: Lifetime value
    definition: What a customer has paid.
    status: approved
    approved: {approved}
    owner: {{name: Grace}}
    columns: [main.ltv.total]
"""

V1 = (
    'create table main.ltv as select customer_id, sum(amount) as total from raw.orders group by customer_id\n'
)
V2 = (
    'create table main.ltv as select customer_id, sum(amount) as total from raw.orders\n'
    "where status <> 'void' group by customer_id\n"
)
V3 = (
    'create table main.ltv as select customer_id, sum(amount * 2) as total from raw.orders\n'
    "where status <> 'void' group by customer_id\n"
)


@pytest.fixture
def shop(tmp_path: Path) -> Project:
    return make_shop(tmp_path)


def make_shop(tmp_path: Path) -> Project:
    """A SQL project in git with a DuckDB warehouse: a filter added, then a formula changed, then the
    term bound to it approved again; a table added and removed; a reformat that changes nothing."""
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute(
        'create table raw.orders as select i as id, 1 + i % 20 as customer_id, (i * 37) % 100 + 1 as amount, '
        "case when i % 9 = 0 then 'void' else 'ok' end as status from range(1, 201) t(i)"
    )
    con.close()
    git(tmp_path, 'init', '-q', '-b', 'main')
    (tmp_path / '.gitignore').write_text('konstruct-out/\n*.duckdb\n*.wal\n')
    (tmp_path / 'ltv.sql').write_text(V1)
    (tmp_path / 'definitions.yml').write_text(TERM.format(approved='2026-01-05'))
    commit(tmp_path, 'Add lifetime value', 'Ada', '2026-01-05T10:00:00+00:00')
    git(tmp_path, 'tag', 'v1')
    (tmp_path / 'ltv.sql').write_text(V2)
    commit(tmp_path, 'Leave void orders out', 'Linus', '2026-02-01T10:00:00+00:00')
    (tmp_path / 'ltv.sql').write_text(V3)
    commit(tmp_path, 'Count each order twice', 'Linus', '2026-03-01T10:00:00+00:00')
    (tmp_path / 'definitions.yml').write_text(TERM.format(approved='2026-03-05'))
    commit(tmp_path, 'Approve lifetime value again', 'Grace', '2026-03-05T10:00:00+00:00')
    (tmp_path / 'refunds.sql').write_text('create table main.refunds as select id, amount from raw.orders\n')
    commit(tmp_path, 'Add refunds', 'Ada', '2026-04-01T10:00:00+00:00')
    (tmp_path / 'refunds.sql').unlink()
    (tmp_path / 'ltv.sql').write_text(V3.replace('select', 'SELECT\n '))
    commit(tmp_path, 'Remove refunds and reformat', 'Ada', '2026-05-01T10:00:00+00:00')
    project = Project(tmp_path)
    project.build()
    return project


def test_notes_since_a_tag(shop: Project) -> None:
    notes = shop.release_notes('v1')
    assert notes.commits == 5
    assert [(c.commit.subject, c.headline()) for c in notes.changes] == [
        ('Remove refunds and reformat', 'refunds.amount: formula removed'),
        ('Remove refunds and reformat', 'refunds.id: formula removed'),
        ('Add refunds', 'refunds.amount: formula added'),
        ('Add refunds', 'refunds.id: formula added'),
        ('Approve lifetime value again', "term 'Lifetime value': approved changed"),
        ('Count each order twice', 'ltv.total: formula changed'),
        ('Leave void orders out', 'ltv: joins, filters or grouping changed'),
    ]
    formula = notes.changes[5]
    assert (formula.before, formula.after) == ('SUM(orders.amount)', 'SUM(orders.amount * 2)')
    assert (formula.before_english, formula.after_english) == (
        'the sum of [amount]',
        'the sum of ([amount] times 2)',
    )
    assert formula.commit.author == 'Linus' and formula.node_id is not None
    rows = notes.changes[6]
    assert rows.added == ["WHERE: orders.status <> 'void'"] and rows.removed == []
    assert rows.added_english == ["rows are kept only where [status] has a value other than 'void'"]
    for change in (formula, rows):
        (approval,) = change.approvals
        assert (approval.term, approval.owner, approval.bound) == ('Lifetime value', 'Grace', True)
        assert approval.approved is not None and approval.approved.subject == 'Approve lifetime value again'
        assert approval.text().startswith(
            "'Lifetime value' (bound to it) was approved in a commit by Grace on 2026-03-05"
        )
    assert formula.affected.terms == ['Lifetime value']
    term = notes.changes[4]
    assert term.fields == {'approved': ('2026-01-05', '2026-03-05')}
    assert notes.changes[0].approvals == [] and notes.changes[0].node_id is None
    assert notes.summary() == '7 changes in 5 of 5 commits since v1'


def test_the_default_start_is_the_latest_tag(shop: Project) -> None:
    notes = shop.release_notes()
    assert notes.since == 'v1' and notes.notes == ['from the latest tag, v1; pass --since for another start']
    assert len(notes.changes) == 7


def test_notes_since_a_date_until_a_commit(shop: Project) -> None:
    notes = shop.release_notes('2026-02-15', until='2026-03-02')
    assert [c.headline() for c in notes.changes] == ['ltv.total: formula changed']
    assert notes.until == '2026-03-02'


def test_the_whole_history_and_a_limit(tmp_path: Path, shop: Project) -> None:
    git(shop.root, 'tag', '-d', 'v1')
    notes = shop.release_notes()
    assert notes.since is None and notes.commits == 6
    assert notes.changes[-1].headline() == 'ltv.total: formula added'
    assert notes.summary().endswith('in the whole history')
    short = shop.release_notes(limit=1)
    assert short.commits == 1 and short.notes == [
        'only the last 1 commits were read; raise the limit to read more'
    ]
    # Exactly as many commits as the limit: every one was read.
    assert shop.release_notes(limit=6).notes == []


def test_a_change_not_yet_approved(shop: Project) -> None:
    (shop.root / 'ltv.sql').write_text(V3.replace('amount * 2', 'amount * 3'))
    commit(shop.root, 'Count each order three times', 'Linus', '2026-06-01T10:00:00+00:00')
    change = shop.release_notes('2026-05-15').changes[0]
    (approval,) = change.approvals
    assert approval.approved is None
    assert approval.text() == "'Lifetime value' (bound to it) is approved, but not since this change"


def test_wrong_ranges(shop: Project) -> None:
    with pytest.raises(QueryError, match='is not a commit, branch, tag or date'):
        shop.release_notes('no-such-ref')
    with pytest.raises(QueryError, match='is not a commit'):
        shop.release_notes('--output=x')
    with pytest.raises(QueryError, match='at least 1'):
        shop.release_notes(limit=0)


def test_markdown_and_text(shop: Project) -> None:
    notes = shop.release_notes('v1')
    markdown = notes.markdown()
    assert markdown.startswith('## Release notes since v1\n\n7 changes in 5 of 5 commits since v1.\n')
    assert '### 2026-03-01 `' in markdown and '- **ltv.total: formula changed**' in markdown
    assert '  - it was the sum of [amount]; it is now the sum of ([amount] times 2)' in markdown
    text = str(notes)
    assert text.startswith('Release notes since v1 (HEAD)\n7 changes in 5 of 5 commits since v1.')
    assert '      was  SUM(orders.amount)\n      now  SUM(orders.amount * 2)' in text
    data = notes.to_dict()
    assert data['changes'][5]['after_english'] == 'the sum of ([amount] times 2)'
    assert data['changes'][4]['fields'] == {'approved': {'before': '2026-01-05', 'after': '2026-03-05'}}


def _oracle(shop: Project, sql: str) -> float:
    con = duckdb.connect(str(shop.root / 'shop.duckdb'), read_only=True)
    try:
        return float(con.execute(sql).fetchone()[0])  # type: ignore[index]
    finally:
        con.close()


def test_the_effect_on_todays_data(shop: Project) -> None:
    notes = shop.effect('v1')
    by = {c.headline(): c for c in notes.changes}
    doubled = by['ltv.total: formula changed'].effect
    assert doubled is not None and doubled.skipped is None
    before = _oracle(shop, "select sum(amount) from raw.orders where status <> 'void'")
    (total,) = doubled.columns
    assert (total.column, total.total_before, total.total_after) == ('total', before, 2 * before)
    assert (doubled.rows_before, doubled.rows_after, doubled.sampled) == (20, 20, False)
    assert doubled.summary() == (
        f"on today's data: 20 rows in both; the sum of total rises from {before:,.0f} to {2 * before:,.0f} "
        '(+100.0%); 20 values of total only before and 20 only after'
    )
    # A filter changes no formula, but every column of the relation: each is compared.
    filtered = by['ltv: joins, filters or grouping changed'].effect
    assert filtered is not None and filtered.skipped is None
    everything = _oracle(shop, 'select sum(amount) from raw.orders')
    assert [(c.column, c.total_before, c.total_after) for c in filtered.columns] == [
        ('customer_id', 210.0, 210.0),
        ('total', everything, before),
    ]
    assert filtered.summary() == (
        f"on today's data: 20 rows in both; the sum of total falls from {everything:,.0f} to {before:,.0f} "
        f'({(before - everything) / everything * 100:+.1f}%); 20 rows only before and 20 only after'
    )
    removed = by['refunds.amount: formula removed'].effect
    assert removed is not None and removed.skipped == 'LookupError: refunds is not in the graph now'
    assert by["term 'Lifetime value': approved changed"].effect is None
    assert notes.warehouse == 'shop.duckdb'
    assert 'Effects run read-only against shop.duckdb.' in str(notes)


def test_the_effect_of_some_models_on_a_sample(shop: Project) -> None:
    notes = shop.effect('v1', models=['ltv'], max_rows=50)
    assert {c.relation for c in notes.changes} == {'ltv', None}
    effect = next(c.effect for c in notes.changes if c.kind == 'formula')
    assert effect is not None and effect.sampled
    assert effect.summary().startswith("on what may be a sample of today's data: ")


def test_compare_by_key() -> None:
    change = Change(commit=None, path='x.sql', kind='formula', status='changed', column='t.v')  # type: ignore[arg-type]
    before = (['id', 'v'], [(1, 10), (2, 20), (3, 30)])
    after = (['id', 'v'], [(1, 10), (2, 25), (4, 40)])
    effect = compare(change, before, after, ['id'], False)
    (column,) = effect.columns
    assert (column.matched, column.changed, effect.gained, effect.lost) == (2, 1, 1, 1)
    assert (column.total_before, column.total_after) == (60.0, 75.0)
    assert effect.summary() == (
        "on today's data: 3 rows in both; the sum of v rises from 60 to 75 (+25.0%); 1 of 2 rows with the "
        'same id change v; 1 id appear and 1 disappear'
    )
    duplicated = compare(change, (['id', 'v'], [(1, 1), (1, 2)]), after, ['id'], False)
    assert duplicated.key == [] and duplicated.columns[0].matched is None
    text = compare(change, (['id', 'v'], [(1, 'a')]), (['id', 'v'], [(1, 'b')]), [], False)
    assert text.columns[0].total_before is None
    # A text column with no key: its values are compared as a whole.
    assert text.summary() == "on today's data: 1 row in both; 1 value of v only before and 1 only after"
    same_text = compare(change, (['v'], [('a',), ('b',)]), (['v'], [('b',), ('a',)]), [], False)
    assert same_text.summary() == "on today's data: 2 rows in both; the same values of v in both"
    nan = compare(
        change, (['id', 'v'], [(1, float('nan'))]), (['id', 'v'], [(1, float('nan'))]), ['id'], False
    )
    assert nan.columns[0].changed == 0
    rows = Change(commit=None, path='x.sql', kind='rows', status='changed')  # type: ignore[arg-type]
    same = compare(rows, (['id', 'v'], [(1, 1)]), (['id', 'v'], [(1, 1)]), ['id'], False)
    assert [c.column for c in same.columns] == ['v'] and same.summary().endswith('; no column changes value')
    # With no key, values that moved between rows leave every sum as it was: the rows differ.
    swapped = compare(
        rows,
        (['id', 'status', 'v'], [(1, 'ok', 10), (2, 'void', 20)]),
        (['id', 'status', 'v'], [(1, 'void', 20), (2, 'ok', 10)]),
        [],
        False,
    )
    assert (swapped.only_before, swapped.only_after) == (2, 2)
    assert swapped.summary() == "on today's data: 2 rows in both; 2 rows only before and 2 only after"
    alike = compare(rows, (['v'], [(1,), (2,)]), (['v'], [(2,), (1,)]), [], False)
    assert alike.summary() == "on today's data: 2 rows in both; the same rows in both"
    # A sum that is NaN in both versions is the same sum, not a fall from NaN to NaN.
    nan_rows = compare(rows, (['k', 'v'], [(1, float('nan'))]), (['k', 'v'], [(1, float('nan'))]), [], False)
    assert not nan_rows.columns[1].moved
    assert nan_rows.summary() == "on today's data: 1 row in both; the same rows in both"
    nan_sum = compare(change, (['v'], [(float('nan'),)]), (['v'], [(float('nan'),)]), [], False)
    assert nan_sum.summary() == (
        "on today's data: 1 row in both; the sum of v is not a number (NaN) in both; the same values of v in "
        'both'
    )
    to_nan = compare(change, (['v'], [(1.0,)]), (['v'], [(float('nan'),)]), [], False)
    assert 'the sum of v changes from 1 to not a number (NaN)' in to_nan.summary()
    gone = compare(change, (['id'], [(1,)]), (['id'], [(1,)]), [], False)
    assert gone.columns[0].total_before is None
    fewer = compare(change, before, (['id', 'v'], [(1, 0.5)]), [], False)
    assert fewer.summary() == (
        "on today's data: 3 rows before and 1 after; the sum of v falls from 60 to 0.50 (-99.2%); "
        '3 values of v only before and 1 only after'
    )
    zero = compare(change, (['id', 'v'], [(1, 0)]), (['id', 'v'], [(1, 2)]), [], False)
    assert 'the sum of v rises from 0 to 2;' in zero.summary()
    assert Effect(skipped='no').summary() == "effect on today's data not worked out: no"


def test_the_command_line(shop: Project, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(shop.root)
    assert main(['--no-color', '-q', '-C', root, 'release-notes', '--since', 'v1']) == 0
    assert capsys.readouterr().out.startswith('Release notes since v1 (HEAD)')
    assert main(['-q', '-C', root, 'release-notes', '--since', 'v1', '--format', 'markdown']) == 0
    assert capsys.readouterr().out.startswith('## Release notes since v1')
    assert main(['-q', '-C', root, 'release-notes', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['since'] == 'v1'
    assert main(['-q', '-C', root, 'verify', '--effect', '--since', 'v1', '-m', 'ltv', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    (total,) = data['changes'][1]['effect']['columns']
    assert total['total_after'] == 2 * total['total_before']
    assert main(['--no-color', '-q', '-C', root, 'verify', '--effect', '--format', 'markdown']) == 0
    assert "on today's data" in capsys.readouterr().out
    assert main(['-q', '-C', root, 'verify', '--since', 'v1']) == 2
    assert '--since and --until pick the commits for --effect' in capsys.readouterr().err
    assert main(['-q', '-C', root, 'verify', '--format', 'markdown']) == 2
    assert '--format picks how --effect prints' in capsys.readouterr().err
    assert main(['-q', '-C', root, 'verify', '--effect', '--mutate']) == 2
    assert '--effect and --mutate are separate modes of verify' in capsys.readouterr().err
    assert main(['-q', '-C', root, 'verify', '--effect', '--why', 'ltv.total']) == 2
    # --mutate with --why ran the mutation tests before --effect existed, and still does.
    assert main(['-q', '-C', root, 'verify', '--mutate', '--why', 'ltv.total', '--json']) != 2
    capsys.readouterr()
    assert main(['-q', '-C', root, 'release-notes', '--since', 'nope']) == 1


@pytest.mark.dbt
def test_a_dbt_model_on_the_example(jaffle_built_graph: Path, tmp_path: Path) -> None:
    import shutil

    root = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, root)
    graph = root / 'konstruct-out' / 'graph.json'
    graph.write_text(graph.read_text().replace(str(jaffle_built_graph), str(root)))
    (root / '.gitignore').write_text('target/\nlogs/\nkonstruct-out/\n*.duckdb\n')
    git(root, 'init', '-q', '-b', 'main')
    commit(root, 'Add the project', 'Ada', '2026-01-05T10:00:00+00:00')
    git(root, 'tag', 'v1')
    customers = root / 'models' / 'customers.sql'
    customers.write_text(
        customers.read_text().replace(
            'sum(amount) as total_amount',
            "sum(case when payment_method <> 'gift_card' then amount else 0 end) as total_amount",
        )
    )
    commit(root, 'Leave gift cards out of lifetime value', 'Sam', '2026-02-01T10:00:00+00:00')
    project = Project(root)
    notes = project.effect('v1', models=['customers'])
    (change,) = notes.changes
    assert (
        change.after_english
        == "the sum of ([amount] if [payment method] has a value other than 'gift_card', else 0)"
    )
    assert [(a.term, a.bound, a.approved) for a in change.approvals] == [
        ('Customer lifetime value', True, None)
    ]
    effect = change.effect
    assert effect is not None and effect.skipped is None, effect
    con = duckdb.connect(str(root / 'jaffle_shop.duckdb'), read_only=True)
    try:
        gift = con.execute(
            "select sum(amount) from main.stg_payments where payment_method = 'gift_card'"
        ).fetchone()
        customers_paid = con.execute(
            'select count(distinct o.customer_id) from main.stg_payments p join main.stg_orders o using (order_id) '
            "where p.payment_method = 'gift_card'"
        ).fetchone()
    finally:
        con.close()
    assert gift is not None and customers_paid is not None
    (ltv,) = effect.columns
    assert ltv.total_before is not None and ltv.total_after is not None
    assert ltv.total_before - ltv.total_after == pytest.approx(gift[0])
    assert effect.key == ['customer_id'] and ltv.changed == customers_paid[0] and ltv.matched == 100


def test_the_mcp_tools(shop: Project) -> None:
    import anyio
    from mcp import Client

    from konstruct.interfaces.mcp_server import UNTRUSTED_CONTENT, build_server

    async def calls() -> tuple[object, ...]:
        async with Client(build_server(shop.root)) as client:
            return (
                await client.call_tool('release_notes', {'since': 'v1'}),
                await client.call_tool('definition_effect', {'since': 'v1', 'models': ['ltv']}),
                await client.call_tool('release_notes', {'since': 'nope'}),
            )

    notes, effect, wrong = anyio.run(calls)
    data = notes.structured_content  # type: ignore[attr-defined]
    assert data['untrusted_content'] == UNTRUSTED_CONTENT and len(data['changes']) == 7
    effects = effect.structured_content['changes']  # type: ignore[attr-defined]
    (total,) = effects[1]['effect']['columns']
    assert total['total_after'] == 2 * total['total_before']
    assert wrong.is_error  # type: ignore[attr-defined]


@pytest.mark.scale
def test_a_large_project(tmp_path: Path) -> None:
    """Release notes and English on the generated project of tests/test_scale.py, with a commit
    that changes twenty models."""
    import time

    from tests.test_scale import MODELS, make_project

    make_project(tmp_path)
    (tmp_path / '.gitignore').write_text('konstruct-out/\n')
    git(tmp_path, 'init', '-q', '-b', 'main')
    commit(tmp_path, 'Add the project', 'Ada', '2026-01-05T10:00:00+00:00')
    git(tmp_path, 'tag', 'v1')
    for index in range(100, 120):
        model = tmp_path / 'models' / f'm{index:03}.sql'
        model.write_text(model.read_text().replace('* 2 as', '* 3 as').replace('b.c01 > 0', 'b.c01 > 1'))
    commit(tmp_path, 'Change twenty models', 'Linus', '2026-02-01T10:00:00+00:00')
    project = Project(tmp_path)
    project.build(run_dbt=False, warehouses=False)
    started = time.perf_counter()
    notes = project.release_notes()
    seconds = time.perf_counter() - started
    assert sum(1 for c in notes.changes if c.kind == 'rows') == 20
    assert sum(1 for c in notes.changes if c.kind == 'formula') == 20 * 5
    started = time.perf_counter()
    worded = project.english(f'm{MODELS - 1:03}.c01')
    english = time.perf_counter() - started
    assert worded.english and not worded.verbatim
    assert seconds < 30 and english < 5, (seconds, english)
    print(
        f'\n{MODELS} models: release notes of 20 changed models {seconds:.1f}s, English of one column {english:.2f}s'
    )


def test_a_moved_file_is_compared_with_itself(shop: Project) -> None:
    (shop.root / 'sql').mkdir()
    git(shop.root, 'mv', 'ltv.sql', 'sql/ltv.sql')
    commit(shop.root, 'Move ltv into sql/', 'Ada', '2026-06-01T10:00:00+00:00')
    git(shop.root, 'tag', 'v2')
    (shop.root / 'sql' / 'ltv.sql').write_text(V3.replace('amount * 2', 'amount * 4'))
    git(shop.root, 'mv', 'sql/ltv.sql', 'sql/lifetime.sql')
    commit(shop.root, 'Count each order four times, and rename the file', 'Ada', '2026-07-01T10:00:00+00:00')
    shop.build()
    assert shop.release_notes('v1', until='v2').changes[0].commit.subject == 'Remove refunds and reformat'
    (change,) = shop.release_notes('v2').changes
    assert change.headline() == 'ltv.total: formula changed'
    assert (change.path, change.previous_path) == ('sql/lifetime.sql', 'sql/ltv.sql')
    assert change.after_english == 'the sum of ([amount] times 4)'
    effect = shop.effect('v2').changes[0].effect
    assert effect is not None and effect.columns[0].total_after == 2 * (effect.columns[0].total_before or 0)


def test_the_default_start_is_the_latest_tag_before_the_end(shop: Project) -> None:
    (shop.root / 'ltv.sql').write_text(V3.replace('amount * 2', 'amount * 3'))
    commit(shop.root, 'Count each order three times', 'Linus', '2026-06-01T10:00:00+00:00')
    git(shop.root, 'tag', 'v2')
    notes = shop.release_notes(until='v1')
    assert notes.since is None and notes.commits == 1
    assert shop.release_notes(until='v2').since == 'v1' and shop.release_notes().since == 'v1'


def test_a_shallow_clone_says_what_it_cannot_compare(shop: Project, tmp_path: Path) -> None:
    clone = tmp_path / 'clone'
    git(tmp_path, 'clone', '-q', '--depth', '2', f'file://{shop.root}', str(clone))
    shutil.copy(shop.root / 'shop.duckdb', clone / 'shop.duckdb')
    project = Project(clone)
    project.build()
    notes = project.release_notes()
    assert notes.commits == 1
    assert notes.notes[-1].startswith('history before ') and 'shallow clone' in notes.notes[-1]
    assert all(c.commit.subject == 'Remove refunds and reformat' for c in notes.changes)


def test_unrendered_templating_is_noted_and_not_run(shop: Project) -> None:
    (shop.root / 'ltv.sql').write_text(V3.replace("'void'", "'{{ params.excluded }}'"))
    commit(shop.root, 'Template the excluded status', 'Ada', '2026-06-01T10:00:00+00:00')
    shop.build()
    notes = shop.effect('2026-05-15')
    (change,) = notes.changes
    assert change.added == ["WHERE: orders.status <> '__konstruct__'"]
    assert any("is templated where it shows '__konstruct__'" in n for n in notes.notes)
    assert change.effect is not None and change.effect.skipped is not None
    assert "templated where it shows '__konstruct__'" in change.effect.skipped
    (shop.root / 'ltv.sql').write_text(V3)
    commit(shop.root, 'Untemplate it', 'Ada', '2026-07-01T10:00:00+00:00')
    (shop.root / 'ltv.sql').write_text('{% if true %}' + V3.replace('* 2', '* 5') + '{% endif %}')
    commit(shop.root, 'Wrap in a block', 'Ada', '2026-08-01T10:00:00+00:00')
    shop.build()
    blocked = shop.effect('2026-07-15').changes
    assert blocked
    assert all(c.effect and c.effect.skipped and 'Jinja blocks' in c.effect.skipped for c in blocked)


def test_no_warehouse_is_opened_when_nothing_would_run(shop: Project) -> None:
    (shop.root / 'definitions.yml').write_text(TERM.format(approved='2026-06-01'))
    commit(shop.root, 'Approve again', 'Grace', '2026-06-01T10:00:00+00:00')
    notes = shop.effect('2026-05-15', warehouse='snowflake')  # none is configured: opening it would fail
    assert [c.kind for c in notes.changes] == ['term'] and notes.warehouse is None


def test_term_history_cut_short_is_said(shop: Project) -> None:
    for day in ('06', '07'):
        (shop.root / 'definitions.yml').write_text(TERM.format(approved=f'2026-{day}-01'))
        commit(shop.root, f'Approve on {day}', 'Grace', f'2026-{day}-01T10:00:00+00:00')
    notes = shop.release_notes('v1', limit=2)
    assert any(n.startswith("term 'Lifetime value': only the last") for n in notes.notes)


@pytest.mark.parametrize(
    ('before', 'after', 'reading'),
    [
        (float('inf'), 5.0, 'the sum of v falls from infinity to 5'),
        (5.0, float('inf'), 'the sum of v rises from 5 to infinity'),
        (float('-inf'), float('inf'), 'the sum of v rises from minus infinity to infinity'),
        (float('inf'), float('inf'), 'the sum of v is infinity in both'),
    ],
)
def test_an_infinite_sum_has_no_percentage(before: float, after: float, reading: str) -> None:
    change = Change(commit=None, path='x.sql', kind='formula', status='changed', column='t.v')  # type: ignore[arg-type]
    summary = compare(change, (['v'], [(before,)]), (['v'], [(after,)]), [], False).summary()
    assert f'; {reading};' in summary and '%' not in summary


def test_values_that_are_equal_count_as_equal_without_a_key() -> None:
    """With no key, values are counted as == compares them, as with one: a cast to DOUBLE, a
    decimal's scale and NaN change nothing."""
    from decimal import Decimal

    change = Change(commit=None, path='x.sql', kind='formula', status='changed', column='t.v')  # type: ignore[arg-type]
    rows = Change(commit=None, path='x.sql', kind='rows', status='changed')  # type: ignore[arg-type]
    nan = float('nan')
    cases: list[tuple[list[Any], list[Any]]] = [
        ([1, 2], [1.0, 2.0]),
        ([Decimal('1.0')], [Decimal('1.00')]),
        ([nan, 1], [1.0, float('nan')]),
        ([[1, 2]], [[1.0, 2.0]]),
        ([{'a': 1}], [{'a': 1.0}]),
        ([None, True], [True, None]),
    ]
    for old, new in cases:
        before, after = (['v'], [(v,) for v in old]), (['v'], [(v,) for v in new])
        assert compare(change, before, after, [], False).summary().endswith('the same values of v in both'), (
            old
        )
        assert compare(rows, before, after, [], False).summary().endswith('the same rows in both'), old
    different = compare(change, (['v'], [(1,), (True,)]), (['v'], [(1,), ('1',)]), [], False)
    assert different.summary().endswith('1 value of v only before and 1 only after')


@pytest.mark.parametrize(
    ('old', 'new', 'same'),
    [
        (1, True, False),
        (0, False, False),
        ([float('nan')], [float('nan')], True),
        (1, 1.0, True),
        (Decimal('1.0'), Decimal('1.00'), True),
    ],
)
def test_the_keyed_and_keyless_comparisons_agree(old: Any, new: Any, same: bool) -> None:
    """One rule says whether a value changed, with a key or without: a number rewritten as a
    boolean (CASE WHEN x > 0 THEN 1 ELSE 0 END as x > 0) is a change, a number's type is not."""
    change = Change(commit=None, path='x.sql', kind='formula', status='changed', column='t.v')  # type: ignore[arg-type]
    keyed = compare(change, (['k', 'v'], [(0, old)]), (['k', 'v'], [(0, new)]), ['k'], False)
    keyless = compare(change, (['k', 'v'], [(0, old)]), (['k', 'v'], [(0, new)]), [], False)
    (by_key,) = keyed.columns
    (whole,) = keyless.columns
    assert by_key.changed == (0 if same else 1)
    assert (whole.only_before, whole.only_after) == ((0, 0) if same else (1, 1))
    assert by_key.moved == whole.moved == (not same)


PAIRS = (
    'create table main.pairs as select o.id, p.id as parent_id, o.amount + p.amount as total '
    'from raw.orders o left join raw.orders p on {on}\n'
)


def make_pairs(root: Path) -> Path:
    """A SQL project whose model reads orders twice, joining each to its parent and then to every
    order with no void parent."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, 'init', '-q', '-b', 'main')
    for name, text in {
        'konstruct.toml': 'dialect = "duckdb"\n',
        '.gitignore': 'konstruct-out/\n',
        'pairs.sql': PAIRS.format(on='p.id = o.parent_id'),
        'definitions.yml': 'terms:\n  - name: Pair total\n    kind: measure\n    columns: [main.pairs.total]\n',
    }.items():
        (root / name).write_text(text)
    commit(root, 'Add pairs', 'Ada', '2026-01-05T10:00:00+00:00')
    git(root, 'tag', 'v1')
    (root / 'pairs.sql').write_text(PAIRS.format(on="true where p.status <> 'void'"))
    commit(root, 'Join every pair', 'Ada', '2026-01-06T10:00:00+00:00')
    return root


def test_the_readings_of_a_relation_read_twice_are_worded_in_the_notes(tmp_path: Path) -> None:
    project = Project(make_pairs(tmp_path / 'pairs'))
    project.build()
    notes = project.release_notes('v1')
    rows = next(c for c in notes.changes if c.headline() == 'pairs: joins, filters or grouping changed')
    assert rows.removed_english == [
        'rows are joined where [id (the second reading of orders)] and [parent id (the first reading of '
        'orders)] both have values and are equal, keeping every row of the left side, with no values from '
        'the right side where it has no match'
    ]
    # The join the build lists once more, naming the reading it joins, is worded once.
    assert rows.added_english == [
        'the rows of the first reading of orders are joined where true, keeping every row of the left side, '
        'with no values from the right side where it has no match',
        "rows are kept only where [status (the first reading of orders)] has a value other than 'void'",
    ]
    formula = next(c for c in notes.changes if c.headline() == 'pairs.total: formula changed')
    assert formula.before_english == (
        '[amount (the first reading of orders)] plus [amount (the second reading of orders)]'
    )
