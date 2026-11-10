"""Mutant generation: semantic mutants of SQL queries, seeded and deduplicated."""

from __future__ import annotations

from typing import Any

import duckdb
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from konstruct.core.mutate import OPERATORS, mutants, parse_query

# Mutant generation, on SQL strings


def by_description(sql: str, **options: Any) -> dict[str, str]:
    return {m.description: m.sql for m in mutants(sql, **options).mutants}


def test_aggregates_are_swapped() -> None:
    found = by_description('select sum(a) as s, max(b) as m, min(c) as n, avg(d) as v from t')
    assert found['SUM(a) to MAX(a)'] == 'SELECT MAX(a) AS s, MAX(b) AS m, MIN(c) AS n, AVG(d) AS v FROM t'
    assert 'MAX(b) to MIN(b)' in found and 'MIN(c) to MAX(c)' in found and 'AVG(d) to MAX(d)' in found
    counts = by_description('select count(distinct a), count(b), count(*) from t')
    assert counts['COUNT(DISTINCT a) to COUNT(a)'] == 'SELECT COUNT(a), COUNT(b), COUNT(*) FROM t'
    assert (
        counts['COUNT(b) to COUNT(DISTINCT b)']
        == 'SELECT COUNT(DISTINCT a), COUNT(DISTINCT b), COUNT(*) FROM t'
    )
    assert len(counts) == 2  # COUNT(*) has nothing to swap


def test_filters_are_dropped_and_negated() -> None:
    found = by_description("select a from t where a > 1 and b = 'x'")
    assert found["drop the WHERE condition `a > 1 AND b = 'x'`"] == 'SELECT a FROM t'
    assert found["negate the WHERE condition `a > 1 AND b = 'x'`"] == (
        "SELECT a FROM t WHERE NOT (a > 1 AND b = 'x')"
    )
    assert found['drop `a > 1` from the WHERE'] == "SELECT a FROM t WHERE b = 'x'"
    assert found["drop `b = 'x'` from the WHERE"] == 'SELECT a FROM t WHERE a > 1'
    having = by_description('select a, count(*) from t group by a having count(*) > 1', operators=('filter',))
    assert having['drop the HAVING condition `COUNT(*) > 1`'] == 'SELECT a, COUNT(*) FROM t GROUP BY a'


def test_join_conditions_and_types_are_changed() -> None:
    found = by_description('select * from a join b on a.id = b.id and a.k >= b.k left join c using (id)')
    assert found['INNER JOIN b to LEFT JOIN'] == (
        'SELECT * FROM a LEFT JOIN b ON a.id = b.id AND a.k >= b.k LEFT JOIN c USING (id)'
    )
    assert (
        found['LEFT JOIN c to INNER JOIN']
        == 'SELECT * FROM a JOIN b ON a.id = b.id AND a.k >= b.k JOIN c USING (id)'
    )
    assert found['negate the join condition `a.id = b.id AND a.k >= b.k`'].startswith(
        'SELECT * FROM a JOIN b ON NOT (a.id = b.id AND a.k >= b.k)'
    )
    assert found['drop `a.id = b.id` from the join condition'].startswith(
        'SELECT * FROM a JOIN b ON a.k >= b.k '
    )
    assert found['a.k >= b.k to a.k > b.k'].startswith('SELECT * FROM a JOIN b ON a.id = b.id AND a.k > b.k')
    single = by_description(
        'select * from a right join b on a.id = b.id', operators=('join_condition', 'join_type')
    )
    assert single['drop the join condition `a.id = b.id`'] == 'SELECT * FROM a RIGHT JOIN b ON TRUE'
    assert single['RIGHT JOIN b to INNER JOIN'] == 'SELECT * FROM a JOIN b ON a.id = b.id'
    assert by_description('select * from a cross join b') == {}


def test_boundaries_coalesce_and_case() -> None:
    found = by_description('select a from t where a < 5 and b <= 6')
    assert found['a < 5 to a <= 5'] == 'SELECT a FROM t WHERE a <= 5 AND b <= 6'
    assert found['b <= 6 to b < 6'] == 'SELECT a FROM t WHERE a < 5 AND b < 6'
    coalesced = by_description('select coalesce(a, b, 0) as x from t')
    assert coalesced['remove the COALESCE from `COALESCE(a, b, 0)`'] == 'SELECT a AS x FROM t'
    case = by_description(
        "select case when a > 1 then 'big' when a > 0 then 'some' else 'none' end as size from t"
    )
    assert case["drop the CASE branch `WHEN a > 1 THEN 'big'`"] == (
        "SELECT CASE WHEN a > 0 THEN 'some' ELSE 'none' END AS size FROM t"
    )
    assert case["drop the CASE default `ELSE 'none'`"] == (
        "SELECT CASE WHEN a > 1 THEN 'big' WHEN a > 0 THEN 'some' END AS size FROM t"
    )
    # Dropping the only branch leaves the default for every row.
    only = by_description('select case when a > 1 then a else 0 end as x from t', operators=('case',))
    assert only['drop the CASE branch `WHEN a > 1 THEN a`'] == 'SELECT 0 AS x FROM t'
    assert by_description('select case when a > 1 then a end from t', operators=('case',)) == {}


def test_comparisons_are_flipped_and_dates_shifted() -> None:
    sql = "select a from t where a = 1 and b <> 2 and c < 3 and d >= date '2024-02-28'"
    found = by_description(sql, operators=('comparison', 'date'))
    rest = "AND b <> 2 AND c < 3 AND d >= CAST('2024-02-28' AS DATE)"
    assert found['a = 1 to a <> 1'] == f'SELECT a FROM t WHERE a <> 1 {rest}'
    assert found['b <> 2 to b = 2'].startswith('SELECT a FROM t WHERE a = 1 AND b = 2 AND c < 3')
    assert found['c < 3 to c > 3'].startswith('SELECT a FROM t WHERE a = 1 AND b <> 2 AND c > 3')
    assert found["d >= CAST('2024-02-28' AS DATE) to d <= CAST('2024-02-28' AS DATE)"].endswith(
        "d <= CAST('2024-02-28' AS DATE)"
    )
    # A leap year: the day after 28 February is the 29th.
    assert found["shift the date '2024-02-28' to '2024-02-29'"].endswith("d >= CAST('2024-02-29' AS DATE)")
    assert len(found) == 5
    # A timestamp keeps its time; a string that is not a valid date is left alone.
    stamps = by_description(
        "select a from t where e < '2024-12-31 23:00:00' and f = 'soon' and g = '2024-13-01'",
        operators=('date',),
    )
    assert stamps == {
        "shift the date '2024-12-31 23:00:00' to '2025-01-01 23:00:00'": (
            "SELECT a FROM t WHERE e < '2025-01-01 23:00:00' AND f = 'soon' AND g = '2024-13-01'"
        )
    }


def test_the_last_date_is_not_shifted() -> None:
    """'9999-12-31', the usual open-ended sentinel, has no next day: it is left alone, not an error."""
    sql = "select id, amount, coalesce(valid_to, '9999-12-31') as valid_to from stg where amount > 0"
    found = mutants(sql, 'duckdb')
    assert found.mutants and not [m for m in found.mutants if m.operator == 'date']
    assert by_description("select a from t where d < '9999-12-30'", operators=('date',)) == {
        "shift the date '9999-12-30' to '9999-12-31'": "SELECT a FROM t WHERE d < '9999-12-31'"
    }


def test_scope_column_and_ids() -> None:
    sql = 'with o as (select k, sum(v) as total from t where v > 0 group by k) select k, coalesce(total, 0) as total from o'
    result = mutants(sql)
    inner = next(m for m in result.mutants if m.description == 'SUM(v) to MAX(v)')
    outer = next(m for m in result.mutants if m.operator == 'coalesce')
    assert (inner.scope, inner.column) == ('o', None)
    assert (outer.scope, outer.column) == (None, 'total')
    assert outer.before == 'COALESCE(total, 0)' and outer.after == 'total'
    assert 'sql' not in inner.to_dict() and inner.to_dict()['operator'] == 'aggregate'
    # An id names the same mutation however the sample is drawn.
    every = {m.id: m.sql for m in result.mutants}
    for seed in range(5):
        for m in mutants(sql, limit=2, seed=seed).mutants:
            assert every[m.id] == m.sql


def test_sample_is_seeded_capped_and_covers_each_operator() -> None:
    sql = (
        'select a.k, sum(a.v) as s, max(a.w) as m, coalesce(min(b.x), 0) as n, '
        "case when sum(a.v) > 10 then 'hi' else 'lo' end as band from a join b on a.k = b.k "
        "where a.v > 0 and b.x < 9 and a.d >= '2020-01-01' group by a.k having count(*) >= 2"
    )
    whole = mutants(sql)
    assert whole.sites == len(whole.mutants) + whole.duplicates
    first = mutants(sql, limit=len(OPERATORS), seed=7)
    assert [m.id for m in first.mutants] == [m.id for m in mutants(sql, limit=len(OPERATORS), seed=7).mutants]
    assert {m.operator for m in first.mutants} == set(OPERATORS)
    assert [m.id for m in mutants(sql, limit=4, seed=8).mutants] != [
        m.id for m in mutants(sql, limit=4, seed=9).mutants
    ]
    assert len(mutants(sql, limit=3).mutants) == 3
    assert {m.operator for m in mutants(sql, operators=('boundary',)).mutants} == {'boundary'}


def test_duplicates_and_bad_input() -> None:
    result = mutants('select a from t where a > 1 and a > 1', operators=('filter',))
    assert result.duplicates == 1 and len(result.mutants) == result.sites - 1
    with pytest.raises(ValueError, match='unknown mutation operators: nope'):
        mutants('select 1', operators=('nope',))
    with pytest.raises(ValueError, match='not a query'):
        mutants('create table x (a int)')
    with pytest.raises(ValueError, match='does not parse'):
        parse_query('select from where (', 'duckdb')
    # A second statement is refused rather than carried into every mutant.
    with pytest.raises(ValueError, match='2 statements'):
        mutants("select a from t where a > 1; copy (select 42) to 'out.csv'")
    assert mutants('select a from t').mutants == []


def test_mutant_of_a_bare_root_is_replaced() -> None:
    # A set operation whose whole tree is not a node any operator replaces still mutates inside.
    found = by_description('select sum(a) from t union all select max(a) from t')
    assert found['SUM(a) to MAX(a)'] == 'SELECT MAX(a) FROM t UNION ALL SELECT MAX(a) FROM t'


QUERY_PARTS = st.fixed_dictionaries(
    {
        'aggregate': st.sampled_from(['sum', 'max', 'min', 'avg', 'count']),
        'distinct': st.booleans(),
        'comparison': st.sampled_from(['>', '>=', '<', '<=']),
        'bound': st.integers(min_value=-2, max_value=12),
        'join': st.sampled_from(['join', 'left join', 'right join']),
        'case': st.booleans(),
        'coalesce': st.booleans(),
        'having': st.booleans(),
    }
)


@settings(max_examples=40, deadline=None)
@given(QUERY_PARTS)
def test_every_mutant_runs_in_duckdb(parts: dict[str, Any]) -> None:
    """Every mutant of a family of queries is SQL DuckDB runs, and none is the original."""
    argument = 'distinct a.v' if parts['distinct'] and parts['aggregate'] == 'count' else 'a.v'
    value = f'{parts["aggregate"]}({argument})'
    if parts['coalesce']:
        value = f'coalesce({value}, 0)'
    band = (
        f"case when max(a.v) {parts['comparison']} {parts['bound']} then 'x' else 'y' end"
        if parts['case']
        else "'z'"
    )
    sql = (
        f'select a.k, {value} as total, {band} as band from a {parts["join"]} b on a.k = b.k and b.w > 0 '
        f'where a.v {parts["comparison"]} {parts["bound"]} group by a.k'
        + (' having count(*) > 1' if parts['having'] else '')
    )
    con = duckdb.connect()
    con.execute('create table a as select i % 4 as k, i as v from range(12) t(i)')
    con.execute('create table b as select i as k, i - 1 as w from range(5) t(i)')
    result = mutants(sql)
    assert result.mutants
    original = parse_query(sql, 'duckdb').sql(dialect='duckdb')
    for mutant in result.mutants:
        assert mutant.sql != original
        con.execute(mutant.sql).fetchall()
    con.close()
