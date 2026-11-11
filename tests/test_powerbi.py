"""Power BI semantic models in TMDL: tables as exposures of the warehouse columns they use."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

from konstruct import Project
from konstruct.engine.config import load_config
from konstruct.inputs.powerbi import (
    model_name,
    native_query,
    native_reads,
    navigation,
    parse,
    references,
    resolve,
)
from konstruct.interfaces.status import inspect, render_config


def tmdl(text: str) -> str:
    """TMDL as Power BI Desktop writes it: tab indentation."""
    return textwrap.dedent(text).replace('    ', '\t')


ORDERS = tmdl(
    """\
    /// Orders as the warehouse holds them
    table Orders
        lineageTag: 1b2c

        measure Revenue = SUM(Orders[Amount])
            formatString: #,0.00
            annotation PBI_FormatHint = {"isDecimal":true}

        measure 'Big Orders' =
                CALCULATE(
                    COUNTROWS(Orders),
                    'Orders'[Status] = "complete" // not [Amount]
                )

        column 'Order ID'
            dataType: int64
            sourceColumn: order_id

        column Amount
            dataType: decimal
            sourceColumn: amount

        column Status
            sourceColumn: status

        column Cost
            sourceColumn: cost

        column 'Is Big' = Orders[Amount] > 100
            dataType: boolean

        partition Orders = m
            mode: import
            source =
                    let
                        Source = Sql.Database("(local)", "analytics"),
                        orders = Source{[Schema="mart",Item="orders"]}[Data]
                    in
                        orders
    """
)

MEASURES = tmdl(
    """\
    table _Measures

        measure Margin = [Revenue] - SUM(Orders[Cost])

        measure 'Customer Count' = DISTINCTCOUNT(Customers[Name])

        measure Loop = [Loop] + 1

        partition _Measures = calculated
            source = Row("x", BLANK())
    """
)

CUSTOMERS = tmdl(
    """\
    table Customers

        column Name
            sourceColumn: name

        partition Customers = m
            mode: directQuery
            source = ```
                let
                    Source = Snowflake.Databases("acme.snowflakecomputing.com", "WH"),
                    db = Source{[Name="ANALYTICS",Kind="Database"]}[Data],
                    schema = db{[Name="MART",Kind="Schema"]}[Data],
                    tbl = schema{[Name="CUSTOMERS",Kind="Table"]}[Data]
                in
                    tbl
                ```
    """
)

DAILY = tmdl(
    """\
    table Daily

        column Revenue
            sourceColumn: revenue

        partition Daily = m
            source = Value.NativeQuery(Sql.Database("(localdb)\\MSSQLLocalDB", "analytics"), "SELECT TOP 10 [d].[revenue] FROM [mart].[orders] AS d#(lf)JOIN mart.customers c ON 1 = 1", null)
    """
)

DATES = tmdl(
    """\
    table LocalDateTable_0a1b
        isHidden
        showAsVariationsOnly

        column Date
            isHidden

        partition LocalDateTable_0a1b = calculated
            mode: import
            source = Calendar(Date(Year(MIN('Orders'[Order Date])), 1, 1), Date(2030, 12, 31))

        annotation __PBI_LocalDateTable = true
    """
)

LAKE = tmdl(
    """\
    table Events

        column Kind
            sourceColumn: kind

        partition Events = entity
            mode: directLake
            source
                entityName: events
                schemaName: dbo
                expressionSource: DatabaseQuery
    """
)

EXPRESSIONS = tmdl(
    """\
    expression 'Shared Invoices' =
            let
                Source = Sql.Database("db.example.com", "analytics"),
                invoices = Source{[Schema="mart",Item="invoices"]}[Data]
            in
                invoices

    table Invoices

        column Total
            sourceColumn: total

        partition Invoices = m
            source = #"Shared Invoices"
    """
)

MODEL = 'reports/Sales.SemanticModel/definition/tables'


def write_model(root: Path) -> Path:
    tables = root / MODEL
    tables.mkdir(parents=True)
    for name, text in (
        ('Orders', ORDERS),
        ('_Measures', MEASURES),
        ('Customers', CUSTOMERS),
        ('Daily', DAILY),
        ('LocalDateTable_0a1b', DATES),
        ('Invoices', EXPRESSIONS),
    ):
        (tables / f'{name}.tmdl').write_text(text)
    (root / 'models').mkdir()
    (root / 'models' / 'orders.sql').write_text(
        'create table analytics.mart.orders as select order_id, amount, status, cost from raw.orders'
    )
    return tables


def test_a_table_its_columns_measures_and_source() -> None:
    (table,) = parse(ORDERS, f'{MODEL}/Orders.tmdl').tables
    assert (table.name, table.model, table.description) == (
        'Orders',
        'Sales',
        'Orders as the warehouse holds them',
    )
    assert [(c.name, c.source_column, c.expression) for c in table.columns] == [
        ('Order ID', 'order_id', None),
        ('Amount', 'amount', None),
        ('Status', 'status', None),
        ('Cost', 'cost', None),
        ('Is Big', None, 'Orders[Amount] > 100'),
    ]
    # A measure's own properties and annotations are not part of its DAX.
    assert table.measures['Revenue'] == 'SUM(Orders[Amount])'
    assert (
        table.measures['Big Orders'].startswith('CALCULATE(')
        and 'formatString' not in table.measures['Big Orders']
    )
    (partition,) = table.partitions
    assert partition.kind == 'm' and partition.source is not None
    assert navigation(partition.source) == ['analytics.mart.orders']


def test_spaces_crlf_and_a_byte_order_mark() -> None:
    text = '﻿' + ORDERS.replace('\t', '  ').replace('\n', '\r\n')
    (table,) = parse(text, 'Orders.tmdl').tables
    assert [c.source_column for c in table.columns][:2] == ['order_id', 'amount']
    assert navigation(table.partitions[0].source or '') == ['analytics.mart.orders']


def test_m_sources() -> None:
    (customers,) = parse(CUSTOMERS, 'Customers.tmdl').tables
    assert navigation(customers.partitions[0].source or '') == ['ANALYTICS.MART.CUSTOMERS']
    (daily,) = parse(DAILY, 'Daily.tmdl').tables
    sql = native_query(daily.partitions[0].source or '')
    assert sql == 'SELECT TOP 10 [d].[revenue] FROM [mart].[orders] AS d\nJOIN mart.customers c ON 1 = 1'
    assert native_reads(sql, 'tsql') == ['mart.customers', 'mart.orders']
    assert native_query('Sql.Database("s", "db", [Query="select ""a"" from [dbo].[t]"])') == (
        'select "a" from [dbo].[t]'
    )
    merged = (
        'let a = Source{[Schema="mart",Item="orders"]}[Data], b = Source{[Item="customers",Schema="mart"]}[Data] '
        'in Table.NestedJoin(a, {"id"}, b, {"id"}, "c")'
    )
    assert navigation(merged) == ['mart.orders', 'mart.customers']
    bigquery = 'GoogleBigQuery.Database(){[Name="my-project"]}[Data]{[Name="shop",Kind="Schema"]}[Data]{[Name="orders",Kind="Table"]}[Data]'
    assert navigation(bigquery) == ['my-project.shop.orders']


def test_dax_references() -> None:
    assert references("SUM('Order Lines'[Net]) + [Margin] - COUNTROWS(Orders) + DATE(2020, 1, 1)") == [
        ('Order Lines', 'Net'),
        (None, 'Margin'),
        ('Orders', None),
    ]
    assert ('Orders', None) in references('COUNTROWS(Orders)')
    assert not [r for r in references('SUM(T[a]) // T[b]\n/* T[c] */ & "T[d]"') if r[1] in ('b', 'c', 'd')]


def test_a_model_resolved_across_files(tmp_path: Path) -> None:
    files = [
        parse(text, f'{MODEL}/{name}.tmdl')
        for name, text in (
            ('Orders', ORDERS),
            ('_Measures', MEASURES),
            ('Customers', CUSTOMERS),
            ('Daily', DAILY),
            ('LocalDateTable_0a1b', DATES),
            ('Invoices', EXPRESSIONS),
        )
    ]
    tables, problems = resolve(files)
    by_name = {t.name: t for t in tables}
    assert 'LocalDateTable_0a1b' not in by_name
    assert problems == []
    orders = by_name['Orders']
    assert orders.reads[('analytics.mart.orders', 'amount')] == {'Amount', 'Is Big', 'Revenue'}
    assert orders.reads[('analytics.mart.orders', 'status')] == {'Big Orders', 'Status'}
    measures = by_name['_Measures']
    assert measures.reads == {
        ('analytics.mart.orders', 'amount'): {'Margin'},
        ('analytics.mart.orders', 'cost'): {'Margin'},
        ('ANALYTICS.MART.CUSTOMERS', 'name'): {'Customer Count'},
    }
    assert by_name['Daily'].sources == ['mart.customers', 'mart.orders']
    assert ('mart.orders', 'revenue') not in by_name['Daily'].reads
    assert by_name['Invoices'].reads[('analytics.mart.invoices', 'total')] == {'Total'}


def test_problems_are_reported() -> None:
    files = [
        parse(
            tmdl('table A\n    partition A = m\n        source = Excel.Workbook(File.Contents("a.xlsx"))\n'),
            'A.tmdl',
        ),
        parse(
            tmdl(
                'table B\n    partition B = m\n        source = Value.NativeQuery(Sql.Database("s", "d"), "select 1")\n'
            ),
            'B.tmdl',
        ),
        parse(
            tmdl('table C\n    partition C = entity\n        source\n            schemaName: dbo\n'), 'C.tmdl'
        ),
        parse(tmdl('table A\n'), 'Other.tmdl'),
        parse(tmdl('table D\n'), 'other/Sales.SemanticModel/D.tmdl'),
        parse(tmdl('table E\n'), 'again/Sales.SemanticModel/E.tmdl'),
    ]
    _, problems = resolve(files)
    assert problems == [
        "Other.tmdl: table 'A' is also defined in A.tmdl",
        "other/Sales.SemanticModel/D.tmdl: semantic model 'Sales' is also in again/Sales.SemanticModel; "
        'kept that one',
        "A.tmdl: table 'A': no warehouse table found in partition 'A'",
        "B.tmdl: table 'B': the native query in partition 'B' reads no table",
        "C.tmdl: table 'C': Direct Lake partition 'C' names no table",
    ]


def test_the_model_a_file_belongs_to() -> None:
    assert model_name(f'{MODEL}/Orders.tmdl') == 'Sales'
    assert model_name('reports\\Finance.SemanticModel\\definition\\model.tmdl') == 'Finance'
    assert model_name('loose/Orders.tmdl') == 'model'


def test_tables_are_exposures_of_the_columns_they_use(tmp_path: Path) -> None:
    write_model(tmp_path)
    (tmp_path / MODEL / 'Events.tmdl').write_text(LAKE)
    project = Project(tmp_path)
    graph = project.build(warehouses=False, run_dbt=False).graph
    assert not [w for w in graph.warnings if w.startswith('powerbi')]
    orders = graph.nodes['powerbi.table.Sales.Orders']
    assert orders.kind == 'exposure' and orders.attrs['type'] == 'power bi table'
    assert orders.description == 'Orders as the warehouse holds them'
    assert orders.attrs['fields'] == [
        'Amount',
        'Big Orders',
        'Cost',
        'Is Big',
        'Order ID',
        'Revenue',
        'Status',
    ]
    # Each measure with its DAX, for the ontology's metrics.
    assert orders.attrs['measures']['Revenue'] == 'SUM(Orders[Amount])'
    assert set(orders.attrs['measures']) == {'Big Orders', 'Revenue'}

    def feeds(node_id: str) -> dict[str, Any]:
        return {
            f'{graph.nodes[e.source].parent}#{graph.nodes[e.source].name}': e.attrs.get('fields')
            for e in graph.edges
            if e.target == node_id and graph.nodes[e.source].kind == 'column'
        }

    assert feeds('powerbi.table.Sales._Measures') == {
        'table.analytics.mart.orders#amount': ['Margin'],
        'table.analytics.mart.orders#cost': ['Margin'],
        'table.analytics.mart.customers#name': ['Customer Count'],
    }
    raw = next(n for n in graph.relations() if n.relation == 'raw.orders')
    impacted = {
        name: {step.node.id for step in graph.impact(column)}
        for name in ('amount', 'cost', 'order_id')
        if (column := graph.column(raw.id, name)) is not None
    }
    assert {'powerbi.table.Sales.Orders', 'powerbi.table.Sales._Measures'} <= impacted['amount']
    assert 'powerbi.table.Sales._Measures' not in impacted['order_id']
    assert 'powerbi.table.Sales.LocalDateTable_0a1b' not in graph.nodes
    events = next(n for n in graph.relations() if n.relation and n.relation.endswith('events'))
    assert graph.column(events.id, 'kind') is not None
    assert graph.meta['inputs']['powerbi_tables'] == 6
    assert {s['detail'] for s in graph.meta['sources'] if s['input'] == 'powerbi'} == {
        'Orders',
        '_Measures',
        'Customers',
        'Daily',
        'Invoices',
        'Events',
    }
    assert sorted(inspect(tmp_path).powerbi_files)[0] == f'{MODEL}/Customers.tmdl'
    rendered = render_config(load_config(tmp_path), inspect(tmp_path), [])
    assert '[powerbi]\npaths = ["reports/Sales.SemanticModel"]' in rendered


def test_an_unknown_column_is_reported(tmp_path: Path) -> None:
    tables = write_model(tmp_path)
    (tables / 'Orders.tmdl').write_text(ORDERS.replace('sourceColumn: status', 'sourceColumn: state'))
    graph = Project(tmp_path).build(warehouses=False, run_dbt=False).graph
    assert (
        f"powerbi: {MODEL}/Orders.tmdl: table 'Orders' reads analytics.mart.orders.state, "
        'which is not a known column' in graph.warnings
    )


def test_a_column_of_an_inferred_table_is_added(tmp_path: Path) -> None:
    write_model(tmp_path)
    (tmp_path / 'models' / 'orders.sql').unlink()
    (tmp_path / 'other.sql').write_text('create table t.x as select order_id from analytics.mart.orders')
    graph = Project(tmp_path).build(warehouses=False, run_dbt=False).graph
    assert not [w for w in graph.warnings if 'not a known column' in w]
    orders = next(n for n in graph.relations() if n.relation == 'analytics.mart.orders')
    assert {c.name for c in graph.columns_of(orders.id)} == {'amount', 'cost', 'order_id', 'status'}


def test_an_edited_file_is_read_again(tmp_path: Path) -> None:
    tables = write_model(tmp_path)
    project = Project(tmp_path)
    project.build(warehouses=False, run_dbt=False)
    (tables / 'Orders.tmdl').write_text(ORDERS.replace('measure Revenue', 'measure Takings'))
    graph = project.build(warehouses=False, run_dbt=False, update=True).graph
    assert 'Takings' in graph.nodes['powerbi.table.Sales.Orders'].attrs['fields']


def test_powerbi_paths_in_konstruct_toml(tmp_path: Path) -> None:
    write_model(tmp_path)
    other = tmp_path / 'old' / 'Old.SemanticModel'
    other.mkdir(parents=True)
    (other / 'Orders.tmdl').write_text(ORDERS)
    (tmp_path / 'konstruct.toml').write_text('[powerbi]\npaths = ["reports"]\n')
    assert load_config(tmp_path).powerbi_paths == [tmp_path / 'reports']
    graph = Project(tmp_path).build(warehouses=False, run_dbt=False).graph
    assert 'powerbi.table.Old.Orders' not in graph.nodes
    assert 'powerbi.table.Sales.Orders' in graph.nodes


def test_incremental_refresh_and_variables() -> None:
    text = tmdl(
        """\
        table Sales

            column Amount
                sourceColumn: amount

            measure Rev =
                    VAR Customers = SUM(Sales[Amount])
                    RETURN Customers

            refreshPolicy
                policyType: basic
                sourceExpression =
                        let
                            Source = Sql.Database("s", "analytics"),
                            sales = Source{[Schema="dbo",Item="sales"]}[Data]
                        in
                            sales

            partition 2024Q1 = policyRange
                mode: import
        """
    )
    customers = tmdl(
        'table Customers\n    partition C = m\n        source = S{[Schema="dbo",Item="customers"]}[Data]\n'
    )
    tables, problems = resolve([parse(text, 'Sales.tmdl'), parse(customers, 'Customers.tmdl')])
    sales = next(t for t in tables if t.name == 'Sales')
    assert problems == [] and sales.sources == ['analytics.dbo.sales']
    assert sales.reads[('analytics.dbo.sales', 'amount')] == {'Amount', 'Rev'}
    assert ('dbo.customers', '') not in sales.reads


def test_long_chains_of_measures_resolve_quickly() -> None:
    import random
    import time

    rng = random.Random(7)
    lines = ['table T', '    column A', '        sourceColumn: a', '    measure M0 = SUM(T[A])']
    for i in range(1, 400):
        lines.append(f'    measure M{i} = [M{rng.randrange(i)}] + [M{max(0, i - 1)}] + [M{max(0, i - 2)}]')
    lines += ['    partition T = m', '        source = S{[Schema="dbo",Item="t"]}[Data]']
    started = time.perf_counter()
    (table,), problems = resolve([parse(tmdl('\n'.join(lines) + '\n'), 'T.tmdl')])
    assert time.perf_counter() - started < 5
    assert problems == []
    assert table.reads[('dbo.t', 'a')] == {'A', *(f'M{i}' for i in range(400))}
