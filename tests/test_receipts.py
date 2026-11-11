"""Answer receipts: what an answer rests on is recorded, and checked later against the graph."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from konstruct import Project, QueryError
from konstruct.analysis.lock import CANONICAL
from konstruct.analysis.receipts import DIRECTORY, Receipt, ReceiptCheck, digest
from konstruct.interfaces.cli import main

LTV = """create table main.ltv as
with paid as (select customer_id, amount from raw.orders where status <> 'returned')
select customer_id, sum(amount) as total, count(*) as orders from paid group by customer_id
"""
REPORT = 'create table main.report as select customer_id, total * 2 as doubled from main.ltv'
TERMS = """version: 1
terms:
  - name: Lifetime value
    definition: {definition}
    owner: {{name: Priya}}
    status: {status}
    approved: 2026-10-30
    columns: [main.ltv.total]
  - name: Churn
    definition: A customer who stopped ordering.
    status: approved
    approved: 2026-10-01
"""


def git(root: Path, *args: str) -> str:
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'Ada', 'GIT_AUTHOR_EMAIL': 'ada@example.com'}
    env |= {'GIT_COMMITTER_NAME': 'Ada', 'GIT_COMMITTER_EMAIL': 'ada@example.com'}
    return subprocess.run(
        ['git', '-C', str(root), '-c', 'commit.gpgsign=false', *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout


def commit(root: Path, message: str) -> str:
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', message)
    return git(root, 'rev-parse', 'HEAD').strip()


def make_shop(root: Path, *, status: str = 'approved', definitions: bool = True) -> Project:
    """A lifetime value table, a report over it and, with ``definitions``, an approved term bound
    to it and one bound to nothing, built and committed."""
    root.mkdir(parents=True, exist_ok=True)
    (root / 'ltv.sql').write_text(LTV)
    (root / 'report.sql').write_text(REPORT)
    (root / '.gitignore').write_text('konstruct-out/\n')
    if definitions:
        (root / 'definitions.yml').write_text(
            TERMS.format(definition='What a customer has paid.', status=status)
        )
    git(root, 'init', '-q', '-b', 'main')
    commit(root, 'Add the shop')
    project = Project(root)
    rebuild(project)
    return project


def rebuild(project: Project) -> None:
    project.build(warehouses=False, run_dbt=False)


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def statuses(result: ReceiptCheck) -> dict[str, str]:
    return {r.id: r.status for r in result.receipts}


def test_a_receipt_records_what_the_answer_rests_on(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop')
    receipt = project.receipt(['report.doubled', 'Churn'], question='Doubled LTV?', answer='70')
    assert receipt.id.startswith('r-') and len(receipt.id) == 14
    assert [p.column for p in receipt.pins] == ['main.report.doubled']
    pin = receipt.pins[0]
    assert pin.formula == 'SUM(raw.orders.amount) * 2'
    assert pin.conditions == ['GROUP BY: raw.orders.customer_id', "WHERE: raw.orders.status <> 'returned'"]
    assert pin.sources == ['raw.orders.amount'] and pin.verified is None
    # Churn defines no column, so the term itself is recorded.
    assert [(t.name, t.status) for t in receipt.terms] == [('Churn', 'approved')]
    assert receipt.build.commit == git(tmp_path / 'shop', 'rev-parse', 'HEAD').strip()
    assert receipt.build.uncommitted is False and receipt.build.changed_inputs == []
    assert receipt.build.graph.startswith('sha256:') and receipt.canonical == CANONICAL
    # The citation claims no verification that did not happen.
    assert receipt.citation == (
        "Relied on report.doubled = SUM(orders.amount) * 2; term 'Churn' (approved 2026-10-01). "
        f'Lineage not checked against the data. Receipt {receipt.id}.'
    )
    files = list((project.out_dir / DIRECTORY).glob('*.jsonl'))
    assert [f.name for f in files] == [f'{receipt.issued[:10]}.jsonl']
    data = json.loads(files[0].read_text())
    assert data['format_version'] == 1 and data['digest'] == digest(data) == receipt.digest
    assert data['question'] == 'Doubled LTV?' and data['answer'] == '70'
    assert Receipt.from_dict({k: v for k, v in data.items() if k != 'format_version'}) == receipt
    assert project.receipts([receipt.id[:6]]) == [receipt]
    text = str(receipt)
    assert text.startswith(f'Receipt {receipt.id} (issued {receipt.issued}, commit ')
    assert (
        '  question  Doubled LTV?' in text and 'main.report.doubled\n  = SUM(raw.orders.amount) * 2' in text
    )
    assert '  verified  lineage not checked against the data' in text


def test_a_harmless_rebuild_passes(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop')
    receipt = project.receipt(['report.doubled', 'Lifetime value', 'Churn'])
    first = project.check_receipts()
    assert first.passed and statuses(first) == {receipt.id: 'holds'} and not first.receipts[0].rebuilt
    root = tmp_path / 'shop'
    # Reformatted SQL, a renamed CTE, a model nothing here reads, a commit: none of it is what the
    # answer rests on.
    (root / 'ltv.sql').write_text(
        'CREATE TABLE main.ltv AS\nWITH kept AS (\n  SELECT customer_id, amount FROM raw.orders\n'
        "  WHERE status <> 'returned'\n)\nSELECT customer_id, SUM(amount) AS total, COUNT(*) AS orders\n"
        'FROM kept\nGROUP BY customer_id\n'
    )
    (root / 'other.sql').write_text('create table main.other as select 1 as one')
    commit(root, 'Reformat and add another table')
    rebuild(project)
    result = project.check_receipts()
    assert result.passed and statuses(result) == {receipt.id: 'holds'}
    assert result.receipts[0].rebuilt and result.receipts[0].changes == []
    assert str(result).startswith('1 receipt checked: every one holds.')
    assert 'graph rebuilt since' in str(result)


def test_a_formula_change_makes_a_receipt_stale(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop')
    root = tmp_path / 'shop'
    kept = project.receipt(['ltv.orders'])
    changed = project.receipt(['report.doubled'])
    edit(root / 'ltv.sql', 'sum(amount)', 'sum(amount * 1.2)')
    sha = commit(root, 'Add tax to lifetime value')
    rebuild(project)
    result = project.check_receipts()
    assert not result.passed and statuses(result) == {kept.id: 'holds', changed.id: 'stale'}
    stale = result.receipts[1]
    [change] = stale.changes
    assert change.reasons == ['formula']
    assert (change.formula_before, change.formula_after) == (
        'SUM(orders.amount) * 2',
        'SUM(orders.amount * 1.2) * 2',
    )
    assert [a.sha for a in change.changed_by] == [sha]
    text = str(result)
    assert text.startswith('2 receipts checked: 1 stale, 1 holds.')
    assert '    report.doubled  formula' in text and 'was  SUM(orders.amount) * 2' in text
    assert 'now  SUM(orders.amount * 1.2) * 2' in text and 'Add tax to lifetime value' in text
    data = result.to_dict()
    assert data['passed'] is False and data['counts'] == {
        'holds': 1,
        'stale': 1,
        'unchecked': 0,
        'tampered': 0,
        'unreadable': 0,
    }
    assert 'accept' not in data['receipts'][1]['changes'][0]
    # Only the receipts named are checked.
    assert project.check_receipts([kept.id]).passed


def test_a_filter_change_makes_a_receipt_stale(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop')
    receipt = project.receipt(['report.doubled'])
    edit(tmp_path / 'shop' / 'ltv.sql', "status <> 'returned'", "status not in ('returned', 'void')")
    rebuild(project)
    [checked] = project.check_receipts(history=False).receipts
    assert checked.id == receipt.id and checked.status == 'stale'
    [change] = checked.changes
    assert change.reasons == ['conditions'] and change.changed_by == []
    assert change.conditions_removed == ["WHERE: orders.status <> 'returned'"]
    assert change.conditions_added == ["WHERE: NOT orders.status IN ('returned', 'void')"]


def test_a_definition_change_makes_a_receipt_stale(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop')
    root = tmp_path / 'shop'
    on_column = project.receipt(['Lifetime value'])
    on_term = project.receipt(['Churn'])
    # A term the caller names is recorded whatever it defines.
    assert [p.column for p in on_column.pins] == ['main.ltv.total']
    assert [t.name for t in on_column.terms] == ['Lifetime value']
    edit(root / 'definitions.yml', 'What a customer has paid.', 'What a customer has paid, net of refunds.')
    edit(root / 'definitions.yml', 'A customer who stopped ordering.', 'A customer with no order in 90 days.')
    commit(root, 'Reword the terms')
    rebuild(project)
    result = project.check_receipts()
    assert statuses(result) == {on_column.id: 'stale', on_term.id: 'stale'}
    [change] = result.receipts[0].changes
    assert change.reasons == ['definition']
    assert change.terms == [
        "'Lifetime value' definition: 'What a customer has paid.' -> 'What a customer has paid, net of refunds.'"
    ]
    assert change.changed_by[0].subject == 'Reword the terms'
    assert result.receipts[1].terms == [
        "'Churn' definition: 'A customer who stopped ordering.' -> 'A customer with no order in 90 days.'"
    ]
    # A term that is no longer defined at all.
    edit(root / 'definitions.yml', '  - name: Churn\n', '  - name: Attrition\n')
    rebuild(project)
    [checked] = project.check_receipts([on_term.id]).receipts
    assert checked.terms == ["'Churn' is no longer defined"]


def test_a_draft_term_reworded_is_a_note(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop', status='draft')
    receipt = project.receipt(['ltv.total'])
    assert "term 'Lifetime value' (draft, owner Priya)" in receipt.citation
    edit(tmp_path / 'shop' / 'definitions.yml', 'What a customer has paid.', 'Paid.')
    rebuild(project)
    [checked] = project.check_receipts().receipts
    assert checked.status == 'holds'
    assert checked.notes == [
        "ltv.total: 'Lifetime value' definition: 'What a customer has paid.' -> 'Paid.' (not approved when issued)"
    ]


def test_a_removed_column_makes_a_receipt_stale(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop')
    root = tmp_path / 'shop'
    receipt = project.receipt(['report.doubled', 'ltv.orders'])
    (root / 'report.sql').write_text('create table main.report as select customer_id from main.ltv')
    edit(root / 'ltv.sql', ', count(*) as orders', '')
    rebuild(project)
    [checked] = project.check_receipts(history=False).receipts
    assert checked.id == receipt.id and checked.status == 'stale'
    assert [(c.column, c.reasons, c.removed) for c in checked.changes] == [
        ('main.ltv.orders', ['removed'], 'no such column in this build'),
        ('main.report.doubled', ['removed'], 'no such column in this build'),
    ]
    assert 'removed  no such column in this build' in str(project.check_receipts(history=False))


def test_tampering_with_the_receipt_file_is_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = make_shop(tmp_path / 'shop')
    honest = project.receipt(['ltv.total'])
    edited = project.receipt(['report.doubled'])
    [path] = (project.out_dir / DIRECTORY).glob('*.jsonl')
    lines = path.read_text().splitlines()
    # The formula recorded is changed by hand to one that would still hold.
    lines[1] = lines[1].replace('SUM(raw.orders.amount) * 2', 'SUM(raw.orders.amount) * 3')
    lines.append('{"not": "closed"')
    newer = json.loads(lines[0]) | {'format_version': 99}
    newer['digest'] = digest(newer)
    lines.append(json.dumps(newer))
    path.write_text('\n'.join(lines) + '\n')
    result = project.check_receipts()
    assert [(r.id, r.status) for r in result.receipts] == [
        (honest.id, 'holds'),
        (edited.id, 'tampered'),
        (f'{path.name}:3', 'unreadable'),
        (honest.id, 'unreadable'),
    ]
    assert result.receipts[1].problem == 'it does not match its digest: it was changed after it was issued'
    assert result.receipts[2].problem is not None and result.receipts[2].problem.startswith('not JSON')
    assert 'written by a newer Konstruct' in (result.receipts[3].problem or '')
    assert result.summary() == '4 receipts checked: 1 tampered, 2 unreadable, 1 holds.'
    with pytest.raises(QueryError, match='is tampered: it does not match its digest'):
        project.receipts([edited.id])
    assert main(['-q', '-C', str(tmp_path / 'shop'), 'receipts', 'check', '--no-history']) == 1
    out = capsys.readouterr().out
    assert f'{edited.id}  tampered  it does not match its digest' in out
    assert f'konstruct-out/receipts/{path.name}, line 2' in out


def test_a_receipt_says_what_verify_confirmed(tmp_path: Path) -> None:
    from tests.test_verify import make_project

    project = make_project(tmp_path)
    before = project.receipt(['report.doubled'], write=False)
    assert before.pins[0].verified is None and 'not checked against the data' in before.citation
    result = project.verify(write=True)
    assert result.checked
    after = project.receipt(['report.doubled'], write=False)
    assert after.pins[0].verified == 'confirmed'
    assert 'Lineage confirmed by konstruct verify.' in after.citation


def test_a_project_without_definitions(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop', definitions=False)
    receipt = project.receipt(['main.report'])
    assert [p.column for p in receipt.pins] == ['main.report.customer_id', 'main.report.doubled']
    assert all(p.terms == [] for p in receipt.pins) and receipt.terms == []
    assert project.check_receipts().passed


def test_what_a_receipt_cannot_rest_on(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop')
    with pytest.raises(QueryError, match='name what the answer rests on'):
        project.receipt([])
    with pytest.raises(QueryError, match="nothing in the graph matches 'nothing_here'"):
        project.receipt(['nothing_here'])
    with pytest.raises(QueryError, match="no receipt 'r-none' in"):
        project.receipts(['r-none'])
    with pytest.raises(QueryError, match="no receipt 'r-none' in"):
        project.check_receipts(['r-none'])
    first, second = project.receipt(['ltv.total']), project.receipt(['ltv.total'])
    # Every id starts with r-, so that names both.
    shared = 'r-'
    with pytest.raises(QueryError, match='is the start of several receipts'):
        project.receipts([shared])
    assert project.receipts() == [first, second]
    assert ReceiptCheck(directory='x').summary() == 'No receipts in x.'


def test_a_receipt_from_a_graph_older_than_its_inputs(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop')
    edit(tmp_path / 'shop' / 'report.sql', '* 2', '* 2 ')
    receipt = project.receipt(['report.doubled'], write=False)
    assert receipt.build.changed_inputs == ['report.sql'] and receipt.build.uncommitted is True
    assert 'The graph was older than 1 changed input when it was issued: report.sql.' in str(receipt)


def test_the_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / 'shop'
    make_shop(root)
    base = ['-q', '-C', str(root), 'receipts']
    assert main([*base, 'issue', 'report.doubled', '--question', 'How much?', '--json']) == 0
    issued = json.loads(capsys.readouterr().out)
    assert issued['format_version'] == 1 and issued['question'] == 'How much?'
    assert main([*base, 'issue', 'ltv.total']) == 0
    out = capsys.readouterr().out
    assert out.startswith('Receipt r-') and out.rstrip().endswith(
        'Recorded in ' + str(root / 'konstruct-out' / 'receipts') + '.'
    )
    assert main([*base, 'show', issued['id'][:8]]) == 0
    assert capsys.readouterr().out.startswith(f'Receipt {issued["id"]} (issued')
    assert main([*base, 'show', issued['id'], '--json']) == 0
    assert json.loads(capsys.readouterr().out)['digest'] == issued['digest']
    assert main([*base, 'check', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['passed'] is True
    edit(root / 'ltv.sql', 'sum(amount)', 'sum(amount * 2)')
    assert main([*base, 'check', '--no-history']) == 1
    assert '2 receipts checked: 2 stale.' in capsys.readouterr().out
    assert main([*base, 'show', 'r-none']) == 1
    assert "no receipt 'r-none'" in capsys.readouterr().err
    with pytest.raises(SystemExit) as stop:
        main([*base])
    assert stop.value.code == 2


def _rewrite(path: Path, change: dict[str, object]) -> None:
    """The first record in ``path`` with ``change`` applied and a digest that matches it."""
    lines = path.read_text().splitlines()
    data = json.loads(lines[0]) | change
    data['digest'] = digest(data)
    path.write_text('\n'.join([json.dumps(data), *lines[1:]]) + '\n')


@pytest.mark.parametrize(
    ('change', 'problem'),
    [
        ({'refs': 'ltv.total'}, "'refs' must be a list of strings"),
        ({'pins': {}}, "'pins' and 'terms' must be lists"),
        ({'relations': [{'name': 1}]}, "a relation needs a 'name' and its 'conditions'"),
        ({'relations': {}}, "'relations' must be a list of objects"),
        ({'id': 7}, "'id' must be a string"),
        ({'question': 7}, "'question' must be a string"),
        ({'build': []}, "'build' must be an object"),
        ({'build': {'graph': 'sha256:0', 'uncommitted': 'no'}}, "'uncommitted' must be true or false"),
        ({'build': {'graph': 'sha256:0', 'changed_inputs': 'a.sql'}}, "'changed_inputs' must be a list"),
        ({'pins': [{'column': 'x'}]}, "'fingerprint' is missing"),
    ],
)
def test_a_record_that_is_not_a_receipt_is_unreadable(
    tmp_path: Path, change: dict[str, object], problem: str
) -> None:
    project = make_shop(tmp_path / 'shop')
    project.receipt(['ltv.total'])
    [path] = (project.out_dir / DIRECTORY).glob('*.jsonl')
    _rewrite(path, change)
    [checked] = project.check_receipts().receipts
    assert checked.status == 'unreadable' and problem in (checked.problem or '')
    assert checked.to_dict()['problem'] == checked.problem


def test_a_receipt_printed_in_another_canonical_form(tmp_path: Path) -> None:
    """A formula that reads differently under a canonical form this Konstruct does not print may
    differ only in how it is printed: it is set aside with a note, as the lockfile check does, so
    an upgrade does not leave every receipt stale. A removed column is still stale."""
    project = make_shop(tmp_path / 'shop')
    project.receipt(['ltv.total', 'ltv.orders'])
    [path] = (project.out_dir / DIRECTORY).glob('*.jsonl')
    _rewrite(path, {'canonical': 'konstruct-0/sqlglot-1.0'})
    assert project.check_receipts().passed
    edit(tmp_path / 'shop' / 'ltv.sql', 'sum(amount)', 'sum(amount * 2)')
    rebuild(project)
    [checked] = project.check_receipts(history=False).receipts
    assert checked.status == 'holds' and checked.changes == []
    assert checked.notes == [
        'issued with canonical form konstruct-0/sqlglot-1.0, which this Konstruct does not print '
        f'({CANONICAL}): 1 column read differently in this form and was not compared on formula or '
        'conditions'
    ]
    edit(tmp_path / 'shop' / 'ltv.sql', ', count(*) as orders', '')
    rebuild(project)
    [checked] = project.check_receipts(history=False).receipts
    assert checked.status == 'stale' and [c.reasons for c in checked.changes] == [['removed']]


def test_source_columns_and_what_is_not_data(tmp_path: Path) -> None:
    from konstruct.core.graph import Node

    project = make_shop(tmp_path / 'shop')
    receipt = project.receipt(['raw.orders.amount'], write=False)
    assert receipt.pins[0].formula is None
    assert receipt.citation.startswith('Relied on orders.amount (a source column).')
    project.graph.add_node(Node('dag.nightly', 'dag', 'nightly'))
    with pytest.raises(QueryError, match=r"'dag\.nightly' is a dag; an answer rests on a column"):
        project.receipt(['dag.nightly'])


def test_a_receipt_claims_no_more_than_was_recorded(tmp_path: Path) -> None:
    """A formula with a gap says so, each column says how much of it verify confirmed when the
    columns differ, and a verification lost since is a note."""
    root = tmp_path / 'chain'
    root.mkdir()
    (root / 'z.sql').write_text('create table main.z as select v - 1 as w from raw.t')
    (root / 'a.sql').write_text('create table main.a as select w + 1 as x from main.z')
    (root / 'b.sql').write_text('create table main.b as select x * 2 as y from main.a')
    project = Project(root)
    rebuild(project)
    graph = project.graph
    for edge in graph.edges:
        if edge.kind == 'derived_from':
            edge.attrs['verified'] = 'confirmed'
    del graph.nodes['table.main.a#x'].attrs['expression']
    receipt = project.receipt(['b.y', 'z.w'])
    assert [(p.column, p.complete, p.verified) for p in receipt.pins] == [
        ('main.b.y', False, 'confirmed'),
        ('main.z.w', True, 'confirmed'),
    ]
    assert receipt.citation.startswith(
        'Relied on b.y = a.x * 2 (formula incomplete: a column on the way has no recorded expression); '
        'z.w = t.v - 1. Lineage confirmed by konstruct verify.'
    )
    assert 'formula  incomplete' in str(receipt)
    [edge] = graph.out_edges('table.main.z#w', 'derived_from')
    edge.attrs['verified'] = 'not exercised'
    mixed = project.receipt(['b.y', 'z.w'], write=False)
    assert 'b.y = a.x * 2 (lineage partly confirmed by konstruct verify; formula incomplete' in mixed.citation
    assert 'z.w = t.v - 1 (lineage not exercised by konstruct verify)' in mixed.citation
    [checked] = project.check_receipts(history=False).receipts
    assert checked.status == 'holds'
    assert checked.notes == [
        'b.y: was verified when issued, and is partly confirmed now',
        'z.w: was verified when issued, and is not exercised now',
    ]


@pytest.mark.dbt
def test_the_jaffle_example(jaffle_built_graph: Path, tmp_path: Path) -> None:
    import re
    import shutil

    root = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, root)
    graph = root / 'konstruct-out' / 'graph.json'
    graph.write_text(graph.read_text().replace(str(jaffle_built_graph), str(root)))
    project = Project(root)
    receipt = project.receipt(
        ['customer_ltv.lifetime_value'], question="What is customer 3's lifetime value?", answer='120'
    )
    assert receipt.citation == (
        'Relied on customer_ltv.lifetime_value = SUM(raw_payments.amount / 100); '
        "term 'Customer lifetime value' (approved 2026-10-30, owner Priya Shah). "
        f'Lineage not checked against the data. Receipt {receipt.id}.'
    )
    assert project.check_receipts(history=False).passed
    # The guide shows this command and this citation.
    guide = (Path(__file__).parents[1] / 'docs' / 'guide' / 'receipts.md').read_text()
    assert (
        'konstruct receipts issue customer_ltv.lifetime_value '
        '--question "What is customer 3\'s lifetime value?" --answer "120"'
    ) in guide
    [shown] = re.findall(r'```text\n(Relied on .*)\n```', guide)
    assert shown == receipt.citation.replace(receipt.id, 'r-7f3a9c1d2e4b')


# The review's findings, each kept as a case


def day_file(project: Project) -> Path:
    [path] = (project.out_dir / DIRECTORY).glob('*.jsonl')
    return path


def test_line_separators_in_recorded_text_do_not_split_a_receipt(tmp_path: Path) -> None:
    """U+2028, U+2029 and NEL are line breaks to str.splitlines but text in JSON written as
    itself: only a newline ends a receipt."""
    project = make_shop(tmp_path / 'shop')
    separators = {'line': chr(0x2028), 'paragraph': chr(0x2029), 'next line': chr(0x85), 'tab': chr(0x0B)}
    questions = [f'a {name}{character}separator' for name, character in separators.items()]
    issued = [project.receipt(['ltv.total'], question=q) for q in questions]
    assert chr(0x2028) in day_file(project).read_text(encoding='utf-8')
    result = project.check_receipts(history=False)
    assert statuses(result) == {r.id: 'holds' for r in issued}
    assert [r.question for r in project.receipts()] == questions


@pytest.mark.skipif(not hasattr(os, 'symlink') or os.name == 'nt', reason='symbolic links and FIFOs')
def test_receipts_are_never_written_or_read_through_a_link(tmp_path: Path) -> None:
    import datetime as dt

    project = make_shop(tmp_path / 'shop')
    outside = tmp_path / 'outside.txt'
    outside.write_text('ORIGINAL\n')
    folder = project.out_dir / DIRECTORY
    folder.mkdir()
    day = folder / f'{dt.datetime.now(dt.UTC).date().isoformat()}.jsonl'
    day.symlink_to(outside)
    # A link read is reported, not followed.
    [checked] = project.check_receipts(history=False).receipts
    assert checked.status == 'unreadable' and checked.problem == 'not a regular file, so not read'
    receipt = project.receipt(['ltv.total'])
    assert outside.read_text() == 'ORIGINAL\n' and not day.is_symlink()
    assert [r.id for r in project.receipts()] == [receipt.id]
    # A FIFO or a device in the folder is reported rather than read, which would wait for ever.
    os.mkfifo(folder / '2026-01-01.jsonl')
    (folder / '2026-01-02.jsonl').symlink_to('/dev/zero')
    result = project.check_receipts(history=False)
    assert [(c.file, c.status) for c in result.receipts] == [
        ('konstruct-out/receipts/2026-01-01.jsonl', 'unreadable'),
        ('konstruct-out/receipts/2026-01-02.jsonl', 'unreadable'),
        (f'konstruct-out/receipts/{day.name}', 'holds'),
    ]
    # A link in place of the folder is replaced by a real one before a receipt is written.
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    for path in folder.iterdir():
        path.unlink()
    folder.rmdir()
    folder.symlink_to(elsewhere, target_is_directory=True)
    [checked] = project.check_receipts(history=False).receipts
    assert (
        checked.status == 'unreadable' and checked.problem == 'it is a symbolic link, which is not followed'
    )
    project.receipt(['ltv.total'])
    assert list(elsewhere.iterdir()) == [] and folder.is_dir() and not folder.is_symlink()


def test_a_line_that_is_not_utf8_is_unreadable(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop')
    first, second = project.receipt(['ltv.total']), project.receipt(['report.doubled'])
    path = day_file(project)
    lines = path.read_bytes().split(b'\n')
    path.write_bytes(b'\n'.join([lines[0], b'\xff\xfe broken', lines[1]]) + b'\n')
    result = project.check_receipts(history=False)
    assert [(c.id, c.status) for c in result.receipts] == [
        (first.id, 'holds'),
        (f'{path.name}:2', 'unreadable'),
        (second.id, 'holds'),
    ]
    assert (result.receipts[1].problem or '').startswith('not UTF-8')
    # Reading every receipt passes over the bad line rather than failing on it.
    assert project.receipts() == [first, second]


def test_an_approved_term_bound_to_a_relation_is_recorded(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop', definitions=False)
    root = tmp_path / 'shop'
    (root / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - name: Customer value report\n'
        '    definition: Twice what each customer has paid.\n    status: approved\n'
        '    approved: 2026-10-30\n    columns: [main.report]\n'
    )
    rebuild(project)
    by_term = project.receipt(['Customer value report'])
    by_column = project.receipt(['report.doubled'])
    for receipt in (by_term, by_column):
        assert [t.name for t in receipt.terms] == ['Customer value report']
        assert "term 'Customer value report' (approved 2026-10-30)" in receipt.citation
    edit(root / 'definitions.yml', 'Twice what', 'Three times what')
    rebuild(project)
    result = project.check_receipts(history=False)
    assert statuses(result) == {by_term.id: 'stale', by_column.id: 'stale'}
    assert result.receipts[1].terms == [
        "'Customer value report' definition: 'Twice what each customer has paid.' -> "
        "'Three times what each customer has paid.'"
    ]
    edit(root / 'definitions.yml', 'status: approved', 'status: retired')
    rebuild(project)
    [checked] = project.check_receipts([by_term.id], history=False).receipts
    assert "'Customer value report' status approved -> retired" in checked.terms


def test_a_measure_formula_is_part_of_a_terms_meaning(tmp_path: Path) -> None:
    """An Ossie metric's formula is what the measure means, so changing it is a new version of the
    term, and a receipt that names the measure is stale."""
    from konstruct.core.graph import Node

    measure = {'kind': 'measure', 'formula': 'SUM(amount) - SUM(refunded)', 'status': 'approved'}
    project = make_shop(tmp_path / 'shop')
    project.graph.add_node(Node('term.revenue', 'term', 'Revenue', attrs=measure))
    receipt = project.receipt(['Revenue'])
    assert [(t.name, t.formula) for t in receipt.terms] == [('Revenue', 'SUM(amount) - SUM(refunded)')]
    project.graph.nodes['term.revenue'].attrs['formula'] = 'SUM(amount)'
    [checked] = project.check_receipts(history=False).receipts
    assert checked.status == 'stale'
    assert checked.terms == ["'Revenue' formula: SUM(amount) - SUM(refunded) -> SUM(amount)"]


def test_a_graph_older_than_its_inputs_holds_no_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / 'shop'
    project = make_shop(root)
    receipt = project.receipt(['report.doubled'])
    edit(root / 'ltv.sql', 'sum(amount)', 'sum(amount * 9)')
    [checked] = project.check_receipts(history=False, refresh=False).receipts
    assert checked.status == 'unchecked' and checked.problem == (
        'the graph is older than 1 changed input (ltv.sql); run `konstruct build -u`, or check '
        'without `--no-refresh`, to know whether this holds'
    )
    base = ['-q', '-C', str(root), 'receipts', 'check', '--no-history']
    assert main([*base, '--no-refresh']) == 1
    assert f'{receipt.id}  unchecked  the graph is older than 1 changed input' in capsys.readouterr().out
    # By default the graph is brought up to date first, and the change is found.
    assert main(base) == 1
    assert f'{receipt.id}  stale' in capsys.readouterr().out
    edit(root / 'ltv.sql', 'sum(amount * 9)', 'sum(amount)')
    assert main(base) == 0


def test_fields_a_later_release_adds_are_ignored(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop')
    first, second = project.receipt(['ltv.total', 'Churn']), project.receipt(['report.doubled'])
    path = day_file(project)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[0]['pins'][0]['owner_team'] = 'finance'
    records[0]['pins'][0]['terms'][0]['steward'] = 'Ada'
    records[0]['terms'][0]['steward'] = 'Ada'
    records[1]['build']['host'] = 'ci'
    records[1]['signature'] = 'later'
    for record in records:
        record['digest'] = digest(record)
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))
    result = project.check_receipts(history=False)
    assert statuses(result) == {first.id: 'holds', second.id: 'holds'}


def test_show_needs_no_graph(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / 'shop'
    project = make_shop(root)
    receipt = project.receipt(['ltv.total'])
    project.graph_path.unlink()
    assert main(['-q', '-C', str(root), 'receipts', 'show', receipt.id]) == 0
    assert capsys.readouterr().out.startswith(f'Receipt {receipt.id}')
    assert not project.graph_path.exists()


def test_text_with_a_lone_surrogate_is_refused_cleanly(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop')
    with pytest.raises(QueryError, match=r'the question is not valid text: it holds U\+D800'):
        project.receipt(['ltv.total'], question='bad \ud800 text')
    with pytest.raises(QueryError, match='the name is not valid text'):
        project.receipt(['ltv.\udcff'])
    assert not (project.out_dir / DIRECTORY).exists()
    # A stored record whose JSON escapes a lone surrogate has no digest, so it is unreadable.
    project.receipt(['ltv.total'])
    path = day_file(project)
    path.write_text(path.read_text().replace('"refs":["ltv.total"]', '"refs":["ltv.\\ud800"]'))
    [checked] = project.check_receipts(history=False).receipts
    assert checked.status == 'unreadable' and 'not valid text' in (checked.problem or '')


def test_a_short_write_is_finished_and_a_failed_one_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_shop(tmp_path / 'shop')
    real = os.write
    monkeypatch.setattr(os, 'write', lambda handle, data: real(handle, bytes(data[:7])))
    receipt = project.receipt(['main.report'], question='q' * 3000)
    assert project.receipts() == [receipt]
    monkeypatch.setattr(os, 'write', lambda handle, data: 0)
    with pytest.raises(QueryError, match='nothing was written'):
        project.receipt(['ltv.total'])


def test_a_forged_copy_does_not_hide_the_receipt_it_copies(tmp_path: Path) -> None:
    project = make_shop(tmp_path / 'shop')
    receipt = project.receipt(['ltv.total'])
    path = day_file(project)
    line = path.read_text().splitlines()[0]
    with path.open('a') as stream:
        stream.write(line.replace('"ltv.total"', '"ltv.orders"') + '\n')
    assert project.receipts([receipt.id]) == [receipt]
    assert project.receipts() == [receipt]
    result = project.check_receipts([receipt.id], history=False)
    assert [c.status for c in result.receipts] == ['holds', 'tampered']
    # Errors name files relative to the project.
    with path.open('w') as stream:
        stream.write(line.replace('"ltv.total"', '"ltv.orders"') + '\n')
    with pytest.raises(QueryError) as raised:
        project.receipts([receipt.id])
    assert f'(konstruct-out/receipts/{path.name}, line 1) is tampered' in str(raised.value)
    with pytest.raises(QueryError, match=r"no receipt 'r-none' in konstruct-out/receipts$"):
        project.receipts(['r-none'])


def test_an_ossie_metrics_formula_change_makes_a_receipt_stale(tmp_path: Path) -> None:
    """An Ossie metric is a draft term defined by its formula: a new formula is a change to what
    it computes, so the receipt is stale whatever the term's status, while a reworded description
    of a draft term is only a note."""
    from tests.test_ossie import _project

    root = _project(tmp_path)
    project = Project(root)
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    receipt = project.receipt(['Revenue'])
    assert [(t.name, t.status, t.formula) for t in receipt.terms] == [
        ('Revenue', 'draft', 'SUM(orders.total)')
    ]
    assert "term 'Revenue' (draft, formula SUM(orders.total))" in receipt.citation
    model = root / 'shop.ossie.yaml'
    edit(model, 'Money in, in pounds.', 'Money taken, in pounds.')
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    [checked] = project.check_receipts(history=False).receipts
    assert checked.status == 'holds'
    assert checked.notes == [
        "'Revenue' definition: 'Money in, in pounds.' -> 'Money taken, in pounds.' (not approved when issued)"
    ]
    edit(model, 'SUM(orders.total)', 'AVG(orders.total)')
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    [checked] = project.check_receipts(history=False).receipts
    assert checked.status == 'stale'
    assert checked.terms == ["'Revenue' formula: SUM(orders.total) -> AVG(orders.total)"]


@pytest.mark.skipif(not hasattr(os, 'O_NOFOLLOW'), reason='O_NOFOLLOW is POSIX')
def test_a_link_planted_after_the_check_is_not_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The day's file is opened without following a link, so one planted between the check for a
    link and the open (here, a check that misses it) is refused rather than written through."""
    import datetime as dt

    project = make_shop(tmp_path / 'shop')
    outside = tmp_path / 'outside.txt'
    outside.write_text('ORIGINAL\n')
    folder = project.out_dir / DIRECTORY
    folder.mkdir()
    (folder / f'{dt.datetime.now(dt.UTC).date().isoformat()}.jsonl').symlink_to(outside)
    monkeypatch.setattr(Path, 'is_symlink', lambda self: False)
    with pytest.raises(QueryError, match='cannot write'):
        project.receipt(['ltv.total'])
    assert outside.read_text() == 'ORIGINAL\n'


def test_a_receipt_issued_before_unnest_was_a_condition_still_holds(tmp_path: Path) -> None:
    """Canonical form 3 records an UNNEST as a ROWS FROM condition, which form 2 did not: a
    receipt issued in form 2 reads differently only in how it is printed, so it is not stale."""
    root = tmp_path / 'tags'
    root.mkdir()
    (root / 'tagged.sql').write_text(
        'create table main.tagged as select o.id, o.amount, t.tag from raw.orders o\n'
        'cross join unnest(o.tags) as t(tag)\n'
    )
    (root / '.gitignore').write_text('konstruct-out/\n')
    git(root, 'init', '-q', '-b', 'main')
    commit(root, 'Add the tags')
    project = Project(root)
    rebuild(project)
    project.receipt(['tagged.amount'])
    [path] = (project.out_dir / DIRECTORY).glob('*.jsonl')
    record = json.loads(path.read_text().splitlines()[0])
    assert record['relations'] == [
        {'name': 'main.tagged', 'conditions': ['ROWS FROM: UNNEST(raw.orders.tags)']}
    ]
    without = [{'name': 'main.tagged', 'conditions': []}]
    _rewrite(path, {'relations': without})
    [checked] = project.check_receipts(history=False).receipts
    assert checked.status == 'stale'  # in the same form, the condition is a change
    _rewrite(path, {'relations': without, 'canonical': 'konstruct-2/sqlglot-30.30'})
    [checked] = project.check_receipts(history=False).receipts
    assert checked.status == 'holds' and checked.changes == []
    assert 'issued with canonical form konstruct-2/sqlglot-30.30' in checked.notes[0]


def test_a_self_join_turned_the_other_way_makes_a_receipt_stale(tmp_path: Path) -> None:
    """Each reading of a relation a query reads twice has a name of its own, so turning a self-join
    the other way is a change, and other aliases are not."""
    root = tmp_path / 'pairs'
    root.mkdir()
    pairs = (
        'create table main.pairs as select {o}.id, sum({p}.amount) as total from raw.orders {o} '
        'join raw.orders {p} on {on} group by {o}.id\n'
    )
    (root / 'pairs.sql').write_text(pairs.format(o='o', p='p', on='p.customer_id = o.id'))
    (root / '.gitignore').write_text('konstruct-out/\n')
    git(root, 'init', '-q', '-b', 'main')
    commit(root, 'Add the pairs')
    project = Project(root)
    rebuild(project)
    project.receipt(['pairs.total'])
    (root / 'pairs.sql').write_text(pairs.format(o='a', p='b', on='b.customer_id = a.id'))
    rebuild(project)
    assert project.check_receipts(history=False).passed
    (root / 'pairs.sql').write_text(pairs.format(o='o', p='p', on='o.customer_id = p.id'))
    rebuild(project)
    [checked] = project.check_receipts(history=False).receipts
    assert checked.status == 'stale'
    [change] = checked.changes
    assert change.reasons == ['conditions']
    assert change.conditions_added == ['JOIN ON: "orders#1".customer_id = "orders#2".id']


def test_a_self_join_turned_from_left_to_right_makes_a_receipt_stale(tmp_path: Path) -> None:
    root = tmp_path / 'sided'
    root.mkdir()
    pairs = (
        'create table main.pairs as select o.id, p.amount from raw.orders o {side} join raw.orders p '
        'on p.customer_id = o.id\n'
    )
    (root / 'pairs.sql').write_text(pairs.format(side='left'))
    (root / '.gitignore').write_text('konstruct-out/\n')
    git(root, 'init', '-q', '-b', 'main')
    commit(root, 'Add the pairs')
    project = Project(root)
    rebuild(project)
    project.receipt(['pairs.amount'])
    (root / 'pairs.sql').write_text(pairs.format(side='right'))
    rebuild(project)
    [checked] = project.check_receipts(history=False).receipts
    assert checked.status == 'stale'


def _stale(root: Path, before: dict[str, str], after: dict[str, str], column: str) -> str:
    """The status of a receipt for ``column`` made with the files ``before`` and checked once
    ``after`` is written over them."""
    root.mkdir()
    for name, sql in before.items():
        (root / name).write_text(sql)
    (root / '.gitignore').write_text('konstruct-out/\n')
    git(root, 'init', '-q', '-b', 'main')
    commit(root, 'Add the models')
    project = Project(root)
    rebuild(project)
    project.receipt([column])
    for name, sql in after.items():
        (root / name).write_text(sql)
    rebuild(project)
    [checked] = project.check_receipts(history=False).receipts
    return checked.status


COUNTED = 'create table main.pairs as select count(*) as n from raw.orders a join raw.orders b on {on}\n'
MODEL = 'create table main.m as select id, customer_id as parent_id, amount as v from raw.orders\n'
COMPOSED = (
    'create table main.d as select m.id, {x} as x from main.m m join raw.orders o on o.id = m.parent_id\n'
)


@pytest.mark.parametrize(
    ('before', 'after', 'column'),
    [
        (
            {'pairs.sql': COUNTED.format(on='a.status = b.status')},
            {'pairs.sql': COUNTED.format(on='a.status = a.status')},
            'pairs.n',
        ),
        (
            {
                'pairs.sql': COUNTED.format(
                    on='b.customer_id = a.id join raw.orders c on c.customer_id = a.id'
                )
            },
            {
                'pairs.sql': COUNTED.format(
                    on='b.customer_id = a.id join raw.orders c on b.customer_id = c.id'
                )
            },
            'pairs.n',
        ),
        (
            {'m.sql': MODEL, 'd.sql': COMPOSED.format(x='m.v - o.amount')},
            {'d.sql': COMPOSED.format(x='o.amount - m.v')},
            'd.x',
        ),
    ],
    ids=['a symmetric join made a tautology', 'readings said the same of', 'a model read once and directly'],
)
def test_a_change_to_which_reading_is_read_makes_a_receipt_stale(
    tmp_path: Path, before: dict[str, str], after: dict[str, str], column: str
) -> None:
    assert _stale(tmp_path / 'project', before, after, column) == 'stale'
