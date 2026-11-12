"""Node, Edge, Step and Graph keep behaving as the dataclass and class they replaced did."""

from __future__ import annotations

import ast
import copy
import dataclasses
import gc
import pickle
import weakref
from pathlib import Path
from typing import Any

import pytest

from konstruct import _core
from konstruct.core.graph import Edge, Graph, Node, Step


def test_nodes_edges_and_steps_are_dataclasses() -> None:
    node = Node('a', 'table', 'a', origins=['x'])
    edge = Edge('a', 'b', 'feeds', {'role': 'reference'})
    step = Step(node, 1, edge, 'a')
    for obj in (node, edge, step):
        assert dataclasses.is_dataclass(obj) and dataclasses.is_dataclass(type(obj))
    assert [f.name for f in dataclasses.fields(Node)] == [
        'id',
        'kind',
        'name',
        'parent',
        'relation',
        'description',
        'data_type',
        'path',
        'origins',
        'attrs',
    ]
    assert dataclasses.asdict(edge) == {
        'source': 'a',
        'target': 'b',
        'kind': 'feeds',
        'attrs': {'role': 'reference'},
    }
    assert dataclasses.replace(node, name='b') == Node('a', 'table', 'b', origins=['x'])
    assert node.__replace__(kind='model').kind == 'model'
    assert Node.__match_args__[:3] == ('id', 'kind', 'name')
    match edge:
        case Edge(source, target, kind):
            assert (source, target, kind) == ('a', 'b', 'feeds')
    assert Node('a', 'table', 'a') == Node('a', 'table', 'a') != Node('b', 'table', 'a')
    assert Node.__hash__ is None


def test_nodes_take_weak_references_and_other_attributes() -> None:
    node = Node('a', 'table', 'a')
    ref = weakref.ref(node)
    node.cached = 1  # type: ignore[attr-defined]
    assert node.cached == 1 and ref() is node  # type: ignore[attr-defined]
    del node
    gc.collect()
    assert ref() is None
    graph = Graph()
    graph.note = 'x'  # type: ignore[attr-defined]
    assert weakref.ref(graph)() is graph


def test_a_subclass_can_call_super_init() -> None:
    class Mine(Node):
        def __init__(self, id: str, kind: Any, name: str) -> None:
            super().__init__(id, kind, name, description='mine')
            self.extra = 1

    mine = Mine('a', 'table', 'a')
    assert mine.description == 'mine' and mine.extra == 1


def test_copy_and_pickle_keep_nodes_equal() -> None:
    node = Node('a', 'table', 'a', attrs={'k': [1]})
    assert copy.deepcopy(node) == node == pickle.loads(pickle.dumps(node))


def test_graph_nodes_can_be_assigned_and_classmethods_follow_the_subclass(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_node(Node('a', 'table', 'a', relation='x.a'))
    other = {'b': Node('b', 'table', 'b', relation='x.b')}
    graph.nodes = other
    assert list(graph.nodes) == ['b']
    graph.nodes = graph.nodes

    class Mine(Graph):
        pass

    source = Graph()
    source.add_node(Node('a', 'table', 'a', relation='x.a'))
    source.write(tmp_path / 'g.json')
    assert isinstance(Mine.read(tmp_path / 'g.json'), Mine)
    assert isinstance(Mine.from_json((tmp_path / 'g.json').read_bytes()), Mine)
    assert isinstance(Mine.from_dict(source.to_dict()), Mine)
    assert list(Mine.read(tmp_path / 'g.json').nodes) == ['a']


def _stub_class(name: str) -> ast.ClassDef:
    tree = ast.parse(
        (Path(__file__).resolve().parents[1] / 'konstruct' / '_core.pyi').read_text(encoding='utf-8')
    )
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


@pytest.mark.parametrize('name', ['Node', 'Edge', 'Step', 'Graph'])
def test_the_stub_describes_the_live_class(name: str) -> None:
    """Whatever the stub declares a class has, the class has: a removal cannot hide in the stub."""
    cls = getattr(_core, name)
    declared = _stub_class(name)
    for item in declared.body:
        if isinstance(item, ast.FunctionDef) and item.name != '__init__':
            assert hasattr(cls, item.name), f'{name}.{item.name}'
        elif (
            isinstance(item, ast.AnnAssign)
            and name != 'Graph'
            and not ast.unparse(item.target).startswith('__')
        ):
            field = ast.unparse(item.target)
            assert field in cls.__dataclass_fields__, f'{name}.{field}'
            assert cls.__dataclass_fields__[field].type == ast.unparse(item.annotation)
    if name != 'Graph':
        stub_fields = [
            ast.unparse(i.target)
            for i in declared.body
            if isinstance(i, ast.AnnAssign) and not ast.unparse(i.target).startswith('__')
        ]
        assert [f.name for f in dataclasses.fields(cls)] == stub_fields
    else:
        graph = Graph()
        for item in declared.body:
            if isinstance(item, ast.AnnAssign):
                assert hasattr(graph, ast.unparse(item.target))


def test_a_subclass_init_may_take_arguments_of_its_own() -> None:
    class Table(Node):
        def __init__(self, relation: str, *, columns: int = 0) -> None:
            super().__init__(f'table.{relation}', 'table', relation, relation=relation)
            self.columns = columns

    table = Table('shop.orders', columns=3)
    assert (table.id, table.relation, table.columns) == ('table.shop.orders', 'shop.orders', 3)

    class Link(Edge):
        def __init__(self, pair: tuple[str, str]) -> None:
            super().__init__(pair[0], pair[1], 'feeds')

    assert Link(('a', 'b')).target == 'b'
    with pytest.raises(TypeError):
        Node('only-an-id')  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Node('a', 'table', 'a', nothing=1)  # type: ignore[call-arg]


def _stub_signature(item: ast.FunctionDef) -> tuple[list[str], dict[str, Any], list[str], str | None]:
    arguments = item.args
    positional = [a for a in [*arguments.posonlyargs, *arguments.args] if a.arg not in ('self', 'cls')]
    defaults: dict[str, Any] = {}
    for argument, default in zip(reversed(positional), reversed(arguments.defaults), strict=False):
        defaults[argument.arg] = ast.literal_eval(default)
    for keyword, fallback in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
        if fallback is not None:
            defaults[keyword.arg] = ast.literal_eval(fallback)
    return (
        [a.arg for a in positional],
        defaults,
        [a.arg for a in arguments.kwonlyargs],
        arguments.kwarg.arg if arguments.kwarg else None,
    )


@pytest.mark.parametrize('name', ['Node', 'Edge', 'Step', 'Graph'])
def test_every_live_method_has_the_stubs_parameters(name: str) -> None:
    """The stub is what the interface snapshot reads, and the snapshot is main's: a parameter
    renamed in the native class (`ref` to `reference`) must fail here."""
    import inspect

    cls = getattr(_core, name)
    for item in _stub_class(name).body:
        if not isinstance(item, ast.FunctionDef) or item.name.startswith('_'):
            continue
        if any(ast.unparse(d) == 'property' for d in item.decorator_list):
            continue
        positional, defaults, keyword_only, variadic = _stub_signature(item)
        live = inspect.signature(getattr(cls, item.name))
        parameters = [p for n, p in live.parameters.items() if n not in ('self', 'cls')]
        names = [p.name for p in parameters if p.kind is not p.VAR_KEYWORD and p.name not in keyword_only]
        assert names == positional, f'{name}.{item.name}'
        assert [p.name for p in parameters if p.kind is p.KEYWORD_ONLY] == keyword_only, f'{name}.{item.name}'
        assert {p.name: p.default for p in parameters if p.default is not p.empty} == defaults, item.name
        assert next((p.name for p in parameters if p.kind is p.VAR_KEYWORD), None) == variadic, item.name


def test_resolve_takes_its_reference_by_keyword() -> None:
    graph = Graph()
    graph.add_node(Node('a', 'table', 'a', relation='x.a'))
    assert [n.id for n in graph.resolve(ref='a')] == ['a']


class Table(Node):
    def __init__(self, relation: str, *, columns: int = 0) -> None:
        super().__init__(f'table.{relation}', 'table', relation, relation=relation)
        self.columns = columns


def test_copies_and_pickles_of_a_subclass_with_its_own_init_keep_their_attributes() -> None:
    table = Table('shop.orders', columns=3)
    table.note = 'x'  # type: ignore[attr-defined]
    for copied in (copy.copy(table), copy.deepcopy(table), pickle.loads(pickle.dumps(table))):
        assert isinstance(copied, Table)
        assert (copied.id, copied.columns, copied.note) == ('table.shop.orders', 3, 'x')  # type: ignore[attr-defined]
        assert copied == table


def test_a_node_pickled_as_a_dataclass_loads() -> None:
    """What main pickled: the class, made without arguments, and its fields as a dict."""
    state: dict[str, Any] = {
        'id': 'a',
        'kind': 'table',
        'name': 'a',
        'parent': None,
        'relation': 'x.a',
        'description': None,
    }
    state.update(data_type=None, path=None, origins=['dbt'], attrs={'k': 1})
    node = Node.__new__(Node)
    node.__setstate__(state)  # type: ignore[attr-defined]
    assert node == Node('a', 'table', 'a', relation='x.a', origins=['dbt'], attrs={'k': 1})


def test_a_graph_pickled_before_graphs_were_native_loads() -> None:
    first = Node('t.a', 'table', 'a', relation='x.a')
    edge = Edge('t.a', 't.b', 'feeds')
    state = {
        'nodes': {'t.a': first},
        '_edges': {edge.key: edge},
        '_out': {},
        '_in': {},
        '_relations': {'x.a': ['t.a']},
        'warnings': ['w'],
        'meta': {'m': 1},
        '_versions': {'graph': 3, 'nodes': 3, 'relations': 3},
        '_indexes': {},
        '_file': None,
    }
    graph = Graph.__new__(Graph)
    graph.__setstate__(state)  # type: ignore[attr-defined]
    assert list(graph.nodes) == ['t.a'] and graph.edges == [edge]
    assert graph.warnings == ['w'] and graph.meta == {'m': 1}
    assert graph.node_for_relation('x.a') is first
    for copied in (copy.deepcopy(graph), pickle.loads(pickle.dumps(graph))):
        assert copied.to_dict() == graph.to_dict()


def test_a_subclass_that_overrides_the_ways_in_is_asked(tmp_path: Path) -> None:
    calls: list[str] = []

    class Counting(Graph):
        def add_node(self, node: Node) -> Node:
            calls.append('node')
            return super().add_node(node)

        def add_edge(self, source: str, target: str, kind: Any, **attrs: Any) -> Edge | None:
            calls.append('edge')
            return super().add_edge(source, target, kind, **attrs)

    graph = Counting()
    graph.add_column(graph.add_node(Node('t.a', 'table', 'a')), 'c')
    assert calls == ['node', 'node', 'edge']
    source = Graph()
    source.add_column(source.add_node(Node('t.a', 'table', 'a')), 'c')
    source.write(tmp_path / 'g.json')
    calls.clear()
    Counting.read(tmp_path / 'g.json')
    Counting.from_dict(source.to_dict())
    assert calls == ['node', 'node', 'edge'] * 2


def test_write_says_what_to_dict_says_for_a_subclass(tmp_path: Path) -> None:
    class Quiet(Node):
        def to_dict(self) -> dict[str, Any]:
            return {**super().to_dict(), 'name': 'renamed'}

    graph = Graph()
    graph.add_node(Quiet('a', 'table', 'a'))
    graph.write(tmp_path / 'g.json')
    assert Graph.read(tmp_path / 'g.json').nodes['a'].name == 'renamed'


def test_type_hints_of_the_classes_resolve() -> None:
    import typing

    assert typing.get_type_hints(Node)['attrs'] == dict[str, Any]
    assert typing.get_type_hints(Edge)['source'] is str


def test_the_module_functions_have_the_stubs_parameters() -> None:
    import inspect

    tree = ast.parse(
        (Path(__file__).resolve().parents[1] / 'konstruct' / '_core.pyi').read_text(encoding='utf-8')
    )
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ('column_id', 'relation_key', 'reading_key', 'split_reading'):
        positional, defaults, _, _ = _stub_signature(functions[name])
        parameters = list(inspect.signature(getattr(_core, name)).parameters.values())
        assert [p.name for p in parameters if p.kind is not p.VAR_POSITIONAL] == positional, name
        assert {p.name: p.default for p in parameters if p.default is not p.empty} == defaults, name
        assert (functions[name].args.vararg is not None) == any(
            p.kind is p.VAR_POSITIONAL for p in parameters
        )


def test_a_graph_subclass_may_take_arguments_and_graph_takes_none() -> None:
    class Mine(Graph):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

    assert Mine('x').name == 'x'
    with pytest.raises(TypeError):
        Graph(1)  # type: ignore[call-arg]


_MUTATING = """
import gc, sys
from dataclasses import dataclass
from konstruct.core.graph import Graph, Node

@dataclass
class Mutating:
    x: int
    def __getattribute__(self, name):
        if name == 'x':
            MUTATION
        return object.__getattribute__(self, name)

SETUP

try:
    graph = Graph()
    graph.add_node(Node('a', 'table', 'a', attrs=attrs))
    graph.write(sys.argv[1])
except Exception as error:
    print(type(error).__name__)
    sys.exit(3)
gc.collect()
print('written')
"""


@pytest.mark.parametrize(
    ('setup', 'mutation'),
    [
        # The list being written is emptied by an item.
        ("items = [Mutating(1), 2, 3]\nattrs = {'v': items}", 'items.clear()'),
        # The dict that holds the list of strings being written is emptied, and collected: the
        # strings are freed under a walk that borrowed them.
        (
            "attrs = {'v': [Mutating(1)] + [str(n) * 8 for n in range(1000)]}",
            'attrs.clear(); gc.collect()',
        ),
        # The dict being written grows.
        ("attrs = {'a': Mutating(1), 'b': 'x'}", "attrs['grown'] = 1"),
        # A tuple and a boolean, which the fast path takes, around a value that needs Python code.
        ("attrs = {'v': (True, False, Mutating(1), None)}", "attrs['v'] = ()"),
    ],
)
def test_a_value_that_changes_the_container_being_written_cannot_crash_the_writer(
    setup: str, mutation: str, tmp_path: Path
) -> None:
    import subprocess
    import sys

    code = _MUTATING.replace('MUTATION', mutation).replace('SETUP', setup)
    done = subprocess.run(
        [sys.executable, '-c', code, str(tmp_path / 'g.json')], capture_output=True, text=True
    )
    # It writes, or refuses as Python refuses a dict that changed size; it never crashes, never
    # fails for another reason, and never raises what `except Exception` misses.
    assert done.returncode in (0, 3), (done.returncode, done.stderr[-600:])
    if done.returncode == 3:
        assert done.stdout.strip() == 'RuntimeError', (done.stdout, done.stderr[-600:])
    else:
        assert 'written' in done.stdout and (tmp_path / 'g.json').exists()
