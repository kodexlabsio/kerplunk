"""konstruct export: OpenLineage (checked against the official schemas), Mermaid, DOT and CSV."""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from konstruct import Project, QueryError
from konstruct.interfaces.cli import main

SCHEMAS = Path(__file__).parent / 'fixtures' / 'openlineage'


@pytest.fixture
def project(jaffle_built_graph: Path, tmp_path: Path) -> Project:
    copy = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph / 'konstruct-out', copy / 'konstruct-out')
    return Project(copy)


def validator(definition: str) -> Draft202012Validator:
    registry: Registry[Any] = Registry()
    for path in SCHEMAS.glob('*.json'):
        schema = json.loads(path.read_text())
        registry = registry.with_resource(schema['$id'], Resource.from_contents(schema))
    spec = json.loads((SCHEMAS / 'OpenLineage.json').read_text())
    return Draft202012Validator({'$ref': f'{spec["$id"]}#/$defs/{definition}'}, registry=registry)


def facet_validator(file: str, definition: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / file).read_text())
    registry: Registry[Any] = Registry()
    for path in SCHEMAS.glob('*.json'):
        other = json.loads(path.read_text())
        registry = registry.with_resource(other['$id'], Resource.from_contents(other))
    return Draft202012Validator({'$ref': f'{schema["$id"]}#/$defs/{definition}'}, registry=registry)


@pytest.mark.dbt
def test_openlineage_events_match_the_specification(project: Project) -> None:
    lines = project.export('openlineage').splitlines()
    events = [json.loads(line) for line in lines]
    assert {e['job']['name'] for e in events} == {
        'jaffle_shop.main.stg_customers',
        'jaffle_shop.main.stg_orders',
        'jaffle_shop.main.stg_payments',
        'jaffle_shop.main.customers',
        'jaffle_shop.main.orders',
        'jaffle_shop.reporting.customer_ltv',
    }
    job_event = validator('JobEvent')
    lineage = facet_validator('ColumnLineageDatasetFacet.json', 'ColumnLineageDatasetFacet')
    schema = facet_validator('SchemaDatasetFacet.json', 'SchemaDatasetFacet')
    for event in events:
        job_event.validate(event)
        assert 'run' not in event
        for dataset in event['inputs'] + event['outputs']:
            schema.validate(dataset['facets']['schema'])
        lineage.validate(event['outputs'][0]['facets']['columnLineage'])


@pytest.mark.dbt
def test_openlineage_column_lineage_and_transformations(project: Project) -> None:
    events = {
        json.loads(line)['job']['name']: json.loads(line)
        for line in project.export('openlineage').splitlines()
    }
    customers = events['jaffle_shop.main.customers']
    assert customers['job']['facets']['sourceCodeLocation']['path'] == 'models/customers.sql'
    output = customers['outputs'][0]
    assert output['namespace'] == 'duckdb://konstruct'
    facet = output['facets']['columnLineage']
    [ltv] = facet['fields']['customer_lifetime_value']['inputFields']
    assert (ltv['name'], ltv['field']) == ('jaffle_shop.main.stg_payments', 'amount')
    assert ltv['transformations'] == [
        {
            'type': 'DIRECT',
            'subtype': 'AGGREGATION',
            'description': 'SUM(jaffle_shop.main.stg_payments.amount)',
        }
    ]
    [identity] = facet['fields']['first_name']['inputFields']
    assert identity['transformations'] == [{'type': 'DIRECT', 'subtype': 'IDENTITY'}]
    indirect = {(f['name'].split('.')[-1], f['field']): f['transformations'] for f in facet['dataset']}
    # Joined on and grouped by: one transformation for each use.
    assert indirect[('stg_orders', 'customer_id')] == [
        {'type': 'INDIRECT', 'subtype': 'GROUP_BY'},
        {'type': 'INDIRECT', 'subtype': 'JOIN'},
    ]
    assert indirect[('stg_customers', 'customer_id')] == [{'type': 'INDIRECT', 'subtype': 'JOIN'}]
    assert {t['type'] for ts in indirect.values() for t in ts} == {'INDIRECT'}
    assert {i['name'] for i in customers['inputs']} == {
        'jaffle_shop.main.stg_customers',
        'jaffle_shop.main.stg_orders',
        'jaffle_shop.main.stg_payments',
    }
    filtered = events['jaffle_shop.reporting.customer_ltv']['outputs'][0]['facets']['columnLineage'][
        'dataset'
    ]
    assert {'type': 'INDIRECT', 'subtype': 'FILTER'} in [f['transformations'][0] for f in filtered]


@pytest.mark.dbt
def test_csv_has_one_row_per_column_edge(project: Project) -> None:
    rows = list(csv.DictReader(io.StringIO(project.export('csv'))))
    assert len(rows) == sum(1 for e in project.graph.edges if e.kind == 'derived_from')
    row = next(
        r
        for r in rows
        if r['relation'] == 'orders'
        and r['column'] == 'credit_card_amount'
        and r['upstream_column'] == 'amount'
    )
    assert row['upstream_relation'] == 'stg_payments'
    assert row['transform'] == 'transformed'
    assert row['relation_kind'] == 'model' and row['path'] == 'models/orders.sql'
    assert 'SUM(CASE WHEN' in row['expression']


@pytest.mark.dbt
def test_select_limits_every_format_to_one_lineage(project: Project) -> None:
    everything = project.export('mermaid')
    selected = project.export('mermaid', select='customer_ltv.lifetime_value')
    assert '["stg_payments"]' in selected and '[/"raw_payments"/]' in selected
    assert 'stg_customers' in everything and 'stg_customers' not in selected
    rows = list(csv.DictReader(io.StringIO(project.export('csv', select='customer_ltv.lifetime_value'))))
    assert {r['relation'] for r in rows} <= {'customer_ltv', 'customers', 'stg_payments'}
    events = project.export('openlineage', select='stg_payments').splitlines()
    assert len(events) < 6
    with pytest.raises(QueryError):
        project.export('csv', select='nothing_like_this')


@pytest.mark.dbt
def test_mermaid_and_dot_with_columns(project: Project) -> None:
    text = project.export('mermaid', select='orders.amount', columns=True)
    assert text.startswith('flowchart LR\n')
    assert 'subgraph' in text and '["amount"]' in text
    assert ' -.-> ' in text  # orders.amount is transformed from stg_payments.amount
    graph = project.export('dot', columns=True)
    assert graph.startswith('digraph konstruct {') and graph.rstrip().endswith('}')
    assert 'shape=record' in graph and '[style=dashed]' in graph
    if shutil.which('dot'):
        result = subprocess.run(['dot', '-Tsvg'], input=graph, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


@pytest.mark.dbt
def test_cli_export(project: Project, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / 'lineage.jsonl'
    assert (
        main(
            ['export', 'openlineage', '-C', str(project.path), '-o', str(out), '--namespace', 'duckdb://shop']
        )
        == 0
    )
    assert json.loads(out.read_text().splitlines()[0])['outputs'][0]['namespace'] == 'duckdb://shop'
    assert main(['export', 'dot', '-C', str(project.path)]) == 0
    assert capsys.readouterr().out.startswith('digraph konstruct {')


def test_unknown_format() -> None:
    from konstruct.core.graph import Graph
    from konstruct.outputs.export import export

    with pytest.raises(ValueError, match='unknown export format'):
        export(Graph(), 'yaml')
    assert export(Graph(), 'openlineage') == ''


def test_a_condition_where_it_applies_keeps_its_subtype() -> None:
    """A filter before a window function or on the nullable side of a LEFT JOIN is a filter; the
    ON of the join is a join."""
    from konstruct.outputs.export import _indirect_subtypes

    conditions = [
        'WHERE BEFORE WINDOW: db.main.orders.amount > 0',
        'LEFT JOIN WHERE: db.main.customers.active',
        'LEFT JOIN ON: db.main.customers.id = db.main.orders.customer_id',
    ]
    assert _indirect_subtypes(conditions, 'db.main.orders', 'amount') == ['FILTER']
    assert _indirect_subtypes(conditions, 'db.main.customers', 'active') == ['FILTER']
    assert _indirect_subtypes(conditions, 'db.main.customers', 'id') == ['JOIN']
