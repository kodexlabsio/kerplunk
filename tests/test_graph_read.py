"""Graph.read against a reference: the standard library's json module reading the same file into
Graph.from_dict. Every graph the corpus, the example and generated projects hold, and graphs
written to cover what a hand-edited file may hold, must load into exactly the same graph."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import orjson
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from konstruct.core.formats import FormatError
from konstruct.core.graph import Edge, Graph, Node
from tests.test_scale import make_project

CORPUS = Path(__file__).parent / 'compat'
SNAPSHOTS = sorted(CORPUS.glob('*/*/graph.json'))


def typed(value: Any) -> Any:
    """``value`` with every type and every dictionary's order made part of its equality, as
    ``1 == 1.0 == True`` and dictionaries compare equal in any order."""
    if isinstance(value, dict):
        return ('dict', [(typed(k), typed(v)) for k, v in value.items()])
    if isinstance(value, list):
        return ('list', [typed(v) for v in value])
    if isinstance(value, float) and value == 0:
        return ('float', str(value))  # -0.0 and 0.0
    return (type(value).__name__, value)


def node_state(node: Node) -> Any:
    return typed(dict(vars(node)))


def edge_state(edge: Edge) -> Any:
    return typed(dict(vars(edge)))


def steps(walk: list[Any]) -> list[Any]:
    return [(s.node.id, s.depth, None if s.via is None else s.via.key, s.previous) for s in walk]


def answer(question: Callable[[], Any]) -> Any:
    """What ``question`` returns, or the exception it raises, as a value to compare."""
    try:
        return question()
    except Exception as error:
        return (type(error), str(error))


def neighbours(graph: Graph, node_id: str) -> Any:
    """The edges a change to ``node_id`` propagates along, or the exception asking raises."""
    return answer(lambda: [(e.key, n) for e, n in graph.downstream_edges(node_id)])


def walked(graph: Graph, walk: str, node: Node) -> Any:
    """The steps of a walk from ``node``, or the exception it raises (a graph whose edges name
    nodes it does not have can make a walk fail, and must fail in the same way)."""
    try:
        return steps(getattr(graph, walk)(node))
    except Exception as error:
        return (type(error), str(error))


def assert_same(read: Graph, reference: Graph) -> None:
    """``read`` is ``reference`` in everything a caller can see, asked first of the edges the
    file still holds and then of the edges made Python objects."""
    assert list(read.nodes) == list(reference.nodes)
    assert [node_state(n) for n in read.nodes.values()] == [node_state(n) for n in reference.nodes.values()]
    assert typed(read.meta) == typed(reference.meta)
    assert typed(read.warnings) == typed(reference.warnings)
    assert read._relations == reference._relations
    ends = list(read.nodes) + [e.source for e in reference.edges] + [e.target for e in reference.edges]
    for _ in range(2):
        for node_id in dict.fromkeys(ends):
            for kind in (None, 'contains', 'derived_from', 'feeds', 'nothing'):
                assert [edge_state(e) for e in read.out_edges(node_id, kind)] == [
                    edge_state(e) for e in reference.out_edges(node_id, kind)
                ], node_id
                assert [edge_state(e) for e in read.in_edges(node_id, kind)] == [
                    edge_state(e) for e in reference.in_edges(node_id, kind)
                ], node_id
        for node in list(reference.nodes.values())[:: max(1, len(reference.nodes) // 200)]:
            mine = read.nodes[node.id]
            for walk in ('impact', 'trace'):
                assert walked(read, walk, mine) == walked(reference, walk, node), node.id
            assert neighbours(read, node.id) == neighbours(reference, node.id)
        # The second time round, every edge is a Python object.
        assert [edge_state(e) for e in read.edges] == [edge_state(e) for e in reference.edges]
    assert read.stats() == reference.stats()
    assert answer(lambda: typed(read.to_dict())) == answer(lambda: typed(reference.to_dict()))


def reference(path: Path) -> Graph:
    return Graph.from_dict(json.loads(path.read_bytes()))


@pytest.mark.parametrize(
    'path', SNAPSHOTS, ids=[f'{p.parent.parent.name}/{p.parent.name}' for p in SNAPSHOTS]
)
def test_every_graph_in_the_corpus(path: Path) -> None:
    assert SNAPSHOTS
    assert_same(Graph.read(path), reference(path))


def test_the_example(jaffle_built_graph: Path) -> None:
    path = jaffle_built_graph / 'konstruct-out' / 'graph.json'
    assert_same(Graph.read(path), reference(path))


@pytest.mark.scale
@pytest.mark.parametrize('models', [100, 2000])
def test_generated_projects(models: int, tmp_path: Path) -> None:
    from konstruct import Project

    make_project(tmp_path, models=models, columns_per_model=20)
    built = Project(tmp_path).build(run_dbt=False, warehouses=False).graph
    path = tmp_path / 'konstruct-out' / 'graph.json'
    read = Graph.read(path)
    assert_same(read, reference(path))
    assert typed(read.to_dict()) == typed(built.to_dict())


def test_a_graph_written_and_read_again_writes_the_same_bytes(tmp_path: Path) -> None:
    for path in SNAPSHOTS:
        Graph.read(path).write(tmp_path / 'graph.json')
        Graph.read(tmp_path / 'graph.json').write(tmp_path / 'again.json')
        assert (tmp_path / 'again.json').read_bytes() == (tmp_path / 'graph.json').read_bytes()


IRREGULAR: dict[str, Any] = {
    'format_version': 1,
    'meta': {'root': '/r', 'dialect': 'duckdb', 'n': [1, 2.5, -0.0, True, None, {'x': 'y'}]},
    'nodes': [
        {'id': 'model.a', 'kind': 'model', 'name': 'a', 'relation': 'db.main.a', 'attrs': {'k': 1}},
        {'id': 'model.a#x', 'kind': 'column', 'name': 'x', 'parent': 'model.a', 'origins': ['dbt', 'dbt']},
        {'id': 'model.a#y', 'kind': 'column', 'name': 'y', 'parent': '', 'description': ''},
        {'id': 'seed.a', 'kind': 'seed', 'name': 'a', 'relation': 'db.main.a'},
        {
            'id': 'source.s.a',
            'kind': 'source',
            'name': 'a',
            'relation': 'db.main.a',
            'attrs': {'source_name': 's'},
        },
        {'id': 'task.d.t', 'kind': 'task', 'name': 't', 'attrs': {'dag': 'd'}},
        {'id': 'model.b', 'kind': 'model', 'name': 'b', 'relation': 'db.main.b', 'path': 'b.sql'},
        {'id': 'model.b#z', 'kind': 'column', 'name': 'z', 'parent': 'model.b', 'data_type': 'INT'},
        {'id': 'term.퟿é', 'kind': 'term', 'name': 'café \U0001f600', 'attrs': {'synonyms': ['q']}},
        {
            'id': 'model.a',
            'kind': 'table',
            'name': 'other',
            'path': 'a.sql',
            'origins': ['warehouse'],
            'attrs': {'k': 2, 'j': 3},
        },
        {'id': 'exposure.e', 'kind': 'exposure', 'name': 'e', 'unknown': [1, 2]},
    ],
    'edges': [
        {'source': 'model.a', 'target': 'model.a#x', 'kind': 'contains'},
        {'source': 'model.a', 'target': 'model.a#y', 'kind': 'contains'},
        {'source': 'model.b', 'target': 'model.b#z', 'kind': 'contains'},
        {
            'source': 'model.b#z',
            'target': 'model.a#x',
            'kind': 'derived_from',
            'attrs': {'transform': 'direct'},
        },
        {'source': 'model.a#y', 'target': 'model.b', 'kind': 'feeds', 'attrs': {'role': 'reference'}},
        {
            'source': 'model.b#z',
            'target': 'model.a#x',
            'kind': 'derived_from',
            'attrs': {'transform': 'x', 'role': 'reference'},
        },
        {'source': 'model.a', 'target': 'model.a', 'kind': 'feeds'},
        {
            'source': 'model.a',
            'target': 'model.b',
            'kind': 'feeds',
            'attrs': {'via': 'task.d.t', 'also_via': ['task.d.t', 3]},
        },
        {'source': 'task.d.t', 'target': 'model.b', 'kind': 'runs'},
        {'source': 'model.b', 'target': 'exposure.e', 'kind': 'feeds'},
        {'source': 'missing.node', 'target': 'model.b', 'kind': 'feeds'},
        {'source': 'model.b', 'target': 'missing.other', 'kind': 'a kind of its own'},
        {'source': 'term.퟿é', 'target': 'model.a#x', 'kind': 'defines', 'attrs': {'n': 1e300, 'm': 2**63}},
    ],
    'warnings': ['one', 'two é'],
}


def write_variants(document: Any, tmp_path: Path) -> list[Path]:
    """``document`` written as Konstruct writes it and as other writers might: compact, escaped
    to ASCII, with odd whitespace."""
    paths = []
    texts = [
        orjson.dumps(document, option=orjson.OPT_INDENT_2),
        json.dumps(document).encode(),
        json.dumps(document, ensure_ascii=True, indent='\t').encode(),
        json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode(),
        b'\r\n ' + json.dumps(document, indent=7).encode().replace(b'\n', b'\r\n') + b' \n',
    ]
    for number, text in enumerate(texts):
        path = tmp_path / f'graph{number}.json'
        path.write_bytes(text)
        paths.append(path)
    return paths


def test_a_hand_edited_graph(tmp_path: Path) -> None:
    for path in write_variants(IRREGULAR, tmp_path):
        assert_same(Graph.read(path), reference(path))


def test_duplicate_keys_keep_the_last_value_where_the_first_was(tmp_path: Path) -> None:
    path = tmp_path / 'graph.json'
    path.write_text(
        '{"meta": {"a": 1, "b": 2, "a": 3}, "nodes": [{"id": "x", "kind": "model", "name": "first", '
        '"name": "second"}], "edges": [], "nodes": [{"id": "y", "kind": "model", "name": "y", '
        '"attrs": {"\\u0061": 1, "a": 2}}]}'
    )
    read = Graph.read(path)
    assert_same(read, reference(path))
    assert list(read.meta.items()) == [('a', 3), ('b', 2)]
    assert list(read.nodes) == ['y']
    assert read.nodes['y'].attrs == {'a': 2}


def test_numbers_read_as_they_were(tmp_path: Path) -> None:
    """Integers outside 64 bits became floats, as orjson read them; the json module would keep
    them as integers, so the reference here is orjson."""
    path = tmp_path / 'graph.json'
    path.write_text(
        '{"meta": {"big": 18446744073709551616, "max": 18446744073709551615, "min": -9223372036854775808,'
        ' "small": -9223372036854775809, "e": 1E5, "z": -0, "f": -0.0, "tiny": 1e-400}}'
    )
    read = Graph.read(path)
    assert typed(read.meta) == typed(orjson.loads(path.read_bytes())['meta'])


def test_the_edges_stay_in_the_file_until_read(tmp_path: Path) -> None:
    path = tmp_path / 'graph.json'
    # Without the node whose id came before: merging it changes the graph, so walks need the
    # index made again from Python objects.
    unique = {**IRREGULAR, 'nodes': [n for n in IRREGULAR['nodes'] if n['kind'] != 'table']}
    path.write_text(json.dumps(unique))
    graph = Graph.read(path)
    assert in_file(graph) and not graph._edges
    first = graph.out_edges('model.a')
    assert graph.out_edges('model.a')[0] is first[0]
    reached = {s.via.key: s.via for s in graph.trace(graph.nodes['model.b#z']) if s.via}
    assert reached and in_file(graph)
    every = {edge.key: edge for edge in graph.edges}
    assert not in_file(graph)
    assert all(edge is every[key] for key, edge in reached.items())
    assert graph.out_edges('model.a')[0] is first[0]


def test_a_read_graph_copies_and_pickles(tmp_path: Path) -> None:
    import copy
    import pickle

    path = tmp_path / 'graph.json'
    path.write_text(json.dumps(IRREGULAR))
    for queried in (False, True):
        graph = Graph.read(path)
        if queried:
            graph.impact(graph.nodes['model.b#z'])
        for copied in (copy.deepcopy(graph), pickle.loads(pickle.dumps(graph)), copy.copy(graph)):
            assert_same(copied, reference(path))


def test_edges_made_while_other_threads_read_them(tmp_path: Path) -> None:
    """The MCP server and the HTTP API answer from threads: while one makes every edge, the
    others see every edge of a node or the file's, never part of a list."""
    import sys

    path = SNAPSHOTS[-1]
    expected = Graph.read(path)
    ids = list(expected.nodes)
    want = {i: [e.key for e in expected.out_edges(i)] for i in ids}
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(30):
            graph = Graph.read(path)
            wrong: list[str] = []
            stop = threading.Event()

            def read(graph: Graph = graph, wrong: list[str] = wrong, stop: threading.Event = stop) -> None:
                while not stop.is_set():
                    for node_id in ids:
                        if [e.key for e in graph.out_edges(node_id)] != want[node_id]:
                            wrong.append(node_id)

            readers = [threading.Thread(target=read) for _ in range(3)]
            for reader in readers:
                reader.start()
            makers = [threading.Thread(target=lambda graph=graph: graph.edges) for _ in range(2)]
            for maker in makers:
                maker.start()
            for maker in makers:
                maker.join()
            stop.set()
            for reader in readers:
                reader.join()
            assert not wrong
            assert [e.key for e in graph.edges] == [e.key for e in expected.edges]
    finally:
        sys.setswitchinterval(interval)


def test_a_graph_pickled_before_reading_was_native_unpickles(tmp_path: Path) -> None:
    """A pickle of a graph from before graphs held their file has no ``_file``."""
    import pickle

    graph = reference(SNAPSHOTS[0])
    state = graph.__getstate__()
    del state['_file']
    old = Graph.__new__(Graph)
    old.__dict__.update(state)
    again = pickle.loads(pickle.dumps(old))
    assert_same(again, graph)


def in_file(graph: Graph) -> bool:
    """Whether the graph's edges are still in the file it was read from."""
    return graph._file is not None


def test_merging_a_node_read_twice_makes_the_index_again(tmp_path: Path) -> None:
    path = tmp_path / 'graph.json'
    path.write_text(json.dumps(IRREGULAR))
    graph = Graph.read(path)
    graph.impact(graph.nodes['model.b#z'])
    assert not in_file(graph)


def test_a_read_graph_changes_like_any_other(tmp_path: Path) -> None:
    path = tmp_path / 'graph.json'
    path.write_text(json.dumps(IRREGULAR))
    for change in ('edit', 'add', 'remove'):
        read, expected = Graph.read(path), reference(path)
        for graph in (read, expected):
            if change == 'edit':
                # An edge read before the change is the edge changed, and walks see it.
                edge = graph.out_edges('model.b#z', 'derived_from')[0]
                edge.attrs['role'] = 'other'
                graph.changed()
            elif change == 'add':
                graph.add_edge('model.a#x', 'model.b#z', 'derived_from', transform='direct')
            else:
                graph.remove_edge(graph.in_edges('model.b', 'runs')[0])
        assert_same(read, expected)


def test_a_newer_format_is_refused_before_anything_else(tmp_path: Path) -> None:
    path = tmp_path / 'graph.json'
    path.write_text('{"format_version": 99, "nodes": [1]}')
    with pytest.raises(FormatError, match='newer Konstruct'):
        Graph.read(path)
    path.write_text('[1, 2]')
    with pytest.raises(FormatError, match='not a Konstruct document'):
        Graph.read(path)


# Values no Konstruct writes, which Graph.from_dict gives a meaning to, or an error.
UNUSUAL = [
    '{"nodes": [{"id": "a", "kind": "model", "name": null}]}',
    '{"nodes": [{"id": "a", "kind": 3, "name": "a"}]}',
    '{"nodes": [{"id": "a", "kind": "model", "name": "a", "attrs": []}]}',
    '{"nodes": [{"id": "a", "kind": "model", "name": "a", "attrs": [["k", 1], "xy"]}]}',
    '{"nodes": [{"id": "a", "kind": "model", "name": "a", "origins": "abc"}]}',
    '{"nodes": [{"id": "a", "kind": "model", "name": "a", "origins": {"dbt": 1}}]}',
    '{"nodes": [{"id": "a", "kind": "model", "name": "a", "parent": 3, "relation": 4}]}',
    '{"nodes": [{"id": 1, "kind": "model", "name": "a"}, {"id": 1.0, "kind": "seed", "name": "b"}]}',
    '{"nodes": [{"id": "a", "kind": "model", "name": "a"}], "edges": [{"source": 1, "target": true, "kind": "feeds"}]}',
    '{"nodes": [{"id": "a", "kind": "model", "name": "a"}], "edges": [{"source": "a", "target": "b", "kind": 2}]}',
    '{"edges": [{"source": "a", "target": "b", "kind": "feeds", "attrs": [["role", "reference"]]}]}',
    '{"nodes": [{"id": "a", "kind": "model"}]}',
    '{"nodes": [{"id": "a", "kind": "model", "name": "a", "origins": 5}]}',
    '{"nodes": [{"id": "a", "kind": "model", "name": "a", "attrs": "xy"}]}',
    '{"nodes": [{"id": [1], "kind": "model", "name": "a"}]}',
    '{"nodes": [3]}',
    '{"nodes": {"a": 1}}',
    '{"nodes": "ab"}',
    '{"edges": [{"source": "a", "target": "b"}]}',
]


@pytest.mark.parametrize('text', UNUSUAL)
def test_values_no_konstruct_writes_mean_what_they_did(text: str, tmp_path: Path) -> None:
    path = tmp_path / 'graph.json'
    path.write_text(text)
    read, expected = answer(lambda: Graph.read(path)), answer(lambda: reference(path))
    if isinstance(expected, Graph):
        assert isinstance(read, Graph), read
        assert_same(read, expected)
    else:
        assert read == expected


@pytest.mark.parametrize(
    ('text', 'message', 'line', 'column'),
    [
        ('{"nodes": [1, 2,, 3]}', 'expected a value', 1, 17),
        ('{\n  "nodes": [\n    {"id": "éé", "kind": 1,}\n  ]\n}', 'trailing comma', 3, 28),
        ('{"a": "éé\x01"}', 'unexpected control character', 1, 10),
        ('{"a": 1}  x', 'unexpected content after the document', 1, 11),
        ('', 'expected a value', 1, 1),
    ],
)
def test_invalid_json_says_where_and_why(
    text: str, message: str, line: int, column: int, tmp_path: Path
) -> None:
    path = tmp_path / 'graph.json'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(json.JSONDecodeError) as raised:
        Graph.read(path)
    assert raised.value.msg == f'{path} is not valid JSON: {message}'
    assert (raised.value.lineno, raised.value.colno) == (line, column)
    # The standard library finds the same document invalid.
    with pytest.raises(ValueError):
        json.loads(text)


def test_invalid_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / 'graph.json'
    path.write_bytes(b'{"a": "\xc3\xa9\xff"}')
    with pytest.raises(json.JSONDecodeError, match='invalid UTF-8: line 1 column 9'):
        Graph.read(path)


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**63), max_value=2**64 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=12),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=6), inner, max_size=4),
    max_leaves=12,
)
ids = st.sampled_from(['a', 'b', 'c', 'a#x', 'a#y', 'b#x', 'task.t', 'é', ''])
kinds = st.sampled_from(['model', 'column', 'task', 'exposure', 'source', 'seed', 'test', 'term'])
edge_kinds = st.sampled_from(['contains', 'feeds', 'derived_from', 'runs', 'tests', 'precedes', 'other'])
optional = st.one_of(st.none(), ids)
nodes = st.fixed_dictionaries(
    {'id': ids, 'kind': kinds, 'name': st.text(max_size=8)},
    optional={
        'parent': optional,
        'relation': st.one_of(st.none(), st.sampled_from(['db.a', 'db.b'])),
        'description': json_values,
        'data_type': json_values,
        'path': json_values,
        'origins': st.lists(st.text(max_size=4), max_size=3),
        'attrs': st.dictionaries(st.text(max_size=6), json_values, max_size=4),
    },
)
edge_attrs = st.dictionaries(
    st.sampled_from(['role', 'via', 'also_via', 'transform', 'x']),
    st.one_of(st.sampled_from(['reference', 'task.t', 'direct']), st.lists(ids, max_size=3), json_values),
    max_size=4,
)
edges = st.fixed_dictionaries(
    {'source': ids, 'target': ids, 'kind': edge_kinds}, optional={'attrs': edge_attrs}
)
documents = st.fixed_dictionaries(
    {'nodes': st.lists(nodes, max_size=12), 'edges': st.lists(edges, max_size=20)},
    optional={
        'meta': st.dictionaries(st.text(max_size=6), json_values, max_size=3),
        'format_version': st.just(1),
        'warnings': st.lists(st.text(max_size=5), max_size=3),
    },
)


@settings(max_examples=150, deadline=None)
@example(
    # A node whose id came before takes its place in the relations where the file has it.
    document={
        'nodes': [
            {'id': 'a', 'kind': 'model', 'name': ''},
            {'id': 'a', 'kind': 'model', 'name': '', 'relation': 'db.b'},
            {'id': 'b', 'kind': 'model', 'name': '', 'relation': 'db.b'},
        ],
        'edges': [],
    },
    ascii_only=False,
    indent=None,
)
@example(
    # also_via that is not a list names no task.
    document={
        'nodes': [{'id': 'a', 'kind': 'model', 'name': ''}, {'id': 'b', 'kind': 'task', 'name': ''}],
        'edges': [
            {'source': 'a', 'target': 'b', 'kind': 'feeds', 'attrs': {'via': 'b', 'also_via': None}},
            {'source': 'b', 'target': 'a', 'kind': 'feeds', 'attrs': {'also_via': 'b'}},
        ],
    },
    ascii_only=False,
    indent=None,
)
@given(document=documents, ascii_only=st.booleans(), indent=st.sampled_from([None, 2, '\t']))
def test_any_graph_document(
    document: Any, ascii_only: bool, indent: Any, tmp_path_factory: pytest.TempPathFactory
) -> None:
    path = tmp_path_factory.mktemp('graph') / 'graph.json'
    path.write_text(json.dumps(document, ensure_ascii=ascii_only, indent=indent), encoding='utf-8')
    assert_same(Graph.read(path), reference(path))


anything = st.one_of(
    json_values, ids, st.lists(st.tuples(st.text(max_size=2), json_values).map(list), max_size=2)
)
unusual_nodes = st.dictionaries(
    st.sampled_from(['id', 'kind', 'name', 'parent', 'relation', 'description', 'origins', 'attrs']),
    st.one_of(ids, kinds, anything),
    max_size=8,
)
unusual_edges = st.dictionaries(
    st.sampled_from(['source', 'target', 'kind', 'attrs']), st.one_of(ids, edge_kinds, anything), max_size=4
)
unusual_documents = st.fixed_dictionaries(
    {},
    optional={
        'nodes': st.one_of(st.lists(st.one_of(nodes, unusual_nodes, json_values), max_size=8), json_values),
        'edges': st.one_of(st.lists(st.one_of(edges, unusual_edges, json_values), max_size=10), json_values),
    },
)


@settings(max_examples=300, deadline=None)
@given(document=unusual_documents)
def test_any_document_means_what_it_did(document: Any, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Whatever a document holds, Graph.read gives the graph Graph.from_dict gives, or the same
    error."""
    path = tmp_path_factory.mktemp('graph') / 'graph.json'
    path.write_text(json.dumps(document))
    read, expected = answer(lambda: Graph.read(path)), answer(lambda: reference(path))
    if isinstance(expected, Graph):
        assert isinstance(read, Graph), read
        assert_same(read, expected)
    else:
        assert read == expected
