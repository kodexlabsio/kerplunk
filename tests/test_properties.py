"""Property-based tests: invariants that must hold for any input, checked on generated cases."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from konstruct.core.graph import Graph, Node, column_id, relation_key
from konstruct.core.sql import analyse, strip_templating
from konstruct.inputs.airflow import parse_file

SETTINGS = settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])

# Graph traversal


@st.composite
def lineage_graphs(draw: st.DrawFn) -> Graph:
    """Relations r0..rn with columns; derived_from edges only point to lower-numbered relations,
    so the graph is acyclic, as column lineage is."""
    graph = Graph()
    count = draw(st.integers(2, 7))
    columns: list[list[str]] = []
    for index in range(count):
        node = graph.add_node(Node(f'model.p.r{index}', 'model', f'r{index}', relation=f'db.s.r{index}'))
        names = [f'c{j}' for j in range(draw(st.integers(1, 4)))]
        columns.append(names)
        for name in names:
            graph.add_column(node, name)
    for index in range(1, count):
        for name in columns[index]:
            for upstream in draw(st.lists(st.integers(0, index - 1), max_size=2, unique=True)):
                source = draw(st.sampled_from(columns[upstream]))
                graph.add_edge(
                    column_id(f'model.p.r{index}', name),
                    column_id(f'model.p.r{upstream}', source),
                    'derived_from',
                    transform=draw(st.sampled_from(['direct', 'transformed'])),
                )
                graph.add_edge(f'model.p.r{upstream}', f'model.p.r{index}', 'feeds')
    return graph


def column_ids(steps: list) -> set[str]:  # type: ignore[type-arg]
    return {s.node.id for s in steps if s.node.kind == 'column'}


@SETTINGS
@given(lineage_graphs())
def test_impact_and_trace_are_inverse(graph: Graph) -> None:
    for column in [n for n in graph.nodes.values() if n.kind == 'column']:
        for reached in column_ids(graph.impact(column)) - {column.id}:
            assert column.id in column_ids(graph.trace(graph.nodes[reached]))


@SETTINGS
@given(lineage_graphs(), st.integers(0, 4))
def test_a_depth_limit_only_removes_results(graph: Graph, depth: int) -> None:
    for node in list(graph.nodes.values()):
        limited = {s.node.id for s in graph.impact(node, max_depth=depth)}
        unlimited = {s.node.id for s in graph.impact(node)}
        assert limited <= unlimited
        assert all(
            s.depth <= depth for s in graph.impact(node, max_depth=depth) if s.via and s.via.kind != 'runs'
        )


@SETTINGS
@given(lineage_graphs())
def test_paths_exist_exactly_when_columns_are_connected(graph: Graph) -> None:
    columns = [n for n in graph.nodes.values() if n.kind == 'column']
    for start in columns[:4]:
        downstream = column_ids(graph.impact(start))
        for end in columns:
            steps = graph.path(start, end)
            if end.id in downstream:
                assert steps and steps[0].node.id == start.id and steps[-1].node.id == end.id


@SETTINGS
@given(lineage_graphs())
def test_json_round_trip_preserves_the_graph(graph: Graph) -> None:
    assert Graph.from_dict(graph.to_dict()).to_dict() == graph.to_dict()


@SETTINGS
@given(st.lists(st.text(alphabet='abcXYZ_"`[]', min_size=1, max_size=6), min_size=1, max_size=4))
def test_relation_key_is_idempotent(parts: list[str]) -> None:
    key = relation_key(*parts)
    assert relation_key(*key.split('.')) == key
    assert key == key.lower()
    assert not key.startswith('.') and not key.endswith('.') and '..' not in key


# SQL lineage against a known answer


@dataclass
class Expected:
    source: str
    transformed: bool


@st.composite
def cte_chains(draw: st.DrawFn) -> tuple[str, dict[str, Expected]]:
    """A query of CTE layers over one table, each renaming, computing or dropping columns,
    with the base column and transform every output column must trace to."""
    base = [f'b{i}' for i in range(draw(st.integers(1, 5)))]
    current = {name: Expected(name, False) for name in base}
    ctes = []
    previous = 'src.base'
    for layer in range(draw(st.integers(1, 4))):
        selects = []
        following: dict[str, Expected] = {}
        names = list(current)
        keep = draw(st.lists(st.sampled_from(names), min_size=1, max_size=len(names), unique=True))
        for position, name in enumerate(keep):
            action = draw(st.sampled_from(['keep', 'rename', 'compute']))
            if action == 'keep':
                selects.append(name)
                following[name] = current[name]
            elif action == 'rename':
                new = f'l{layer}_{position}'
                selects.append(f'{name} as {new}')
                following[new] = current[name]
            else:
                new = f'l{layer}_{position}x'
                selects.append(f'{name} * 2 + 1 as {new}')
                following[new] = Expected(current[name].source, True)
        ctes.append(f'layer{layer} as (select {", ".join(selects)} from {previous})')
        previous = f'layer{layer}'
        current = following
    star = draw(st.booleans())
    final = f'select * from {previous}' if star else f'select {", ".join(current)} from {previous}'
    return f'with {", ".join(ctes)} {final}', current


@SETTINGS
@given(cte_chains(), st.booleans())
def test_lineage_through_generated_ctes(case: tuple[str, dict[str, Expected]], with_schema: bool) -> None:
    sql, expected = case
    schema = {'db.src.base': {f'b{i}': 'int' for i in range(5)}} if with_schema else {}
    [result] = analyse(sql, dialect='duckdb', schema=schema, default_catalog='db', default_db='main')
    assert set(result.columns) == set(expected), sql
    for name, want in expected.items():
        sources = result.columns[name]
        assert [(s.relation, s.column) for s in sources] == [('db.src.base', want.source)], sql
        assert sources[0].transform == ('transformed' if want.transformed else 'direct'), sql


@settings(deadline=None)
@given(st.text(max_size=200))
def test_analyse_never_raises(text: str) -> None:
    for result in analyse(text, dialect='duckdb'):
        assert isinstance(result.errors, list)
    strip_templating(text)


@settings(deadline=None)
@given(
    st.sampled_from(['duckdb', 'postgres', 'snowflake', 'bigquery', 'trino', 'tsql', 'databricks']),
    cte_chains(),
)
def test_every_dialect_handles_plain_sql(dialect: str, case: tuple[str, dict[str, Expected]]) -> None:
    sql, expected = case
    [result] = analyse(sql, dialect=dialect)
    assert set(result.columns) == set(expected)


# Airflow dependencies


@st.composite
def dag_sources(draw: st.DrawFn) -> tuple[str, int, set[tuple[str, str]]]:
    count = draw(st.integers(1, 8))
    edges = set(
        draw(
            st.lists(
                st.tuples(st.integers(0, count - 1), st.integers(0, count - 1)).filter(lambda p: p[0] < p[1]),
                max_size=12,
            )
        )
    )
    lines = []
    for upstream, downstream in sorted(edges):
        style = draw(st.sampled_from(['>>', '<<', 'set_downstream', 'set_upstream', 'chain']))
        a, b = f't{upstream}', f't{downstream}'
        lines.append(
            {
                '>>': f'{a} >> {b}',
                '<<': f'{b} << {a}',
                'set_downstream': f'{a}.set_downstream({b})',
                'set_upstream': f'{b}.set_upstream({a})',
                'chain': f'chain({a}, {b})',
            }[style]
        )
    tasks = '\n'.join(f'    t{i} = EmptyOperator(task_id="task_{i}")' for i in range(count))
    body = '\n'.join(f'    {line}' for line in lines)
    source = (
        textwrap.dedent(
            """
        from airflow.sdk import DAG, chain
        from airflow.providers.standard.operators.empty import EmptyOperator

        with DAG("generated"):
        """
        )
        + tasks
        + '\n'
        + body
        + '\n'
    )
    return source, count, {(f'task_{a}', f'task_{b}') for a, b in edges}


@SETTINGS
@given(dag_sources())
def test_airflow_dependencies_match_generated_dags(case: tuple[str, int, set[tuple[str, str]]]) -> None:
    import tempfile

    source, count, edges = case
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'dag.py'
        path.write_text(source)
        [dag] = parse_file(path)
    assert len(dag.tasks) == count
    assert set(dag.dependencies) == edges
