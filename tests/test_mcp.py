from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters

from konstruct.interfaces.mcp_server import build_server

pytestmark = pytest.mark.dbt


def structured(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    data = result.structured_content
    assert isinstance(data, dict)
    return data


async def _exercise(client: Client) -> None:
    tools = await client.list_tools()
    assert {t.name for t in tools.tools} == {
        'impact',
        'trace',
        'search',
        'describe',
        'conflicts',
        'define',
        'ontology',
        'explain',
        'history',
        'path',
        'query',
        'diff',
        'gate',
    }
    assert all(t.description and t.input_schema for t in tools.tools)
    impact = structured(await client.call_tool('impact', {'ref': 'stg_payments.amount'}))
    assert impact['exposures'][0]['label'] == 'customer_dashboard'
    trace = structured(await client.call_tool('trace', {'ref': 'orders.amount', 'max_depth': 1}))
    assert [c['label'] for c in trace['columns']] == ['stg_payments.amount']
    search = structured(
        await client.call_tool('search', {'text': 'lifetime', 'limit': 2, 'kinds': ['column']})
    )
    assert len(search['results']) == 2
    describe = structured(await client.call_tool('describe', {'ref': 'customers'}))
    assert describe['kind'] == 'model'
    assert structured(await client.call_tool('conflicts', {}))['conflicts'] == []
    # Acceptance: "define lifetime value" returns the approved definition and its bound column.
    defined = structured(await client.call_tool('define', {'ref': 'lifetime value'}))
    [term] = defined['terms']
    assert term['name'] == 'Customer lifetime value' and term['status'] == 'approved'
    assert term['definition'].startswith('Total value of every payment a customer has made')
    assert term['owner'] == {'name': 'Priya Shah', 'role': 'Head of Finance'}
    assert 'customers.customer_lifetime_value' in [c['label'] for c in term['columns']]
    ontology = structured(await client.call_tool('ontology', {}))
    assert [e['name'] for e in ontology['entities']] == ['customer', 'order', 'payment']
    customer = structured(await client.call_tool('ontology', {'name': 'Customer'}))
    assert [e['name'] for e in customer['entities']] == ['customer']
    assert {(r['source'], r['target']) for r in customer['relationships']} == {('order', 'customer')}
    measure = structured(await client.call_tool('ontology', {'name': 'order amount'}))
    assert measure['measures'][0]['columns'][0]['formula'] == 'SUM(raw_payments.amount / 100)'
    assert (await client.call_tool('ontology', {'name': 'nothing'})).is_error
    explained = structured(await client.call_tool('explain', {'ref': 'customer_ltv.lifetime_value'}))
    assert explained['composed'] == 'SUM(raw_payments.amount / 100)'
    path = structured(await client.call_tool('path', {'start': 'raw_orders', 'end': 'orders'}))
    assert path['found'] is True
    answer = structured(
        await client.call_tool('query', {'question': 'what breaks if stg_payments.amount changes'})
    )
    assert answer['impact']['summary'].endswith('downstream.')
    missing = await client.call_tool('impact', {'ref': 'nope'})
    assert missing.is_error
    assert 'nothing in the graph matches' in str(missing.content)


def test_in_process(jaffle_built_graph: Path) -> None:
    async def main() -> None:
        async with Client(build_server(jaffle_built_graph)) as client:
            await _exercise(client)

    anyio.run(main)


def test_over_stdio(jaffle_built_graph: Path) -> None:
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    params = StdioServerParameters(
        command=sys.executable,
        args=['-m', 'konstruct.interfaces.cli', 'mcp', '--path', str(jaffle_built_graph)],
        env=env,
    )

    async def main() -> None:
        async with Client(params) as client:
            await _exercise(client)

    anyio.run(main)


def test_reloads_a_rebuilt_graph_and_reports_a_missing_one(jaffle_built_graph: Path, tmp_path: Path) -> None:
    project = tmp_path / 'p'
    (project / 'konstruct-out').mkdir(parents=True)

    async def main() -> None:
        async with Client(build_server(project)) as client:
            missing = await client.call_tool('describe', {'ref': 'orders'})
            assert missing.is_error and 'konstruct build' in str(missing.content)
            shutil.copy(
                jaffle_built_graph / 'konstruct-out' / 'graph.json', project / 'konstruct-out' / 'graph.json'
            )
            found = structured(await client.call_tool('describe', {'ref': 'orders'}))
            assert found['kind'] == 'model'

    anyio.run(main)
