"""The files Konstruct puts on disk whole, and graph.json as the native graph writer writes it: the
text orjson wrote, which the golden files and the compatibility corpus hold."""

from __future__ import annotations

import datetime
import enum
import errno
import os
import random
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from konstruct.core import jsonio
from konstruct.core.graph import Graph, Node

OPTIONS = orjson.OPT_NON_STR_KEYS | orjson.OPT_INDENT_2
NASTY = [
    '',
    'a',
    'é',
    chr(0x2028),
    '\U0001f600',
    '"',
    '\\',
    '\n\t\r\x08\x0c',
    '\x00\x01\x1f\x7f',
    '/',
    'a' * 40,
]


def graph_with(attrs: dict[str, Any], meta: dict[str, Any] | None = None) -> Graph:
    graph = Graph()
    graph.add_node(Node('t.a', 'table', 'a', attrs=attrs, description='é"\\\n'))
    graph.meta = meta or {}
    return graph


def written(graph: Graph, tmp_path: Path) -> bytes:
    graph.write(tmp_path / 'graph.json')
    return (tmp_path / 'graph.json').read_bytes()


def orjson_text(graph: Graph) -> bytes:
    return orjson.dumps(graph.to_dict(), option=OPTIONS) + b'\n'


@settings(max_examples=300, deadline=None)
@given(
    value=st.recursive(
        st.one_of(
            st.none(),
            st.booleans(),
            st.integers(-(2**63), 2**64 - 1),
            st.floats(),
            st.text(alphabet=st.characters(codec='utf-8')),
        ),
        lambda inner: st.one_of(
            st.lists(inner, max_size=4),
            st.tuples(inner, inner),
            st.dictionaries(
                st.one_of(st.text(), st.integers(-(2**63), 2**63 - 1), st.booleans()), inner, max_size=4
            ),
        ),
        max_leaves=20,
    )
)
def test_a_graph_is_written_as_orjson_writes_it(value: Any, tmp_path_factory: pytest.TempPathFactory) -> None:
    graph = graph_with({'v': value}, {'m': value})
    graph.warnings = [str(value)[:20]]
    assert written(graph, tmp_path_factory.mktemp('g')) == orjson_text(graph)


def test_floats_are_the_shortest_digits_that_read_back_with_ties_to_even(tmp_path: Path) -> None:
    rng = random.Random(7)
    values = [2.0**50 + 0.25, 2.0**52 + 0.5, 5e-324, 1.7976931348623157e308, 9007199254740993.0, 0.3, 2.5e-5]
    values += [1e-5, 9e-6, 1e15, 1e16, 1.2345e16, -0.0, 100.0, float('nan'), float('inf')]
    values += [struct.unpack('<d', rng.getrandbits(64).to_bytes(8, 'little'))[0] for _ in range(20000)]
    values += [rng.randrange(1, 2**53) / 2 ** rng.randrange(0, 60) for _ in range(20000)]
    values += [rng.randrange(2**50, 2**54) + rng.choice([0.25, 0.5, 0.75, 0.0]) for _ in range(5000)]
    graph = graph_with({'floats': values})
    assert written(graph, tmp_path) == orjson_text(graph)


def test_strings_and_integers_are_written_as_orjson_writes_them(tmp_path: Path) -> None:
    rng = random.Random(3)
    strings = [''.join(rng.choice(NASTY) for _ in range(rng.randrange(6))) for _ in range(3000)]
    ints = [
        0,
        -1,
        9,
        10,
        2**63 - 1,
        -(2**63),
        2**64 - 1,
        *(rng.randrange(-(10**12), 10**12) for _ in range(2000)),
    ]
    graph = graph_with({'s': strings, 'i': ints, 'k': dict.fromkeys(strings[:200], 1)})
    assert written(graph, tmp_path) == orjson_text(graph)


def test_what_json_has_no_form_for_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match='not JSON serializable'):
        written(graph_with({'v': {1, 2}}), tmp_path)
    with pytest.raises(TypeError, match='64-bit'):
        written(graph_with({'v': 2**64}), tmp_path)
    with pytest.raises(TypeError):
        written(graph_with({'v': '\ud800'}), tmp_path)


def test_io_errors_carry_the_path_and_errno(tmp_path: Path) -> None:
    missing = tmp_path / 'nothing' / 'graph.json'
    with pytest.raises(OSError) as caught:
        Graph.read(missing)
    assert isinstance(caught.value, FileNotFoundError)
    assert caught.value.errno == errno.ENOENT and caught.value.filename == str(missing)
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    with pytest.raises(OSError) as caught:
        jsonio.write_text(blocker / 'inside.json', 'x')
    assert caught.value.filename is not None and caught.value.errno is not None
    with pytest.raises(OSError) as caught:
        jsonio.real_directory(blocker, 'sub')
    assert caught.value.filename is not None


def test_text_with_a_lone_surrogate_raises_the_error_python_raises(tmp_path: Path) -> None:
    with pytest.raises(UnicodeEncodeError) as caught:
        jsonio.write_text(tmp_path / 'a', 'a\ud800')
    assert caught.value.start == 1 and caught.value.encoding == 'utf-8'


def test_files_are_written_whole_or_not_at_all(tmp_path: Path) -> None:
    path = tmp_path / 'out' / 'graph.json'
    jsonio.write_text(path, '{"a": 1}')
    assert path.read_text() == '{"a": 1}'
    umask = os.umask(0)
    os.umask(umask)
    assert path.stat().st_mode & 0o777 == 0o666 & ~umask
    blocked = tmp_path / 'out' / 'directory'
    blocked.mkdir()
    (blocked / 'kept').write_text('x')
    with pytest.raises(OSError):
        jsonio.write_text(blocked, 'text')
    assert sorted(p.name for p in path.parent.iterdir()) == ['directory', 'graph.json']


def test_a_rewritten_file_keeps_its_mode_and_a_link_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / 'REPORT.md'
    path.write_text('old')
    path.chmod(0o600)
    jsonio.write_text(path, 'new')
    assert path.read_text() == 'new' and path.stat().st_mode & 0o777 == 0o600
    target = tmp_path / 'outside.md'
    target.write_text('theirs')
    target.chmod(0o640)
    link = tmp_path / 'linked.md'
    link.symlink_to(target)
    umask = os.umask(0)
    os.umask(umask)
    jsonio.write_text(link, 'ours')
    assert not link.is_symlink() and link.stat().st_mode & 0o777 == 0o666 & ~umask
    assert target.read_text() == 'theirs'


def test_text_is_written_as_it_is_with_no_line_ending_changes(tmp_path: Path) -> None:
    jsonio.write_text(tmp_path / 'a.txt', 'one\r\ntwo\né')
    assert (tmp_path / 'a.txt').read_bytes() == b'one\r\ntwo\n\xc3\xa9'


def test_a_file_in_the_current_directory_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    jsonio.write_text(Path('plain.json'), '{}')
    assert (tmp_path / 'plain.json').read_text() == '{}'


def test_a_link_in_the_way_of_a_directory_is_removed(tmp_path: Path) -> None:
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'out').symlink_to(outside)
    made = jsonio.real_directory(tmp_path, 'out', 'sub')
    assert made == tmp_path / 'out' / 'sub' and made.is_dir()
    assert not (tmp_path / 'out').is_symlink() and list(outside.iterdir()) == []
    assert jsonio.real_directory(tmp_path, 'out', 'sub') == made


class Colour(enum.Enum):
    RED = 'red'


@dataclass
class Point:
    x: int
    _hidden: int = 1


def test_values_orjson_wrote_beyond_json_are_written_the_same(tmp_path: Path) -> None:
    zone = datetime.timezone(datetime.timedelta(hours=5, minutes=30, seconds=15))
    leaves = [
        datetime.date(2026, 1, 2),
        datetime.datetime(2026, 1, 2, 3, 4, 5),
        datetime.datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=zone),
        datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC),
        datetime.time(1, 2, 3, 4),
        datetime.time(0, 0, 0, 10000),
        uuid.UUID(int=5),
        Colour.RED,
        Point(1),
    ]
    keys = {datetime.date(2026, 1, 2): 1, Colour.RED: 2, uuid.UUID(int=7): 3, 4: 4, None: 5}
    graph = graph_with({'leaves': leaves, 'keys': keys})
    assert written(graph, tmp_path) == orjson_text(graph)


def test_numeric_node_ids_are_sorted_and_written(tmp_path: Path) -> None:
    graph = Graph.from_dict(
        {'nodes': [{'id': 2, 'kind': 'model', 'name': 'b'}, {'id': 1, 'kind': 'model', 'name': 'a'}]}
    )
    text = written(graph, tmp_path).decode()
    assert text.index('"id": 1') < text.index('"id": 2')
    assert [n['id'] for n in graph.to_dict()['nodes']] == [1, 2]
