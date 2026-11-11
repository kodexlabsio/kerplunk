"""The terminal output: colour and its off switches, plain text for pipes, fitting to the window,
trees, SQL highlighting, progress, errors and help."""

from __future__ import annotations

import io
import re
import sys
import time
from pathlib import Path

import pytest

from konstruct.interfaces import cli
from konstruct.interfaces import console as console_module
from konstruct.interfaces.cli import main
from konstruct.interfaces.console import Console, colour_enabled, style_for, terminal_width, unicode_enabled
from konstruct.interfaces.layout import Branch, Style, Text, pairs, sql, table, tree, wrap

ESCAPE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


class Terminal(io.StringIO):
    """A stream that says it is a terminal."""

    encoding = 'utf-8'

    def isatty(self) -> bool:
        return True


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ('NO_COLOR', 'FORCE_COLOR', 'CI'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.setenv('COLUMNS', '100')
    return monkeypatch


# Detecting what the terminal can show


def test_a_terminal_gets_colour_unicode_and_its_width(clean_env: pytest.MonkeyPatch) -> None:
    stream = Terminal()
    assert colour_enabled(stream) and unicode_enabled(stream)
    assert terminal_width(stream) == 100
    clean_env.setenv('COLUMNS', '20')
    assert terminal_width(stream) == 40  # never narrower than a usable minimum


def test_no_color_turns_colour_off_but_keeps_the_layout(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv('NO_COLOR', '1')
    style = style_for(Terminal())
    assert not style.colour and style.unicode and style.width == 100
    assert not style_for(Terminal(), False).colour  # --no-color


def test_a_dumb_terminal_and_a_pipe_get_plain_ascii(clean_env: pytest.MonkeyPatch) -> None:
    assert style_for(io.StringIO()) == Style(colour=False, unicode=False, width=None)
    clean_env.setenv('TERM', 'dumb')
    assert style_for(Terminal()) == Style(colour=False, unicode=False, width=None)


def test_force_color_colours_a_pipe(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv('FORCE_COLOR', '1')

    class Pipe(io.StringIO):
        encoding = 'utf-8'

    style = style_for(Pipe())
    assert style.colour and style.width is None


# Styled text


def test_roles_paint_only_with_colour() -> None:
    line = Text(('name', 'subject'), ' ', ('(model)', 'dim'))
    assert Style().line(line) == 'name (model)'
    assert Style(colour=True).line(line) == '\x1b[1;36mname\x1b[0m \x1b[2m(model)\x1b[0m'
    assert Style(colour=True).paint('x', 'error+strong') == '\x1b[31;1mx\x1b[0m'


def test_truncation_keeps_roles_and_marks_the_cut() -> None:
    line = Text(('abcdef', 'accent'), ('ghij', ''))
    assert line.truncate(20, '...').plain == 'abcdefghij'
    cut = line.truncate(8, '...')
    assert cut.plain == 'abcde...' and cut.parts[0] == ('abcde', 'accent')
    assert Style(unicode=True, width=5).fit(Text('abcdefgh')).plain == 'abcd…'


def test_wrapping_hangs_under_the_first_line() -> None:
    body = Text('one two three four five six seven eight nine ten')
    assert [line.plain for line in wrap(Style(), body, first=Text('key  '))] == ['key  ' + body.plain]
    lines = wrap(Style(width=30), body, first=Text('key  '), hanging=5)
    assert [line.plain for line in lines] == ['key  one two three four five', '     six seven eight nine ten']
    assert all(len(line) <= 30 for line in lines)


# Tables


ROWS = [
    ['orders', 'model', 'models/orders.sql'],
    ['stg_payments', 'model', 'models/staging/stg_payments.sql'],
]


def test_tables_align_and_right_align_numbers() -> None:
    lines = table(Style(), [['customers', '7'], ['stg_orders', '13']], right=[1])
    assert [line.plain for line in lines] == ['  customers    7', '  stg_orders  13']
    assert [Style().line(line) for line in table(Style(), ROWS, headers=['name', 'kind', 'path'])] == [
        '  name          kind   path',
        '  orders        model  models/orders.sql',
        '  stg_payments  model  models/staging/stg_payments.sql',
    ]


def test_tables_never_cut_when_piped_and_cut_to_fit_a_terminal() -> None:
    long = [['x', 'y' * 300]]
    assert table(Style(), long)[0].plain == '  x  ' + 'y' * 300
    cut = table(Style(unicode=True, width=40), long)[0]
    assert len(cut) == 40 and cut.plain.endswith('…')


def test_columns_are_dropped_before_the_flexible_one_is_cut_short() -> None:
    rows = [['a.very_long_column_name', 'transformed', 'model', 'DOUBLE', 'models/staging/stg_payments.sql']]
    wide = table(Style(width=120), rows, drop=[2, 3])[0].plain
    assert 'model' in wide and 'DOUBLE' in wide
    narrow = table(Style(width=72), rows, drop=[2, 3])[0].plain
    assert 'DOUBLE' not in narrow and narrow.endswith('models/staging/stg_payments.sql')


def test_empty_cells_and_missing_columns() -> None:
    assert [line.plain for line in table(Style(), [['', None], [None]])] == ['', '']
    assert [line.plain for line in table(Style(), [['a'], ['b', 'c']])] == ['  a', '  b  c']
    assert [line.plain for line in table(Style(width=40), [['a'], ['b', 'c']])] == ['  a', '  b  c']


def test_cells_are_one_line() -> None:
    lines = table(Style(), [['status', 'one\n| two |\n| three |']])
    assert [line.plain for line in lines] == ['  status  one | two | | three |']


def test_key_value_blocks_align_and_skip_empty_values() -> None:
    lines = pairs(Style(), [('owner', 'Priya'), ('approved', None), ('also called', 'LTV')])
    assert [line.plain for line in lines] == ['  owner        Priya', '  also called  LTV']


# Trees


def test_trees_draw_box_connectors_on_a_terminal_and_ascii_in_pipes() -> None:
    root = Branch(Text('stg_payments.amount'), ['', 'model'])
    child = Branch(Text('customers.ltv'), ['transformed', 'model'])
    child.children.append(Branch(Text('customer_ltv.ltv'), ['direct', 'table']))
    root.children += [child, Branch(Text('orders.amount'), ['transformed', 'model'])]
    assert [line.plain for line in tree(Style(unicode=True), root)] == [
        '  stg_payments.amount                  model',
        '  ├─ customers.ltv        transformed  model',
        '  │  └─ customer_ltv.ltv  direct       table',
        '  └─ orders.amount        transformed  model',
    ]
    ascii_ = [line.plain for line in tree(Style(), root)]
    assert ascii_[1:] == [
        '  |- customers.ltv        transformed  model',
        '  |  `- customer_ltv.ltv  direct       table',
        '  `- orders.amount        transformed  model',
    ]
    assert all(line.isascii() for line in ascii_)


@pytest.mark.dbt
def test_impact_is_a_tree_of_the_columns_reached(jaffle_built_graph: Path) -> None:
    from konstruct import Project

    walk = Project(jaffle_built_graph).impact('stg_payments.amount')
    text = str(walk)
    lineage = text.split('\nLineage\n', 1)[1].split('\n\n', 1)[0].splitlines()
    assert lineage[0].startswith('  stg_payments.amount ')
    assert re.match(r'  \|- customers\.customer_lifetime_value +transformed +model +DOUBLE', lineage[1])
    # customer_ltv is reached from customers.customer_lifetime_value, so it hangs under it.
    assert re.match(r'  \|  `- customer_ltv\.lifetime_value +direct', lineage[3])
    assert lineage[-1].startswith('  `- orders.gift_card_amount ')
    # Every column reached is in the tree, once.
    for column in walk.columns:
        assert sum(1 for line in lineage if f' {column.label} ' in line) == 1
    trace = str(Project(jaffle_built_graph).trace('orders.amount'))
    assert '  `- stg_payments.amount' in trace and '     `- raw_payments.amount' in trace


@pytest.mark.dbt
def test_impact_of_a_relation_is_a_tree_of_relations(jaffle_built_graph: Path) -> None:
    from konstruct import Project

    text = str(Project(jaffle_built_graph).impact('stg_orders'))
    assert '\nLineage\n  stg_orders ' in text
    assert re.search(r'^  \|  `- customer_ltv +table +via jaffle_shop_daily\.build_customer_ltv$', text, re.M)
    assert '\nColumns (9)\n' in text


# SQL


def test_sql_highlights_keywords_functions_and_literals_only() -> None:
    highlighted = sql("SUM(CASE WHEN orders.status = 'done' THEN orders.amount / 100 ELSE 0 END)")
    roles = {value: role for value, role in highlighted.parts if role}
    assert roles == {'SUM': 'keyword', 'CASE': 'keyword', 'WHEN': 'keyword', "'done'": 'literal'} | {
        'THEN': 'keyword',
        '100': 'literal',
        'ELSE': 'keyword',
        '0': 'literal',
        'END': 'keyword',
    }
    assert highlighted.plain == "SUM(CASE WHEN orders.status = 'done' THEN orders.amount / 100 ELSE 0 END)"
    # Lower-case words and qualified names are identifiers, even when they spell a keyword.
    assert not [p for p in sql('orders.date + date').parts if p[1]]


# Rendering whole results


@pytest.mark.dbt
def test_str_is_the_plain_rendering(jaffle_built_graph: Path) -> None:
    from konstruct import Project

    project = Project(jaffle_built_graph)
    for result in (project.impact('orders'), project.explain('orders.amount'), project.describe('orders')):
        text = str(result)
        assert text == result.text() and text.isascii() and '\x1b' not in text
        coloured = result.text(Style(colour=True, unicode=True))
        assert '\x1b[' in coloured
        # Colour changes how the text looks, never what it says.
        assert ESCAPE.sub('', coloured).replace('├─', '|-').replace('└─', '`-').replace('│', '|') == text


@pytest.mark.dbt
def test_the_cli_writes_plain_text_to_a_pipe(
    jaffle_built_graph: Path, capsys: pytest.CaptureFixture[str], clean_env: pytest.MonkeyPatch
) -> None:
    assert main(['impact', 'stg_payments.amount', '-C', str(jaffle_built_graph)]) == 0
    out = capsys.readouterr().out
    assert '\x1b' not in out and out.isascii()
    assert max(len(line) for line in out.splitlines()) > 100  # not cut to any width


@pytest.mark.dbt
def test_the_cli_fits_a_terminal(
    jaffle_built_graph: Path, capsys: pytest.CaptureFixture[str], clean_env: pytest.MonkeyPatch
) -> None:
    screen = Terminal()
    clean_env.setattr(sys, 'stdout', screen)
    clean_env.setenv('COLUMNS', '72')
    assert main(['impact', 'stg_payments.amount', '-C', str(jaffle_built_graph)]) == 0
    out = screen.getvalue()
    assert '\x1b[1;36mstg_payments.amount\x1b[0m' in out
    lines = [ESCAPE.sub('', line) for line in out.splitlines()]
    assert max(len(line) for line in lines) <= 72
    assert any(line.startswith('  ├─ customers.customer_lifetime_value') for line in lines)


@pytest.mark.dbt
def test_no_color_on_a_terminal(
    jaffle_built_graph: Path, capsys: pytest.CaptureFixture[str], clean_env: pytest.MonkeyPatch
) -> None:
    screen = Terminal()
    clean_env.setattr(sys, 'stdout', screen)
    clean_env.setenv('NO_COLOR', '1')
    assert main(['trace', 'orders.amount', '-C', str(jaffle_built_graph)]) == 0
    out = screen.getvalue()
    assert '\x1b' not in out and '  └─ stg_payments.amount' in out


@pytest.mark.dbt
def test_json_is_unchanged_by_the_terminal(
    jaffle_built_graph: Path, capsys: pytest.CaptureFixture[str], clean_env: pytest.MonkeyPatch
) -> None:
    import json

    assert main(['impact', 'orders.amount', '--json', '-C', str(jaffle_built_graph)]) == 0
    piped = capsys.readouterr().out
    screen = Terminal()
    clean_env.setattr(sys, 'stdout', screen)
    assert main(['impact', 'orders.amount', '--json', '-C', str(jaffle_built_graph)]) == 0
    assert screen.getvalue() == piped
    assert set(json.loads(piped)) >= {'direction', 'target', 'columns', 'summary'}
    assert 'parents' not in json.loads(piped)


# Progress


def test_progress_is_silent_off_a_terminal_in_ci_and_when_quiet(clean_env: pytest.MonkeyPatch) -> None:
    console = Console()
    console.err = Terminal()
    assert console.progress('x').enabled
    assert not Console(quiet=True).progress('x').enabled
    assert not Console(verbose=True).progress('x').enabled
    clean_env.setenv('CI', 'true')
    assert not console.progress('x').enabled
    console.err = io.StringIO()
    clean_env.delenv('CI')
    assert not console.progress('x').enabled


def test_the_spinner_shows_the_build_stage_and_clears_itself(clean_env: pytest.MonkeyPatch) -> None:
    import logging

    clean_env.setattr(console_module.Progress, 'DELAY', 0.0)
    console = Console()
    console.err = Terminal()
    console.err_ui = Style(colour=False, unicode=True, width=80)
    with console.progress('Building') as progress:
        logging.getLogger('konstruct.engine.build').info('analysing dbt models')
        logging.getLogger('konstruct.progress').debug('3 of 5 models')
        assert progress.frame(0) == '⠋ Analysing dbt models  3 of 5 models'
        time.sleep(0.2)
    written = console.err.getvalue()
    assert 'Analysing dbt models' in written
    assert written.endswith('\r\x1b[2K\x1b[?25h')  # erased, cursor shown again
    assert logging.getLogger('konstruct').handlers == []


# Errors and help


@pytest.mark.dbt
def test_a_wrong_name_says_what_it_may_have_meant(
    jaffle_built_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(['impact', 'orders.amout', '-C', str(jaffle_built_graph)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "konstruct: error: nothing in the graph matches 'orders.amout'"
    assert err[1].startswith('  did you mean orders.amount')
    assert err[2] == '  try `konstruct search orders amout`'


def test_a_mistyped_command_is_corrected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(['imapct', 'orders'])
    assert stop.value.code == 2
    err = capsys.readouterr().err.splitlines()
    assert err == [
        "konstruct: error: unknown command 'imapct'",
        '  did you mean impact?',
        '  run konstruct --help for its options and examples',
    ]


@pytest.mark.parametrize('choices', ["'impact', 'trace'", 'impact, trace'])
def test_a_mistyped_command_is_corrected_whether_python_quotes_the_choices(
    choices: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from konstruct.interfaces.cli import _Parser

    with pytest.raises(SystemExit):
        _Parser(prog='konstruct').error(f"argument command: invalid choice: 'imapct' (choose from {choices})")
    assert capsys.readouterr().err.splitlines()[1] == '  did you mean impact?'


def test_help_groups_the_commands_and_each_command_has_examples(capsys: pytest.CaptureFixture[str]) -> None:
    help_text = cli.parser().format_help()
    for title, names in cli.GROUPS:
        assert f'\n{title}\n' in help_text
        for name in names:
            assert re.search(rf'^  {name} +\w', help_text, re.M)
    commands = {name for _, names in cli.GROUPS for name in names}
    parser = cli.parser()
    subparsers = next(a for a in parser._actions if a.dest == 'command')
    assert commands == set(subparsers.choices)  # type: ignore[arg-type]
    for name, sub in subparsers.choices.items():  # type: ignore[union-attr]
        text = sub.format_help()
        assert '\nExamples:\n  konstruct' in text or name == 'diff', name
        assert text.startswith('Usage: konstruct')


# Less common results, built directly


def test_a_diff_of_dags_yaml_and_definitions() -> None:
    from konstruct.analysis.diff import DagChange, DefinitionsChange, Diff, RelationChange, YamlChange

    diff = Diff(
        base='main',
        head='working tree',
        files={'dags/a.py': 'M', 'models/schema.yml': 'M', 'definitions.yml': 'M', 'notes.txt': 'M'},
        relations=[
            RelationChange('models/x.sql', 'x', 'model', 'modified', note='the SQL could not be parsed')
        ],
        dags=[DagChange('dags/a.py', 'a', 'modified', tasks_added=['load'], dependencies_removed=['b >> c'])],
        yaml=[YamlChange('models/schema.yml', tests_removed=['x.id: unique'])],
        definitions=[DefinitionsChange('definitions.yml', added=['Margin'])],
        other=['notes.txt'],
    )
    text = str(diff)
    assert text.startswith('Diff main..working tree (4 files changed)\n')
    for line in (
        'dags/a.py  (DAG a, modified)\n  task +   load\n  after -  b >> c',
        'models/schema.yml  (dbt YAML)\n  test removed: x.id: unique',
        'definitions.yml  (business definitions)\n  term added: Margin',
        'models/x.sql  (model x, modified)\n  the SQL could not be parsed',
        'Not analysed: notes.txt',
    ):
        assert line in text


def test_gate_verify_history_and_ontology_edge_cases() -> None:
    from konstruct.analysis.gate import Gate
    from konstruct.analysis.history import Commit, FormulaChange, History, TermChange
    from konstruct.analysis.verify import ModelResult, Verification
    from konstruct.semantics.ontology import Ontology, Rule

    assert str(Gate(base='main', changed=2)) == 'Gate passed: 2 changed files, nothing breaks.'
    colour = Gate(base='main', changed=1).text(Style(colour=True))
    assert colour.startswith('\x1b[1;32mGate passed\x1b[0m')

    verification = Verification(
        warehouse='shop.duckdb',
        seed=1,
        max_rows=10,
        strategies=('shift',),
        models=[
            ModelResult('a', 'orders', claimed=2, confirmed=[('x', 'y')], unexplained=[('z', 'raw.z')]),
            ModelResult('b', 'payments', skipped='no warehouse table'),
        ],
    )
    text = str(verification)
    assert '  payments  skipped: no warehouse table' in text
    assert '  orders.z  <-  raw.z  unexplained' in text
    assert '←' in verification.text(Style(unicode=True))

    working = Commit(sha='', author='', date='', subject='')
    added = Commit(
        sha='abc1234def', author='Ada', date='2026-01-05T10:00:00+00:00', subject='Add it', order=1
    )
    history = History(
        column='c',
        label='t.c',
        formula='SUM(x)',
        path='t.sql',
        read_formulas=True,
        formulas=[
            FormulaChange(added, 't.sql', None, 'SUM(x)'),
            FormulaChange(working, 't.sql', 'SUM(x)', None),
        ],
        terms=[TermChange(added, 'Total', 'definitions.yml', {'status': (None, 'draft')}, 'draft')],
    )
    text = str(history)
    assert '  2026-01-05  abc1234  Ada  Add it\n    added  SUM(x)' in text
    assert '  working tree (uncommitted)\n    removed  SUM(x)' in text

    ontology = Ontology(rules=[Rule('one-formula-per-measure', 'One formula.', ['Revenue has two formulas'])])
    text = str(ontology)
    assert text.startswith('Ontology: 0 entities, 0 measures, 0 relationships, 1 rule violation.')
    assert 'Rule violations (1)\n  one-formula-per-measure: Revenue has two formulas' in text


def test_a_check_of_sql_and_of_query_history() -> None:
    from konstruct.analysis.checksql import Adoption, Finding, HistoryCheck, ShadowMetric, SqlCheck

    finding = Finding(
        'missing-filter',
        'error',
        "revenue computes 'Revenue' without its filter",
        'Apply the filter.',
        column='revenue',
        term='Revenue',
        evidence=['WHERE in orders'],
    )
    fan = Finding('fan-out', 'warning', 'SUM(o.amount) repeats rows', 'Aggregate first.', column='total')
    result = SqlCheck('select 1', [finding, fan], corrected='SELECT\n  1', statements=1)
    text = str(result)
    assert text.startswith('1 error, 1 warning, 0 notes in 1 statement.\n\n')
    # A message that names its column is not led by it again.
    assert "\nerror    missing-filter  revenue computes 'Revenue' without its filter\n" in text
    assert '\nwarning  fan-out  total: SUM(o.amount) repeats rows\n' in text
    assert '\n         evidence  WHERE in orders\n' in text and text.endswith(
        'Corrected SQL\n  SELECT\n    1'
    )
    drawn = result.text(Style(colour=True, unicode=True, width=40))
    plain = re.sub(r'\x1b\[[0-9;]*m', '', drawn)
    assert drawn.startswith('\x1b[1;31m1') and '\x1b[31merror  \x1b[0m' in drawn
    assert plain.startswith('1 error, 1 warning, 0 notes in 1\nstatement.')
    assert max(len(line) for line in plain.splitlines()) <= 40
    assert str(SqlCheck('select 1', statements=1)) == 'No findings in 1 statement.'

    history = HistoryCheck(
        queries=3,
        runs=9,
        shadow=[ShadowMetric('Revenue', 'approved', 'formula-differs', 'SUM(x)', 'Use the column.', 1, 2, 1)],
        adoption=[Adoption('Revenue', 2, 7, 2)],
        notes=['Some texts were withheld.'],
    )
    text = str(history)
    assert text.startswith('3 queries checked (9 runs); 1 shadow metric, 1 of an approved measure.\n')
    assert (
        "\n  1 query by 1 person computes 'Revenue' (approved) as SUM(x)\n    rule  formula-differs\n" in text
    )
    assert text.endswith('  term     queries  runs  people\n  Revenue        2     7       2')
    assert '\x1b[32mapproved\x1b[0m' in history.text(Style(colour=True))


def test_laws_results_on_a_terminal() -> None:
    from konstruct.analysis.laws import Blocked, Failure, Law, Laws, Term

    term = Term('source.raw.orders', 'raw.orders', 'amount')
    broken = Law(
        'conservation:model.mart#total',
        'conservation',
        'model.mart',
        'mart',
        'total',
        [term],
        status='broken',
        held=2,
        checked=3,
        failures=[Failure('2026-01-31', 100, 120)],
        broken_at='model.shop.joined',
    )
    unchecked = Law(
        'count:model.mart', 'count', 'model.mart', 'mart', None, [term], status='unchecked', note='no rows'
    )
    laws = Laws(
        laws=[broken, unchecked],
        blocked=[Blocked('model.shop.joined', 'amount', 'a join no test covers')],
        warehouse='shop.duckdb',
    )
    text = str(laws)
    assert text.startswith('Data laws checked against shop.duckdb (tolerance 1e-09)\n')
    assert (
        '  broken    SUM(mart.total) = SUM(raw.orders.amount)  (2 of 3 partitions; parts at joined)' in text
    )
    assert '\n            2026-01-31: 120 against 100\n' in text
    assert '  unchecked COUNT(*) of mart = SUM(raw.orders.amount)  (no rows)' in text
    assert 'Measures with no law (1)\n  joined.amount  a join no test covers' in text
    drawn = laws.text(Style(colour=True, width=40))
    assert '\x1b[31mbroken' in drawn and all(len(ESCAPE.sub('', line)) <= 40 for line in drawn.splitlines())
    assert str(Laws()) == 'Data laws proposed from the SQL (not checked)\nNo law follows from the lineage.'
    many = Laws(blocked=[Blocked('model.m', f'c{i}', 'a filter') for i in range(25)])
    assert str(many).endswith('\n  and 5 more; the JSON lists them all')


def test_commands_in_status_values_are_picked_out() -> None:
    from konstruct.interfaces.render import _commands

    shown = _commands('not installed; run `konstruct install`', 'warn')
    assert shown.plain == 'not installed; run `konstruct install`'
    assert shown.parts[-1] == ('`konstruct install`', 'accent')
    assert _commands('a ` b', 'warn').parts == [('a ` b', 'warn')]


def test_empty_results_say_so() -> None:
    from konstruct.analysis.conflicts import Conflicts
    from konstruct.analysis.query import Definitions, Item, Listing, PathResult, QueryResult, SearchResult

    orders = Item(id='orders', kind='model', label='orders', name='orders')
    assert str(PathResult(orders, orders, [])) == 'No path between orders and orders.'
    assert str(SearchResult('zzz', [])) == "Nothing matches 'zzz'."
    assert str(Listing(['seed'], [])) == 'No seed nodes.'
    assert str(QueryResult('zzz')).startswith("Nothing in the graph matches 'zzz'.")
    assert (
        str(Definitions('zzz', column=orders))
        == 'No business term defines orders. Add one to definitions.yml.'
    )
    assert str(Conflicts(candidates=3)) == 'No conflicting definitions among 3 computed columns.'


# Review cases: plain text is whole, widths are display widths, and the work is linear


@pytest.mark.dbt
def test_piped_text_is_never_shortened(jaffle_built_graph: Path) -> None:
    from konstruct import Project

    text = str(Project(jaffle_built_graph).describe('orders'))
    assert '| returned | The order has been returned by the customer and received at the warehouse |' in text
    assert '...' not in text


def test_sql_highlighting_is_linear() -> None:
    formula = 'SUM(orders.amount * 2) + ' * 25_000 + "'x'"
    started = time.perf_counter()
    highlighted = sql(formula)
    elapsed = time.perf_counter() - started
    assert highlighted.plain == formula and len(formula) > 600_000
    assert elapsed < 3  # quadratic concatenation took about 20 s at this size


def test_wide_characters_take_two_columns() -> None:
    from konstruct.interfaces.layout import pad, width_of

    assert width_of('顧客') == 4 and width_of('naïve') == 5 and width_of('é') == 1
    assert pad('顧客', 6) == '顧客  '
    rows = [['顧客.金額', 'model'], ['orders.amount', 'model']]
    assert [line.plain for line in table(Style(), rows)] == [
        '  顧客.金額      model',
        '  orders.amount  model',
    ]
    coloured = table(Style(colour=True), rows)
    assert [line.width for line in coloured] == [len('  orders.amount  model')] * 2
    cut = Text('顧客の生涯価値').truncate(7, '…')
    assert cut.plain == '顧客の…' and cut.width == 7
    lines = wrap(Style(width=24), Text('顧客 ' * 10), first=Text('key  '), hanging=5)
    assert all(line.width <= 24 for line in lines)


def test_a_line_never_breaks_inside_a_word_where_its_colour_changes() -> None:
    body = Text('total ', ('SUM', 'keyword'), '(orders.amount)', ' and more words here')
    for width in range(30, 45):
        lines = wrap(Style(width=width), body, first=Text('key  '), hanging=5)
        assert not any(line.plain.endswith('SUM') for line in lines), width
        assert [w for line in lines for w in line.plain.split()][1:] == body.plain.split()


def test_a_long_command_breaks_between_words_and_still_runs() -> None:
    from konstruct.interfaces.layout import command

    selector = ' '.join(f'model_{i:02}' for i in range(12))
    assert [line.plain for line in command(Style(), f'dbt build --select {selector}')] == [
        f'  dbt build --select {selector}'
    ]
    lines = command(Style(width=40), f'dbt build --select {selector}')
    assert len(lines) > 1 and all(line.width <= 40 for line in lines)
    assert all(line.plain.endswith(' \\') for line in lines[:-1])
    joined = ' '.join(line.plain.removesuffix(' \\').strip() for line in lines)
    assert joined == f'dbt build --select {selector}'


def test_items_and_edges_print_as_their_rows() -> None:
    from konstruct.analysis.query import EdgeItem, Item

    amount = Item(
        id='a', kind='column', label='orders.amount', name='amount', relation_kind='model',
        data_type='DOUBLE', path='models/orders.sql', depth=1, transform='transformed', verified='confirmed',
    )  # fmt: skip
    assert str(amount) == '1  orders.amount  transformed  confirmed  model  DOUBLE  models/orders.sql'
    edge = EdgeItem('a', 'b', 'derived_from', 'orders.amount', 'stg_payments.amount', transform='direct')
    assert str(edge) == 'orders.amount  derived_from ->  stg_payments.amount  direct'


def test_status_rows_say_what_needs_attention() -> None:
    from konstruct.interfaces.status import Status

    status = Status(root=Path(), config_file=None, dbt_on_path=True, graph={'exists': False})
    states = {key: state for key, _, state in status.rows()}
    assert (states['dbt on PATH'], states['graph'], states['skills'], states['config']) == (
        'ok',
        'attention',
        'attention',
        '',
    )
    assert status.lines() == [(key, value) for key, value, _ in status.rows()]


def test_gate_and_history_summaries_are_built_from_their_parts() -> None:
    from konstruct.analysis.gate import Gate
    from konstruct.analysis.history import History

    gate = Gate(base='main', changed=1)
    assert (
        gate.summary() == f'{gate.verdict}: {gate.detail()}' == 'Gate passed: 1 changed file, nothing breaks.'
    )
    history = History(column='c', label='t.c', formula=None, path=None)
    assert history.summary() == f't.c: {history.findings()}'


@pytest.mark.dbt
def test_a_typo_is_answered_with_the_closest_names(
    jaffle_built_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from konstruct import Project
    from konstruct.analysis.query import suggest

    graph = Project(jaffle_built_graph).graph
    assert suggest(graph, 'custmers')[:2] == ['customers', 'stg_customers']
    assert suggest(graph, 'custmers.numbr_of_orders')[0] == 'customers.number_of_orders'
    assert suggest(graph, 'zzzz') == []
    assert main(['describe', 'stg_paymnts', '-C', str(jaffle_built_graph)]) == 1
    assert '  did you mean stg_payments' in capsys.readouterr().err


def test_the_spinner_restores_the_cursor_on_sigterm(clean_env: pytest.MonkeyPatch) -> None:
    import os
    import signal

    clean_env.setattr(console_module.Progress, 'DELAY', 0.0)
    console = Console()
    console.err = Terminal()
    console.err_ui = Style(colour=False, unicode=True, width=80)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as stop, console.progress('Building'):
        time.sleep(0.2)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)
    assert stop.value.code == 128 + signal.SIGTERM
    assert console.err.getvalue().endswith('\x1b[?25h')
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.parametrize('args', [['impact', '--help'], ['--help'], ['diff', '--help']])
def test_help_fits_a_narrow_terminal(
    args: list[str], clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from konstruct.interfaces.layout import width_of

    screen = Terminal()
    clean_env.setattr(sys, 'stdout', screen)
    clean_env.setenv('COLUMNS', '40')
    with pytest.raises(SystemExit):
        main(args)
    lines = [ESCAPE.sub('', line) for line in screen.getvalue().splitlines()]
    too_wide = [line for line in lines if width_of(line) > 40 and 'kodexlabsio.github.io' not in line]
    # argparse's own usage block is the only thing allowed to run over, by an option's width.
    assert all(line.strip().startswith('[') for line in too_wide), too_wide


def test_usage_errors_honour_no_color_and_name_the_commands_help(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_env.setenv('FORCE_COLOR', '1')
    with pytest.raises(SystemExit):
        main(['--no-color', 'imapct', 'orders'])
    assert '\x1b' not in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(['--no-color', 'impact', 'orders', '--bogus'])
    assert capsys.readouterr().err.splitlines() == [
        'konstruct: error: unrecognized arguments: --bogus',
        '  run konstruct impact --help for its options and examples',
    ]
