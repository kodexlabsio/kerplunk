"""Format versions: every document carries one, and a newer one is refused, never misread."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from konstruct import FormatError, Project
from konstruct.core import formats
from konstruct.core.graph import GRAPH_FORMAT_VERSION, Graph, Node
from konstruct.interfaces.cli import main
from konstruct.interfaces.mcp_server import build_server


def write_graph(root: Path, **fields: Any) -> Path:
    graph = Graph()
    graph.add_node(Node('table.orders', 'table', 'orders', relation='shop.main.orders'))
    graph.add_node(Node('table.orders#amount', 'column', 'amount', parent='table.orders'))
    graph.add_edge('table.orders', 'table.orders#amount', 'contains')
    data = {**graph.to_dict(), **fields}
    for key in [k for k, v in data.items() if v is None]:
        del data[key]
    path = root / 'konstruct-out' / 'graph.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_a_graph_carries_its_format_version(tmp_path: Path) -> None:
    path = write_graph(tmp_path)
    assert json.loads(path.read_text())['format_version'] == GRAPH_FORMAT_VERSION == 1


def test_a_graph_without_a_format_version_is_format_1(tmp_path: Path) -> None:
    write_graph(tmp_path, format_version=None)
    assert [i.label for i in Project(tmp_path).list(['column']).items] == ['orders.amount']


@pytest.mark.parametrize('version', [GRAPH_FORMAT_VERSION + 1, 99])
def test_a_newer_graph_is_refused_with_an_upgrade_message(tmp_path: Path, version: int) -> None:
    path = write_graph(tmp_path, format_version=version)
    with pytest.raises(FormatError, match=f'{path} was written by a newer Konstruct') as raised:
        Project(tmp_path).list()
    assert f'format {version};' in str(raised.value) and 'upgrade Konstruct' in str(raised.value)


@pytest.mark.parametrize('version', ['2', 0, True, 1.5])
def test_an_unknown_format_version_is_refused(tmp_path: Path, version: object) -> None:
    write_graph(tmp_path, format_version=version)
    with pytest.raises(FormatError, match='unknown format_version'):
        Project(tmp_path).list()


def test_a_document_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(FormatError, match='is not a Konstruct document'):
        formats.check([], 1, 'graph.json')


def test_the_cli_reports_a_newer_graph_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_graph(tmp_path, format_version=GRAPH_FORMAT_VERSION + 1)
    assert main(['--no-color', '-C', str(tmp_path), 'list', '--json']) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'written by a newer Konstruct' in captured.err


def test_the_mcp_server_reports_a_newer_graph_as_a_tool_error(tmp_path: Path) -> None:
    write_graph(tmp_path, format_version=GRAPH_FORMAT_VERSION + 1)

    async def call() -> Any:
        async with Client(build_server(tmp_path)) as client:
            return await client.call_tool('describe', {'ref': 'orders'})

    result = anyio.run(call)
    assert result.is_error
    assert 'written by a newer Konstruct' in str(result.content)


def test_json_output_carries_the_format_version_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_graph(tmp_path)
    assert main(['--no-color', '-C', str(tmp_path), 'describe', 'orders', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert next(iter(data)) == 'format_version' and data['format_version'] == 1
    assert data['name'] == 'orders'


def test_files_in_konstruct_out_carry_the_format_version(tmp_path: Path) -> None:
    write_graph(tmp_path)
    Project(tmp_path).conflicts(write=True)
    assert json.loads((tmp_path / 'konstruct-out' / 'conflicts.json').read_text())['format_version'] == 1


def test_status_reports_a_newer_graph_instead_of_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_graph(tmp_path, format_version=GRAPH_FORMAT_VERSION + 1)
    assert main(['--no-color', '-C', str(tmp_path), 'status', '--json']) == 0
    graph = json.loads(capsys.readouterr().out)['graph']
    assert graph['exists'] and 'written by a newer Konstruct' in graph['unreadable']
    assert main(['--no-color', '-C', str(tmp_path), 'status']) == 0
    [line] = [line for line in capsys.readouterr().out.splitlines() if line.lstrip().startswith('graph')]
    assert 'written by a newer Konstruct' in line and 'upgrade Konstruct' in line
