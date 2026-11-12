"""The core's YAML reader against PyYAML's safe loader, which it replaced: the same value, down to
its type (a bool is not an int, a date is not a string), and the same answer to whether a text can
be read, over every YAML file in the repository, the YAML 1.1 type examples, and documents
Hypothesis writes and damages. A text PyYAML refuses at the scanner, the parser or the composer is
refused by the same rule at the same place, with the same words."""

from __future__ import annotations

import datetime as dt
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from konstruct import _core

ROOT = Path(__file__).resolve().parents[1]
# More examples find more: KONSTRUCT_YAML_EXAMPLES=100000 uv run pytest tests/test_yaml_differential.py
EXAMPLES = int(os.environ.get('KONSTRUCT_YAML_EXAMPLES', '300'))
_STAGES = {
    'scanner': 'ScannerError',
    'parser': 'ParserError',
    'composer': 'ComposerError',
}
# What the core refuses that PyYAML, unguarded, builds: a limit, and a surrogate no pair completes.
_GUARDED = re.compile(r'a lone surrogate|an integer of more than')


def pyyaml(source: str | bytes) -> tuple[str, Any]:
    """What PyYAML's own safe loader (the pure Python one) makes of ``source``."""
    try:
        return 'value', yaml.load(source, Loader=yaml.SafeLoader)
    except yaml.YAMLError as error:
        return 'error', error
    except (ValueError, OverflowError, LookupError, AttributeError, TypeError, RecursionError) as error:
        return 'error', error


def core(source: str | bytes, **options: Any) -> tuple[str, Any]:
    try:
        return 'value', _core.yaml_load(source, **options)
    except _core.YamlFailure as failure:
        return 'error', failure


def paired(value: Any) -> Any:
    """``value`` as the core reads an escaped surrogate pair (``"\\ud83d\\udce6"``, as JSON writes a
    character past the Basic Multilingual Plane): the character, which PyYAML leaves as two halves."""
    if isinstance(value, str):
        try:
            return value.encode('utf-16', 'surrogatepass').decode('utf-16')
        except UnicodeDecodeError:
            return value
    if isinstance(value, dict):
        return {paired(key): paired(item) for key, item in value.items()}
    if isinstance(value, list):
        return [paired(item) for item in value]
    if isinstance(value, tuple):
        return tuple(paired(item) for item in value)
    if isinstance(value, set):
        return {paired(item) for item in value}
    return value


def difference(a: Any, b: Any, where: str = '$') -> str | None:
    """Where two values differ, comparing types as well as values; None when they are the same."""
    if type(a) is not type(b):
        return f'{where}: {type(a).__name__} {a!r} is not {type(b).__name__} {b!r}'
    if isinstance(a, float):
        if math.isnan(a) and math.isnan(b):
            return None
        same = a == b and math.copysign(1, a) == math.copysign(1, b)
        return None if same else f'{where}: {a!r} is not {b!r}'
    if isinstance(a, dict):
        if len(a) != len(b):
            return f'{where}: keys {list(a)!r} are not {list(b)!r}'
        for (key, item), (other_key, other_item) in zip(a.items(), b.items(), strict=True):
            found = difference(key, other_key, f'{where} key') or difference(
                item, other_item, f'{where}[{key!r}]'
            )
            if found:
                return found
        return None
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return f'{where}: {len(a)} items are not {len(b)}'
        for at, (x, y) in enumerate(zip(a, b, strict=True)):
            found = difference(x, y, f'{where}[{at}]')
            if found:
                return found
        return None
    if isinstance(a, dt.datetime):
        same = a == b and a.utcoffset() == b.utcoffset() and a.microsecond == b.microsecond
        return None if same else f'{where}: {a!r} is not {b!r}'
    return None if a == b else f'{where}: {a!r} is not {b!r}'


def mark(place: yaml.Mark | None) -> tuple[int, int, int] | None:
    return None if place is None else (place.index, place.line, place.column)


# What PyYAML raises that is not its own error: the constructors' ValueError, KeyError and the rest,
# and the scanner's chr() of a code past Unicode, which the core words as a problem of its own.
_OTHER = (ValueError, OverflowError, LookupError, AttributeError, TypeError, RecursionError)


def assert_same_reading(source: str | bytes) -> None:
    """The core reads ``source`` as PyYAML does, or refuses it as PyYAML does: at the same stage,
    and where PyYAML's error is its own, at the same mark and in the same words."""
    expected_kind, expected = pyyaml(source)
    kind, got = core(source)
    if kind == 'error' and (got.args[0] == 'limit' or _GUARDED.search(got.args[3])):
        return
    assert kind == expected_kind, (kind, got.args if kind == 'error' else got, expected)
    if kind == 'value':
        expected = paired(expected)
        assert difference(got, expected) is None, difference(got, expected)
        return
    stage, context, context_mark, problem, problem_mark = got.args
    if stage == 'reader':
        assert isinstance(expected, yaml.reader.ReaderError), expected
        assert problem == str(expected).splitlines()[0]
    elif stage in _STAGES:
        assert type(expected).__name__ == _STAGES[stage], (stage, expected)
    elif stage == 'constructor':
        # The first fault in the order PyYAML's constructors visit the nodes: a collection is made
        # first and filled in the next round.
        assert isinstance(expected, (yaml.constructor.ConstructorError, *_OTHER)), (stage, expected)
    else:
        assert stage == 'unreadable' and isinstance(expected, _OTHER), (stage, expected)
    if isinstance(expected, yaml.MarkedYAMLError):
        assert (problem, context) == (expected.problem, expected.context)
        assert problem_mark == mark(expected.problem_mark)
        assert context_mark == mark(expected.context_mark)


def repository_files() -> list[Path]:
    """The YAML files of the repository: the tracked ones, or every one under it when it is not a
    git checkout."""
    try:
        tracked = subprocess.run(
            ['git', 'ls-files', '*.yml', '*.yaml'], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.split()
        return [ROOT / name for name in tracked if (ROOT / name).is_file()]
    except (OSError, subprocess.CalledProcessError):
        skipped = {'.git', '.venv', 'node_modules', 'target', '_build'}
        return sorted(
            p for p in ROOT.rglob('*') if p.suffix in {'.yml', '.yaml'} and not skipped & set(p.parts)
        )


FILES = repository_files()


@pytest.mark.parametrize('path', FILES, ids=[str(p.relative_to(ROOT)) for p in FILES])
def test_every_yaml_file_of_the_repository_reads_as_it_did(path: Path) -> None:
    assert_same_reading(path.read_bytes())


# The type examples of the YAML 1.1 working draft (yaml.org/type), and the readings PyYAML makes
# of the rest of what its resolver and constructors recognise.
TYPES = [
    # bool
    'a: [yes, Yes, YES, no, No, NO, true, True, TRUE, false, False, FALSE, on, On, ON, off, Off, OFF]',
    'a: !<tag:%ff> b',
    'a: !<tag:%e2%82> b',
    'a: !<tag:%e2%82%41> b',
    '%YAML 1.1\n--- a',
    '%YAML 1.' + '0' * 5000 + '1\n--- a',
    'a: [y, n, Y, N, yEs, tRUE, oN, "yes", \'no\', "true"]',
    'a: [~, null, Null, NULL, "", "~", "null"]\nb:\nc: ~\nd: !!null foo',
    # int
    'a: [0, 1, -1, +1, 685230, +685_230, 02472256, 0x_0A_74_AE, 0b1010_0111_0100_1010_1110, 190:20:30]',
    'a: [0_, 017, 08, 0b_, 0x, -0x1f, +0b11, 1_000, 1_0_0, 1:30, 1:60, 0:30, 60:1:2]',
    'a: [!!int 5, !!int "0x1F", !!int "1_000", !!int "-0b11", !!int " 5", !!int 1:30]',
    # float
    'a: [6.8523015e+5, 685.230_15e+03, 685_230.15, 190:20:30.15, -.inf, .inf, .NaN, +.INF, .nan]',
    'a: [1., .5, -1.5, 1.e+5, 1e5, 1.5e5, 1_0.5, 0.0, -0.0, 1:30.5, 1e400, 1.0e+400]',
    'a: [!!float 1, !!float "1e5", !!float .5, !!float "1_0", !!float -.inf, !!float 1:30]',
    # timestamp
    'a: [2001-12-14t21:59:43.10-05:00, 2001-12-14 21:59:43.10 -5, 2001-12-15 2:59:43.10, 2002-12-14]',
    'a: [2001-12-14T21:59:43Z, 2001-12-14 21:59:43 +5:30, 2001-1-1, 2001-12-14 21:59:43.1234567, 2001-12-14 1:02:03.]',
    'a: !!timestamp 2001-12-14\nb: !!timestamp "2001-1-1"\nc: !!str 2001-12-14',
    # binary
    'a: !!binary R0lGODlhDAAMAIQAAP//9/X17unp5WZmZgAAAOfn515eXvPz7Y6OjuDg4J+fn5OTk6enp56enmleECcgggoBADs=',
    'a: !!binary |\n  R0lGODlhDAAMAIQAAP//9/X17unp5WZmZgAAAOfn515eXvPz7Y6OjuDg4J+fn5OTk6enp56enmleECcgggoBADs=\n',
    # merge
    'base: &b {x: 1, y: 2}\nother: &o {y: 3, z: 4}\nm: {<<: *b, y: 9}\nn: {<<: [*b, *o]}\nl:\n  <<: [*o, *b]\n  w: 0',
    'a: &a {k: 1}\nb: &b {<<: *a, j: 2}\nc: {<<: *b}\nd: {"<<": 1, <<: *a}',
    'a: {<<: [{<<: 1}, 5]}',
    'a: {<<: [{<<: {a: 1}}, {<<: [{b: 2}, 7]}, 5]}',
    'a: {<<: {<<: {<<: 1}}, <<: 2}',
    'a: {<<: {<<: {x: 1}}, <<: {<<: {y: 2}}, z: 3}',
    'a: {<<: 1}',
    'a: {<<: [1]}',
    'a: [<<]',
    # omap, pairs, set
    'a: !!omap [a: 1, b: 2, c: 3]\nb: !!pairs [a: 1, a: 2]\nc: !!set {x, y, z: }',
    'a: !!omap [{a: 1, b: 2}]',
    'a: !!omap [1]',
    'a: !!set [x]',
    # value, seq, map, str
    'a: =\nb: {=: 1}',
    'a: ! "*"\nb: ! "&"',
    'a: ! "!"',
    'a: !!float 1:x:y',
    'a: "\\ud83d\\\n\n  \\udce6"',
    'a: "\\ud83d\\\n  \\udce6"',
    'a: !!seq [1, 2]\nb: !!map {c: 1}\nc: !!str [1]\nd: !!seq 1',
    'a: !!str 12\nb: !!str\nc: !!str ~\nd: ! 12\ne: ! "12"\nf: !foo 1',
    # keys that are not strings, repeated keys
    '1: a\n2.5: b\ntrue: c\n~: d\n2001-01-01: e\n? [1, 2]\n: f',
    '{a: 1, a: 2, b: 1, 1: 1, true: 2, 1.0: 3}',
    'a: &k x\n*k : 1',
    # documents
    '',
    '# only a comment\n',
    '---\n',
    '--- a\n--- b\n',
    '%YAML 1.1\n---\na: 1\n...\n',
    '%YAML 2.0\n---\na: 1\n',
    '%TAG !e! tag:example.com,2000:\n--- !e!foo bar\n',
    '--- !<tag:yaml.org,2002:int> 12\n',
    '--- !e!foo bar\n',
    # quoting
    'a: \'it\'\'s\'\nb: "tab\\there\\n\\x41\\u00e9\\U0001F4E6\\ \\/\\_\\N\\L\\P\\e\\0"\nc: "folded\n  over\n\n  lines"',
    'a: "\\ud83d\\udce6"\nb: "\\ud83d x"\nc: "\\U0001F4E6"',
    # block scalars
    'a: |\n  line 1\n  line 2\nb: >\n  folded\n  text\n\n  more\nc: |-\n  strip\nd: |+\n  keep\n\ne: >2\n    indented\n',
    'a: |\n  text\n\n\nb: 1\n',
    # anchors and tags
    'a: &x [1, 2]\nb: *x\nc: &y !!str 5\nd: !!str &z 6\ne: *z',
    'a: &x 1\nb: &x 2',
    'a: *nothing',
    # block structure
    '- a\n- - b\n  - c\n- ? d\n  : e\n-\n- - \n',
    'a:\n- 1\n- 2\nb:\n  - 3\n',
    'a:\n  b:\n    c:\n      - d\n      -   e: 1\n          f: 2\n',
    '? a\n? b\n: c\n',
    'a: b: c',
    'a: [1,\n2]\nb: "x\ny"\nc: \'p\nq\'',
    '[a, b: c, {d: e}, ? f : g]',
    '{? a, b: , : c}',
    '\ta: 1',
    'a:\t1',
    'a: 1\n\tb: 2',
    # line breaks of YAML 1.1
    'a: b\u2028c: d',
    'a: b\x85c: d',
    'a: b\rc: d\r\ne: f',
    'a: "x\u2028y"\nb: |\n  p\u2029  q\n',
    # the byte order mark
    '\ufeffa: 1',
    # characters
    'a: caf\u00e9 \u65e5\u672c \N{PACKAGE}',
    'a: \u00e9\nb: "\\u00e9"',
]


@pytest.mark.parametrize('text', TYPES, ids=[f'{at}' for at in range(len(TYPES))])
def test_the_yaml_1_1_types_read_as_they_did(text: str) -> None:
    assert_same_reading(text)
    assert_same_reading(text.encode())


def test_a_byte_order_mark_and_utf16_read_as_they_did() -> None:
    text = 'name: caf\u00e9\nb: [1, 2]\n'
    for encoded in (
        b'\xef\xbb\xbf' + text.encode(),
        b'\xff\xfe' + text.encode('utf-16-le'),
        b'\xfe\xff' + text.encode('utf-16-be'),
        text.encode(),
        b'a: caf\xe9\n',
        b'\xff\xfea\x00',
        b'\xff\xfe\x00\xd8',
        b'\x00',
        b'a: \x07',
        b'a: \x7f',
        b'a: \xc2\x86',
        b'a: \xef\xbf\xbe',
    ):
        kind, _ = core(encoded)
        expected_kind, _ = pyyaml(encoded)
        assert kind == expected_kind, encoded


# Documents in the shape of the ones people write, and the shapes that go wrong.
WORDS = [
    'a',
    'name',
    'x',
    'description',
    'tests',
    'columns',
    'k1',
    'caf\u00e9',
    'yes',
    'No',
    'on',
    'true',
    'null',
    '~',
    '<<',
    '=',
    '0',
    '12',
    '-3',
    '+4',
    '0x1f',
    '0b101',
    '017',
    '08',
    '1_000',
    '1:30',
    '190:20:30',
    '1.5',
    '.5',
    '-.inf',
    '.NaN',
    '1e5',
    '1.e+5',
    '2026-11-12',
    '2026-11-12 10:00:00',
    '2026-11-12T10:00:00.5Z',
    '2026-11-12 10:00:00 +5:30',
    '2026-13-45',
    'a b',
    'a:b',
    'a #b',
    "it's",
    'say "hi"',
    '-',
    '?',
    ':',
    'a: b',
    '---',
    '...',
    '- a',
    '[a',
    '}',
    '%x',
    '@x',
    '`x',
    '|',
    '>',
    '*x',
    '&x',
    '!x',
    'foo\\bar',
    '\u2028',
    '\x85',
]
QUOTED = st.sampled_from(WORDS).map(lambda w: '"' + w.replace('\\', '\\\\').replace('"', '\\"') + '"')
SINGLE = st.sampled_from(WORDS).map(lambda w: "'" + w.replace("'", "''") + "'")
ESCAPED = st.sampled_from(
    [
        '"\\n"',
        '"\\t\\x41"',
        '"\\u00e9"',
        '"\\U0001F4E6"',
        '"\\ud83d\\udce6"',
        '"\\q"',
        '"\\x4"',
        '"a\\\n  b"',
        '"\\ "',
    ]
)
TAGS = st.sampled_from(
    [
        *[
            '!!str',
            '!!int',
            '!!float',
            '!!bool',
            '!!null',
            '!!timestamp',
            '!!binary',
            '!!set',
            '!!omap',
            '!!pairs',
        ],
        *['!!seq', '!!map', '!', '!foo', '!!foo', '!<tag:yaml.org,2002:str>', '!e!x'],
    ]
)
ANCHORS = st.sampled_from(['&a', '&b', '&c'])
ALIASES = st.sampled_from(['*a', '*b', '*c', '*d'])
SCALARS = st.one_of(
    st.sampled_from(WORDS),
    QUOTED,
    SINGLE,
    ESCAPED,
    st.text(alphabet='ab :#-?[]{},&*!|>\'"%@`\\\t\u00e9\u2028', min_size=0, max_size=8),
)
PROPERTIES = st.one_of(
    st.just(''),
    st.just(''),
    st.just(''),
    TAGS.map(lambda t: t + ' '),
    ANCHORS.map(lambda a: a + ' '),
    st.tuples(TAGS, ANCHORS).map(lambda ta: f'{ta[0]} {ta[1]} '),
    st.tuples(ANCHORS, TAGS).map(lambda at: f'{at[0]} {at[1]} '),
)
FLOW = st.recursive(
    st.one_of(SCALARS, ALIASES),
    lambda inner: st.one_of(
        st.lists(st.tuples(PROPERTIES, inner), max_size=4).map(
            lambda items: '[' + ', '.join(p + v for p, v in items) + ']'
        ),
        st.lists(st.tuples(inner, inner), max_size=4).map(
            lambda pairs: '{' + ', '.join(f'{k}: {v}' for k, v in pairs) + '}'
        ),
        st.lists(inner, max_size=3).map(lambda items: '{' + ', '.join(items) + '}'),
        st.lists(st.tuples(inner, inner), max_size=3).map(
            lambda pairs: '[' + ', '.join(f'{k}: {v}' for k, v in pairs) + ']'
        ),
    ),
    max_leaves=8,
)
VALUES = st.one_of(
    SCALARS,
    FLOW,
    ALIASES,
    st.tuples(PROPERTIES, SCALARS).map(lambda pv: pv[0] + pv[1]),
    st.sampled_from(['|', '>', '|-', '>+', '|2', '>-1', '| # c', '|x']),
)
INDENTS = st.sampled_from(['', '', '', '  ', '  ', '    ', ' ', '   ', '\t', '      '])
PREFIXES = st.sampled_from(['', '', '', '- ', '- ', '? ', '- - ', '-  ', ': ', '-'])
COMMENTS = st.sampled_from(['', '', '', '', ' # comment', '#c'])


@st.composite
def line(draw: st.DrawFn) -> str:
    indent = draw(INDENTS)
    prefix = draw(PREFIXES)
    shape = draw(st.integers(0, 6))
    value = draw(VALUES)
    key = draw(SCALARS)
    if shape <= 2:
        body = f'{draw(PROPERTIES)}{key}: {value}'
    elif shape == 3:
        body = f'{key}:'
    elif shape == 4:
        body = value
    elif shape == 5:
        body = draw(
            st.sampled_from(['---', '--- a', '...', '%YAML 1.1', '%TAG !e! tag:x,2000:', '--- >', ''])
        )
        return body
    else:
        body = f'{draw(PROPERTIES)}{key}'
    return indent + prefix + body + draw(COMMENTS)


@st.composite
def documents(draw: st.DrawFn) -> str:
    lines = draw(st.lists(line(), min_size=1, max_size=10))
    ending = draw(st.sampled_from(['\n', '\n', '\r\n', '', '\n\n', '\r']))
    return draw(st.sampled_from(['\n', '\n', '\r\n'])).join(lines) + ending


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=list(HealthCheck))
@given(documents())
def test_documents_read_as_they_did(text: str) -> None:
    assert_same_reading(text)


@st.composite
def damaged(draw: st.DrawFn) -> str:
    """A repository file with a few characters inserted, deleted or moved."""
    text = draw(st.sampled_from(CORPUS))
    for _ in range(draw(st.integers(1, 3))):
        at = draw(st.integers(0, len(text)))
        kind = draw(st.integers(0, 2))
        if kind == 0:
            text = text[:at] + draw(st.sampled_from(PIECES)) + text[at:]
        elif kind == 1:
            text = text[:at] + text[at + draw(st.integers(1, 3)) :]
        else:
            other = draw(st.integers(0, len(text)))
            low, high = sorted((at, other))
            text = text[:low] + text[high:] + text[low:high]
    return text


PIECES = [*list('\n :-[]{},#&*!|>\'"%?@`=~\t\\abxyz019.eE+_'), '<<', '- ', ': ', '---\n', '...\n']
PIECES += ['!!str ', '!!int ', '&a ', '*a', '\u00e9', '\u65e5', '\x85', '\u2028', '\ufeff', 'yes', 'null']
PIECES += ['0x1f', '1:30', '2001-01-01', '"\\u00e9"', '\\ud83d\\udce6', '\r\n', '\r']
CORPUS = [
    text
    for text in (p.read_text(errors='replace') for p in FILES if p.stat().st_size < 3000)
    if '\x00' not in text
] + [t for t in TYPES if len(t) < 3000]


@settings(max_examples=2 * EXAMPLES, deadline=None, suppress_health_check=list(HealthCheck))
@given(damaged())
def test_damaged_files_read_or_fail_as_they_did(text: str) -> None:
    assert_same_reading(text)


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=list(HealthCheck))
@given(st.text(alphabet=st.characters(codec='utf-8'), max_size=80))
def test_any_text_reads_as_it_did(text: str) -> None:
    assert_same_reading(text)


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=list(HealthCheck))
@given(st.binary(max_size=80))
def test_any_bytes_read_as_they_did(data: bytes) -> None:
    assert_same_reading(data)
    assert_same_reading(b'\xff\xfe' + data)
    assert_same_reading(b'\xef\xbb\xbf' + data)


class LineLoader(yaml.SafeLoader):
    """``konstruct.semantics.definitions._LineLoader`` as main has it: the oracle of ``lines=True``."""


def _mapping_with_line(loader: LineLoader, node: yaml.MappingNode) -> dict[str, Any]:
    mapping = {str(k): v for k, v in loader.construct_mapping(node, deep=True).items()}
    mapping['__line__'] = node.start_mark.line + 1
    return mapping


LineLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_with_line)


def assert_same_lines(source: str | bytes) -> None:
    try:
        expected: tuple[str, Any] = ('value', yaml.load(source, Loader=LineLoader))
    except yaml.YAMLError as error:
        expected = ('error', error)
    except _OTHER as error:
        expected = ('error', error)
    kind, got = core(source, lines=True)
    if kind == 'error' and (got.args[0] == 'limit' or _GUARDED.search(got.args[3])):
        return
    assert kind == expected[0], (kind, got.args if kind == 'error' else got, expected[1])
    if kind == 'value':
        assert difference(got, paired(expected[1])) is None, difference(got, paired(expected[1]))
    elif isinstance(expected[1], yaml.constructor.ConstructorError):
        assert got.args[4] == mark(expected[1].problem_mark)


@pytest.mark.parametrize('path', FILES, ids=[str(p.relative_to(ROOT)) for p in FILES])
def test_lines_read_as_the_line_loader_read_them(path: Path) -> None:
    assert_same_lines(path.read_bytes())


@pytest.mark.parametrize('text', TYPES, ids=[f'{at}' for at in range(len(TYPES))])
def test_lines_of_the_yaml_1_1_types(text: str) -> None:
    assert_same_lines(text)


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=list(HealthCheck))
@given(documents())
def test_lines_of_documents(text: str) -> None:
    assert_same_lines(text)
