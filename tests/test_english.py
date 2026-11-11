"""Plain-English formulas: each wording means what the SQL means, two formulas never read alike,
and nothing in a sentence is absent from its formula."""

from __future__ import annotations

import random
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from konstruct import Project, QueryError
from konstruct.analysis.english import OPPOSITES, Names, Wording, bare, column_keys, words
from konstruct.interfaces.cli import main


def worded(sql: str, dialect: str = 'duckdb') -> str:
    tree = sqlglot.parse_one(sql, read=dialect)
    return Wording(dialect, Names(column_keys([tree]))).sentence(tree)


# Each wording, checked by reading it against its SQL. Brackets mark where the SQL groups.
GOLDEN = {
    # Arithmetic keeps SQL's grouping: a chain of one kind reads left to right, anything else is bracketed.
    't.a + t.b - t.c': '[a] plus [b] minus [c]',
    't.a - (t.b - t.c)': '[a] minus ([b] minus [c])',
    't.a + t.b * t.c': '[a] plus ([b] times [c])',
    '(t.a + t.b) * t.c': '([a] plus [b]) times [c]',
    't.a / 100': '[a] divided by 100',
    't.a % 2': '[a] modulo 2',
    '-t.a': 'the negative of [a]',
    "t.a || '-' || t.b": "[a] followed by '-' followed by [b]",
    # Comparisons: a value is named with `is`, two computations are compared with `equals`.
    "t.status = 'returned'": "[status] is 'returned'",
    "t.status <> 'returned'": "[status] has a value other than 'returned'",
    't.a = t.b': '[a] and [b] both have values and are equal',
    't.a <> t.b': '[a] and [b] both have values and differ',
    't.a > 0': '[a] is greater than 0',
    't.a >= 0': '[a] is at least 0',
    't.a < 0': '[a] is less than 0',
    't.a <= 0': '[a] is at most 0',
    't.a + 1 > t.b': '[a] plus 1 is greater than [b]',
    # NULL is no value.
    't.a IS NULL': '[a] has no value',
    't.a IS NOT NULL': '[a] has a value',
    "t.a IN ('x', 'y')": "[a] is one of ('x', 'y')",
    "t.a NOT IN ('x', 'y')": "[a] has a value that is not one of ('x', 'y')",
    't.a BETWEEN 1 AND 5': '[a] is between 1 and 5 inclusive',
    't.a NOT BETWEEN 1 AND 5': '[a] has a value that is not between 1 and 5 inclusive',
    "t.a LIKE 'gift%'": "[a] matches the pattern 'gift%'",
    "t.a NOT LIKE 'gift%'": "[a] has a value that does not match the pattern 'gift%'",
    "t.a ILIKE 'gift%'": "[a] matches the pattern 'gift%', ignoring case",
    # Connectives: mixing and with or is bracketed, as SQL groups it.
    't.a > 0 AND t.b > 0 AND t.c > 0': '[a] is greater than 0 and [b] is greater than 0 and [c] is greater than 0',
    't.a > 0 OR t.b > 0 AND t.c > 0': '[a] is greater than 0 or ([b] is greater than 0 and [c] is greater than 0)',
    '(t.a > 0 OR t.b > 0) AND t.c > 0': '([a] is greater than 0 or [b] is greater than 0) and [c] is greater than 0',
    'NOT (t.a > 0 OR t.b > 0)': '`NOT (t.a > 0 OR t.b > 0)`',
    # CASE: the first branch that holds wins, which `else` between branches says; no ELSE is no value.
    "CASE WHEN t.a > 100 THEN 'big' WHEN t.a > 10 THEN 'medium' ELSE 'small' END": (
        "'big' if [a] is greater than 100, else 'medium' if [a] is greater than 10, else 'small'"
    ),
    "CASE WHEN t.a > 100 THEN 'big' END": "'big' if [a] is greater than 100, else no value",
    "CASE t.method WHEN 'card' THEN 1 ELSE 0 END": "1 if [method] is 'card', else 0",
    'CASE WHEN t.a > 0 THEN t.a + t.b ELSE 0 END': '[a] plus [b] if [a] is greater than 0, else 0',
    'CASE WHEN t.a > 0 THEN CASE WHEN t.b > 0 THEN 1 ELSE 2 END ELSE 3 END': (
        '(1 if [b] is greater than 0, else 2) if [a] is greater than 0, else 3'
    ),
    'IF(t.a > 0, 1, 0)': '1 if [a] is greater than 0, else 0',
    # COALESCE and NULLIF.
    'COALESCE(t.amount, 0)': '[amount], or 0 when [amount] has no value',
    'COALESCE(t.a, t.b, 0)': 'the first of ([a], [b], 0) that has a value',
    'COALESCE(t.a + t.b, 0)': 'the first of ([a] plus [b], 0) that has a value',
    'COALESCE(t.a, 0) + 1': '([a], or 0 when [a] has no value) plus 1',
    'NULLIF(t.a, 0)': '[a], or no value when [a] is 0',
    't.b / NULLIF(t.a, 0)': '[b] divided by ([a], or no value when [a] is 0)',
    # Aggregates.
    'SUM(t.amount)': 'the sum of [amount]',
    'SUM(t.amount / 100)': 'the sum of ([amount] divided by 100)',
    'SUM(t.amount) / 100': '(the sum of [amount]) divided by 100',
    'AVG(t.amount)': 'the average of [amount]',
    'MIN(t.d)': 'the minimum of [d]',
    'MAX(t.d)': 'the maximum of [d]',
    'COUNT(*)': 'the number of rows',
    'COUNT(t.id)': 'the number of rows in which [id] has a value',
    'COUNT(DISTINCT t.id)': 'the number of distinct values of [id]',
    'SUM(DISTINCT t.a)': 'the sum of the distinct values of [a]',
    "COUNT(*) FILTER (WHERE t.status = 'paid')": "the number of rows where [status] is 'paid'",
    "SUM(t.a) FILTER (WHERE t.status = 'paid')": "the sum of [a], counting only the rows where [status] is 'paid'",
    "SUM(CASE WHEN t.payment_method = 'credit_card' THEN t.amount / 100 ELSE 0 END)": (
        "the sum of ([amount] divided by 100 if [payment method] is 'credit_card', else 0)"
    ),
    # Windows: the rows read, their order and the frame, SQL's default frame included.
    'SUM(t.a) OVER (PARTITION BY t.c)': 'the sum of [a] over the rows with the same [c]',
    'SUM(t.a) OVER ()': 'the sum of [a] over all rows',
    'SUM(t.a) OVER (PARTITION BY t.c ORDER BY t.d)': (
        'the sum of [a] over the rows with the same [c] in order of [d] (no value last), from the first row to this row and the rows tied with it'
    ),
    'SUM(t.a) OVER (PARTITION BY t.c ORDER BY t.d ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)': (
        'the sum of [a] over the rows with the same [c] in order of [d] (no value last), from 6 rows before this row to this row'
    ),
    'SUM(t.a) OVER (ORDER BY t.d ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)': (
        'the sum of [a] over all rows in order of [d] (no value last), from the first row to the last row'
    ),
    'SUM(t.a) OVER (ORDER BY t.d RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING)': (
        'the sum of [a] over all rows in order of [d] (no value last), from this row and the rows tied with it to the last row'
    ),
    'ROW_NUMBER() OVER (PARTITION BY t.c ORDER BY t.d DESC)': (
        'the position of this row, counting from one, among the rows with the same [c] in order of [d] descending (no value last)'
    ),
    'RANK() OVER (ORDER BY t.a)': (
        'the rank of this row among all rows in order of [a] (no value last), where tied rows share a rank and the next rank skips the tied rows'
    ),
    'DENSE_RANK() OVER (ORDER BY t.a)': (
        'the rank of this row among all rows in order of [a] (no value last), where tied rows share a rank with no gaps'
    ),
    'LAG(t.a) OVER (PARTITION BY t.c ORDER BY t.d)': (
        'the value of [a] in the previous row among the rows with the same [c] in order of [d] (no value last)'
    ),
    'LEAD(t.a, 2, 0) OVER (ORDER BY t.d)': (
        'the value of [a] in the row 2 rows after this one among all rows in order of [d] (no value last), or 0 where there is no such row'
    ),
    'LAST_VALUE(t.a) OVER (PARTITION BY t.c, t.e ORDER BY t.d, t.f DESC)': (
        'the value of [a] in the last of the rows with the same ([c], [e]) in order of ([d] (no value last), [f] descending (no value last)), from the first row to this row and the rows tied with it'
    ),
    # Functions.
    'ROUND(t.a, 2)': '[a] rounded to 2 decimal places',
    'ROUND(t.a / 3, 1)': '([a] divided by 3) rounded to 1 decimal place',
    'ROUND(t.a)': '[a] rounded to a whole number',
    'ABS(t.a)': 'the absolute value of [a]',
    'FLOOR(t.a)': '[a] rounded down to a whole number',
    'LOWER(t.email)': '[email] in lower case',
    'LENGTH(t.name)': 'the length of [name]',
    'CAST(t.a AS DECIMAL(10, 2))': '[a] converted to DECIMAL(10, 2)',
    'TRY_CAST(t.a AS INT)': '[a] converted to INT, or no value where it cannot be',
    "DATE_TRUNC('month', t.d)": 'the start of the month of [d]',
    'EXTRACT(year FROM t.d)': 'the year of [d]',
    "t.d + INTERVAL '7' DAY": '[d] plus 7 days',
    'CURRENT_DATE': "today's date",
    'my_udf(t.a, 1)': '`MY_UDF(t.a, 1)`',
    # Names: two columns that would read alike carry their relation.
    'o.customer_id = c.customer_id': '[customer id (o)] and [customer id (c)] both have values and are equal',
}


@pytest.mark.parametrize(('sql', 'english'), list(GOLDEN.items()))
def test_each_wording(sql: str, english: str) -> None:
    assert worded(sql) == english


@pytest.mark.parametrize(
    'sql',
    [
        't.a = NULL',  # never true: not worded as a test for no value
        '(SELECT MAX(x) FROM y)',
        't.a IN (SELECT x FROM y)',
        'NTILE(4) OVER (ORDER BY t.a)',
        'SUM(t.a) OVER (ORDER BY t.d RANGE BETWEEN 3 PRECEDING AND CURRENT ROW)',
        'MD5(t.a)',
        "t.a LIKE 'x!%' ESCAPE '!'",
    ],
)
def test_what_has_no_wording_is_quoted_as_sql(sql: str) -> None:
    tree = sqlglot.parse_one(sql, read='duckdb')
    wording = Wording('duckdb', Names(column_keys([tree])))
    text = wording.sentence(tree)
    assert wording.verbatim and all(f'`{v}`' in text for v in wording.verbatim)


def test_quoted_sql_inside_a_wording_is_only_that_part() -> None:
    tree = sqlglot.parse_one('SUM(MD5(t.a)) + 1', read='duckdb')
    wording = Wording('duckdb', Names(column_keys([tree])))
    assert wording.sentence(tree) == '(the sum of `MD5(t.a)`) plus 1'
    assert wording.verbatim == ['MD5(t.a)']


def test_words() -> None:
    assert words('customer_lifetime_value') == 'customer lifetime value'
    assert words('__x') == 'x'
    assert words('_') == '_'


# Two formulas that compute different things never read alike.

_COLUMNS = [
    exp.column('a', 't'),
    exp.column('b', 't'),
    exp.column('a', 'u'),
    exp.column('paid_or_refunded', 't'),
    exp.column('no_value', 't'),
    exp.column('a_plus_b', 't'),
    exp.column(exp.to_identifier('true', quoted=True), 't'),
    exp.column(exp.to_identifier('customer id', quoted=True), 't'),
    exp.column('customer_id', 't'),
]
_NAMES = Names(column_keys(_COLUMNS))
_DIALECTS = ('duckdb', 'postgres', 'snowflake', 'bigquery', 'tsql', 'mysql')


def _tree(rng: random.Random, depth: int) -> exp.Expr:
    if depth == 0 or rng.random() < 0.25:
        leaves: list[Callable[[], exp.Expr]] = [
            lambda: rng.choice(_COLUMNS).copy(),
            lambda: exp.Literal.number(rng.choice((0, 1, 2))),
            lambda: exp.Literal.string(rng.choice(('x', 'a plus b', 'no value'))),
            exp.Null,
            lambda: exp.Boolean(this=rng.random() < 0.5),
        ]
        return rng.choice(leaves)()

    def sub() -> exp.Expr:
        return _tree(rng, depth - 1)

    def ordered() -> exp.Ordered:
        return exp.Ordered(this=sub(), desc=rng.random() < 0.5, nulls_first=rng.random() < 0.5)

    def window(function: exp.Expr) -> exp.Window:
        partition = [sub()] if rng.random() < 0.5 else []
        order = exp.Order(expressions=[ordered() for _ in range(rng.randint(0, 2))])
        return exp.Window(this=function, partition_by=partition, order=order if order.expressions else None)

    makers: list[Callable[[], exp.Expr]] = [
        *(
            lambda k=k: k(this=sub(), expression=sub())
            for k in (exp.Add, exp.Sub, exp.Mul, exp.Mod, exp.DPipe)
        ),
        lambda: exp.Div(this=sub(), expression=sub(), typed=rng.random() < 0.5, safe=rng.random() < 0.5),
        *(lambda k=k: k(this=sub(), expression=sub()) for k in (exp.EQ, exp.NEQ, exp.GT, exp.LTE, exp.GTE)),
        lambda: exp.And(this=sub(), expression=sub()),
        lambda: exp.Or(this=sub(), expression=sub()),
        lambda: exp.Not(this=sub()),
        lambda: exp.Is(this=sub(), expression=exp.Null()),
        lambda: exp.In(this=sub(), expressions=[sub() for _ in range(rng.randint(1, 3))]),
        lambda: exp.Between(this=sub(), low=sub(), high=sub()),
        lambda: exp.Like(this=sub(), expression=exp.Literal.string('x%')),
        lambda: exp.Coalesce(this=sub(), expressions=[sub() for _ in range(rng.randint(1, 2))]),
        lambda: exp.Nullif(this=sub(), expression=sub()),
        lambda: exp.Case(
            ifs=[exp.If(this=sub(), true=sub()) for _ in range(rng.randint(1, 2))],
            default=sub() if rng.random() < 0.5 else None,
        ),
        *(lambda k=k: k(this=sub()) for k in (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count, exp.Abs)),
        lambda: exp.Count(this=exp.Distinct(expressions=[sub()])),
        lambda: exp.Neg(this=sub()),
        lambda: exp.Round(this=sub(), decimals=exp.Literal.number(rng.randint(0, 2))),
        lambda: exp.Cast(this=sub(), to=exp.DataType.build('INT')),
        lambda: exp.TryCast(this=sub(), to=exp.DataType.build('INT')),
        lambda: window(exp.Sum(this=sub())),
        lambda: window(exp.RowNumber()),
        lambda: window(exp.Lag(this=sub(), offset=exp.Literal.number(rng.randint(1, 2)))),
    ]
    return rng.choice(makers)()


def _shape(node: exp.Expr) -> str:
    """A formula's structure, with the differences that change nothing taken out: brackets, NOT of
    a comparison and its opposite, a simple CASE and its searched form, IF and CASE, an
    identifier's quotes and arguments that only say how a dialect printed it."""
    node = bare(node)
    if isinstance(node, exp.Not):
        inner = bare(node.this)
        if isinstance(inner, exp.Not):
            return _shape(inner.this)
        opposite = OPPOSITES.get(type(inner))
        if opposite is not None:
            return _shape(opposite(this=inner.this, expression=inner.expression))
    if isinstance(node, exp.Case) and node.this is not None:
        ifs = [
            exp.If(this=exp.EQ(this=node.this, expression=i.this), true=i.args['true'])
            for i in node.args['ifs']
        ]
        return _shape(exp.Case(ifs=ifs, default=node.args.get('default')))
    if isinstance(node, exp.If) and not isinstance(node.parent, exp.Case):
        return _shape(
            exp.Case(ifs=[exp.If(this=node.this, true=node.args['true'])], default=node.args.get('false'))
        )
    if isinstance(node, exp.Identifier):
        return f'Identifier({node.name})'
    parts = []
    for key, value in sorted(node.args.items()):
        if key.startswith('_') or key in ('big_int', 'is_nvl', 'is_null', 'requires_string'):
            continue
        if isinstance(node, exp.DPipe) and key == 'safe':
            continue
        values = value if isinstance(value, list) else [value]
        shown = [_shape(v) if isinstance(v, exp.Expr) else repr(v) for v in values if v not in (None, False)]
        if shown:
            parts.append(f'{key}=[{",".join(shown)}]')
    return f'{type(node).__name__}({";".join(parts)})'


def test_no_two_formulas_read_alike() -> None:
    """Random formulas, printed as SQL in six dialects and parsed back as each would read them:
    one sentence never stands for two formulas that compute different things. Where a reader
    could group a sentence in two ways, or the wording dropped something that changes the result,
    two formulas would share it."""
    rng = random.Random(1729)
    readings: dict[str, tuple[str, str]] = {}
    clashes = []
    for _ in range(4_000):
        tree = _tree(rng, 3)
        for dialect in _DIALECTS:
            try:
                sql = tree.sql(dialect=dialect, unsupported_level=ErrorLevel.RAISE)
                parsed = sqlglot.parse_one(sql, read=dialect)
            except (sqlglot.errors.SqlglotError, ValueError):
                continue
            wording = Wording(dialect, _NAMES)
            text = wording.sentence(parsed)
            if wording.verbatim:
                continue
            shape = _shape(parsed)
            first = readings.setdefault(text, (shape, f'{dialect}: {sql}'))
            if first[0] != shape:
                clashes.append((text, first[1], f'{dialect}: {sql}'))
    assert len(readings) > 4_000
    assert clashes == []


@pytest.mark.parametrize(
    ('dialect', 'one', 'other'),
    [
        ('duckdb', 'paid_or_refunded', 'paid OR refunded'),
        ('duckdb', 'a_plus_b', 'a + b'),
        ('duckdb', 'no_value', 'NULL'),
        ('duckdb', '"true"', 'TRUE'),
        ('duckdb', 'customer_id', '"customer id"'),
        ('duckdb', 'ROW_NUMBER() OVER (ORDER BY x)', 'ROW_NUMBER() OVER (ORDER BY x NULLS FIRST)'),
        ('duckdb', 'ROW_NUMBER() OVER (ORDER BY x DESC)', 'ROW_NUMBER() OVER (ORDER BY x DESC NULLS FIRST)'),
        ('bigquery', 'ROW_NUMBER() OVER (ORDER BY x)', 'ROW_NUMBER() OVER (ORDER BY x NULLS LAST)'),
        ('postgres', 'a / b', 'a::float / b'),
        ('duckdb', 'a / b * c', 'a / (b * c)'),
        ('duckdb', 'SUM(x) + 1', 'SUM(x + 1)'),
        ('duckdb', '-a - b', '-(a - b)'),
    ],
)
def test_formulas_that_differ_read_differently(dialect: str, one: str, other: str) -> None:
    def read(sql: str) -> str:
        tree = sqlglot.parse_one(sql, read=dialect)
        names = Names(column_keys([sqlglot.parse_one(f'{one} + {other}', read=dialect)]))
        return Wording(dialect, names).sentence(tree)

    assert read(one) != read(other)


def test_postgres_and_duckdb_division_read_differently() -> None:
    """Postgres divides whole numbers as whole numbers; DuckDB does not."""
    assert (
        worded('a / b', 'postgres') == '[a] divided by [b], rounded towards zero when both are whole numbers'
    )
    assert worded('a / b', 'duckdb') == '[a] divided by [b]'
    assert worded('a / b', 'mysql') == '[a] divided by [b], with no value where [b] is 0'
    assert worded('SAFE_DIVIDE(a, b)', 'bigquery') == '[a] divided by [b], with no value where [b] is 0'


# Nothing invented: what a sentence names is in its formula, and what the formula holds is named.

_QUOTED = re.compile(r"'(?:[^']|'')*'")


@pytest.mark.parametrize('sql', list(GOLDEN))
def test_a_sentence_names_only_what_its_formula_holds(sql: str) -> None:
    tree = sqlglot.parse_one(sql, read='duckdb')
    names = Names(column_keys([tree]))
    wording = Wording('duckdb', names)
    text = wording.sentence(tree)
    # An interval's amount is a string in SQL (INTERVAL '7' DAY) and a number in English (7 days).
    strings = sorted(
        n.sql() for n in tree.find_all(exp.Literal) if n.is_string and not isinstance(n.parent, exp.Interval)
    )
    numbers = {str(n.this) for n in tree.find_all(exp.Literal)}
    rest = text
    for quoted in wording.verbatim:
        rest = rest.replace(f'`{quoted}`', '')
    if not wording.verbatim:
        assert sorted(_QUOTED.findall(rest)) == strings
    rest = _QUOTED.sub('', rest)
    for number in re.findall(r'(?<![\w.])\d+(?:\.\d+)?(?![\w.])', re.sub(r'\[[^\]]*\]', '', rest)):
        assert number in numbers, f'{number} is not in {sql}'
    for column in tree.find_all(exp.Column):
        assert names(column) in text or wording.verbatim
    assert {n.column for n in names.legend()} == {
        '.'.join(p.name for p in c.parts) for c in tree.find_all(exp.Column)
    }


# Columns of a project


@pytest.fixture
def shop(tmp_path: Path) -> Project:
    """A small SQL project with no definitions.yml: a filter, a join and a grouping."""
    (tmp_path / 'ltv.sql').write_text(
        'create table main.ltv as\n'
        'select c.customer_id, coalesce(sum(o.amount - o.refund), 0) as lifetime_value\n'
        'from raw.customers c left join raw.orders o on o.customer_id = c.customer_id\n'
        "where o.status <> 'void'\n"
        'group by c.customer_id\n'
    )
    project = Project(tmp_path)
    project.build(warehouses=False, run_dbt=False)
    return project


def test_a_column_of_a_project_with_no_definitions(shop: Project) -> None:
    result = shop.english('ltv.lifetime_value')
    assert result.formula == 'COALESCE(SUM(orders.amount - orders.refund), 0)'
    assert result.english == 'the first of (the sum of ([amount] minus [refund]), 0) that has a value'
    assert result.sentence == (
        'ltv.lifetime_value is the first of (the sum of ([amount] minus [refund]), 0) that has a value.'
    )
    assert result.rows[0].relation == 'ltv'
    assert sorted(result.rows[0].english) == [
        'rows are grouped by [customer id (customers)], one row for each distinct value',
        'rows are joined where [customer id (orders)] and [customer id (customers)] both have values and are '
        'equal, keeping every row of the left side, with no values from the right side where it has no match',
        "rows are kept only where [status] has a value other than 'void'",
    ]
    assert [n.to_dict() for n in result.names] == [
        {'wording': '[amount]', 'column': 'orders.amount'},
        {'wording': '[customer id (customers)]', 'column': 'customers.customer_id'},
        {'wording': '[customer id (orders)]', 'column': 'orders.customer_id'},
        {'wording': '[refund]', 'column': 'orders.refund'},
        {'wording': '[status]', 'column': 'orders.status'},
    ]
    assert result.exact and result.terms == [] and result.grain is None
    data = result.to_dict()
    assert data['english'] == result.english and data['rows'][0]['relation'] == 'ltv'
    text = str(result)
    assert text.startswith('In plain English ltv.lifetime_value\n  = COALESCE(')
    assert 'Names (5)' in text


def test_a_source_column_has_no_formula(shop: Project) -> None:
    result = shop.english('customers.customer_id')
    assert result.formula is None and result.english is None and result.sentence is None
    assert 'is read from a source' in str(result)


def test_only_a_column_is_explained_in_english(shop: Project) -> None:
    with pytest.raises(QueryError, match='takes a column'):
        shop.english('ltv')


def test_terms_and_their_filters_are_worded(tmp_path: Path) -> None:
    (tmp_path / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) as total from raw.orders group by 1'
    )
    (tmp_path / 'definitions.yml').write_text(
        'terms:\n'
        '  - name: Lifetime value\n'
        '    definition: What a customer has paid.\n'
        '    status: approved\n'
        '    approved: 2026-01-01\n'
        '    owner: {name: Grace}\n'
        '    grain: one row per customer\n'
        '    unit: currency\n'
        '    currency: AUD\n'
        '    columns: [main.ltv.total]\n'
        '    filters: ["orders.status <> \'returned\'"]\n'
    )
    project = Project(tmp_path)
    project.build(warehouses=False, run_dbt=False)
    result = project.english('ltv.total')
    assert result.sentence == 'Lifetime value (ltv.total) is the sum of [amount].'
    assert result.grain == "one row per customer, as the term 'Lifetime value' states"
    (term,) = result.terms
    assert (term.status, term.owner, term.unit, term.currency) == ('approved', 'Grace', 'currency', 'AUD')
    assert term.english == ["[status] has a value other than 'returned'"]
    assert {n.wording: n.column for n in result.names}['[status]'] == 'orders.status'
    text = str(result)
    assert "term 'Lifetime value'  approved, owner Grace, unit currency (AUD)" in text
    assert "requires [status] has a value other than 'returned'" in text


@pytest.mark.dbt
def test_the_example(jaffle_built_graph: Path, tmp_path: Path) -> None:
    copy = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph / 'konstruct-out', copy / 'konstruct-out')
    project = Project(copy)
    result = project.english('orders.credit_card_amount')
    assert result.english == (
        "the sum of ([amount] divided by 100 if [payment method] is 'credit_card', else 0)"
    )
    assert {n.wording: n.column for n in result.names}['[payment method]'] == 'raw_payments.payment_method'
    assert (
        result.grain == 'one row per order: orders.order_id is its key, by the unique test on orders.order_id'
    )
    ltv = project.english('customers.customer_lifetime_value')
    assert ltv.sentence == (
        'Customer lifetime value (customers.customer_lifetime_value) is the sum of ([amount] divided by 100).'
    )
    assert ltv.terms[0].name == 'Customer lifetime value' and ltv.terms[0].currency == 'AUD'
    reported = project.english('customer_ltv.lifetime_value')
    assert reported.rows[0].english == [
        'rows are kept only where [customer lifetime value] is greater than 0'
    ]


def test_explain_english_on_the_command_line(shop: Project, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    assert main(['--no-color', '-q', '-C', str(shop.path), 'explain', 'ltv.lifetime_value', '--english']) == 0
    text = capsys.readouterr().out
    assert text.startswith('Formula of ltv.lifetime_value')
    assert (
        'In plain English\n  ltv.lifetime_value is the first of (the sum of ([amount] minus [refund]), 0)'
        in text
    )
    assert main(['-q', '-C', str(shop.path), 'explain', 'ltv.lifetime_value', '--english', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['composed'] == 'COALESCE(SUM(orders.amount - orders.refund), 0)'
    assert {'wording': '[refund]', 'column': 'orders.refund'} in data['english']['names']


@pytest.mark.parametrize(
    'sql',
    [
        't.*',
        't.a IS TRUE',
        'INTERVAL (t.n) DAY',
        't.a BETWEEN SYMMETRIC 1 AND 2',
        "t.a NOT LIKE 'x!%' ESCAPE '!'",
        't.a NOT IN (SELECT x FROM y)',
        'NOT t.a BETWEEN SYMMETRIC 1 AND 2',
        'CASE WHEN 1 = 1 THEN 1 END + COUNT(DISTINCT t.a, t.b)',
        'SUM(DISTINCT t.a, t.b)',
        'COUNT(t.a, t.b)',
        "STRING_AGG(t.a, ',') FILTER (WHERE t.b > 0)",
        'ROUND(t.a, 2, 1)',
        'ROUND(t.a, t.b)',
        "STRPTIME(t.a, '%Y')::DATE",
        "CAST(t.a AS DATE FORMAT 'YYYY')",
        'DATE_TRUNC(t.unit, t.d)',
        'SUM(t.a) OVER w',
        'ROW_NUMBER() OVER (ORDER BY t.a ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)',
        'RANK() OVER (PARTITION BY t.a)',
        'LAG(t.a) OVER (ORDER BY t.b ROWS UNBOUNDED PRECEDING)',
        'LAG(t.a, t.n) OVER (ORDER BY t.b)',
        'SUM(t.a) OVER (ORDER BY t.b GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW)',
        'SUM(t.a) OVER (ORDER BY t.b ROWS BETWEEN CURRENT ROW AND 2 FOLLOWING EXCLUDE TIES)',
        'MEDIAN(t.a) OVER (ORDER BY t.b)',
        'SUM(MD5(t.a)) OVER ()',
    ],
)
def test_more_that_has_no_wording(sql: str) -> None:
    tree = sqlglot.parse_one(sql, read='duckdb')
    wording = Wording('duckdb', Names(column_keys([tree])))
    text = wording.sentence(tree)
    assert wording.verbatim and all(f'`{v}`' in text for v in wording.verbatim)


@pytest.mark.parametrize(
    ('sql', 'english'),
    [
        ('TRUE', 'true'),
        ('FALSE AND t.a', 'false and [a]'),
        ('COUNT_IF(t.a > 0)', 'the number of rows where [a] is greater than 0'),
        (
            'SUM(t.a) OVER (ORDER BY t.b ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING)',
            (
                'the sum of [a] over all rows in order of [b] (no value last), from this row to 1 row after this row'
            ),
        ),
        (
            'FIRST_VALUE(t.a) OVER (PARTITION BY t.c)',
            'the value of [a] in the first of the rows with the same [c]',
        ),
        (
            'LAG(t.a, 1) OVER (ORDER BY t.b)',
            'the value of [a] in the previous row among all rows in order of [b] (no value last)',
        ),
        (
            'LEAD(t.a) OVER (ORDER BY t.b)',
            'the value of [a] in the next row among all rows in order of [b] (no value last)',
        ),
        (
            'LAG(t.a, 3) OVER (ORDER BY t.b)',
            'the value of [a] in the row 3 rows before this one among all rows in order of [b] (no value last)',
        ),
        ('COUNT(*) OVER (PARTITION BY t.c)', 'the number of rows over the rows with the same [c]'),
        ('CEIL(t.a)', '[a] rounded up to a whole number'),
        ('UPPER(t.a)', '[a] in upper case'),
        ("INTERVAL '1' MONTH", '1 month'),
        ('CURRENT_TIMESTAMP', 'the current time'),
        ('my_now()', '`MY_NOW()`'),
        ('t.a // 2', '`t.a // 2`'),
    ],
)
def test_the_rarer_wordings(sql: str, english: str) -> None:
    assert worded(sql) == english


@pytest.mark.parametrize(
    ('condition', 'english'),
    [
        ('HAVING: SUM(t.a) > 0', 'groups are kept only where the sum of [a] is greater than 0'),
        (
            'QUALIFY: ROW_NUMBER() OVER (ORDER BY t.a) = 1',
            (
                'rows are kept only where (the position of this row, counting from one, among all rows in order of [a] (no value last)) is 1, after the window functions'
            ),
        ),
        (
            'JOIN ON: t.a = u.a',
            (
                'rows are joined where [a (t)] and [a (u)] both have values and are equal, and a row with no match on the other side is left out'
            ),
        ),
        (
            'FULL JOIN ON: t.a = u.a',
            (
                'rows are joined where [a (t)] and [a (u)] both have values and are equal, keeping every row of both sides, with no values from the other side where a row has no match'
            ),
        ),
        (
            'LEFT JOIN WHERE: u.b > 0',
            'on the side a left join may find no match in, rows are joined only where [b] is greater than 0',
        ),
        ('GROUP BY: t.a, t.b', 'rows are grouped by ([a], [b]), one row for each distinct combination'),
        ('DISTINCT: t.a, t.b', 'repeated rows are kept once'),
        ('DISTINCT ON: t.a', 'one row is kept for each distinct [a], the first in the order given'),
        (
            'DISTINCT ON: t.a, t.b',
            'one row is kept for each distinct ([a], [b]), the first in the order given',
        ),
        ('ORDER BY: t.a DESC', 'rows are taken in order of [a] descending (no value last)'),
        ('ORDER BY: t.a, t.b', 'rows are taken in order of ([a] (no value last), [b] (no value last))'),
        ('LIMIT: 10', 'at most 10 rows'),
        ('OFFSET: 5', 'the first 5 rows are skipped'),
        ('UNION: DISTINCT', 'the rows of each part combined, with repeated rows kept once'),
        (
            'WHERE BEFORE WINDOW, LIMIT: t.a > 0',
            ('rows are kept only where [a] is greater than 0 (before the window functions and the limit)'),
        ),
        ('INTERSECT: ALL', '`INTERSECT: ALL`'),
        ('ROWS FROM: range(3)', '`ROWS FROM: range(3)`'),
        ('WHERE: select from where', '`WHERE: select from where`'),
        ('LIMIT: t.n', '`LIMIT: t.n`'),
    ],
)
def test_each_condition(condition: str, english: str) -> None:
    from sqlglot.dialects.dialect import Dialect

    from konstruct.analysis.english import Condition, word_condition

    parsed = Condition(condition, Dialect.get_or_raise('duckdb'))
    wording = Wording('duckdb', Names(column_keys(parsed.trees)))
    assert word_condition(parsed, wording) == english
    assert bool(wording.verbatim) == english.startswith('`')


def test_an_unparsed_formula_and_filter_are_quoted(tmp_path: Path) -> None:
    from konstruct.analysis.english import english
    from konstruct.core.graph import Graph, Node

    graph = Graph()
    graph.add_node(Node(id='t', kind='table', name='t', relation='main.t'))
    graph.add_node(
        Node(id='t#x', kind='column', name='x', parent='t', attrs={'expression': 'select from where'})
    )
    graph.add_node(Node(id='term.x', kind='term', name='X', attrs={'status': 'draft', 'filters': ['nope (']}))
    graph.add_edge('term.x', 't#x', 'defines')
    result = english(graph, graph.nodes['t#x'])
    assert result.english == '`select from where`' and result.sentence is None
    assert result.terms[0].english == ['`nope (`']
    assert result.verbatim == ['select from where', 'nope (']
    assert 'Quoted as SQL (2)' in str(result)
    assert result.terms[0].to_dict() == {
        'name': 'X',
        'status': 'draft',
        'filters': ['nope ('],
        'english': ['`nope (`'],
    }


@pytest.mark.parametrize(
    'sql',
    [
        't.x = (NULL)',
        't.x <> CAST(NULL AS INT)',
        't.x IN (1, NULL)',
        't.x NOT IN (1, NULL)',
        't.x NOT IN (1, t.y)',  # a column in the list may have no value, and then no row is kept
        't.x NOT BETWEEN 1 AND t.y',
        't.x NOT LIKE t.pattern',
        'NOT t.flag',
        'NOT (t.a > 0 OR t.b > 0)',
        'EXTRACT(epoch FROM t.d)',
        'EXTRACT(dow FROM t.d)',
        "DATE_TRUNC('decade', t.d)",
        "t.d + INTERVAL '1.5' DAY",
        'ROUND(t.x, 1.5)',
        'ROUND(t.x, -1)',
        'LAG(t.x, 0) OVER (ORDER BY t.d)',
        'my_udf(t.x)',
        't.a // t.b',
    ],
)
def test_what_could_be_misread_is_quoted(sql: str) -> None:
    tree = sqlglot.parse_one(sql, read='duckdb')
    wording = Wording('duckdb', Names(column_keys([tree])))
    text = wording.sentence(tree)
    assert wording.verbatim and all(f'`{v}`' in text for v in wording.verbatim), text


@pytest.mark.parametrize(
    ('dialect', 'sql'),
    [('snowflake', 'SYSDATE()'), ('snowflake', 'CURRENT_TIMESTAMP(3)'), ('bigquery', 'CURRENT_DATE("UTC")')],
)
def test_clock_arguments_are_not_dropped(dialect: str, sql: str) -> None:
    tree = sqlglot.parse_one(sql, read=dialect)
    wording = Wording(dialect, Names([]))
    assert wording.sentence(tree).startswith('`') and wording.verbatim


@pytest.mark.parametrize(
    ('sql', 'english'),
    [
        ('NOT t.x > 1', '[x] is at most 1'),
        ('NOT t.x = 1', '[x] has a value other than 1'),
        ('NOT t.x <> t.y', '[x] and [y] both have values and are equal'),
        ('NOT NOT t.x > 1', '[x] is greater than 1'),
        (
            "CASE WHEN t.m <> 'gift_card' THEN t.amount ELSE 0 END",
            "[amount] if [m] has a value other than 'gift_card', else 0",
        ),
    ],
)
def test_negations(sql: str, english: str) -> None:
    assert worded(sql) == english


def test_negated_wordings_hold_where_sql_does() -> None:
    """Each negated test, evaluated by DuckDB for values with and without a value, holds exactly
    where its English says: where the value is there and the positive test fails."""
    import duckdb

    con = duckdb.connect()
    readings: dict[str, tuple[str, Callable[[str | None], bool]]] = {
        "x <> 'a'": ("[x] has a value other than 'a'", lambda x: x is not None and x != 'a'),
        "x NOT IN ('a', 'b')": (
            "[x] has a value that is not one of ('a', 'b')",
            lambda x: x is not None and x not in 'ab',
        ),
        "x NOT LIKE 'a%'": (
            "[x] has a value that does not match the pattern 'a%'",
            lambda x: x is not None and not x.startswith('a'),
        ),
        "x NOT BETWEEN 'a' AND 'b'": (
            "[x] has a value that is not between 'a' and 'b' inclusive",
            lambda x: x is not None and not 'a' <= x <= 'b',
        ),
        'x IS NOT NULL': ('[x] has a value', lambda x: x is not None),
    }
    for sql, (english, holds) in readings.items():
        assert worded(sql) == english
        for value in (None, 'a', 'b', 'abc', 'c'):
            row = con.execute(f'SELECT ({sql}) IS TRUE FROM (SELECT ?::VARCHAR AS x)', [value]).fetchone()
            assert row is not None and row[0] == holds(value), (sql, value)
    con.close()


def test_an_unknown_function_makes_the_result_inexact(shop: Project) -> None:
    (shop.root / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, my_udf(sum(amount)) as lifetime_value from raw.orders '
        'group by customer_id\n'
    )
    shop.build(warehouses=False, run_dbt=False)
    result = shop.english('ltv.lifetime_value')
    assert result.verbatim == ['MY_UDF(SUM(orders.amount))'] and not result.exact


def test_a_true_or_false_column_says_what_it_is_without_a_value(tmp_path: Path) -> None:
    (tmp_path / 'flags.sql').write_text(
        'create table main.flags as select id, a <> b as changed, a is null as missing, '
        'a > 0 and b is null as mixed, a + 1 as plain from raw.items\n'
    )
    project = Project(tmp_path)
    project.build(warehouses=False, run_dbt=False)
    assert project.english('flags.changed').sentence == (
        'flags.changed is whether [a] and [b] both have values and differ; where a value it tests has none, '
        'it can have no value rather than be true or false.'
    )
    assert project.english('flags.missing').sentence == 'flags.missing is whether [a] has no value.'
    assert project.english('flags.mixed').sentence == (
        'flags.mixed is whether [a] is greater than 0 and [b] has no value; where a value it tests has none, '
        'it can have no value rather than be true or false.'
    )
    assert project.english('flags.plain').sentence == 'flags.plain is [a] plus 1.'


def test_the_unknown_case_holds_where_the_sentence_says() -> None:
    """A test of a value with none gives no value, not false, as the sentence warns."""
    import duckdb

    con = duckdb.connect()
    row = con.execute(
        "SELECT (x <> 'a') IS NULL, (x IS NULL) IS NULL FROM (SELECT NULL::VARCHAR AS x)"
    ).fetchone()
    con.close()
    assert row == (True, False)


@pytest.mark.parametrize(
    'sql',
    ['"x (t)" + t.x + u.x', 'customer_id + "customer id"', '"t.x" + t.x', 'x + "X"', 't.a + u.a + "a (t)"'],
)
def test_no_two_columns_share_a_wording(sql: str) -> None:
    tree = sqlglot.parse_one(sql, read='duckdb')
    names = Names(column_keys([tree]))
    wordings = [n.wording for n in names.legend()]
    assert len(wordings) == len(set(wordings)) == len(set(column_keys([tree])))
