"""Time konstruct verify's checks of the ontology: two keys and a relationship over generated tables.

    uv run python .kodex/scripts/integrity_benchmark.py 1000000
    uv run python .kodex/scripts/integrity_benchmark.py 1000000 --postgres postgresql://...

Writes ``orders`` with the given number of rows, each referring to one of a tenth as many
``customers``, checks each table's key and the relationship between them, and prints the time.
On Postgres the tables go in a schema of their own, dropped afterwards.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

import duckdb

from konstruct.analysis.integrity import check
from konstruct.analysis.verify import DuckDBEngine, Engine, PostgresEngine
from konstruct.core.graph import Graph, Node

SCHEMA = 'konstruct_integrity_benchmark'


def ontology_graph(database: str, schema: str) -> Graph:
    """customers and orders, each keyed by a unique test, and a relationships test between them."""
    graph = Graph()
    for name, columns in (('customers', ['customer_id']), ('orders', ['order_id', 'customer_id'])):
        node = graph.add_node(Node(f'table.{name}', 'table', name, relation=f'{database}.{schema}.{name}'))
        for column in columns:
            graph.add_column(node, column)
    for column in ('table.customers#customer_id', 'table.orders#order_id'):
        test = graph.add_node(Node(f'test.{column}', 'test', 'unique', attrs={'test': 'unique'}))
        graph.add_edge(test.id, column, 'tests')
    test = graph.add_node(Node('test.rel', 'test', 'relationships', attrs={'test': 'relationships'}))
    graph.add_edge(test.id, 'table.orders#customer_id', 'tests')
    graph.add_edge(test.id, 'table.customers#customer_id', 'tests', role='relationship target')
    return graph


def timed_check(graph: Graph, engine: Engine) -> float:
    started = time.perf_counter()
    try:
        keys, links = check(graph, engine)
    finally:
        engine.close()
    assert {k.state for k in keys} | {link.state for link in links} == {'confirmed'}
    return time.perf_counter() - started


def on_duckdb(rows: int) -> float:
    with tempfile.TemporaryDirectory(prefix='konstruct-integrity-') as directory:
        path = Path(directory) / 'shop.duckdb'
        con = duckdb.connect(str(path))
        con.execute(
            f'create table main.customers as select i as customer_id from range(1, {rows // 10 + 1}) t(i)'
        )
        con.execute(
            f'create table main.orders as select i as order_id, 1 + i % {rows // 10} as customer_id '
            f'from range(1, {rows + 1}) t(i)'
        )
        con.close()
        return timed_check(ontology_graph('shop', 'main'), DuckDBEngine(path))


def on_postgres(rows: int, dsn: str) -> float:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f'drop schema if exists {SCHEMA} cascade')
        con.execute(f'create schema {SCHEMA}')
        con.execute(
            f'create table {SCHEMA}.customers as select i as customer_id from generate_series(1, {rows // 10}) i'
        )
        con.execute(
            f'create table {SCHEMA}.orders as select i as order_id, 1 + i % {rows // 10} as customer_id '
            f'from generate_series(1, {rows}) i'
        )
        con.execute(f'analyze {SCHEMA}.customers')
        con.execute(f'analyze {SCHEMA}.orders')
        row = con.execute('select current_database()').fetchone()
    try:
        return timed_check(ontology_graph(str(row[0]) if row else '', SCHEMA), PostgresEngine(dsn))
    finally:
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'drop schema {SCHEMA} cascade')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('rows', type=int, help='rows of orders (customers has a tenth as many)')
    parser.add_argument('--postgres', metavar='DSN', help='check on this Postgres database, not DuckDB')
    args = parser.parse_args()
    seconds = on_postgres(args.rows, args.postgres) if args.postgres else on_duckdb(args.rows)
    print(f'{"postgres" if args.postgres else "duckdb"}: {args.rows:,} rows, {seconds:.2f} s')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
