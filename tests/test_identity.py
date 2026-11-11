"""Formula identity: whether two formulas compute the same thing, however they are written."""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import cast

import duckdb
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from konstruct.analysis.identity import (
    Formulas,
    Verdict,
    Witness,
    column_fingerprints,
    equivalent,
    fingerprint,
)
from konstruct.core.graph import Graph, Node

# The same whatever the data, NULLs and empty tables included.
SAME = [
    ('SUM(3 * a)', '3 * SUM(a)'),
    ('AVG(a)', 'SUM(a) / COUNT(a)'),
    ('COUNT(1)', 'COUNT(*)'),
    ('(a + b) * (a - b)', 'a * a - b * b'),
    ('a / c + b / c', '(a + b) / c'),
    ('1.5 * a', '3 * a / 2'),
    ('0.1 + 0.2', '0.3'),
    ('a - (b - c)', 'a - b + c'),
    ('CASE WHEN x > 0 THEN a ELSE b END + d', 'CASE WHEN 0 < x THEN a + d ELSE b + d END'),
    ('CASE x WHEN 1 THEN a END', 'CASE WHEN 1 = x THEN a END'),
    ('IF(x > 0, a, b)', 'CASE WHEN x > 0 THEN a ELSE b END'),
    ('NOT a > b', 'a <= b'),
    ('NOT NOT a = b', 'b = a'),
    ('NOT (NOT (a > b))', 'b < a'),
    ('NOT NOT (p AND q)', 'q AND p'),
    ('NOT NOT TRUE', 'TRUE'),
    ('a BETWEEN 1 AND 2', 'a >= 1 AND 2 >= a'),
    ('a BETWEEN SYMMETRIC 2 AND 1', 'a BETWEEN 1 AND 2 OR a BETWEEN 2 AND 1'),
    ('a IN (1, 2)', 'a = 2 OR a = 1'),
    ('(p AND q) AND r', 'r AND (q AND p)'),
    ('COALESCE(a + b, 0)', 'COALESCE(b + a, 0)'),
    ('COALESCE(NULL, a)', 'a'),
    ('IFNULL(a, 0)', 'COALESCE(a, 0)'),
    ('GREATEST(a, b)', 'GREATEST(b, a)'),
    ('POWER(a, 2)', 'a * a'),
    ("SUM(a) FILTER (WHERE s = 'x')", "SUM(CASE WHEN 'x' = s THEN a END)"),
    ("COUNT(*) FILTER (WHERE s = 'x')", "COUNT(CASE WHEN s = 'x' THEN 1 END)"),
    (
        "AVG(CASE WHEN s = 'x' THEN a END)",
        "SUM(CASE WHEN s = 'x' THEN a END) / COUNT(CASE WHEN s = 'x' THEN a END)",
    ),
    ('SUM(a) OVER (PARTITION BY b)', 'SUM(a) OVER (PARTITION BY b)'),
    ('MIN(a + b)', 'MIN(b + a)'),
    # Where the CASE is not NULL, both are a + 1.
    ('CASE WHEN c > 0 THEN a END + 1', 'CASE WHEN c > 0 THEN a + 1 END'),
    ('2 * CASE WHEN c > 0 THEN NULL ELSE a END', 'CASE WHEN c > 0 THEN NULL ELSE a + a END'),
]

# The same only when no source value is NULL and every group has rows.
SAME_WITHOUT_NULLS = [
    ('SUM(a) - SUM(b)', 'SUM(a - b)'),
    ('SUM(price * quantity) - SUM(discount)', 'SUM(quantity * price - discount)'),
    ('AVG(a)', 'SUM(a) / COUNT(*)'),
    ('SUM(a) / COUNT(*)', 'SUM(a) / COUNT(a)'),
    ('SUM(1)', 'COUNT(*)'),
    ('a - a', '0'),
    ('COALESCE(a, 0)', 'a'),
    ("COUNT(*) FILTER (WHERE s = 'x')", "SUM(CASE WHEN s = 'x' THEN 1 ELSE 0 END)"),
]

DIFFERENT = [
    ('SUM(a * b)', 'SUM(a) * SUM(b)', 'values'),
    ('a + b', 'a * b', 'values'),
    ('MIN(a)', 'MAX(a)', 'functions'),
    ('CASE WHEN x > 0 THEN a ELSE b END', 'CASE WHEN NOT x > 0 THEN b ELSE a END', 'functions'),
    ('CASE WHEN x > 0 THEN a ELSE b END', 'CASE WHEN x > 0 THEN b ELSE a END', 'values'),
    ('CASE WHEN c THEN a END', 'CASE WHEN c THEN a ELSE 0 END', 'values'),
    ("SUM(CASE WHEN s = 'x' THEN a END)", "SUM(CASE WHEN s = 'x' THEN a ELSE 0 END)", 'values'),
    ('SUM(a) OVER (PARTITION BY b)', 'SUM(a) OVER (PARTITION BY c)', 'functions'),
    ('COUNT(DISTINCT a)', 'COUNT(a)', 'functions'),
    ('a BETWEEN SYMMETRIC 2 AND 1', 'a BETWEEN 2 AND 1', 'functions'),
    # NOT NOT cancels only around a predicate: in MySQL NOT NOT 5 is 1.
    ('NOT NOT a', 'a', 'functions'),
    # Each call of a volatile function gives a value of its own.
    ('RANDOM() + RANDOM()', '2 * RANDOM()', 'functions'),
]


@pytest.mark.parametrize(('a', 'b'), SAME)
def test_formulas_written_differently_are_the_same(a: str, b: str) -> None:
    verdict = equivalent(a, b)
    assert verdict.equal, verdict
    assert verdict.equal_without_nulls
    assert 0 < verdict.error_bound < 1e-30
    assert verdict.witness is None
    assert fingerprint(a) == fingerprint(b)
    assert str(verdict).startswith('The same (error at most ')


@pytest.mark.parametrize(('a', 'b'), SAME_WITHOUT_NULLS)
def test_formulas_the_same_only_without_nulls(a: str, b: str) -> None:
    verdict = equivalent(a, b)
    assert not verdict.equal and verdict.equal_without_nulls, verdict
    assert verdict.witness is not None and verdict.witness.kind == 'values'
    assert 'NULL' in str(verdict.witness) or verdict.witness.rows == []
    head = 'Not shown to be the same' if verdict.witness.assumed else 'Different'
    assert str(verdict).startswith(
        f'{head}, but the same unless a source value is NULL or there are no rows: '
    )
    assert fingerprint(a) != fingerprint(b)
    columns = ['a', 'b', 'price', 'quantity', 'discount', 's']
    assert equivalent(a, b, not_null=columns).equal_without_nulls


@pytest.mark.parametrize(('a', 'b', 'kind'), DIFFERENT)
def test_formulas_that_differ_say_why(a: str, b: str, kind: str) -> None:
    verdict = equivalent(a, b)
    assert not verdict.equal and not verdict.equal_without_nulls
    assert verdict.error_bound == 0
    assert verdict.witness is not None
    assert verdict.witness.kind == kind, verdict
    assert fingerprint(a) != fingerprint(b)


def test_columns_known_not_to_be_null() -> None:
    columns = ['a', 'b']
    assert equivalent('SUM(a) - SUM(b)', 'SUM(a - b)', not_null=columns).equal
    assert equivalent('a - a', '0', not_null=columns).equal
    assert fingerprint('SUM(a - b)', not_null=columns) == fingerprint('SUM(a) - SUM(b)', not_null=columns)


def _run(sql: list[str], rows: list[dict[str, str]] | None, inputs: dict[str, str]) -> list[str]:
    """The formulas' results in DuckDB over the witness's table, as the witness shows them."""
    con = duckdb.connect()
    con.execute('create table t (a bigint, b bigint, c bigint, x bigint)')

    def value(values: dict[str, str], name: str) -> int | None:
        text = values.get(name, '0')
        return None if text == 'NULL' else int(text)

    table = rows if rows is not None else [inputs]
    if table:
        con.executemany(
            'insert into t values (?, ?, ?, ?)', [tuple(value(r, n) for n in 'abcx') for r in table]
        )
    found = con.execute(f'select {", ".join(sql)} from t').fetchall()
    return ['NULL' if v is None else str(v) for v in found[0]] if found else []


_COMPARED = re.compile(r'([abcx]) > (-?\d+)|(-?\d+) > ([abcx])')


def _expressed(values: dict[str, str]) -> dict[str, str] | None:
    """The values a witness gives, with each assumed comparison of a column with a constant
    given as a value of the column that makes it so: for ``c > k`` k + 1 when true and k when
    false, for ``k > c`` k - 1 and k, and NULL for NULL. None when an assumption cannot be
    expressed so, or two disagree on a column."""
    columns = {name: value for name, value in values.items() if name in 'abcx'}
    for name, value in values.items():
        if name in columns:
            continue
        found = _COMPARED.fullmatch(name)
        if found is None:
            return None
        column, constant, above = (found[1], int(found[2]), 1) if found[1] else (found[4], int(found[3]), -1)
        made = {'true': str(constant + above), 'false': str(constant), 'NULL': 'NULL'}[value]
        if columns.setdefault(column, made) != made:
            return None
    return columns


def _agrees(shown: list[str], witness: Witness) -> bool:
    """Whether DuckDB's results are the witness's, a double compared with the exact value."""
    exact = [witness.a, witness.b]
    if len(shown) != 2:
        return False
    for got, want in zip(shown, exact, strict=True):
        if got == 'NULL' or want == 'NULL' or want is None:
            if got != want:
                return False
        elif abs(float(got) - float(Fraction(want))) > 1e-9 * max(1.0, abs(float(Fraction(want)))):
            return False
    return True


@pytest.mark.parametrize(
    ('a', 'b'),
    [
        # Each was called the same while constants wrapped modulo 2^61 - 1.
        ('a * 1073741824 * 2147483648', 'a'),
        ('a * POWER(2, 61)', 'a'),
        ('a * 0.5', 'a * 1152921504606846976'),
        ('a * 2305843009213693951', '0'),
        # Each was called the same while a NULL from a CASE was a number.
        ('CASE WHEN c > 0 THEN a END * 0', '0'),
        ('SUM(b + CASE WHEN c > 0 THEN a END - CASE WHEN c > 0 THEN a END)', 'SUM(b)'),
        ('COALESCE(MAX(CASE WHEN c > 0 THEN a END) * 0, 5)', 'COALESCE(0, 5)'),
        # And these while every value was assumed present.
        ('AVG(a)', 'SUM(a) / COUNT(*)'),
        ('SUM(CASE WHEN c > 0 THEN a END)', 'SUM(CASE WHEN c > 0 THEN a ELSE 0 END)'),
        ('SUM(1)', 'COUNT(*)'),
        # And these while the presence of a sum or product kept whole and that of a COALESCE
        # of the same operands were one atom.
        ('(COALESCE(a, b) + COALESCE(c, x)) * 0', 'COALESCE(COALESCE(a, b), COALESCE(c, x)) * 0'),
        ('COALESCE(a, b) * COALESCE(c, x) * 0', 'COALESCE(COALESCE(a, b), COALESCE(c, x)) * 0'),
    ],
)
def test_pairs_duckdb_tells_apart_are_different(a: str, b: str) -> None:
    verdict = equivalent(a, b, dialect='duckdb')
    assert not verdict.equal
    witness = verdict.witness
    assert witness is not None
    if witness.kind == 'values':
        # A value given to a comparison of a column with a constant is a value of the column; a
        # value given to any other expression may be one it never takes, and is not run.
        inputs = _expressed(witness.inputs)
        rows = None if witness.rows is None else [_expressed(row) for row in witness.rows]
        if inputs is not None and (rows is None or all(row is not None for row in rows)):
            shown = _run([a, b], cast('list[dict[str, str]] | None', rows), inputs)
            assert _agrees(shown, witness), (a, b, witness)
            assert witness.a != witness.b
    # NULL itself is never an input given a value.
    assert 'NULL' not in witness.inputs


def test_a_witness_gives_values_that_show_the_difference() -> None:
    verdict = equivalent('SUM(a * b)', 'SUM(a) * SUM(b)', not_null=['a', 'b'])
    witness = verdict.witness
    assert witness is not None and witness.kind == 'values' and witness.rows is not None
    assert len(witness.rows) >= 2 and not witness.inputs
    assert _run(['SUM(a * b)', 'SUM(a) * SUM(b)'], witness.rows, {}) == [witness.a, witness.b]
    assert witness.a != witness.b
    assert str(verdict).startswith(f'Different: over {len(witness.rows)} rows (a = ')
    assert verdict.to_dict()['witness']['rows'] == witness.rows
    # NULL as a value, and a table with no rows.
    verdict = equivalent('SUM(1)', 'COUNT(*)')
    assert verdict.witness is not None and verdict.witness.rows == []
    assert str(verdict.witness) == 'over no rows: NULL against 0'
    assert verdict.to_dict()['witness']['rows'] == []
    # Fractions are exact; a shared uninterpreted value is an input like a column.
    verdict = equivalent('ROUND(a) / 3', 'ROUND(a) / 4')
    assert verdict.witness is not None
    [(name, value)] = verdict.witness.inputs.items()
    assert name == 'ROUND(a)'
    a, b = verdict.witness.a, verdict.witness.b
    assert a is not None and b is not None and a != b
    # ROUND(a) may never take the value it is given, so this is no proof.
    assert verdict.witness.assumed == ['ROUND(a)']
    assert str(verdict) == (
        f'Not shown to be the same: they differ if ROUND(a) take these values: '
        f'where ROUND(a) = {value}: {a} against {b}.'
    )


def test_a_witness_that_sets_an_expression_is_no_proof() -> None:
    # x > 0 is given false while x is free: the witness holds only if x > 0 can be false.
    verdict = equivalent('CASE WHEN x > 0 THEN a END', 'CASE WHEN x > 0 THEN a ELSE 0 END')
    assert verdict.witness is not None and verdict.witness.kind == 'values'
    assert verdict.witness.assumed == ['x > 0']
    assert str(verdict).startswith('Not shown to be the same: they differ if x > 0 take these values: ')
    assert verdict.witness.to_dict()['assumed'] == ['x > 0']
    # Values of source columns alone are a proof.
    proof = equivalent('a + b', 'a * b')
    assert proof.witness is not None and proof.witness.assumed == []
    assert str(proof).startswith('Different: where ')


def test_a_scalar_subquery_may_be_null() -> None:
    # A subquery that finds no row is NULL, which COALESCE turns into 0.
    query = '(SELECT MAX(x) FROM t)'
    assert not equivalent(f'COALESCE({query}, 0)', query).equal
    assert equivalent(f'{query} + 1', f'1 + {query}').equal
    assert not equivalent(query, '(SELECT MIN(x) FROM t)').equal


@pytest.mark.parametrize(
    'call', ['RANDOM()', 'RAND()', 'RANDN()', 'UUID()', 'NEWID()', 'GEN_RANDOM_UUID()', 'UUID_STRING()']
)
def test_each_call_of_a_volatile_function_is_its_own(call: str) -> None:
    verdict = equivalent(call, call)
    assert not verdict.equal and not verdict.equal_without_nulls
    assert verdict.witness is not None and len(verdict.witness.functions) == 1
    assert equivalent('ABS(a)', 'ABS(a)').equal


def test_a_function_difference_names_the_function() -> None:
    verdict = equivalent('UPPER(TRIM(s))', 'UPPER(s)')
    assert verdict.witness is not None
    assert verdict.witness.functions == ['TRIM']
    assert verdict.witness.only_a == ['TRIM(s)']
    assert verdict.witness.only_b == ['UPPER(s)']
    assert (
        str(verdict)
        == 'Not shown to be the same: TRIM applied differently: TRIM(s) only in the first; UPPER(s) only in the second.'
    )
    verdict = equivalent('CASE WHEN x > 1 THEN a ELSE 0 END', 'CASE WHEN x > 2 THEN a ELSE 0 END')
    assert verdict.witness is not None
    assert verdict.witness.functions == ['GT']
    assert verdict.witness.only_a == ['x > 1']


def test_a_case_condition_in_a_witness_is_true_or_false() -> None:
    verdict = equivalent('CASE WHEN x > 0 THEN a ELSE b END', 'CASE WHEN x > 0 THEN b ELSE a END')
    witness = verdict.witness
    assert witness is not None
    assert witness.inputs['x > 0'] in ('true', 'false')
    assert set(witness.inputs) == {'a', 'b', 'x > 0'}


def test_aggregates_and_windows_under_other_conditions_are_other_formulas() -> None:
    for formula in ('SUM(amount)', 'SUM(amount) OVER ()', 'ROW_NUMBER() OVER (ORDER BY amount)'):
        formulas = Formulas()
        plain = formulas.add(formula)
        completed = formulas.add(formula, context=["WHERE: status = 'completed'"])
        again = formulas.add(formula, context=["WHERE: status = 'completed'", "WHERE: status = 'completed'"])
        verdict = formulas.compare(plain, completed)
        assert not verdict.equal, formula
        assert verdict.witness == Witness(kind='conditions')
        assert formulas.compare(completed, again).equal
    assert (
        str(Witness(kind='conditions'))
        == 'the same formula, aggregated over rows chosen by different conditions'
    )
    assert fingerprint('amount * 2', context=['WHERE: x']) == fingerprint('2 * amount')
    assert fingerprint('SUM(amount)', context=['WHERE: x']) != fingerprint('SUM(amount)')


def test_fingerprints_group_formulas() -> None:
    formulas = Formulas(fixed_key=True)
    texts = ['SUM(a) - SUM(b)', 'SUM(a - b)', 'SUM(a) + SUM(b)', '-SUM(b) + SUM(a)', 'SUM(a + b)']
    prints = formulas.fingerprints([formulas.add(t, not_null=['a', 'b']) for t in texts])
    assert all(len(p) == 32 for p in prints)
    groups: dict[str, list[str]] = {}
    for text, value in zip(texts, prints, strict=True):
        groups.setdefault(value, []).append(text)
    assert sorted(groups.values()) == [
        ['SUM(a) + SUM(b)', 'SUM(a + b)'],
        ['SUM(a) - SUM(b)', 'SUM(a - b)', '-SUM(b) + SUM(a)'],
    ]
    # The fixed key gives the same fingerprints in every instance, and fingerprint() uses it.
    assert prints[0] == fingerprint('SUM(a) - SUM(b)', not_null=['a', 'b'])


def test_comparisons_draw_a_fresh_key() -> None:
    # Fingerprints under one random key are comparable only with each other.
    first, second = Formulas(), Formulas()
    assert first.fingerprint(first.add('SUM(a)')) != second.fingerprint(second.add('SUM(a)'))
    assert first.compare(first.add('a + b'), first.add('b + a')).equal
    # The witness does not depend on the key.
    found = [f.compare(f.add('SUM(a * b)'), f.add('SUM(a) * SUM(b)')).witness for f in (first, second)]
    assert found[0] == found[1]


def test_unreadable_formulas_are_refused_and_odd_literals_kept() -> None:
    with pytest.raises(ValueError, match='cannot read'):
        fingerprint('SUM(a')
    deep = ''.join(f'f{i}(' for i in range(250)) + 'b' + ')' * 250
    with pytest.raises(ValueError, match='nested too deeply'):
        fingerprint(deep)
    assert Verdict(True, 1e-50, True).to_dict() == {
        'equal': True,
        'error_bound': 1e-50,
        'equal_without_nulls': True,
    }
    assert str(Verdict(False, 0.0, False)) == 'Not shown to be the same.'
    # A string holding the byte 1, which the translation uses internally, is kept as it is.
    assert equivalent("'a\x01b' || c", "'a\x01b' || c").equal
    assert not equivalent("'a\x01b' || c", "'ab' || c").equal


def test_a_witness_that_sets_a_comparison_sets_its_column() -> None:
    assert _expressed({'a': '1', 'c > 0': 'true'}) == {'a': '1', 'c': '1'}
    assert _expressed({'2 > c': 'true', 'x > -1': 'false', 'b > 3': 'NULL'}) == {
        'c': '1',
        'x': '-1',
        'b': 'NULL',
    }
    assert _expressed({'0 > c': 'false'}) == {'c': '0'}
    # Two that give one column two values, or an expression that is not such a comparison.
    assert _expressed({'c > 0': 'true', 'c > 1': 'true'}) is None
    assert _expressed({'ROUND(a)': '1'}) is None
    for a, b in [('CASE WHEN 0 > c THEN a END * 0', '0'), ('CASE WHEN 1 > c THEN a ELSE b END', 'b')]:
        witness = equivalent(a, b, dialect='duckdb').witness
        assert witness is not None and witness.kind == 'values'
        rows = None if witness.rows is None else [_expressed(row) for row in witness.rows]
        inputs = _expressed(witness.inputs)
        assert inputs is not None and (rows is None or None not in rows), witness
        assert _agrees(_run([a, b], cast('list[dict[str, str]] | None', rows), inputs), witness)


def test_an_operand_present_wherever_another_is_changes_nothing_past_the_limit() -> None:
    # COALESCE(a, b) and CASE WHEN c > 0 THEN a ELSE 0 END are present wherever a is: beside a,
    # with nine CASEs of their own past the limit on terms, they change nothing.
    others = ' + '.join(f'CASE WHEN d{i} > 0 THEN w{i} ELSE 0 END' for i in range(9))
    for extra in ['COALESCE(a, b)', 'CASE WHEN c > 0 THEN a ELSE 0 END']:
        for _ in range(5):
            verdict = equivalent(f'0 * (a + {extra} + {others})', f'0 * (a + {others})', dialect='duckdb')
            assert verdict.equal, (extra, verdict)


def test_a_reordered_sum_has_one_verdict_under_every_key() -> None:
    # Each comparison draws a fresh key; a sum and the same sum reordered are the same under all
    # of them, past the limit on terms too, and have one fingerprint.
    cases = [
        'CASE WHEN a > d THEN b ELSE c END',
        'CASE WHEN a > d THEN c ELSE 1 END',
        'CASE WHEN b > c THEN 1 WHEN d > 0 THEN a ELSE c END',
        'CASE WHEN a > a THEN 0 WHEN a > -1 THEN d ELSE b END',
        'CASE WHEN c > c THEN b ELSE c END',
        'CASE WHEN a > b THEN b ELSE 2 END',
    ]
    x = ' + '.join(cases)
    y = ' + '.join(cases[i] for i in (5, 4, 1, 3, 2, 0))
    assert all(equivalent(x, y, dialect='duckdb').equal for _ in range(40))
    assert fingerprint(x) == fingerprint(y)
    # Two CASEs that together are present only where x is, with k CASEs over w beside them.
    f1, f2 = 'CASE WHEN c > 0 THEN x ELSE y END', 'CASE WHEN c > 0 THEN w ELSE x END'
    for k in range(6, 10):
        others = [f'CASE WHEN d{i} > 0 THEN w ELSE 0 END' for i in range(k)]
        first = '0 * (' + ' + '.join([f1, f2, *others]) + ')'
        moved = '0 * (' + ' + '.join([f2, *others, f1]) + ')'
        assert all(equivalent(first, moved).equal for _ in range(20)), k
        assert fingerprint(first) == fingerprint(moved)


# Against an independent oracle: random formulas evaluated by DuckDB on random tables, with
# NULLs and, for aggregates, empty tables.


@dataclass(frozen=True)
class F:
    """A formula over integer columns a, b and c, and how to write it in SQL two ways."""

    sql: str
    rewritten: str
    polynomial: bool = True
    """No CASE: a difference must then come with values that DuckDB reproduces."""


def _leaf() -> st.SearchStrategy[F]:
    columns = st.sampled_from('abc').map(lambda c: F(c, c))
    numbers = st.integers(-3, 3).map(lambda n: F(f'({n})', f'({n})'))
    return columns | numbers


def _combine(inner: st.SearchStrategy[F]) -> st.SearchStrategy[F]:
    pairs = st.tuples(inner, inner)
    triples = st.tuples(inner, inner, inner)
    return st.one_of(
        pairs.map(
            lambda p: F(
                f'({p[0].sql} + {p[1].sql})',
                f'({p[1].rewritten} + {p[0].rewritten})',
                p[0].polynomial and p[1].polynomial,
            )
        ),
        pairs.map(
            lambda p: F(
                f'({p[0].sql} - {p[1].sql})',
                f'({p[0].rewritten} + (-1) * {p[1].rewritten})',
                p[0].polynomial and p[1].polynomial,
            )
        ),
        triples.map(
            lambda t: F(
                f'({t[0].sql} * ({t[1].sql} + {t[2].sql}))',
                f'({t[2].rewritten} * {t[0].rewritten} + {t[0].rewritten} * {t[1].rewritten})',
                t[0].polynomial and t[1].polynomial and t[2].polynomial,
            )
        ),
        triples.map(
            lambda t: F(
                f'(CASE WHEN {t[0].sql} > b THEN {t[1].sql} ELSE {t[2].sql} END)',
                f'(CASE WHEN b < {t[0].rewritten} THEN {t[1].rewritten} ELSE {t[2].rewritten} END)',
                False,
            )
        ),
        pairs.map(
            lambda p: F(
                f'COALESCE({p[0].sql}, {p[1].sql})',
                f'COALESCE({p[0].rewritten}, {p[1].rewritten})',
                # Which argument it gives depends on presence, as a CASE does.
                False,
            )
        ),
    )


ROWS = st.recursive(_leaf(), _combine, max_leaves=6)


def _aggregate(row: F) -> st.SearchStrategy[F]:
    return st.sampled_from(['sum', 'scaled', 'count', 'avg']).map(
        lambda how: {
            'sum': F(f'SUM({row.sql})', f'SUM({row.rewritten})', row.polynomial),
            'scaled': F(f'(2 * SUM({row.sql}))', f'SUM({row.rewritten} * 2)', row.polynomial),
            'count': F(f'COUNT({row.sql})', f'COUNT({row.rewritten})', row.polynomial),
            'avg': F(f'AVG({row.sql})', f'(SUM({row.rewritten}) / COUNT({row.rewritten}))', False),
        }[how]
    )


AGGREGATES = st.recursive(
    ROWS.flatmap(_aggregate),
    lambda inner: (
        st.tuples(inner, inner).map(
            lambda p: F(
                f'({p[0].sql} * {p[1].sql})',
                f'({p[1].rewritten} * {p[0].rewritten})',
                p[0].polynomial and p[1].polynomial,
            )
        )
        | st.tuples(inner, inner).map(
            lambda p: F(
                f'({p[0].sql} - {p[1].sql})',
                f'((-1) * {p[1].rewritten} + {p[0].rewritten})',
                p[0].polynomial and p[1].polynomial,
            )
        )
    ),
    max_leaves=3,
)
CELLS = st.none() | st.integers(-4, 4)
TABLES = st.lists(st.tuples(CELLS, CELLS, CELLS), min_size=0, max_size=4)
PLAIN = st.lists(st.tuples(*[st.integers(-4, 4)] * 3), min_size=1, max_size=4)


def _results(
    con: duckdb.DuckDBPyConnection, table: list[tuple[int | None, ...]], a: str, b: str
) -> list[tuple[object, object]]:
    con.execute('create or replace table t (a bigint, b bigint, c bigint)')
    if table:
        con.executemany('insert into t values (?, ?, ?)', table)
    return con.execute(f'select {a}, {b} from t').fetchall()


def _same(x: object, y: object) -> bool:
    return bool(x == y) or (x is not None and y is not None and abs(float(x) - float(y)) < 1e-9)  # type: ignore[arg-type]


def _check(
    first: F,
    second: F,
    tables: list[list[tuple[int | None, ...]]],
    plain: list[list[tuple[int, ...]]],
    aggregates: bool,
) -> None:
    verdict = equivalent(first.sql, second.sql, dialect='duckdb')
    con = duckdb.connect()
    if verdict.equal:
        for table in tables:
            for x, y in _results(con, table, first.sql, second.sql):
                assert _same(x, y), (first.sql, second.sql, table)
    if verdict.equal_without_nulls:
        for numbers in plain:
            for x, y in _results(con, list(numbers), first.sql, second.sql):
                assert _same(x, y), (first.sql, second.sql, numbers)
    if verdict.equal or not (first.polynomial and second.polynomial):
        return
    witness = verdict.witness
    if witness is None:
        # Not shown to be the same, with nothing to replay: allowed over aggregates, whose presences
        # are atoms the model does not relate (SUM(0 * (a + b)) is present only where SUM(a) is).
        # Row polynomials are decided exactly and always give a witness.
        assert aggregates, (first.sql, second.sql, verdict)
        return
    assert witness.kind == 'values', (first.sql, second.sql, verdict)

    def row(values: dict[str, str]) -> tuple[int | None, ...]:
        return tuple(None if values.get(n, '0') == 'NULL' else int(values.get(n, '0')) for n in 'abc')

    table = [row(r) for r in witness.rows] if witness.rows is not None else [row(witness.inputs)]
    [(x, y)] = _results(con, table, first.sql, second.sql)
    assert ('NULL' if x is None else str(x), 'NULL' if y is None else str(y)) == (witness.a, witness.b)
    assert x != y


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    first=ROWS,
    second=ROWS,
    rewrite=st.booleans(),
    tables=st.lists(TABLES.filter(bool), min_size=6, max_size=6),
    plain=st.lists(PLAIN, min_size=4, max_size=4),
)
# The same in SQL, and not shown to be the same, with no witness: COALESCE is not a polynomial.
@example(
    first=F('(b + (b + (1)))', '((b + 1) + b)'),
    second=F('((b + (1)) + COALESCE(b, a))', '(COALESCE(b, a) + (b + (1)))', False),
    rewrite=False,
    tables=[[(None, 1, 2), (1, None, 0), (2, 3, None)]],
    plain=[[(1, 2, 3)]],
)
# The same rewritten, with the operands of + swapped. Each CASE is NULL where it takes a branch
# whose column is NULL; the sum is NULL where either is, in whichever order they are added.
@example(
    first=F(
        '((CASE WHEN a > b THEN b ELSE (0) END) + (CASE WHEN b > b THEN a ELSE (0) END))',
        '((CASE WHEN b < b THEN a ELSE (0) END) + (CASE WHEN b < a THEN b ELSE (0) END))',
        False,
    ),
    second=F('a', 'a'),
    rewrite=True,
    tables=[[(None, None, None)], [(1, None, 0), (2, 1, None), (None, 3, 1)]],
    plain=[[(0, 0, 0)], [(2, 1, 0)]],
)
# The same distributed: the presence of a product is the same however its factors are grouped.
@example(
    first=F(
        '(a * ((CASE WHEN a > b THEN b ELSE (0) END) + COALESCE(a, c)))',
        '(COALESCE(a, c) * a + a * (CASE WHEN b < a THEN b ELSE (0) END))',
        False,
    ),
    second=F('a', 'a'),
    rewrite=True,
    tables=[[(None, None, None)], [(1, None, 0), (2, 1, None), (None, 3, 1)]],
    plain=[[(0, 0, 0)], [(2, 1, 0)]],
)
def test_row_formulas_agree_with_duckdb(
    first: F,
    second: F,
    rewrite: bool,
    tables: list[list[tuple[int | None, ...]]],
    plain: list[list[tuple[int, ...]]],
) -> None:
    other = F(first.rewritten, first.sql, first.polynomial) if rewrite else second
    if rewrite:
        assert equivalent(first.sql, other.sql).equal, (first.sql, other.sql)
    _check(first, other, tables, plain, aggregates=False)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    first=AGGREGATES,
    second=AGGREGATES,
    rewrite=st.booleans(),
    tables=st.lists(TABLES, min_size=6, max_size=6),
    plain=st.lists(PLAIN, min_size=4, max_size=4),
)
# The same, but not shown to be: "a row has a and b" implies "a row has a", which the model of
# SUM's presence does not relate (Hypothesis seed 3838).
@example(
    first=F('SUM(((0) * (a + b)))', 'SUM((b * (0) + (0) * a))'),
    second=F('(SUM(a) * SUM(((0) * (a + b))))', '(SUM((b * (0) + (0) * a)) * SUM(a))'),
    rewrite=False,
    tables=[[], [], [], [], [], []],
    plain=[[(0, 0, 0)], [(0, 0, 0)], [(0, 0, 0)], [(0, 0, 0)]],
)
def test_aggregate_formulas_agree_with_duckdb(
    first: F,
    second: F,
    rewrite: bool,
    tables: list[list[tuple[int | None, ...]]],
    plain: list[list[tuple[int, ...]]],
) -> None:
    other = F(first.rewritten, first.sql, first.polynomial) if rewrite else second
    if rewrite:
        assert equivalent(first.sql, other.sql).equal, (first.sql, other.sql)
    _check(first, other, tables, plain, aggregates=True)


# In a graph.


def _orders(not_null: bool = False) -> Graph:
    """Three models over raw.orders computing net revenue, one under another filter."""
    graph = Graph()
    orders = graph.add_node(Node('source.raw.orders', 'source', 'orders', relation='db.raw.orders'))
    for name in ('amount', 'discount'):
        column = graph.add_column(orders, name)
        if not_null:
            test = graph.add_node(
                Node(f'test.not_null_{name}', 'test', f'not_null_{name}', attrs={'test': 'not_null'})
            )
            graph.add_edge(test.id, column.id, 'tests')
    formulas = {
        'revenue': 'SUM(db.raw.orders.amount) - SUM(db.raw.orders.discount)',
        'daily': 'SUM(db.raw.orders.amount - db.raw.orders.discount)',
        'completed': 'SUM(db.raw.orders.amount - db.raw.orders.discount)',
    }
    for name, formula in formulas.items():
        conditions = ['GROUP BY: db.raw.orders.order_date']
        if name == 'completed':
            conditions.append("WHERE: db.raw.orders.status = 'completed'")
        model = graph.add_node(
            Node(f'model.{name}', 'model', name, relation=f'db.s.{name}', attrs={'conditions': conditions})
        )
        column = graph.add_column(model, 'net_revenue', attrs={'expression': formula})
        graph.add_edge(orders.id, model.id, 'feeds')
        for source in ('amount', 'discount'):
            graph.add_edge(column.id, f'{orders.id}#{source}', 'derived_from')
    return graph


def test_column_fingerprints_follow_lineage_conditions_and_tests() -> None:
    graph = _orders(not_null=True)
    plain = column_fingerprints(graph, conditions=False)
    keyed = column_fingerprints(graph)
    revenue, daily, completed = (f'model.{n}#net_revenue' for n in ('revenue', 'daily', 'completed'))
    assert plain[revenue] == plain[daily] == plain[completed]
    assert keyed[revenue] == keyed[daily] != keyed[completed]
    assert plain['source.raw.orders#amount'] != plain['source.raw.orders#discount']
    # Without the not_null tests the two formulas differ where discount is NULL.
    nullable = column_fingerprints(_orders(), conditions=False)
    assert nullable[revenue] != nullable[daily]


def _graph() -> Graph:
    """A hand-made graph: a cycle, an unreadable expression and a column of an unknown relation."""
    graph = Graph()
    for name in ('one', 'two'):
        graph.add_node(Node(f'model.{name}', 'model', name, relation=f'db.s.{name}'))
    graph.add_column(graph.nodes['model.one'], 'x', attrs={'expression': 'db.s.two.y + 1'})
    graph.add_column(graph.nodes['model.two'], 'y', attrs={'expression': 'db.s.one.x * 2'})
    graph.add_column(graph.nodes['model.two'], 'z', attrs={'expression': 'SUM('})
    graph.add_column(graph.nodes['model.two'], 'w', attrs={'expression': 'db.s.gone.v + elsewhere'})
    graph.add_column(
        graph.nodes['model.two'], 'v', attrs={'expression': "CASE WHEN db.s.one.x > 'a''b' THEN 1 END"}
    )
    graph.add_edge('model.one#x', 'model.two#y', 'derived_from')
    graph.add_edge('model.two#y', 'model.one#x', 'derived_from')
    return graph


def test_lineage_cycles_and_unreadable_expressions() -> None:
    graph = _graph()
    formulas = Formulas(graph)
    x = formulas.column(graph.nodes['model.one#x'])
    y = formulas.column(graph.nodes['model.two#y'])
    assert formulas.column(graph.nodes['model.one#x']) == x
    # x = y + 1 with y = x * 2: the cycle is cut at x, which y then reads as a variable.
    assert not formulas.compare(x, formulas.add('x * 2 + 1')).equal
    z = formulas.column(graph.nodes['model.two#z'])
    w = formulas.column(graph.nodes['model.two#w'])
    assert not formulas.compare(z, w).equal
    assert formulas.compare(w, y).witness is not None
    v = formulas.column(graph.nodes['model.two#v'])
    witness = formulas.compare(v, formulas.add('1')).witness
    assert witness is not None and witness.kind == 'values'
    assert witness.inputs == {"one.x > 'a''b'": 'false'}
    assert (witness.a, witness.b) == ('NULL', '1')
    prints = column_fingerprints(graph)
    assert len(prints) == 5 and len(set(prints.values())) == 5


def _customer_orders(join: str) -> Graph:
    """Customers joined to their orders, and two marts of amount less credit by region, with
    ``not_null`` tests on both inputs."""
    graph = Graph()
    for name, column in (('orders', 'amount'), ('customers', 'credit')):
        source = graph.add_node(Node(f'source.raw.{name}', 'source', name, relation=f'db.raw.{name}'))
        tested = graph.add_column(source, column)
        test = graph.add_node(
            Node(f'test.not_null_{column}', 'test', f'not_null_{column}', attrs={'test': 'not_null'})
        )
        graph.add_edge(test.id, tested.id, 'tests')
        graph.add_column(source, 'region' if name == 'customers' else 'customer_id')
    conditions = [f'{join} ON: db.raw.customers.id = db.raw.orders.customer_id']
    joined = graph.add_node(
        Node(
            'model.customer_orders',
            'model',
            'customer_orders',
            relation='db.s.customer_orders',
            attrs={'conditions': conditions},
        )
    )
    for relation, column in (('orders', 'amount'), ('customers', 'credit'), ('customers', 'region')):
        graph.add_edge(f'source.raw.{relation}', joined.id, 'feeds')
        out = graph.add_column(joined, column, attrs={'expression': f'db.raw.{relation}.{column}'})
        graph.add_edge(out.id, f'source.raw.{relation}#{column}', 'derived_from')
    marts = {
        'apart': 'SUM(db.s.customer_orders.amount) - SUM(db.s.customer_orders.credit)',
        'together': 'SUM(db.s.customer_orders.amount - db.s.customer_orders.credit)',
    }
    for name, formula in marts.items():
        mart = graph.add_node(
            Node(
                f'model.{name}',
                'model',
                name,
                relation=f'db.s.{name}',
                attrs={'conditions': ['GROUP BY: db.s.customer_orders.region']},
            )
        )
        graph.add_edge(joined.id, mart.id, 'feeds')
        out = graph.add_column(mart, 'net', attrs={'expression': formula})
        for column in ('amount', 'credit'):
            graph.add_edge(out.id, f'{joined.id}#{column}', 'derived_from')
    return graph


def test_a_not_null_column_read_through_an_outer_join_may_be_null() -> None:
    # In DuckDB a customer with no order gives the two marts different results.
    con = duckdb.connect()
    con.execute('create table customers (id int, region text, credit int)')
    con.execute('create table orders (customer_id int, amount int)')
    con.execute("insert into customers values (1, 'n', 2), (2, 'n', 4)")
    con.execute('insert into orders values (1, 9)')
    joined = 'customers left join orders on customers.id = orders.customer_id'
    found = con.execute(
        f'select sum(amount) - sum(credit), sum(amount - credit) from {joined} group by region'
    ).fetchall()
    assert found == [(3, 7)]
    graph = _customer_orders('LEFT JOIN')
    formulas = Formulas(graph, conditions=False)
    apart, together = (formulas.column(graph.nodes[f'model.{n}#net']) for n in ('apart', 'together'))
    verdict = formulas.compare(apart, together)
    assert not verdict.equal and verdict.equal_without_nulls
    assert verdict.witness is not None and verdict.witness.kind == 'values'
    assert verdict.witness.rows is not None
    shown = 'orders.amount matched in customer_orders'
    assert any(row.get(shown) == 'false' for row in verdict.witness.rows), verdict.witness
    # Without the outer join the tests hold on every route, and the two are the same.
    inner = _customer_orders('JOIN')
    formulas = Formulas(inner, conditions=False)
    apart, together = (formulas.column(inner.nodes[f'model.{n}#net']) for n in ('apart', 'together'))
    assert formulas.compare(apart, together).equal
    plain = column_fingerprints(inner, conditions=False)
    assert plain['model.apart#net'] == plain['model.together#net']
    outer = column_fingerprints(graph, conditions=False)
    assert outer['model.apart#net'] != outer['model.together#net']


def test_volatile_functions_in_columns_are_their_own() -> None:
    graph = Graph()
    for name in ('one', 'two'):
        model = graph.add_node(Node(f'model.{name}', 'model', name, relation=f'db.s.{name}'))
        graph.add_column(model, 'r', attrs={'expression': 'RANDOM() + RANDOM()'})
    formulas = Formulas(graph)
    one, two = (formulas.column(graph.nodes[f'model.{n}#r']) for n in ('one', 'two'))
    assert formulas.compare(one, one).equal
    assert not formulas.compare(one, two).equal
    assert not formulas.compare(one, formulas.add('2 * RANDOM()')).equal
