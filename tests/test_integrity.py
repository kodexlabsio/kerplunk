"""The ontology's keys and relationships, checked against the warehouse by `konstruct verify`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import duckdb
import pytest

from konstruct import Project
from konstruct.analysis.integrity import KeyCheck, LinkCheck, check
from konstruct.analysis.verify import DuckDBEngine, Engine, SampledEngine

REPORT = """
create table main.report as
select o.order_id, c.name
from main.orders o join main.customers c on o.customer_id = c.customer_id
"""

# A key named in definitions.yml has no constraint, so the warehouse can hold duplicates of it.
DEFINITIONS = """
version: 1
terms:
  - name: Customer
    kind: entity
    columns: [customers.customer_id]
"""


def shop(root: Path, *, orphans: int = 0, duplicates: int = 0, nulls: int = 0) -> Project:
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create table main.customers (customer_id integer, name varchar)')
    con.execute("insert into main.customers select i, 'c' || i from range(1, 11) t(i)")
    for i in range(duplicates):
        con.execute(f"insert into main.customers values ({i + 1}, 'again')")
    for _ in range(nulls):
        con.execute("insert into main.customers values (null, 'nobody')")
    con.execute('create table main.orders (order_id integer primary key, customer_id integer)')
    con.execute('insert into main.orders select i, 1 + i % 10 from range(1, 31) t(i)')
    for i in range(orphans):
        con.execute(f'insert into main.orders values ({100 + i}, {500 + i})')
    con.execute('insert into main.orders values (200, null)')  # no customer, which is not an orphan
    con.close()
    (root / 'report.sql').write_text(REPORT)
    (root / 'definitions.yml').write_text(DEFINITIONS)
    project = Project(root)
    project.build()
    return project


def checks(project: Project) -> tuple[dict[str, KeyCheck], list[LinkCheck]]:
    engine = DuckDBEngine(project.root / 'shop.duckdb')
    try:
        keys, links = check(project.graph, engine)
    finally:
        engine.close()
    return {k.column: k for k in keys}, links


def test_true_keys_and_relationships_are_confirmed(tmp_path: Path) -> None:
    keys, [link] = checks(shop(tmp_path))
    assert {c: k.state for c, k in keys.items()} == {
        'customers.customer_id': 'confirmed',
        'orders.order_id': 'confirmed',
    }
    assert (keys['customers.customer_id'].rows, keys['orders.order_id'].rows) == (10, 31)
    assert (link.source, link.target, link.state, link.values, link.orphans) == (
        'order',
        'customer',
        'confirmed',
        30,
        0,
    )
    assert link.detail == 'all 30 values of orders.customer_id are found in customers.customer_id'


def test_an_orphan_fails_the_relationship(tmp_path: Path) -> None:
    _, [link] = checks(shop(tmp_path, orphans=2))
    assert (link.state, link.values, link.orphans) == ('failed', 32, 2)
    assert link.detail == '2 of 32 values of orders.customer_id are not found in customers.customer_id'


def test_duplicates_and_nulls_fail_the_key_and_its_relationships(tmp_path: Path) -> None:
    keys, [link] = checks(shop(tmp_path, duplicates=2, nulls=1))
    key = keys['customers.customer_id']
    assert (key.state, key.rows, key.duplicates, key.nulls) == ('failed', 13, 2, 1)
    assert key.detail == 'customers.customer_id has 2 duplicate values and 1 null in 13 rows'
    # Every order has its customer, but a customer_id that repeats is no key: many-to-one fails.
    assert (link.state, link.orphans) == ('failed', 0)
    assert link.detail.endswith(
        'but customers.customer_id, the column it refers to, is not unique and never null'
    )


def test_verify_marks_the_ontology_verified_only_where_the_data_agrees(tmp_path: Path) -> None:
    good = tmp_path / 'good'
    good.mkdir()
    project = shop(good)
    before = project.ontology()
    assert [e.confidence for e in before.entities] == ['declared', 'declared']
    assert before.relationships[0].confidence == 'inferred'
    result = project.verify(warehouse=good / 'shop.duckdb')
    assert result.checks() == (
        'Ontology: 2 of 2 keys confirmed, 0 failed, 0 not checked; '
        '1 of 1 relationships confirmed, 0 failed, 0 not checked.'
    )
    after = Project(good).ontology()
    assert [(e.confidence, e.check) for e in after.entities] == [('verified', 'confirmed')] * 2
    [rel] = after.relationships
    assert (rel.confidence, rel.check) == ('verified', 'confirmed')
    assert rel.evidence[-1] == (
        'konstruct verify: all 30 values of orders.customer_id are found in customers.customer_id '
        '(shop.duckdb)'
    )
    assert 'verified' in (good / 'konstruct-out' / 'ontology.yml').read_text()
    assert '## Ontology keys and relationships' in (good / 'konstruct-out' / 'VERIFY.md').read_text()

    bad = tmp_path / 'bad'
    bad.mkdir()
    project = shop(bad, orphans=1)
    result = project.verify(warehouse=bad / 'shop.duckdb')
    [rel] = Project(bad).ontology().relationships
    # The join still shows the relationship; the data contradicts it, so it is never verified.
    assert (rel.confidence, rel.check) == ('inferred', 'failed')
    assert 'check failed' in str(Project(bad).ontology())
    assert 'failed  1 of 31 values of orders.customer_id' in str(result)


def test_only_the_named_models_are_checked(tmp_path: Path) -> None:
    project = shop(tmp_path)
    engine = DuckDBEngine(tmp_path / 'shop.duckdb')
    try:
        keys, links = check(project.graph, engine, ['customers'])
    finally:
        engine.close()
    assert [k.column for k in keys] == ['customers.customer_id']
    assert links == []


class _Sampled(SampledEngine):
    def __init__(self) -> None:
        self.label = 'snowflake (sampled)'


@pytest.mark.parametrize(
    ('engine', 'reason'),
    [
        (_Sampled(), 'verify reads only a sample of each table of a remote warehouse'),
        (Engine(), 'does not read whole tables'),
    ],
)
def test_a_sample_checks_nothing(tmp_path: Path, engine: Engine, reason: str) -> None:
    """A duplicate or the row a value refers to can lie outside a sample, so nothing is checked
    and nothing becomes verified."""
    project = shop(tmp_path)
    keys, links = check(project.graph, engine)
    assert {k.state for k in keys} == {'skipped'} and {link.state for link in links} == {'skipped'}
    assert all(k.reason and k.reason.endswith(reason) for k in keys)
    from konstruct.analysis.integrity import annotate

    annotate(project.graph, keys, links, engine.label)
    assert project.graph.meta['integrity']['keys'] == {} and project.graph.meta['integrity']['links'] == {}
    assert {e.confidence for e in project.ontology().entities} == {'declared'}


def test_types_the_warehouse_cannot_compare_skip_the_check(tmp_path: Path) -> None:
    project = shop(tmp_path)
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('alter table main.customers alter customer_id type varchar')
    con.execute("update main.customers set customer_id = 'c' || customer_id")
    con.close()
    keys, [link] = checks(project)
    assert keys['customers.customer_id'].state == 'confirmed'
    assert link.state == 'skipped' and link.reason
    assert link.to_dict()['detail'].startswith('orders.customer_id = customers.customer_id not checked: ')


def test_an_empty_table_is_not_evidence(tmp_path: Path) -> None:
    project = shop(tmp_path)
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('delete from main.orders')
    con.close()
    keys, [link] = checks(project)
    assert (keys['orders.order_id'].state, keys['orders.order_id'].reason) == (
        'skipped',
        'its table is empty',
    )
    assert (link.state, link.reason) == ('skipped', 'the referring column holds no value')


@pytest.mark.postgres
def test_postgres_keys_and_relationships_are_checked_in_a_read_only_transaction() -> None:
    import uuid

    import psycopg

    from konstruct.analysis.verify import PostgresEngine
    from konstruct.core.graph import Graph, Node

    from .test_warehouse import require

    dsn = require('KONSTRUCT_TEST_PG_DSN')
    schema = f'integrity_{uuid.uuid4().hex[:8]}'
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f'create schema {schema}')
        con.execute(
            f'create table {schema}.customers as select i as customer_id from generate_series(1, 5) i'
        )
        con.execute(
            f'create table {schema}.orders as select i as order_id, 1 + i % 6 as customer_id from generate_series(1, 12) i'
        )
        database = str(con.execute('select current_database()').fetchone()[0])  # type: ignore[index]
    try:
        graph = Graph()
        for name, columns in (('customers', ['customer_id']), ('orders', ['order_id', 'customer_id'])):
            node = graph.add_node(
                Node(f'table.{name}', 'table', name, relation=f'{database}.{schema}.{name}')
            )
            for column in columns:
                graph.add_column(node, column)
        for column in ('table.customers#customer_id', 'table.orders#order_id'):
            test = graph.add_node(Node(f'test.{column}', 'test', 'unique', attrs={'test': 'unique'}))
            graph.add_edge(test.id, column, 'tests')
        relationship = graph.add_node(
            Node('test.rel', 'test', 'relationships', attrs={'test': 'relationships'})
        )
        graph.add_edge(relationship.id, 'table.orders#customer_id', 'tests')
        graph.add_edge(relationship.id, 'table.customers#customer_id', 'tests', role='relationship target')
        engine = PostgresEngine(dsn)
        try:
            keys, [link] = check(graph, engine)
        finally:
            engine.close()
        assert {k.column: k.state for k in keys} == {
            'customers.customer_id': 'confirmed',
            'orders.order_id': 'confirmed',
        }
        # customer_id 6 has no customer: orders 5 and 11.
        assert (link.state, link.values, link.orphans) == ('failed', 12, 2)
    finally:
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'drop schema {schema} cascade')


def _two_keys(root: Path) -> Project:
    """customer keyed in two tables, the second of which repeats its keys."""
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create table main.customers (customer_id integer, name varchar)')
    con.execute("insert into main.customers select i, 'c' || i from range(1, 11) t(i)")
    con.execute('create table main.crm_customers (customer_id integer, email varchar)')
    con.execute("insert into main.crm_customers select 1 + i % 3, 'e' from range(1, 11) t(i)")
    con.close()
    (root / 'report.sql').write_text(
        'create table main.report as select c.customer_id, c.name, k.email from main.customers c '
        'join main.crm_customers k on k.customer_id = c.customer_id\n'
    )
    (root / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - name: Customer\n    kind: entity\n'
        '    columns: [customers.customer_id, crm_customers.customer_id]\n'
    )
    project = Project(root)
    project.build()
    return project


def test_an_entity_checked_in_part_is_not_verified(tmp_path: Path) -> None:
    project = _two_keys(tmp_path)
    result = project.verify(warehouse=tmp_path / 'shop.duckdb', models=['customers'])
    assert [k.column for k in result.keys] == ['customers.customer_id']
    assert result.only == ['customers']
    [customer] = Project(tmp_path).ontology().entities
    assert (customer.confidence, customer.check) == ('declared', None)
    assert customer.evidence[-1] == 'konstruct verify did not check crm_customers.customer_id'
    # The whole run finds the second key repeated; what the first run found is kept beside it.
    project.verify(warehouse=tmp_path / 'shop.duckdb', models=['crm_customers'])
    [customer] = Project(tmp_path).ontology().entities
    assert (customer.confidence, customer.check) == ('declared', 'failed')
    assert len(Project(tmp_path).graph.meta['integrity']['keys']) == 2


def test_the_ontology_checks_can_be_left_out(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from konstruct.interfaces.cli import main

    shop(tmp_path)
    assert main(['-C', str(tmp_path), 'verify', '--no-build', '--no-ontology', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data['keys'], data['links'], data['ontology_seconds']) == ([], [], 0.0)
    assert 'integrity' not in Project(tmp_path).graph.meta
    result = Project(tmp_path).verify(warehouse=tmp_path / 'shop.duckdb')
    assert result.ontology_seconds > 0


def test_the_recorded_checks_name_the_warehouse_not_its_path(tmp_path: Path) -> None:
    project = shop(tmp_path)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    shutil.copy(tmp_path / 'shop.duckdb', elsewhere / 'copy.duckdb')
    project.verify(warehouse=elsewhere / 'copy.duckdb')
    recorded = json.dumps(Project(tmp_path).graph.meta['integrity'])
    assert str(tmp_path) not in recorded
    assert Project(tmp_path).graph.meta['integrity']['warehouse'] == 'copy.duckdb'
    assert str(tmp_path) not in (tmp_path / 'konstruct-out' / 'ontology.yml').read_text()


def test_a_one_to_one_claim_needs_a_unique_referring_column(tmp_path: Path) -> None:
    from konstruct.core.graph import Graph, Node

    con = duckdb.connect(str(tmp_path / 'w.duckdb'))
    con.execute('create table main.people as select i as person_id from range(1, 6) t(i)')
    con.execute(
        'create table main.passports as select i as passport_id, 1 + i % 2 as person_id from range(1, 6) t(i)'
    )
    con.close()
    graph = Graph()
    for name, columns in (('people', ['person_id']), ('passports', ['passport_id', 'person_id'])):
        node = graph.add_node(Node(f'table.{name}', 'table', name, relation=f'w.main.{name}'))
        for column in columns:
            graph.add_column(node, column)
    # Tests claim each passport's person_id unique, so the relationship is one-to-one.
    for column in ('table.people#person_id', 'table.passports#passport_id', 'table.passports#person_id'):
        test = graph.add_node(Node(f'test.{column}', 'test', 'unique', attrs={'test': 'unique'}))
        graph.add_edge(test.id, column, 'tests')
    test = graph.add_node(Node('test.rel', 'test', 'relationships', attrs={'test': 'relationships'}))
    graph.add_edge(test.id, 'table.passports#person_id', 'tests')
    graph.add_edge(test.id, 'table.people#person_id', 'tests', role='relationship target')
    engine = DuckDBEngine(tmp_path / 'w.duckdb')
    try:
        _, links = check(graph, engine)
    finally:
        engine.close()
    [link] = [item for item in links if item.target == 'person']
    # Every passport's person exists, so the relationship holds; it is many-to-one, not one-to-one.
    assert (link.state, link.orphans, link.one_to_one) == ('confirmed', 0, 'failed')
    assert link.detail.endswith('and passports.person_id repeats, so it is many-to-one')
    from konstruct.analysis.integrity import annotate
    from konstruct.semantics.ontology import derive

    annotate(graph, *check_on(graph, tmp_path / 'w.duckdb'), 'w.duckdb')
    [rel] = [r for r in derive(graph).relationships if r.target == 'person']
    assert (rel.cardinality, rel.check, rel.confidence) == ('many-to-one', 'confirmed', 'verified')


def check_on(graph: Any, warehouse: Path) -> tuple[list[KeyCheck], list[LinkCheck]]:
    engine = DuckDBEngine(warehouse)
    try:
        return check(graph, engine)
    finally:
        engine.close()


def test_a_one_to_one_claim_verify_could_not_check_stays_declared() -> None:
    from konstruct.core.graph import Graph, Node
    from konstruct.semantics.ontology import derive

    graph = Graph()
    for name, columns in (('people', ['person_id']), ('passports', ['passport_id', 'person_id'])):
        node = graph.add_node(Node(f'table.{name}', 'table', name, relation=f'w.main.{name}'))
        for column in columns:
            graph.add_column(node, column)
    for column in ('table.people#person_id', 'table.passports#passport_id', 'table.passports#person_id'):
        test = graph.add_node(Node(f'test.{column}', 'test', 'unique', attrs={'test': 'unique'}))
        graph.add_edge(test.id, column, 'tests')
    test = graph.add_node(Node('test.rel', 'test', 'relationships', attrs={'test': 'relationships'}))
    graph.add_edge(test.id, 'table.passports#person_id', 'tests')
    graph.add_edge(test.id, 'table.people#person_id', 'tests', role='relationship target')
    # Every value was found, but the referring column's uniqueness was not checked.
    graph.meta['integrity'] = {
        'keys': {},
        'links': {
            'table.passports#person_id|table.people#person_id': {'state': 'confirmed', 'detail': 'all found'}
        },
    }
    [rel] = [r for r in derive(graph).relationships if r.target == 'person']
    assert (rel.cardinality, rel.confidence) == ('one-to-one', 'verified')
    assert rel.evidence[-1] == (
        'konstruct verify did not check that passports.person_id is unique: one-to-one is declared, not verified'
    )
