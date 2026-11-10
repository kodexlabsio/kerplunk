"""Mutation testing: mutants are made from SQL, run read-only, and told apart from the data's gaps."""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from konstruct import Project
from konstruct.analysis import mutation
from konstruct.analysis.mutation import ROW_COUNT, MutationTesting, _holds, _reduce, _Run, mutate
from konstruct.analysis.verify import Candidate
from konstruct.core.graph import Graph, Node
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


# Orchestration helpers


def test_thresholds_follow_dbt() -> None:
    assert _holds('!= 0', 1) and not _holds('!= 0', 0)
    assert _holds('>10', 11) and not _holds('> 10', 10)
    assert (
        _holds('>= 2', 2) and _holds('<= 2', 2) and _holds('< 1', 0) and _holds('= 3', 3) and _holds('==3', 3)
    )
    assert _holds('<> 0', 2) and _holds('not a threshold', 1)


def test_reduce_finds_a_small_witness() -> None:
    def keep(items: list[int]) -> bool:
        return 3 in items and 17 in items

    assert sorted(_reduce(list(range(40)), keep, budget=500)) == [3, 17]
    assert len(_reduce(list(range(40)), keep, budget=3)) > 2  # the budget bounds the work


def test_suggestions_from_two_outputs() -> None:
    graph = Graph()
    node = Node(id='model.p.m', kind='model', name='m', attrs={'dbt_name': 'm'})
    graph.add_node(node)
    candidate = Candidate(node, 'select 1', None, None)
    mutant = mutants('select 1 where 1 > 0').mutants[0]
    names = ['id', 'status', 'low', 'high', 'amount']
    baseline = [(i, ['a', 'b'][i % 2], i, i + 1, i * 2) for i in range(1, 7)]
    rows = [(1, 'c', 5, 2, -3), (1, None, 1, 2, 4), (None, 'a', 1, 2, 4)]
    run = cast(_Run, None)  # not read: there are no columns to relate, and no unit test is asked for
    found = mutation._suggest(run, graph, candidate, names, baseline, rows, mutant, False)
    tests = [(s.test, s.column) for s in found]
    assert tests == [('not_null', 'id'), ('unique', 'id'), ('not_null', 'status')]
    every = mutation._suggest(
        run, graph, candidate, names[1:2], [r[1:2] for r in baseline], [('c',)], mutant, False
    )
    assert every[0].test == 'accepted_values' and "values: ['a', 'b']" in every[0].yaml
    assert yaml.safe_load(every[0].yaml) == [{'accepted_values': {'arguments': {'values': ['a', 'b']}}}]
    assert every[0].where == 'the data_tests of column status of model m'
    assert yaml.safe_load(found[0].yaml) == ['not_null']
    negative = mutation._non_negative('m', 'amount', [1, 2], [3, -1])
    assert negative[0].test == 'dbt_utils.expression_is_true' and 'amount >= 0' in negative[0].yaml
    assert mutation._non_negative('m', 'amount', [0, 0], [-1]) == []
    order = mutation._orderings('m', ['low', 'high'], [(1, 2), (2, 2)], [(3, 2)])
    assert [s.reason.split(' in ')[0] for s in order] == ['low <= high']
    equal = mutation._orderings('m', ['a', 'b'], [(1, 1), (2, 2)], [(3, 2)])
    assert [yaml.safe_load(s.yaml) for s in equal] == [
        [{'dbt_utils.expression_is_true': {'arguments': {'expression': 'a = b'}}}]
    ]
    assert equal[0].where == 'the data_tests of model m'


# End to end: a tiny dbt project on DuckDB, with a hand-written manifest


MODEL_SQL = """
with paid as (
    select customer_id, sum(amount) as paid, count(*) as payments
    from "shop"."main"."payments"
    where status <> 'void'
    group by customer_id
)
select
    c.id as customer_id,
    c.name,
    coalesce(paid.paid, 0) as total_paid,
    paid.payments,
    case when coalesce(paid.paid, 0) >= 100 then 'gold' else 'standard' end as tier
from "shop"."main"."customers" c
left join paid on paid.customer_id = c.id
where c.id > 0
"""


def _node(kind: str, name: str, sql: str | None, depends: list[str], **extra: Any) -> dict[str, Any]:
    unique_id = f'{kind}.shop.{name}'
    item: dict[str, Any] = {
        'unique_id': unique_id,
        'resource_type': kind,
        'name': name,
        'alias': name,
        'package_name': 'shop',
        'database': 'shop',
        'schema': 'main',
        'original_file_path': f'models/{name}.sql',
        'fqn': ['shop', name],
        'language': 'sql',
        'depends_on': {'nodes': depends, 'macros': []},
        'config': {'materialized': 'table', 'severity': 'ERROR'},
        'tags': [],
        'columns': {},
    }
    if sql is not None:
        item['raw_code'] = item['compiled_code'] = sql
    if kind == 'model':
        item['relation_name'] = f'"shop"."main"."{name}"'
    item.update(extra)
    return item


def _test(
    name: str, sql: str | None, kind: str | None = None, column: str | None = None, **config: Any
) -> dict[str, Any]:
    return _node(
        'test',
        name,
        sql,
        ['model.shop.customer_value'],
        test_metadata={'name': kind, 'kwargs': {'column_name': column}} if kind else None,
        column_name=column,
        attached_node='model.shop.customer_value',
        config={'severity': 'ERROR', **config},
    )


TABLE = '"shop"."main"."customer_value"'


def _unit(
    name: str, given: list[dict[str, Any]], expect: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    """A dbt unit test of customer_value, as dbt writes it to the manifest."""
    return {
        'unique_id': f'unit_test.shop.customer_value.{name}',
        'resource_type': 'unit_test',
        'name': name,
        'model': 'customer_value',
        'package_name': 'shop',
        'depends_on': {'nodes': ['model.shop.customer_value'], 'macros': []},
        'given': [{'format': 'dict', 'fixture': None, **g} for g in given],
        'expect': {'format': 'dict', 'fixture': None, 'rows': expect},
        'overrides': None,
        **extra,
    }


def make_shop(
    root: Path,
    *,
    tests: list[dict[str, Any]] | None = None,
    units: list[dict[str, Any]] | None = None,
    sql: str | None = None,
) -> Project:
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create table customers as select i as id, chr((64 + i)::int) as name from range(1, 9) t(i)')
    # Customer 8 has no payments; every customer's payments are positive; none is void.
    con.execute(
        'create table payments as select i as id, 1 + i % 7 as customer_id, 10 + (i * 13) % 40 as amount, '
        "'ok' as status from range(1, 25) t(i)"
    )
    con.close()
    return write_shop(root, tests=tests, units=units, sql=sql)


def _relocate(value: Any, database: str, schema: str) -> Any:
    """The manifest for tables in ``database.schema`` rather than ``shop.main``."""
    if isinstance(value, dict):
        return {
            k: (database if k == 'database' else schema if k == 'schema' else _relocate(v, database, schema))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_relocate(v, database, schema) for v in value]
    if isinstance(value, str):
        return value.replace('"shop"."main"', f'"{database}"."{schema}"')
    return value


def write_shop(
    root: Path,
    *,
    tests: list[dict[str, Any]] | None = None,
    units: list[dict[str, Any]] | None = None,
    sql: str | None = None,
    adapter: str = 'duckdb',
    database: str = 'shop',
    schema: str = 'main',
) -> Project:
    (root / 'dbt_project.yml').write_text('name: shop\nprofile: shop\n')
    (root / 'target').mkdir()
    sources: dict[str, dict[str, Any]] = {
        f'source.shop.raw.{name}': {
            'unique_id': f'source.shop.raw.{name}',
            'resource_type': 'source',
            'name': name,
            'identifier': name,
            'source_name': 'raw',
            'package_name': 'shop',
            'database': 'shop',
            'schema': 'main',
            'relation_name': f'"shop"."main"."{name}"',
            'columns': {},
        }
        for name in ('customers', 'payments')
    }
    if tests is None:
        tests = [
            _test(
                'not_null_total_paid',
                f'select total_paid from {TABLE} where total_paid is null',
                'not_null',
                'total_paid',
            ),
            _test(
                'unique_customer_id',
                f'select customer_id from {TABLE} where customer_id is not null group by customer_id having count(*) > 1',
                'unique',
                'customer_id',
            ),
            _test('fails_already', f'select * from {TABLE} where total_paid = 0'),
            _test('not_compiled', None, 'not_null', 'name'),
            _test('reads_a_missing_table', 'select * from "shop"."main"."nowhere"'),
            _test('lenient', f'select * from {TABLE} where payments is null', error_if='> 5', warn_if='> 5'),
        ]
    model = _node('model', 'customer_value', sql or MODEL_SQL, list(sources))
    nodes = {model['unique_id']: model, **{t['unique_id']: t for t in tests}}
    manifest = {
        'metadata': {'adapter_type': adapter},
        'nodes': nodes,
        'sources': sources,
        'exposures': {},
        'unit_tests': {u['unique_id']: u for u in units or []},
    }
    (root / 'target' / 'manifest.json').write_text(json.dumps(_relocate(manifest, database, schema)))
    project = Project(root)
    project.build(run_dbt=False)
    return project


@pytest.fixture
def shop(tmp_path: Path) -> Project:
    return make_shop(tmp_path)


def run_mutate(project: Project, **options: Any) -> MutationTesting:
    return mutate(project.graph, project.root, project.root / 'shop.duckdb', **options)


def outcome(result: MutationTesting, description: str, column: str | None = None) -> mutation.MutantResult:
    [model] = result.models
    return next(
        m
        for m in model.mutants
        if m.mutant.description == description and (column is None or m.mutant.column == column)
    )


def test_mutants_are_killed_survive_or_are_equivalent_on_the_data(shop: Project) -> None:
    result = run_mutate(shop, max_mutants=100)
    [model] = result.models
    assert model.skipped is None and model.rows == 8
    # Removing the COALESCE gives customer 8 a null total, which not_null catches.
    removed = outcome(result, 'remove the COALESCE from `COALESCE(paid.paid, 0)`', 'total_paid')
    assert removed.status == 'killed' and removed.killed_by == ['not_null_total_paid']
    assert removed.changed == ['total_paid']
    # In the tier, a null total falls to the default as a zero does: the same output on any data.
    tier = outcome(result, 'remove the COALESCE from `COALESCE(paid.paid, 0)`', 'tier')
    assert tier.status == 'indistinguishable'
    # Summing the largest payment instead of all of them changes totals no test looks at.
    swapped = outcome(result, 'SUM(amount) to MAX(amount)')
    assert swapped.status == 'survived' and swapped.killed_by == []
    assert set(swapped.changed) == {'total_paid', 'tier'}
    # No payment is void and no customer id is 0: the data cannot tell these mutants apart.
    for description in ("drop the WHERE condition `status <> 'void'`", 'c.id > 0 to c.id >= 0'):
        assert outcome(result, description).status == 'indistinguishable'
    # Fanning out the join repeats customers, which the unique test catches.
    fanned = outcome(result, 'drop the join condition `paid.customer_id = c.id`')
    assert (
        fanned.status == 'killed' and 'unique_customer_id' in fanned.killed_by and ROW_COUNT in fanned.changed
    )
    killed, survived = model.count('killed'), model.count('survived')
    assert model.score == killed / (killed + survived)
    assert model.count('indistinguishable') >= 2 and model.count('invalid') == 0
    assert model.sites == len(model.mutants)
    columns = model.columns()
    assert columns['total_paid'].killed >= 1 and columns['total_paid'].survived >= 1
    assert model.survivors == sorted(model.survivors, key=lambda m: -m.reach)


def test_tests_are_judged_on_the_original_first(shop: Project) -> None:
    result = run_mutate(shop, max_mutants=5)
    states = {t.name: (t.state, t.reason) for t in result.models[0].tests}
    assert states['not_null_total_paid'] == ('used', None)
    assert states['fails_already'] == ('fails on the sample', None)
    assert states['not_compiled'][0] == 'not run' and 'no compiled SQL' in str(states['not_compiled'][1])
    assert states['reads_a_missing_table'][0] == 'not run' and 'shop.main.nowhere' in str(
        states['reads_a_missing_table'][1]
    )
    # The lenient test fails only above five failing rows, so one null in payments passes.
    assert states['lenient'] == ('used', None)


def test_survivors_get_a_test_that_kills_them(shop: Project) -> None:
    # A flipped comparison empties the model, and those survivors, with the most reach, would take
    # the unit tests; they are left out so the aggregate swap is given one.
    result = run_mutate(shop, max_mutants=100, operators=tuple(o for o in OPERATORS if o != 'comparison'))
    swapped = outcome(result, 'SUM(amount) to MAX(amount)')
    [unit] = swapped.suggestions
    assert unit.test == 'unit_test'
    assert unit.where == 'the unit_tests of a properties file'
    [spec] = yaml.safe_load(unit.yaml)
    assert spec['model'] == 'customer_value'
    inputs = {given['input']: given['rows'] for given in spec['given']}
    assert set(inputs) == {"source('raw', 'customers')", "source('raw', 'payments')"}
    assert 0 < sum(len(rows) for rows in inputs.values()) <= 4
    # The unit test is a witness: on its rows the original gives the expected rows, the mutant not.
    con = duckdb.connect()
    con.execute('attach ":memory:" as shop')
    for name in ('customers', 'payments'):
        rows = inputs[f"source('raw', '{name}')"]
        columns = 'id, name' if name == 'customers' else 'id, customer_id, amount, status'
        types = (
            'id int, name varchar'
            if name == 'customers'
            else 'id int, customer_id int, amount int, status varchar'
        )
        con.execute(f'create table shop.main.{name} ({types})')
        for row in rows:
            con.execute(
                f'insert into shop.main.{name} ({columns}) values ({", ".join("?" for _ in row)})',
                list(row.values()),
            )
    expected = sorted(tuple(r.values()) for r in spec['expect']['rows'])
    assert sorted(con.execute(MODEL_SQL).fetchall()) == expected
    assert sorted(con.execute(swapped.mutant.sql).fetchall()) != expected
    con.close()
    # Unit tests, which take the most runs, go to the three survivors with the most reach.
    survivors = result.models[0].survivors
    assert all(s.suggestions for s in survivors[:3]) and len(survivors) > 3
    assert all(s.test != 'unit_test' for m in survivors[3:] for s in m.suggestions)
    # A survivor a generic test kills is given that test instead.
    dropped = outcome(result, "drop the CASE default `ELSE 'standard'`")
    assert dropped.status == 'survived'
    assert [(s.test, s.column) for s in dropped.suggestions] == [('not_null', 'tier')]
    assert (
        dropped.suggestions[0].reason == 'tier is never null on the sample, and null in 5 rows of the mutant'
    )


def test_the_unit_test_search_starts_from_a_few_rows(shop: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    """A witness is looked for in the first rows of each input before the whole sample, so delta
    debugging does not start by running every row the sample read."""
    sizes: list[int] = []
    values_sql = mutation._values_sql

    def recorded(columns: list[tuple[str, str]], rows: list[tuple[Any, ...]]) -> str:
        sizes.append(len(rows))
        return values_sql(columns, rows)

    monkeypatch.setattr(mutation, '_values_sql', recorded)
    result = run_mutate(shop, max_mutants=100, operators=('aggregate',))
    swapped = outcome(result, 'SUM(amount) to MAX(amount)')
    [unit] = swapped.suggestions
    assert unit.test == 'unit_test' and sizes and max(sizes) <= 8 < 24


def test_a_unit_test_is_suggested_once(shop: Project) -> None:
    """Two survivors with the same witness rows share one unit test rather than two copies of it."""
    survivors = run_mutate(shop, max_mutants=100).models[0].survivors
    units = [s for m in survivors for s in m.suggestions if s.test == 'unit_test']
    written = [yaml.safe_load(s.yaml)[0]['name'] for s in units if s.yaml]
    shared = [s for s in units if not s.yaml]
    assert shared and len(written) == len(set(written))
    for suggestion in shared:
        [name] = [n for n in written if f'the unit test {n},' in suggestion.reason]
        assert suggestion.reason == f'the unit test {name}, suggested for another survivor, kills it too'
    assert len({yaml.dump(yaml.safe_load(s.yaml)[0]['given']) for s in units if s.yaml}) == len(written)
    markdown = run_mutate(shop, max_mutants=100).markdown()
    assert 'suggested for another survivor, kills it too.' in markdown


def test_runs_are_reproducible_and_read_only(shop: Project) -> None:
    warehouse = shop.root / 'shop.duckdb'
    before = warehouse.stat().st_mtime_ns, warehouse.read_bytes()
    first = run_mutate(shop, max_mutants=6, seed=3).to_dict()
    second = run_mutate(shop, max_mutants=6, seed=3).to_dict()
    for data in (first, second):
        data.pop('seconds')
        for model in data['models']:
            model.pop('seconds')
    assert first == second
    assert len(first['models'][0]['mutants']) == 6
    other = run_mutate(shop, max_mutants=6, seed=4).to_dict()
    assert [m['id'] for m in other['models'][0]['mutants']] != [
        m['id'] for m in first['models'][0]['mutants']
    ]
    assert (warehouse.stat().st_mtime_ns, warehouse.read_bytes()) == before


def test_reports(shop: Project) -> None:
    result = run_mutate(shop, max_mutants=100)
    text = str(result)
    assert 'customer_value' in text and 'Mutation score' in text and 'Survivors, by downstream reach' in text
    assert 'fails_already (fails on the sample)' in text
    markdown = result.markdown()
    assert markdown.startswith('# Konstruct mutation testing')
    assert (
        '| `total_paid` |' in markdown
        and '```yaml' in markdown
        and 'Not distinguishable on this data' in markdown
    )
    data = result.to_dict()
    assert data['totals']['mutants'] == len(result.models[0].mutants)
    assert data['score'] == round(cast(float, result.score), 4)
    assert set(data['models'][0]['counts']) == set(mutation.STATUSES)
    json.dumps(data)


def test_suggestions_can_be_left_out_and_models_chosen(shop: Project) -> None:
    result = run_mutate(shop, max_mutants=100, suggest=False)
    assert all(not m.suggestions for m in result.models[0].mutants)
    assert run_mutate(shop, only=['nothing']).models == []
    assert str(run_mutate(shop, only=['nothing'])).endswith('No model could be mutation tested.')
    assert run_mutate(shop, only=['customer_value'], operators=('coalesce',)).totals()['mutants'] == 2


def test_a_model_that_will_not_run_is_skipped(tmp_path: Path) -> None:
    project = make_shop(tmp_path)
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('drop table payments')
    con.close()
    result = run_mutate(project)
    [model] = result.models
    assert model.skipped is not None and 'payments' in model.skipped
    assert 'skipped' in str(result) and 'skipped' in result.markdown()
    assert result.score is None and result.totals()['skipped'] == 1


def test_without_tests_every_distinguishable_mutant_survives(tmp_path: Path) -> None:
    project = make_shop(tmp_path, tests=[])
    result = run_mutate(project, max_mutants=8, suggest=False)
    [model] = result.models
    assert model.count('killed') == 0 and model.count('survived') > 0 and model.score == 0.0


def test_relationships_are_suggested_from_the_derived_ontology(tmp_path: Path) -> None:
    """A survivor that gives a customer id no customer has is killed by a relationships test."""
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('create table customers as select i as id from range(1, 6) t(i)')
    con.execute(
        "create table orders as select i as id, 1 + i % 5 as customer_id, case when i % 4 = 0 then 'void' else 'ok' end "
        'as status from range(1, 21) t(i)'
    )
    con.close()
    sql = (
        "select o.id as order_id, case when o.status = 'void' then -1 else c.id end as customer_id "
        'from "shop"."main"."orders" o join "shop"."main"."customers" c on c.id = o.customer_id '
        "where o.status <> 'void'"
    )
    (tmp_path / 'dbt_project.yml').write_text('name: shop\n')
    (tmp_path / 'target').mkdir()
    model = _node('model', 'order_customers', sql, [])
    unique = _node(
        'test',
        'unique_customers_id',
        'select id from "shop"."main"."customers" group by id having count(*) > 1',
        ['source.shop.raw.customers'],
        test_metadata={'name': 'unique', 'kwargs': {}},
        column_name='id',
        attached_node='source.shop.raw.customers',
    )
    source = {
        'unique_id': 'source.shop.raw.customers',
        'resource_type': 'source',
        'name': 'customers',
        'identifier': 'customers',
        'source_name': 'raw',
        'package_name': 'shop',
        'database': 'shop',
        'schema': 'main',
        'relation_name': '"shop"."main"."customers"',
        'columns': {'id': {'name': 'id'}},
    }
    manifest = {
        'metadata': {'adapter_type': 'duckdb'},
        'nodes': {model['unique_id']: model, unique['unique_id']: unique},
        'sources': {source['unique_id']: source},
        'exposures': {},
    }
    (tmp_path / 'target' / 'manifest.json').write_text(json.dumps(manifest))
    project = Project(tmp_path)
    project.build(run_dbt=False)
    result = run_mutate(project, max_mutants=50)
    dropped = outcome(result, "drop the WHERE condition `o.status <> 'void'`")
    assert dropped.status == 'survived'
    suggestion = next(s for s in dropped.suggestions if s.test == 'relationships')
    assert suggestion.column == 'customer_id'
    [arguments] = yaml.safe_load(suggestion.yaml)
    assert arguments == {'relationships': {'arguments': {'to': "source('raw', 'customers')", 'field': 'id'}}}


def test_sql_that_writes_is_refused_before_anything_runs(tmp_path: Path) -> None:
    """A model or test whose compiled SQL holds a second statement is never run, in whole or in part."""
    written = tmp_path / 'written.csv'
    copy = f"; copy (select 42) to '{written}'"
    model = make_shop(tmp_path, sql=MODEL_SQL.rstrip() + copy, tests=[])
    [result] = run_mutate(model).models
    assert result.skipped is not None and '2 statements' in result.skipped and result.mutants == []
    assert not written.exists()
    other = tmp_path / 'other'
    other.mkdir()
    test = _test('copies', f'select * from {TABLE} where total_paid is null' + copy.replace('written', 'w2'))
    [checked] = run_mutate(make_shop(other, tests=[test]), max_mutants=4).models
    [state] = checked.tests
    assert state.state == 'not run' and '2 statements' in str(state.reason)
    assert not (tmp_path / 'w2.csv').exists()


def test_a_model_whose_tests_cannot_run_is_skipped_not_scored(tmp_path: Path) -> None:
    """Tests that could not run say nothing about the model: it is skipped, not given 0%."""
    tests = [
        _test('not_compiled', None, 'not_null', 'name'),
        _test('fails_already', f'select * from {TABLE} where total_paid = 0'),
    ]
    result = run_mutate(make_shop(tmp_path, tests=tests), max_mutants=5)
    [model] = result.models
    assert model.skipped is not None and 'run dbt compile' in model.skipped
    assert [(t.name, t.state) for t in model.tests] == [
        ('fails_already', 'fails on the sample'),
        ('not_compiled', 'not run'),
    ]
    assert model.mutants == [] and model.score is None and result.score is None
    assert result.totals()['skipped'] == 1 and 'run dbt compile' in str(result)


SHOP_UNIT_GIVEN = [
    {'input': "source('raw', 'customers')", 'rows': [{'id': 0, 'name': 'Z'}, {'id': 1, 'name': 'A'}]},
    {
        'input': 'source("raw","payments")',
        # Rows given in CSV reach the manifest as strings.
        'rows': [
            {'id': '1', 'customer_id': '1', 'amount': '30', 'status': 'ok'},
            {'id': '2', 'customer_id': '1', 'amount': '5', 'status': 'void'},
        ],
    },
]


def test_dbt_unit_tests_kill_mutants(tmp_path: Path) -> None:
    """A project's own unit tests run against each mutant on their given rows, not on the sample."""
    units = [
        _unit(
            'counts_only_paid_customers',
            SHOP_UNIT_GIVEN,
            [{'customer_id': 1, 'total_paid': 30, 'tier': 'standard'}],
        ),
        _unit('expects_wrongly', SHOP_UNIT_GIVEN, [{'customer_id': 1, 'total_paid': 31}]),
        _unit('misses_an_input', SHOP_UNIT_GIVEN[:1], [{'customer_id': 1}]),
        _unit('overrides', SHOP_UNIT_GIVEN, [{'customer_id': 1}], overrides={'vars': {'x': 1}}),
        _unit(
            'reads_a_fixture', [{**SHOP_UNIT_GIVEN[0], 'rows': None, 'fixture': 'f'}, SHOP_UNIT_GIVEN[1]], []
        ),
    ]
    project = make_shop(tmp_path, tests=[], units=units)
    result = run_mutate(project, max_mutants=100, suggest=False)
    [model] = result.models
    states = {t.name: (t.test, t.state, t.reason) for t in model.tests}
    assert states['counts_only_paid_customers'] == ('unit_test', 'used', None)
    assert states['expects_wrongly'][1] == 'fails on the original'
    assert states['misses_an_input'][1:] == ('not run', "it gives no rows for source('raw', 'payments')")
    assert states['overrides'][1] == 'not run' and 'overrides' in str(states['overrides'][2])
    assert states['reads_a_fixture'][1] == 'not run' and 'fixture' in str(states['reads_a_fixture'][2])
    # The sample has no customer 0 and no void payment, so only the unit test's rows tell these apart.
    for description in ('c.id > 0 to c.id >= 0', "drop the WHERE condition `status <> 'void'`"):
        found = outcome(result, description)
        assert found.status == 'killed' and found.killed_by == ['counts_only_paid_customers']
        assert found.changed == []
    # One paid payment: its sum is its maximum, so the unit test lets this through.
    assert outcome(result, 'SUM(amount) to MAX(amount)').status == 'survived'
    unit = next(t for t in model.tests if t.name == 'counts_only_paid_customers')
    assert unit.kills == sum(1 for m in model.mutants if 'counts_only_paid_customers' in m.killed_by)


def test_unit_test_values_compare_as_dbt_casts_them() -> None:
    assert mutation._unit_key(2.5, 'number') == mutation._unit_key('2.50', 'number')
    assert mutation._unit_key(30, 'number') == mutation._unit_key('30', 'number')
    assert mutation._unit_key(True, 'boolean') == mutation._unit_key('true', 'boolean')
    stamp = datetime.datetime(2024, 1, 2)
    assert mutation._unit_key(stamp, 'time') == mutation._unit_key('2024-01-02', 'time')
    assert mutation._unit_key(datetime.date(2024, 1, 2), 'time') == mutation._unit_key(
        '2024-01-02 00:00:00', 'time'
    )
    assert mutation._unit_key('x', 'number') != mutation._unit_key(0, 'number')
    assert mutation._unit_key(None, 'text') is None and mutation._unit_key('a', 'text') == ('text', 'a')


def test_a_model_with_the_last_date_is_mutation_tested(tmp_path: Path) -> None:
    sql = (
        "select id, name, coalesce(cast(null as varchar), '9999-12-31') as valid_to "
        'from "shop"."main"."customers" where id > 0'
    )
    [model] = run_mutate(make_shop(tmp_path, sql=sql, tests=[]), suggest=False).models
    assert model.skipped is None and model.mutants


def test_a_unit_test_compares_the_columns_of_its_first_expected_row(tmp_path: Path) -> None:
    """As in dbt, the first expected row names the columns compared; a later row's extra column is
    not compared."""
    given = [
        {'input': "source('raw', 'customers')", 'rows': [{'id': 1, 'name': 'A'}, {'id': 2, 'name': 'B'}]},
        {
            'input': "source('raw', 'payments')",
            'rows': [{'id': 1, 'customer_id': 1, 'amount': 30, 'status': 'ok'}],
        },
    ]
    expect: list[dict[str, Any]] = [
        {'customer_id': 1, 'total_paid': 30},
        {'customer_id': 2, 'total_paid': 0, 'name': 'not compared'},
    ]
    project = make_shop(tmp_path, tests=[], units=[_unit('first_row_columns', given, expect)])
    [model] = run_mutate(project, max_mutants=5, suggest=False).models
    assert [(t.name, t.state) for t in model.tests] == [('first_row_columns', 'used')]


def test_equivalent_mutants_are_told_from_gaps_in_the_data(tmp_path: Path) -> None:
    """A mutant that is the same query in canonical form is equivalent; one whose output is only
    the same on this sample is not distinguishable on this data, which is a different claim."""
    sql = (
        'with p as (select id, customer_id, coalesce(amount, 0) as unused, amount, status '
        'from "shop"."main"."payments") '
        "select customer_id, sum(amount) as paid from p where status = 'ok' and id > 0 group by customer_id"
    )
    result = run_mutate(make_shop(tmp_path, sql=sql, tests=[]), max_mutants=100, suggest=False)
    removed = outcome(result, 'remove the COALESCE from `COALESCE(amount, 0)`')
    assert removed.status == 'equivalent'
    assert outcome(result, 'id > 0 to id >= 0').status == 'indistinguishable'
    text = str(result)
    assert '1 mutant is the same query as the original' in text
    assert 'gave the same output as the original on this data' in text
    markdown = result.markdown()
    assert 'Equivalent to the original:' in markdown and 'Not distinguishable on this data:' in markdown


def test_jaffle_unit_tests(jaffle: Path, dbt_on_path: None) -> None:
    """A unit test dbt itself passes is run against each mutant of the jaffle_shop orders model."""
    from .conftest import run_dbt

    (jaffle / 'models' / 'unit_tests.yml').write_text(JAFFLE_UNIT_TEST)
    run_dbt(jaffle, 'test', '--select', 'test_type:unit')  # it passes on the real model
    run_dbt(jaffle, 'compile')
    project = Project(jaffle)
    project.build(run_dbt=False)
    result = mutate(
        project.graph, jaffle, jaffle / 'jaffle_shop.duckdb', only=['orders'], max_mutants=40, suggest=False
    )
    [orders] = result.models
    unit = next(t for t in orders.tests if t.test == 'unit_test')
    assert (unit.name, unit.state) == ('orders_add_up_payments', 'used')
    killed = [m for m in orders.mutants if 'orders_add_up_payments' in m.killed_by]
    assert killed and unit.kills == len(killed)
    # A coupon swapped for its own case still gives the coupon amount, so the case default is the witness.
    assert any(m.mutant.description == 'drop the CASE default `ELSE 0`' for m in killed)


JAFFLE_UNIT_TEST = """
unit_tests:
  - name: orders_add_up_payments
    model: orders
    given:
      - input: ref('stg_orders')
        rows:
          - {order_id: 1, customer_id: 7, order_date: '2018-01-01', status: completed}
          - {order_id: 2, customer_id: 7, order_date: '2018-01-02', status: returned}
      - input: ref('stg_payments')
        format: csv
        rows: |
          payment_id,order_id,payment_method,amount
          1,1,coupon,10
          2,1,credit_card,5
    expect:
      rows:
        - {order_id: 1, coupon_amount: 10, credit_card_amount: 5, gift_card_amount: 0, amount: 15}
        - {order_id: 2, coupon_amount: null, credit_card_amount: null, gift_card_amount: null, amount: null}
"""


def test_jaffle_shop(jaffle_built_graph: Path) -> None:
    from konstruct.analysis.query import load_graph

    graph = load_graph(jaffle_built_graph / 'konstruct-out')
    started = time.perf_counter()
    result = mutate(graph, jaffle_built_graph, jaffle_built_graph / 'jaffle_shop.duckdb', max_mutants=12)
    assert time.perf_counter() - started < 60
    by_label = {m.label: m for m in result.models}
    assert {'customers', 'orders'} <= set(by_label)
    orders = by_label['orders']
    assert orders.skipped is None and orders.rows == 99
    assert len(orders.mutants) <= 12
    # Dropping a CASE default gives nulls the not_null tests on the payment amounts catch.
    defaults = [m for m in orders.mutants if m.mutant.description == 'drop the CASE default `ELSE 0`']
    assert defaults and all(m.status == 'killed' for m in defaults)
    customers = by_label['customers']
    assert customers.count('survived') > 0 and all(s.suggestions for s in customers.survivors[:3])
    assert result.totals()['killed'] > 0 and 0 < cast(float, result.score) < 1


@pytest.mark.postgres
def test_postgres_in_a_rolled_back_transaction(tmp_path: Path) -> None:
    """The same project on Postgres: the same verdicts, and nothing left behind."""
    import uuid
    from urllib.parse import urlparse

    import psycopg

    from .test_warehouse import require

    dsn = require('KONSTRUCT_TEST_PG_DSN')
    database = urlparse(dsn).path.lstrip('/')
    schema = f'kp_{uuid.uuid4().hex[:8]}'
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f'create schema {schema}')
        con.execute(
            f'create table {schema}.customers as select i as id, chr(64 + i) as name from generate_series(1, 8) i'
        )
        con.execute(
            f'create table {schema}.payments as select i as id, 1 + i % 7 as customer_id, '
            f"10 + (i * 13) % 40 as amount, 'ok'::text as status from generate_series(1, 24) i"
        )
    try:
        project = write_shop(tmp_path, adapter='postgres', database=database, schema=schema)
        operators = tuple(o for o in OPERATORS if o != 'comparison')  # as in the DuckDB test above
        result = mutate(project.graph, project.root, dsn, max_mutants=100, operators=operators)
        [model] = result.models
        assert model.skipped is None and model.rows == 8 and result.warehouse.startswith('postgres database')
        removed = outcome(result, 'remove the COALESCE from `COALESCE(paid.paid, 0)`', 'total_paid')
        assert removed.status == 'killed' and removed.killed_by == ['not_null_total_paid']
        swapped = outcome(result, 'SUM(amount) to MAX(amount)')
        assert swapped.status == 'survived' and swapped.suggestions[0].test == 'unit_test'
        assert outcome(result, 'c.id > 0 to c.id >= 0').status == 'indistinguishable'
        with psycopg.connect(dsn) as con:
            leftovers = con.execute("select count(*) from pg_class where relname like 'kp\\_%'").fetchone()
            tables = con.execute(
                'select count(*) from information_schema.tables where table_schema = %s', [schema]
            ).fetchone()
        assert leftovers == (0,) and tables == (2,)
    finally:
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'drop schema if exists {schema} cascade')


class _Snowflake:
    """A Snowflake stand-in whose samples are read from the shop's DuckDB file."""

    dialect = 'snowflake'

    def __init__(self, path: Path) -> None:
        self.con = duckdb.connect(str(path), read_only=True)
        self.read: list[tuple[str, ...]] = []

    def sample(
        self, parts: tuple[str, ...], limit: int
    ) -> tuple[list[str], list[str | None], list[tuple[Any, ...]]]:
        self.read.append(parts)
        cursor = self.con.execute(f'select * from {".".join(parts)} limit {limit}')
        return [d[0] for d in cursor.description], [None] * len(cursor.description), cursor.fetchall()

    def close(self) -> None:
        self.con.close()


def test_a_sampled_cloud_warehouse(shop: Project) -> None:
    """Mutants of Snowflake SQL run translated to DuckDB on a sample read once per input."""
    from konstruct.analysis.verify import SampledEngine

    source = _Snowflake(shop.root / 'shop.duckdb')
    engine = SampledEngine(source, 'snowflake (sampled)', shop.graph)
    result = mutate(shop.graph, shop.root, engine, max_mutants=100, suggest=False)
    [model] = result.models
    assert model.skipped is None and result.warehouse == 'snowflake (sampled)'
    assert (
        outcome(result, 'remove the COALESCE from `COALESCE(paid.paid, 0)`', 'total_paid').status == 'killed'
    )
    assert outcome(result, 'SUM(amount) to MAX(amount)').status == 'survived'
    # Each relation is asked for once, the one a test reads that is not there included.
    assert sorted(source.read) == [('shop', 'main', name) for name in ('customers', 'nowhere', 'payments')]
