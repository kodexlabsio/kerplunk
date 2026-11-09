"""Apache Ossie semantic models: read as an input, written as an export."""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path
from typing import Any

import duckdb
import jsonschema
import pytest
import yaml

from konstruct import Project
from konstruct.cli import main
from konstruct.ossie import datatype, parse

FIXTURES = Path(__file__).parent / 'fixtures' / 'ossie'
SCHEMA = json.loads((FIXTURES / 'ossie-schema.json').read_text())


def valid(text: str) -> Any:
    document = yaml.safe_load(text)
    jsonschema.Draft202012Validator(SCHEMA).validate(document)
    assert isinstance(document, dict)
    return document


def test_reads_the_draft_and_the_released_shape() -> None:
    draft = parse(
        textwrap.dedent(
            """\
            version: "0.2.0.dev0"
            name: shop
            datasets:
              - name: orders
                source: main.orders
                primary_key: [order_id]
                fields:
                  - {name: order_id, expression: {sql: id}}
                  - name: total
                    expression:
                      dialects:
                        - {dialect: SNOWFLAKE, expression: "amount_cents / 100"}
                        - {dialect: ANSI_SQL, expression: "amount_cents / 100.0"}
                metrics:
                  - {name: order_total, expression: {sql: SUM(total)}}
            metrics:
              - name: revenue
                description: Money in.
                ai_context: {synonyms: [sales]}
                expression: {sql: SUM(orders.total)}
            """
        ),
        'shop.ossie.yaml',
    )
    [model] = draft.models
    assert model.datasets['orders'].fields == {
        'order_id': ('id', None),
        'total': ('amount_cents / 100.0', None),
    }
    assert model.datasets['orders'].primary_key == ['order_id']
    assert [(m.name, m.dataset, m.sql) for m in model.metrics] == [
        ('order_total', 'orders', 'SUM(total)'),
        ('revenue', None, 'SUM(orders.total)'),
    ]
    assert model.metrics[1].synonyms == ['sales']
    released = parse(
        'version: "0.1.1"\nsemantic_model:\n  - name: a\n    datasets: [{name: t, source: main.t}]\n'
        '  - name: b\n    datasets: [{name: u, source: main.u}]\n',
        'old.ossie.yaml',
    )
    assert [m.name for m in released.models] == ['a', 'b']
    assert parse('- just a list', 'x.ossie.yaml').errors == ['x.ossie.yaml: expected an Ossie semantic model']


def _project(root: Path) -> Path:
    (root / 'models.sql').write_text(
        'create table main.customers as select id as customer_id, name from raw.people;\n'
        'create table main.orders as select id, customer_id, amount_cents from raw.orders;\n'
    )
    (root / 'shop.ossie.yaml').write_text(
        textwrap.dedent(
            """\
            version: "0.2.0.dev0"
            name: shop
            datasets:
              - name: orders
                source: main.orders
                primary_key: [order_id]
                fields:
                  - {name: order_id, expression: {sql: id}}
                  - {name: customer, expression: {sql: customer_id}}
                  - {name: total, expression: {sql: amount_cents / 100.0}}
              - name: customers
                source: main.customers
                primary_key: [id]
                fields:
                  - {name: id, expression: {sql: customer_id}}
              - name: missing
                source: main.nowhere
            relationships:
              - {name: orders_to_customers, from: orders, to: customers, from_columns: [customer], to_columns: [id]}
            metrics:
              - name: Revenue
                description: Money in, in pounds.
                ai_context: {synonyms: [sales]}
                expression: {sql: SUM(orders.total)}
              - {name: Broken, expression: {sql: SUM(orders.nothing)}}
            """
        )
    )
    return root


def test_the_build_reads_ossie_models(tmp_path: Path) -> None:
    project = Project(_project(tmp_path))
    result = project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    graph = result.graph
    revenue = graph.nodes['term.revenue']
    assert revenue.attrs['kind'] == 'measure' and revenue.origins == ['ossie']
    assert revenue.description == 'Money in, in pounds.'
    assert [graph.label(graph.nodes[e.target]) for e in graph.out_edges(revenue.id, 'defines')] == [
        'orders.amount_cents'
    ]
    assert (
        "ossie: shop.ossie.yaml: model 'shop': dataset 'missing' reads 'main.nowhere', which does not exist"
        in (graph.warnings)
    )
    assert any("metric 'Broken' reads 'orders.nothing'" in w for w in graph.warnings)
    assert {'input': 'ossie', 'path': 'shop.ossie.yaml', 'detail': '1 metric'} in graph.meta['sources']
    ontology = project.ontology()
    entities = {e.name: e for e in ontology.entities}
    assert entities['order'].evidence == ['primary key in shop.ossie.yaml']
    assert entities['customer'].relations == ['customers']
    [rel] = ontology.relationships
    assert (rel.source, rel.target, rel.via) == (
        'order',
        'customer',
        'orders.customer_id = customers.customer_id',
    )
    assert rel.evidence == ['Ossie relationship orders_to_customers (shop.ossie.yaml)']
    measure = ontology.measure('revenue')
    assert measure is not None and measure.expression == 'SUM(orders.total)'
    assert ontology.violations == []


def test_ossie_paths_in_konstruct_toml(tmp_path: Path) -> None:
    _project(tmp_path)
    (tmp_path / 'semantic').mkdir()
    (tmp_path / 'shop.ossie.yaml').rename(tmp_path / 'semantic' / 'shop.ossie.yaml')
    (tmp_path / 'other.ossie.yaml').write_text(
        'version: "0.2.0.dev0"\nname: x\ndatasets: [{name: t, source: main.gone}]\n'
    )
    (tmp_path / 'konstruct.toml').write_text('[ossie]\npaths = ["semantic"]\n')
    graph = Project(tmp_path).build(dialect='duckdb', warehouses=False, run_dbt=False).graph
    assert 'term.revenue' in graph.nodes
    assert not any('main.gone' in w for w in graph.warnings)


def test_the_export_is_a_valid_ossie_model(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _project(tmp_path)
    (root / 'shop.ossie.yaml').unlink()
    (root / 'models.sql').write_text(
        'create table main.customers as select id as customer_id, name from raw.people;\n'
        'create table main.orders as select o.id as order_id, o.customer_id, o.amount_cents / 100.0 as total\n'
        'from raw.orders o join main.customers c on o.customer_id = c.customer_id;\n'
        'create table main.spend as select customer_id, sum(amount_cents) / 100.0 as spend\n'
        'from raw.orders group by customer_id;\n'
    )
    (root / 'definitions.yml').write_text(
        textwrap.dedent(
            """\
            version: 1
            terms:
              - name: Customer
                kind: entity
                definition: Someone who buys.
                columns: [main.customers.customer_id]
              - name: Order total
                kind: measure
                definition: What an order is worth, in pounds.
                grain: one row per order
                columns: [main.orders.total]
              - name: Customer spend
                kind: measure
                synonyms: [basket value]
                definition: What a customer has spent, in pounds.
                grain: one row per customer
                columns: [main.spend.spend]
            """
        )
    )
    assert main(['build', str(root), '--no-warehouse', '--no-dbt', '--dialect', 'duckdb', '-q']) == 0
    capsys.readouterr()
    assert main(['export', 'ossie', '-C', str(root)]) == 0
    document = valid(capsys.readouterr().out)
    assert document['version'] == '0.2.0.dev0'
    datasets = {d['name']: d for d in document['datasets']}
    assert datasets['customers']['primary_key'] == ['customer_id']
    assert datasets['customers']['source'] == 'main.customers'
    # Order total is a per-row formula, not an aggregate, so it is not an Ossie metric.
    [metric] = document['metrics']
    assert metric['name'] == 'customer_spend'
    # main.orders holds the order entity (from the grain), so raw.orders takes its full name.
    assert metric['expression'] == {'sql': 'SUM(raw_orders.amount_cents) / 100.0'}
    assert datasets['raw_orders']['source'] == 'raw.orders'
    assert datasets['orders']['primary_key'] == ['order_id']
    assert metric['ai_context'] == {
        'synonyms': ['Customer spend', 'basket value'],
        'instructions': 'Grain: one row per customer.',
    }
    [extension] = metric['custom_extensions']
    assert extension['vendor_name'] == 'KONSTRUCT'
    data = json.loads(extension['data'])
    assert data['columns'][0]['column'] == 'spend.spend'
    with pytest.raises(ValueError, match='does not take a selection'):
        Project(root).export('ossie', select='main.orders')


def test_nothing_to_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / 'x.sql').write_text('create table t2 as select a from t1')
    project = Project(tmp_path)
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    with pytest.raises(ValueError, match='nothing to export'):
        project.export('ossie')
    assert main(['export', 'ossie', '-C', str(tmp_path)]) == 1
    out, err = capsys.readouterr()
    assert out == '' and 'konstruct: error: nothing to export' in err
    assert main(['export', 'ossie', '-s', 't2', '-C', str(tmp_path)]) == 1
    out, err = capsys.readouterr()
    assert out == '' and 'does not take a selection' in err


def test_a_bug_in_an_export_is_not_reported_as_a_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / 'x.sql').write_text('create table t2 as select a from t1')
    Project(tmp_path).build(dialect='duckdb', warehouses=False, run_dbt=False)

    def broken(*_: Any, **__: Any) -> str:
        raise ValueError('a bug')

    monkeypatch.setattr('konstruct.export.mermaid', broken)
    with pytest.raises(ValueError, match='a bug'):
        main(['export', 'mermaid', '-C', str(tmp_path), '--no-build'])


@pytest.mark.dbt
def test_the_example_exports_to_ossie(jaffle_built_graph: Path) -> None:
    document = valid(Project(jaffle_built_graph).export('ossie'))
    assert {m['name'] for m in document['metrics']} == {'customer_lifetime_value', 'order_amount'}
    assert {r['name'] for r in document['relationships']} == {'order_to_customer', 'payment_to_order'}


def test_relations_that_share_a_name_stay_two_datasets(tmp_path: Path) -> None:
    (tmp_path / 'models.sql').write_text(
        'create table main.orders as select o.id as order_id, sum(o.amount_cents) / 100.0 as total from raw.orders o\n'
        'group by o.id;\n'
    )
    (tmp_path / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - {name: Order total, kind: measure, definition: x, columns: [main.orders.total]}\n'
        '  - {name: Order, kind: entity, definition: y, columns: [main.orders.order_id]}\n'
    )
    project = Project(tmp_path)
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    document = valid(project.export('ossie'))
    assert {d['name']: d['source'] for d in document['datasets']} == {
        'orders': 'main.orders',
        'raw_orders': 'raw.orders',
    }
    assert document['metrics'][0]['expression'] == {'sql': 'SUM(raw_orders.amount_cents) / 100.0'}


@pytest.mark.parametrize(
    ('sql_type', 'expected'),
    [
        ('INTEGER', 'Integer'),
        ('bigint', 'Integer'),
        ('DECIMAL(10,2)', 'Decimal'),
        ('NUMBER(38, 0)', 'Decimal'),
        ('double precision', 'Float'),
        ('VARCHAR(200)', 'String'),
        ('character varying', 'String'),
        ('TIMESTAMP WITH TIME ZONE', 'DateTimeTz'),
        ('timestamp(6) with time zone', 'DateTimeTz'),
        ('timestamp(3) without time zone', 'DateTime'),
        ('time(3) with time zone', 'Time'),
        ('smallserial', 'Integer'),
        ('TIMESTAMP_NTZ', 'DateTime'),
        ('timestamp', 'DateTime'),
        ('DATE', 'Date'),
        ('BOOLEAN', 'Boolean'),
        ('INTERVAL', 'Opaque'),
        ('POINT', 'Opaque'),
        ('STRUCT(a INTEGER)', 'Opaque'),
        ('INTEGER[]', 'Opaque'),
        ('ARRAY<STRING>', 'Opaque'),
        ('JSON', 'Opaque'),
        ('', None),
        (None, None),
    ],
)
def test_data_types(sql_type: str | None, expected: str | None) -> None:
    assert datatype(sql_type) == expected


def test_composite_keys_and_unmatched_relationships(tmp_path: Path) -> None:
    (tmp_path / 'models.sql').write_text(
        'create table main.lines as select order_id, line, sku from raw.lines;\n'
        'create table main.products as select sku, title from raw.products;\n'
    )
    (tmp_path / 'shop.ossie.yaml').write_text(
        textwrap.dedent(
            """\
            version: "0.2.0.dev0"
            name: shop
            datasets:
              - name: lines
                source: main.lines
                primary_key: [order_id, line]
                fields:
                  - {name: order_id, expression: {sql: order_id}}
                  - {name: line, expression: {sql: line}}
                  - {name: sku, expression: {sql: sku}}
                  - {name: broken}
              - name: products
                source: main.products
                fields:
                  - {name: sku, expression: {sql: sku}}
            relationships:
              - {name: two, from: lines, to: products, from_columns: [order_id, line], to_columns: [sku, sku]}
              - {name: uneven, from: lines, to: products, from_columns: [sku], to_columns: []}
            """
        )
    )
    project = Project(tmp_path)
    graph = project.build(dialect='duckdb', warehouses=False, run_dbt=False).graph
    assert project.ontology().entities == []
    warnings = [w for w in graph.warnings if w.startswith('ossie: ')]
    assert any('field lines.broken has no expression Konstruct can read' in w for w in warnings)
    assert any("relationship 'two' joins on several fields" in w for w in warnings)
    assert any("relationship 'uneven' has 1 from_columns and 0 to_columns" in w for w in warnings)


def test_a_metric_read_from_ossie_is_written_back(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / 'shop.ossie.yaml').write_text(
        textwrap.dedent(
            """\
            version: "0.2.0.dev0"
            name: shop
            datasets:
              - name: o
                source: main.orders
                primary_key: [order_id]
                fields:
                  - {name: order_id, expression: {sql: id}}
                  - {name: cents, expression: {sql: amount_cents}}
                metrics:
                  - {name: Cents, expression: {dialects: [{dialect: POSTGRES, expression: "SUM(source.amount_cents)"}]}}
            metrics:
              - {name: Revenue, expression: {sql: SUM(o.cents) / 100.0}}
            """
        )
    )
    project = Project(root)
    graph = project.build(dialect='postgres', warehouses=False, run_dbt=False).graph
    assert graph.nodes['term.cents'].attrs['formula_dialect'] == 'postgres'
    document = valid(project.export('ossie'))
    metrics = {m['name']: m['expression'] for m in document['metrics']}
    # Read as portable SQL, so it is written back only as portable SQL: translating it to
    # Postgres would add a cast that changes its meaning.
    assert metrics['revenue'] == {'sql': 'SUM(orders.amount_cents) / 100.0'}
    # Read in Postgres and scoped to its dataset, so it stays both.
    [orders] = [d for d in document['datasets'] if d['source'] == 'main.orders']
    assert orders['metrics'][0]['name'] == 'cents'
    assert orders['metrics'][0]['expression'] == {
        'sql': 'SUM(amount_cents)',
        'dialects': [{'dialect': 'POSTGRES', 'expression': 'SUM(amount_cents)'}],
    }


def test_the_upstream_scott_model_round_trips(tmp_path: Path) -> None:
    shutil.copy(FIXTURES / 'scott_model.ossie.fixture.yaml', tmp_path / 'scott.ossie.yaml')
    con = duckdb.connect(str(tmp_path / 'warehouse.duckdb'))
    con.execute('create schema scott')
    con.execute('create table scott.dept (deptno integer primary key, dname varchar, loc varchar)')
    con.execute(
        'create table scott.emp (empno integer primary key, ename varchar, job varchar, mgr integer, '
        'hiredate date, sal decimal(7, 2), comm decimal(7, 2), deptno integer)'
    )
    con.execute('create table scott.salgrade (grade integer primary key, losal integer, hisal integer)')
    con.close()
    project = Project(tmp_path)
    graph = project.build(run_dbt=False).graph
    assert not [w for w in graph.warnings if w.startswith('ossie: ') and 'metric' in w]
    document = valid(project.export('ossie'))
    scoped = {d['source']: [m['name'] for m in d.get('metrics', [])] for d in document['datasets']}
    assert scoped['warehouse.scott.emp'] == [
        'avg_comm',
        'avg_sal',
        'comm_count',
        'emp_count',
        'sum_comm',
        'sum_sal',
    ]
    assert scoped['warehouse.scott.dept'] == ['dept_count'] and scoped['warehouse.scott.salgrade'] == [
        'grade_count'
    ]
    emp = next(d for d in document['datasets'] if d['source'] == 'warehouse.scott.emp')
    assert {m['name']: m['expression']['sql'] for m in emp['metrics']}['emp_count'] == 'COUNT(*)'


def test_metrics_over_computed_fields(tmp_path: Path) -> None:
    (tmp_path / 'models.sql').write_text(
        'create table main.orders as select id, status, amount_cents from raw.orders;\n'
        'create table main.refunds as select order_id, cents from raw.refunds;\n'
    )
    (tmp_path / 'shop.ossie.yaml').write_text(
        textwrap.dedent(
            """\
            version: "0.2.0.dev0"
            name: shop
            datasets:
              - name: orders
                source: main.orders
                fields:
                  - {name: status, expression: {sql: status}}
                  - {name: total, expression: {sql: amount_cents / 100.0}}
              - name: refunds
                source: main.refunds
                fields:
                  - {name: cents, expression: {sql: cents}}
            metrics:
              - {name: Takings, expression: {sql: SUM(orders.total)}}
              - name: Completed takings
                expression: {sql: "SUM(CASE WHEN orders.status = 'completed' THEN orders.total ELSE 0 END)"}
              - {name: Net revenue, expression: {sql: SUM(orders.total) - SUM(refunds.cents) / 100.0}}
            """
        )
    )
    project = Project(tmp_path)
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    metrics = {m['name']: m['expression']['sql'] for m in valid(project.export('ossie'))['metrics']}
    assert metrics == {
        'completed_takings': "SUM(CASE WHEN orders.status = 'completed' THEN (orders.amount_cents / 100.0) ELSE 0 END)",
        'net_revenue': 'SUM((orders.amount_cents / 100.0)) - SUM(refunds.cents) / 100.0',
        'takings': 'SUM((orders.amount_cents / 100.0))',
    }
    # The columns a metric reads are its inputs: they do not each hold it, so they do not conflict.
    assert project.conflicts().conflicts == []


def test_a_measure_that_is_not_an_aggregate_is_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / 'models.sql').write_text(
        'create table main.running as select id, sum(amount) over (order by id) as total from raw.orders;\n'
    )
    (tmp_path / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - {name: Running total, kind: measure, definition: x, columns: [main.running.total]}\n'
    )
    project = Project(tmp_path)
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    with (
        caplog.at_level('WARNING', logger='konstruct.ossie'),
        pytest.raises(ValueError, match='nothing to export'),
    ):
        project.export('ossie')
    assert "measure 'Running total' is left out: it is not an aggregate" in caplog.text


def test_a_field_that_repeats_a_column(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / 'models.sql').write_text(
        'create table main.refunds as select id, sum(cents) as cents from raw.refunds group by id;\n'
    )
    (tmp_path / 'shop.ossie.yaml').write_text(
        textwrap.dedent(
            """\
            version: "0.2.0.dev0"
            name: shop
            datasets:
              - name: refunds
                source: main.refunds
                fields:
                  - {name: pos, expression: {sql: "CASE WHEN cents > 0 THEN cents ELSE 0 END"}}
                  - {name: gone, expression: {sql: "missing + 1"}}
            metrics:
              - {name: Largest refund, expression: {sql: MAX(refunds.pos)}}
              - {name: Broken total, expression: {sql: SUM(refunds.gone)}}
            """
        )
    )
    project = Project(tmp_path)
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    with caplog.at_level('WARNING', logger='konstruct.ossie'):
        document = valid(project.export('ossie'))
    assert {m['name']: m['expression']['sql'] for m in document['metrics']} == {
        'largest_refund': 'MAX((CASE WHEN refunds.cents > 0 THEN refunds.cents ELSE 0 END))'
    }
    assert "measure 'Broken total' is left out: its expression does not resolve" in caplog.text
