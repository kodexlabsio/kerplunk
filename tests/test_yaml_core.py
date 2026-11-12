"""The core's YAML reader, beyond what PyYAML did: the limits that make a file safe to read, the
reading of a stream of documents with one budget, aliases that are one object, and reading from
several threads at once."""

from __future__ import annotations

import contextlib
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from konstruct import _core
from konstruct.core import yamlio


def failure(text: str | bytes, **options: Any) -> tuple[str, str | None, str]:
    with pytest.raises(_core.YamlFailure) as raised:
        _core.yaml_load(text, **options)
    stage, context, _, problem, _ = raised.value.args
    return stage, context, problem


def test_an_alias_is_the_object_its_anchor_made() -> None:
    value = yamlio.load('a: &x [1, 2]\nb: *x\nc: {d: *x}\n')
    assert value['a'] is value['b'] is value['c']['d']
    assert yamlio.load('a: &n .nan\nb: .nan\n')['a'] is not None
    keys = yamlio.load('.nan: 1\n.NaN: 2\n')
    assert len(keys) == 1


def test_lines_gives_every_mapping_string_keys_and_its_line() -> None:
    text = 'version: 1\nterms:\n  - name: A\n    n: 1\n  - {name: B}\n1: x\n~: y\n'
    value = yamlio.load(text, lines=True)
    assert value == {
        'version': 1,
        'terms': [{'name': 'A', 'n': 1, '__line__': 3}, {'name': 'B', '__line__': 5}],
        '1': 'x',
        'None': 'y',
        '__line__': 1,
    }
    assert yamlio.load('- a: 1\n', lines=True) == [{'a': 1, '__line__': 1}]
    # A repeated key is one key whatever its type: 1 and true are the same key, the first one's.
    assert yamlio.load('1: x\ntrue: y\n', lines=True) == {'1': 'y', '__line__': 1}


def test_only_text_is_read() -> None:
    with pytest.raises(TypeError):
        _core.yaml_load(12)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _core.yaml_load(None)  # type: ignore[arg-type]


def test_nesting_is_refused_past_the_depth_given() -> None:
    assert _core.yaml_load('[' * 250 + ']' * 250) is not None
    assert failure('[' * 251 + ']' * 251)[2] == 'nested too deeply to read'
    assert _core.yaml_load('[[[x]]]', max_depth=3) == [[['x']]]
    assert failure('[[[x]]]', max_depth=2) == ('limit', None, 'nested too deeply to read')
    # Aliases carry their depth with them: ten short anchors stand for eleven levels.
    chain = 'a0: &a0 [x]\n' + ''.join(f'a{n}: &a{n} [*a{n - 1}]\n' for n in range(1, 10))
    assert _core.yaml_load(chain, max_depth=11) is not None
    assert failure(chain, max_depth=9)[2] == 'nested too deeply to read'


def test_the_digit_limit_is_the_one_given() -> None:
    assert _core.yaml_load('n: ' + '9' * 40, digits=40) == {'n': int('9' * 40)}
    assert failure('n: ' + '9' * 41, digits=40) == ('constructor', None, 'an integer of more than 40 digits')
    assert failure('n: 0x' + 'f' * 41, digits=40)[0] == 'constructor'
    assert failure('n: 1' + ':59' * 20, digits=40)[0] == 'constructor'
    assert failure('n: !!int ' + '1' * 41, digits=40)[0] == 'constructor'


def test_a_stream_is_read_document_by_document() -> None:
    text = 'a: 1\n---\n---\nb: [1, 2]\n---\n- x\n...\n---\n'
    assert _core.yaml_load_all(text) == [{'a': 1}, {'b': [1, 2]}, ['x']]
    assert _core.yaml_load_all('') == []
    assert _core.yaml_load_all('# nothing\n') == []
    with pytest.raises(_core.YamlFailure):
        _core.yaml_load_all('a: 1\n---\nb: [\n')


def test_a_stream_has_one_alias_budget_for_all_its_documents() -> None:
    document = 's: &s [a, b, c, d, e, f, g, h]\nt: [*s, *s, *s]\n'
    stream = '---\n'.join([document] * 3)
    assert len(_core.yaml_load_all(stream)) == 3
    # Each document adds 27 values: three of them are over a budget of 70, each alone is not.
    assert len(_core.yaml_load_all(stream, max_values=81)) == 3
    with pytest.raises(_core.YamlFailure) as raised:
        _core.yaml_load_all(stream, max_values=70)
    assert raised.value.args[3] == 'the aliases of the documents stand for more than 70 values together'
    for single in (document, document, document):
        assert _core.yaml_load(single, max_values=70) is not None
    assert failure(document, max_values=26)[2] == 'its aliases stand for more than 26 values'


def test_many_threads_read_at_once() -> None:
    texts = [f'name: m{n}\ncolumns:\n' + ''.join(f'  - name: c{c}\n' for c in range(200)) for n in range(16)]
    results: list[Any] = [None] * len(texts)

    def read(at: int) -> None:
        for _ in range(20):
            results[at] = _core.yaml_load(texts[at])

    threads = [threading.Thread(target=read, args=(at,)) for at in range(len(texts))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [r['name'] for r in results] == [f'm{n}' for n in range(16)]
    assert all(len(r['columns']) == 200 for r in results)


def test_text_with_a_surrogate_is_refused_where_it_is() -> None:
    with pytest.raises(_core.YamlFailure) as raised:
        _core.yaml_load('a: 1\nb: \ud800\n')
    stage, _, _, problem, mark = raised.value.args
    assert stage == 'reader'
    assert problem == 'unacceptable character #xd800: special characters are not allowed'
    assert mark == (8, 1, 3)


def seconds(text: str, **options: Any) -> float:
    best = float('inf')
    for _ in range(3):
        began = time.perf_counter()
        with contextlib.suppress(_core.YamlFailure):
            _core.yaml_load(text, **options)
        best = min(best, time.perf_counter() - began)
    return best


def sized(make: Callable[[int], str], megabytes: int) -> str:
    """``make`` of a size that gives about ``megabytes`` MB."""
    probe = make(1000)
    return make(max(1, megabytes * 1_000_000 * 1000 // max(1, len(probe))))


# Text that makes a reader slow when some step of it looks again at what it has read: each is read
# at 1 MB and at 4 MB, and four times the text must not take much more than four times as long.
GROWING: dict[str, Callable[[int], str]] = {
    'plain': lambda n: 'a: ' + 'word ' * (n // 5) + '\n',
    'flow': lambda n: '[' + ', '.join(['x'] * (n // 3)) + ']',
    'flow mappings': lambda n: '{' + ', '.join(f'k{i}: v' for i in range(n // 8)) + '}',
    'keys': lambda n: ''.join(f'k{i}: {i}\n' for i in range(n // 9)),
    'quoted': lambda n: 'a: "' + 'word\\n ' * (n // 7) + '"\n',
    'tags': lambda n: 'a: !' + 'x' * n + ' y\n',
    'tag handles': lambda n: 'a: !' + 'x' * n + '!y z\n',
    'verbatim tags': lambda n: 'a: !<' + 'x' * n + '> y\n',
    'tag directives': lambda n: ''.join(f'%TAG !h{i}! tag:x{i}:\n' for i in range(n // 22)) + '--- a\n',
    'anchors': lambda n: 'a: &' + 'x' * (n // 2) + ' [*' + 'x' * (n // 2) + ']\n',
    'comments': lambda n: 'a: 1 # ' + 'x' * n + '\n',
    'escapes': lambda n: 'a: "' + '\\u00e9' * (n // 6) + '"\n',
    'block|9': lambda n: 'a: |9\n' + '         line\n' * (n // 14),
    'block': lambda n: 'a: |\n' + '  line\n' * (n // 7),
    'merge sequences': lambda n: 't: {<<: [' + ', '.join(['{<<: {}}'] * (n // 10)) + ']}\n',
    'merge mappings': lambda n: 't: {' + ', '.join(['<<: {<<: {}}'] * (n // 14)) + '}\n',
    'merge keys': lambda n: 'b: &b {x: 1}\n' + ''.join(f'k{i}: {{<<: *b}}\n' for i in range(n // 14)),
    'sexagesimal floats': lambda n: 'a: !!float 1' + ':59' * (n // 3) + '.5\n',
    'blank lines': lambda n: 'a: 1\n' + '\n' * n + 'b: 2\n',
    'mixed CR and CRLF': lambda n: 'a: 1\r' + 'k: [1,\r\n 2]\n\r' * (n // 14),
    'documents': lambda n: '---\na: 1\n' * (n // 9),
    'anchors and aliases': lambda n: ''.join(f'k{i}: &a{i} {i}\n' for i in range(n // 14)),
}


@pytest.mark.parametrize('name', GROWING)
@pytest.mark.parametrize('lines', [False, True])
def test_four_times_the_text_takes_about_four_times_as_long(name: str, lines: bool) -> None:
    make = GROWING[name]
    read = _core.yaml_load_all if name == 'documents' else _core.yaml_load
    small, large = sized(make, 1), sized(make, 4)

    def best(text: str) -> float:
        times = []
        for _ in range(5):
            began = time.perf_counter()
            with contextlib.suppress(_core.YamlFailure):
                read(text, lines=lines)
            times.append(time.perf_counter() - began)
        return min(times)

    first, second = best(small), best(large)
    # Linear is 4; quadratic is 16. The floor keeps a fast reading from being all noise.
    assert second < 10 * max(first, 0.005), (name, first, second)


def test_a_merge_chain_is_flattened_once_for_every_mapping_that_merges_it() -> None:
    chain = 'm0: &m0 {k0: 0}\n' + ''.join(
        f'm{n}: &m{n} {{<<: *m{n - 1}, k{n}: {n}}}\n' for n in range(1, 200)
    )
    siblings = ''.join(f's{n}: {{<<: *m199, own: {n}}}\n' for n in range(1_000))
    began = time.perf_counter()
    value = _core.yaml_load(chain + siblings)
    assert time.perf_counter() - began < 1
    assert len(value['s999']) == 201
    assert list(value['m3']) == ['k0', 'k1', 'k2', 'k3']


def test_values_are_built_on_a_small_stack() -> None:
    """A line mapping, and merges, are built by recursion in the core, which must not use the
    caller's stack: a thread of 128 KB reads the deepest document the limits allow."""
    texts = [
        '{a: ' * 250 + '1' + '}' * 250,
        '{a: [' * 125 + '1' + ']}' * 125,
        '[' * 250 + ']' * 250,
        'k0: &k0 {x: 1}\n' + ''.join(f'k{n}: &k{n} {{<<: *k{n - 1}}}\n' for n in range(1, 240)),
        '- ' * 120 + 'x',
    ]
    outcomes: list[Any] = []

    def read() -> None:
        for text in texts:
            for lines in (False, True):
                outcomes.append(_core.yaml_load(text, lines=lines))

    threading.stack_size(128 * 1024)
    try:
        thread = threading.Thread(target=read)
        thread.start()
        thread.join()
    finally:
        threading.stack_size(0)
    assert len(outcomes) == 2 * len(texts)
    # The deepest of the line mappings: 250 levels of `a`, then 1.
    deepest = outcomes[1]
    for _ in range(249):
        deepest = deepest['a']
    assert deepest == {'a': 1, '__line__': 1}


def test_load_all_reads_a_stream_with_the_limits_of_a_stream() -> None:
    assert yamlio.load_all('a: 1\n---\n---\nb: [x]\n') == [{'a': 1}, {'b': ['x']}]
    assert yamlio.load_all('') == []
    depth = yamlio.LOAD_ALL_DEPTH
    assert yamlio.load_all('[' * depth + ']' * depth) is not None
    for text in ('[' * (depth + 1) + ']' * (depth + 1), 'a: 1\n---\n' + '[' * 1_000):
        with pytest.raises(yaml.MarkedYAMLError, match='nested too deeply'):
            yamlio.load_all(text)
    # A document is deeper than it is written when an alias carries a deep node into it: sixty
    # short anchors are sixty levels, and sixty more on top of them are past the limit.
    chain = 'a0: &a0 [x]\n' + ''.join(f'a{n}: &a{n} [*a{n - 1}]\n' for n in range(1, 60))
    assert yamlio.load_all(chain) is not None
    deeper = chain + ''.join(f'c{n}: &c{n} [*{"a59" if n == 0 else f"c{n - 1}"}]\n' for n in range(60))
    with pytest.raises(yaml.MarkedYAMLError, match='nested too deeply'):
        yamlio.load_all(deeper)
    assert yamlio.load(deeper) is not None


def test_load_all_has_one_alias_budget_for_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yamlio, 'MAX_VALUES', 100)
    document = 's: &s [' + ', '.join(['x'] * 9) + ']\nt: [*s, *s, *s]\n'
    assert len(yamlio.load_all('---\n'.join([document] * 3))) == 3
    with pytest.raises(yaml.MarkedYAMLError, match='stand for more than 100 values together') as raised:
        yamlio.load_all('---\n'.join([document] * 8))
    assert raised.value.problem_mark is not None
    assert len(yamlio.load(document)) == 2


def test_an_escaped_line_break_does_not_join_the_halves_of_a_pair() -> None:
    assert yamlio.load('a: "\\ud83d\\\n  \\udce6"\n') == {'a': '\N{PACKAGE}'}
    for text in ('a: "\\ud83d\\\n\n  \\udce6"\n', 'a: "\\ud83d\\\n \n \n \\udce6"\n'):
        with pytest.raises(yaml.MarkedYAMLError, match='a lone surrogate'):
            yamlio.load(text)


def test_the_non_specific_tag_gives_a_quoted_yaml_indicator_no_value() -> None:
    for text in ('a: ! "*"', 'a: ! "&"', 'a: ! "!"'):
        with pytest.raises(yaml.constructor.ConstructorError, match='could not determine a constructor'):
            yamlio.load(text)
    assert yamlio.load('a: ! "**"') == {'a': '**'}


def test_a_float_in_base_60_names_its_first_bad_part() -> None:
    with pytest.raises(yaml.constructor.ConstructorError, match="could not convert string to float: 'x'"):
        yamlio.load('a: !!float 1:x:y')


def test_a_uri_escape_that_is_not_utf8_says_what_python_says() -> None:
    for text, said in (
        ('a: !<tag:%ff> b', "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"),
        ('a: !<tag:%e2%82> b', "'utf-8' codec can't decode bytes in position 0-1: unexpected end of data"),
        (
            'a: !<tag:%e2%82%41> b',
            "'utf-8' codec can't decode bytes in position 0-1: invalid continuation byte",
        ),
    ):
        with pytest.raises(yaml.scanner.ScannerError) as raised:
            yamlio.load(text)
        assert raised.value.problem == said


def test_a_yaml_version_of_too_many_digits_is_refused() -> None:
    with pytest.raises(yaml.MarkedYAMLError, match='Exceeds the limit'):
        yamlio.load('%YAML 1.' + '0' * 5000 + '1\n--- a\n')
    assert yamlio.load('%YAML 1.1\n--- a\n') == 'a'


def test_names_in_messages_are_quoted_as_python_quotes_them() -> None:
    """An unassigned code point, which no table in the core could know Python escapes."""
    for text in ('%YAML 1.1\U00014647\n--- a\n', '%YAML 1.1\u00ad\n--- a\n', '%YAML 1.1\u00e9\n--- a\n'):
        with pytest.raises(yaml.scanner.ScannerError) as raised:
            yamlio.load(text)
        with pytest.raises(yaml.scanner.ScannerError) as pyyaml:
            yaml.load(text, yaml.SafeLoader)
        assert raised.value.problem == pyyaml.value.problem


def test_the_loader_a_caller_passes_is_the_safe_loader_or_nothing() -> None:
    assert yamlio.load('a: 1', yaml.SafeLoader) == {'a': 1}
    assert yamlio.load('a: 1', yaml.CSafeLoader) == {'a': 1}
    assert yamlio.load('a: 1', None, lines=True) == {'a': 1, '__line__': 1}
    assert yamlio.load_all('a: 1', yaml.SafeLoader) == [{'a': 1}]

    class Plain(yaml.SafeLoader):
        pass

    class Adds(yaml.SafeLoader):
        pass

    Adds.add_constructor('!x', lambda loader, node: 1)

    class Resolves(yaml.SafeLoader):
        pass

    Resolves.add_implicit_resolver('!x', re.compile('x'), ['x'])

    class Multi(yaml.SafeLoader):
        pass

    Multi.add_multi_constructor('!', lambda loader, suffix, node: 1)

    for loader in (Plain, Adds, Resolves, Multi, yaml.Loader, yaml.UnsafeLoader, dict, 3):
        with pytest.raises(TypeError, match=r'yaml\.SafeLoader'):
            yamlio.load('a: 1', loader)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match='is neither'):
            yamlio.load_all('a: 1', loader)  # type: ignore[arg-type]


def test_a_source_that_is_not_text_is_a_yaml_error(tmp_path: Path) -> None:
    for source in (None, 12, ['a'], bytearray(b'a: 1'), memoryview(b'a: 1')):
        with pytest.raises(yaml.YAMLError, match='str or bytes'):
            yamlio.load(source)  # type: ignore[arg-type]
        with pytest.raises(yaml.YAMLError):
            yamlio.load_all(source)  # type: ignore[arg-type]
    (tmp_path / 'a.yml').write_text('a: 1\n')
    assert yamlio.read(tmp_path / 'a.yml', yaml.SafeLoader) == {'a': 1}


def test_documents_with_merge_keys_and_lines_are_read_without_a_thread_each() -> None:
    """Building the values uses no thread, which would cost about a millisecond a file."""
    text = 'base: &b {x: 1}\nm: {<<: *b, y: 2}\n'
    began = time.perf_counter()
    for _ in range(2000):
        _core.yaml_load(text)
        _core.yaml_load(text, lines=True)
    assert time.perf_counter() - began < 0.5


def test_a_scalar_read_through_equals_keys_needs_no_stack() -> None:
    """``!!str {=: {=: ... x}}`` is the scalar its innermost ``=`` stands for."""
    text = '!!str ' + '{=: ' * 250 + 'x' + '}' * 250
    result: list[Any] = []

    def read() -> None:
        result.append(_core.yaml_load(text))
        result.append(_core.yaml_load(text, lines=True))

    threading.stack_size(128 * 1024)
    try:
        thread = threading.Thread(target=read)
        thread.start()
        thread.join()
    finally:
        threading.stack_size(0)
    assert result == ['x', 'x']
