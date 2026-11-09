"""Business definitions: definitions.yml, terms in the graph, define, and their checks."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import duckdb
import pytest

from konstruct import Project, QueryError
from konstruct.analysis.diff import compare_definitions
from konstruct.interfaces.cli import main
from konstruct.semantics.definitions import Owner, Term, parse, slug, validate

FULL = """\
version: 1
terms:
  - name: Customer lifetime value
    synonyms: [lifetime value, LTV]
    definition: >
      Total value of every payment
      a customer has made.
    owner: {name: Priya Shah, role: Head of Finance}
    status: approved
    approved: 2026-10-30
    grain: one row per customer
    columns: [customers.customer_lifetime_value]
    filters: ["orders.status <> 'returned'"]
    unit: currency
    currency: AUD
    related: [Order amount]
    exceptions: Refunds are not netted off.
  - name: Order amount
    definition: Payments for an order.
    owner: Tom Okafor (Operations Manager)
    status: agreed
    columns: orders.amount
"""


# The format


def test_a_full_definitions_file() -> None:
    parsed = parse(FULL, 'definitions.yml')
    assert parsed.errors == []
    ltv, amount = parsed.terms
    assert ltv == Term(
        name='Customer lifetime value',
        definition='Total value of every payment a customer has made.',
        path='definitions.yml',
        line=3,
        synonyms=['lifetime value', 'LTV'],
        owner=Owner('Priya Shah', 'Head of Finance'),
        status='approved',
        approved='2026-10-30',
        grain='one row per customer',
        columns=['customers.customer_lifetime_value'],
        filters=["orders.status <> 'returned'"],
        unit='currency',
        currency='AUD',
        related=['Order amount'],
        exceptions='Refunds are not netted off.',
    )
    assert amount.owner == Owner('Tom Okafor', 'Operations Manager')
    assert amount.columns == ['orders.amount']
    assert amount.line == 18
    assert ltv.slug == 'customer_lifetime_value' and slug('  ') == 'term'
    assert Term.from_dict(ltv.to_dict()) == ltv
    assert str(ltv.owner) == 'Priya Shah (Head of Finance)'


@pytest.mark.parametrize(
    ('text', 'message'),
    [
        (
            'terms:\n  - name: X\n    definition: d\n    stauts: draft\n',
            "definitions.yml:2: term 'X': unknown field `stauts`; did you mean `status`?",
        ),
        (
            'terms:\n  - name: X\n    definition: d\n    status: signed off\n',
            "status 'signed off' is not one of draft, agreed, approved, retired",
        ),
        (
            'terms:\n  - name: X\n    definition: d\n    approved: last week\n',
            "`approved` must be a date such as 2026-10-30, not 'last week'",
        ),
        (
            'terms:\n  - name: X\n    definition: d\n    currency: pounds\n',
            "currency 'pounds' should be an ISO 4217 code",
        ),
        ('terms:\n  - definition: d\n', 'definitions.yml:2: term has no `name`'),
        ('terms:\n  - name: X\n', "term 'X': no `definition`"),
        (
            'terms:\n  - name: X\n    definition: d\n    synonyms: {a: b}\n',
            '`synonyms` must be a list of strings',
        ),
        ('terms:\n  - name: X\n    definition: d\n    owner: {role: CFO}\n', '`owner` needs a `name`'),
        (
            'terms:\n  - name: X\n    definition: d\n    owner: {name: A, team: B}\n',
            'unknown owner field `team`',
        ),
        ('terms: [unclosed\n', 'definitions.yml:2: not valid YAML'),
        ('version: 2\nterms: []\n', 'unsupported version 2'),
        ('- just a list\n', 'expected a mapping with a `terms` list'),
        ('terms: a string\n', '`terms` must be a list'),
        ('terms:\n  - just text\n', 'each term must be a mapping'),
    ],
)
def test_problems_name_the_file_and_line(text: str, message: str) -> None:
    errors = parse(text, 'definitions.yml').errors
    assert any(message in e for e in errors), errors


def test_an_empty_file_has_no_terms() -> None:
    assert parse('', 'd.yml').terms == [] and parse('', 'd.yml').errors == []
    assert parse('version: 1\n', 'd.yml').terms == []


def test_checks_across_terms() -> None:
    terms = parse(
        textwrap.dedent(
            """
            terms:
              - {name: Revenue, definition: d, synonyms: [sales], status: approved, columns: [a.b]}
              - {name: Sales, definition: d, status: agreed, related: [Margin]}
            """
        ),
        'd.yml',
    ).terms
    problems = validate(terms)
    assert any("'Sales' is also a name of 'Revenue'" in p for p in problems)
    assert any("term 'Revenue': approved but has no owner" in p for p in problems)
    assert any("term 'Revenue': approved but has no `approved` date" in p for p in problems)
    assert any("term 'Sales': agreed but bound to no columns" in p for p in problems)
    assert any("related term 'Margin' is not defined" in p for p in problems)


# The example project: acceptance


@pytest.fixture
def project(jaffle_built_graph: Path, tmp_path: Path) -> Project:
    copy = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph / 'konstruct-out', copy / 'konstruct-out')
    return Project(copy)


@pytest.mark.dbt
def test_the_example_definitions_build_cleanly(project: Project) -> None:
    graph = project.graph
    assert not [w for w in graph.warnings if w.startswith('definitions')]
    terms = {n.name: n for n in graph.nodes.values() if n.kind == 'term'}
    assert len(terms) == 5
    ltv = terms['Customer lifetime value']
    assert {graph.label(graph.nodes[e.target]) for e in graph.out_edges(ltv.id, 'defines')} == {
        'customers.customer_lifetime_value',
        'customer_ltv.lifetime_value',
    }
    assert {graph.nodes[e.target].name for e in graph.out_edges(ltv.id, 'relates_to')} == {
        'Order amount',
        'Customer',
    }
    assert graph.meta['inputs']['definitions'] == 5
    assert any(s['input'] == 'definitions' and s['detail'] == '5 terms' for s in graph.meta['sources'])


@pytest.mark.dbt
def test_define_by_name_synonym_and_column(project: Project) -> None:
    by_synonym = project.define('lifetime value')
    [term] = by_synonym.terms
    assert term.name == 'Customer lifetime value'
    assert term.status == 'approved' and term.owner == {'name': 'Priya Shah', 'role': 'Head of Finance'}
    assert [c.label for c in term.columns] == [
        'customer_ltv.lifetime_value',
        'customers.customer_lifetime_value',
    ]
    assert project.define('LTV').terms[0].name == term.name
    assert project.define('customer   lifetime VALUE').terms[0].name == term.name
    by_column = project.define('orders.amount')
    assert by_column.column is not None and by_column.column.label == 'orders.amount'
    assert [t.name for t in by_column.terms] == ['Order amount']
    nothing = project.define('stg_orders.status')
    assert nothing.terms == [] and 'No business term defines stg_orders.status' in str(nothing)
    with pytest.raises(QueryError, match="no term or column matches 'gross margin'"):
        project.define('gross margin')
    text = str(by_synonym)
    assert 'Customer lifetime value  [approved, Priya Shah (Head of Finance), approved 2026-10-30]' in text
    assert 'also called: lifetime value, LTV, CLV' in text
    assert json.loads(json.dumps(by_synonym.to_dict()))['terms'][0]['currency'] == 'AUD'


@pytest.mark.dbt
def test_terms_in_describe_search_query_and_impact(project: Project) -> None:
    column = project.describe('customers.customer_lifetime_value')
    assert column.terms == ['Customer lifetime value (approved)']
    assert 'Defined by (1)' in str(column)
    term = project.describe('Customer lifetime value')
    assert term.defines == ['customer_ltv.lifetime_value', 'customers.customer_lifetime_value']
    assert term.related == ['Customer', 'Order amount']
    text = str(term)
    assert 'owner: Priya Shah (Head of Finance)' in text and 'synonyms: lifetime value, LTV, CLV' in text
    assert project.search('ltv').results[0].label == 'Customer lifetime value'
    answer = project.query('what is lifetime value')
    assert [d.name for d in answer.definitions] == ['Customer lifetime value']
    assert str(answer).startswith('Definitions\n  Customer lifetime value')
    walk = project.impact('stg_payments.amount')
    assert {t.label for t in walk.terms} == {'Customer lifetime value', 'Order amount'}
    assert walk.summary().endswith('and 2 business terms downstream.')
    assert project.trace('orders.amount').terms[0].label == 'Order amount'


@pytest.mark.dbt
def test_define_on_the_command_line(project: Project, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(['define', 'lifetime', 'value', '-C', str(project.path)]) == 0
    assert capsys.readouterr().out.startswith('Customer lifetime value  [approved')
    assert main(['define', 'stg_orders.status', '-C', str(project.path)]) == 3
    assert main(['define', 'LTV', '-C', str(project.path), '--json']) == 0
    assert (
        json.loads(capsys.readouterr().out.split('\n', 1)[1])['terms'][0]['grain'] == 'one row per customer'
    )


@pytest.mark.dbt
def test_the_report_has_a_glossary(project: Project) -> None:
    report = (project.out_dir / 'REPORT.md').read_text()
    assert '## Business terms' in report
    assert (
        '| Customer lifetime value | approved | Priya Shah (Head of Finance) | '
        '`customer_ltv.lifetime_value`, `customers.customer_lifetime_value` |'
    ) in report
    assert '| Payment method | draft | none | `stg_payments.payment_method` |' in report
    assert '4 columns hold an approved definition.' in report


@pytest.mark.dbt
def test_renaming_a_bound_column_warns(jaffle: Path, dbt_on_path: None) -> None:
    """Acceptance: rename the column a term is bound to, rebuild, and the build says so."""
    from .conftest import run_dbt

    model = jaffle / 'models' / 'customers.sql'
    model.write_text(model.read_text().replace('as customer_lifetime_value', 'as lifetime_spend'))
    schema = jaffle / 'models' / 'schema.yml'
    schema.write_text(schema.read_text().replace('customer_lifetime_value', 'lifetime_spend'))
    run_dbt(jaffle, 'run', '--select', 'customers')
    result = Project(jaffle).build()
    warning = next(w for w in result.graph.warnings if 'bound column' in w)
    assert warning.startswith("definitions: definitions.yml:5: term 'Customer lifetime value': bound column")
    assert (
        "'customers.customer_lifetime_value' does not exist; did you mean customers.lifetime_spend" in warning
    )
    assert main(['build', str(jaffle), '--strict', '-q']) == 1


# Validation against the graph, on a small SQL project


def _shop(root: Path, definitions: str) -> Project:
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute('create table raw.orders (id int, amount int, status varchar)')
    con.close()
    (root / 'report.sql').write_text(
        "create table main.revenue as select id, amount as revenue from raw.orders where status = 'done'"
    )
    (root / 'definitions.yml').write_text(textwrap.dedent(definitions))
    return Project(root)


def test_binding_and_filter_checks(tmp_path: Path) -> None:
    project = _shop(
        tmp_path,
        """
        terms:
          - name: Revenue
            definition: Money in.
            owner: Finance
            status: approved
            approved: 2026-01-01
            columns: [revenue.revenue, revenue.gross]
            filters: ["revenue > 0", "revenue.channel = 'web'", "this is not sql ((("]
          - name: Order
            definition: An order.
            columns: [orders]
        """,
    )
    warnings = project.build().graph.warnings
    assert any("term 'Revenue': bound column 'revenue.gross' does not exist" in w for w in warnings)
    assert any(
        "required filter \"revenue.channel = 'web'\" reads 'revenue.channel', which does not exist" in w
        for w in warnings
    )
    assert any("required filter 'this is not sql (((' is not valid SQL" in w for w in warnings)
    assert not any('revenue > 0' in w for w in warnings)  # resolved against the bound table
    graph = project.graph
    assert [e.target for e in graph.out_edges('term.order', 'defines')] == ['table.shop.raw.orders']


def test_duplicate_ids_and_configured_paths(tmp_path: Path) -> None:
    project = _shop(tmp_path, 'terms:\n  - {name: Revenue!, definition: a}\n')
    (tmp_path / 'glossary').mkdir()
    (tmp_path / 'glossary' / 'definitions.yaml').write_text('terms:\n  - {name: revenue, definition: b}\n')
    (tmp_path / 'konstruct.toml').write_text('[definitions]\npaths = ["definitions.yml", "glossary"]\n')
    graph = project.build().graph
    assert any('has the same id as another term (term.revenue); skipped' in w for w in graph.warnings)
    assert sum(1 for n in graph.nodes.values() if n.kind == 'term') == 1


# diff


def test_definitions_changes_in_diff() -> None:
    before = FULL
    after = (
        FULL.replace('status: agreed', 'status: approved\n    approved: 2026-11-05')
        .replace('a customer has made.', 'a customer has made, net of refunds.')
        .replace(
            '[customers.customer_lifetime_value]',
            '[customers.customer_lifetime_value, customer_ltv.lifetime_value]',
        )
        + '  - name: Margin\n    definition: Revenue less cost.\n'
    )
    change = compare_definitions('definitions.yml', before, after)
    assert change.added == ['Margin'] and change.removed == []
    assert 'Order amount: status agreed -> approved' in change.changed
    assert 'Customer lifetime value: columns +customer_ltv.lifetime_value' in change.changed
    assert any(line.startswith('Customer lifetime value: definition changed:') for line in change.changed)
    assert change.approved_changed == ['Customer lifetime value']
    assert (
        'Customer lifetime value: approved, but its meaning changed; check it was re-approved'
        in change.lines()
    )


def test_a_changed_kind_is_a_definitions_change() -> None:
    before = 'version: 1\nterms:\n  - {name: Customer, definition: A buyer.}\n'
    after = 'version: 1\nterms:\n  - {name: Customer, definition: A buyer., kind: entity}\n'
    assert compare_definitions('definitions.yml', before, after).changed == ['Customer: kind none -> entity']


def test_diff_reports_definitions_files(tmp_path: Path) -> None:
    root = tmp_path / 'repo'
    root.mkdir()
    project = _shop(root, FULL)

    def git(*args: str) -> None:
        subprocess.run(
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
        )

    (root / '.gitignore').write_text('konstruct-out/\n*.duckdb\n')
    git('init', '-q', '-b', 'main')
    git('add', '-A')
    git('commit', '-qm', 'base')
    project.build()
    (root / 'definitions.yml').write_text(FULL.replace('status: agreed', 'status: retired'))
    result = project.diff(base='HEAD')
    assert result.counts()['terms'] == 1
    assert '1 business term changed' in result.summary()
    assert 'Order amount: status agreed -> retired' in str(result)
    assert '**Business definitions** (definitions.yml)' in result.markdown()
    assert result.to_dict()['definitions'][0]['changed'] == ['Order amount: status agreed -> retired']
