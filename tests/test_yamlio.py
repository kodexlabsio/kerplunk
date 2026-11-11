"""Every YAML file Konstruct reads goes through one function, so every reader of YAML lists a
problem, and never raises, for any text PyYAML cannot read: inputs the fuzzer found, and the
other ways PyYAML's scanner and constructors fail."""

from __future__ import annotations

import codecs
import json
import sys
from pathlib import Path

import pytest
import yaml

from konstruct.analysis.diff import _yaml_facts, compare_yaml
from konstruct.core import yamlio
from konstruct.engine.build import build
from konstruct.engine.config import _read_target, load_config
from konstruct.inputs import ingestion
from konstruct.inputs.dbt import read_project
from konstruct.interfaces import hook
from konstruct.semantics import definitions, ossie

DIGITS = sys.get_int_max_str_digits() or sys.int_info.default_max_str_digits
# Aliases to aliases: 7 levels of 9 stand for 5.3 million values in a few hundred bytes.
ALIASES = ''.join(
    f'{level}: &{level} [' + ', '.join([f'*{chr(ord(level) - 1)}' if level > 'a' else 'x'] * 9) + ']\n'
    for level in 'abcdefg'
)
HOSTILE = {
    # The fuzzer's input: chr() of a code past a C int raises OverflowError in the scanner.
    'fuzzed escape': '"\\Uc6666666666666666666666666666666666666666666666666666664-****',
    'escape past a C int': 'name: "\\UFFFFFFFF"\n',
    # chr() of a code past Unicode raises ValueError.
    'escape past Unicode': 'name: "\\U00110000"\n',
    # A lone surrogate written as an escape, which the reader refuses when it is written as is.
    'escaped surrogate': 'name: "\\ud800"\n',
    # The constructors raise ValueError, KeyError, AttributeError and IndexError on these.
    'impossible date': 'name: x\napproved: 2026-13-01\n',
    'not a bool': 'name: x\ndefinition: !!bool maybe\n',
    'not a timestamp': 'name: !!timestamp foo\n',
    'empty int': 'name: !!int ""\n',
    'empty float': 'name: !!float ""\n',
    'not base64': 'name: !!binary a\n',
    # Integers whose decimal digits str() refuses, written in bases int() reads in linear time,
    # and a sexagesimal one, which PyYAML computes in quadratic time.
    'long hex': 'name: 0x' + 'f' * (DIGITS - 10) + '\n',
    'long binary': 'name: 0b' + '1' * (DIGITS + 1) + '\n',
    'long sexagesimal': 'name: 1' + ':59' * DIGITS + '\n',
    'long decimal': 'name: 1' + '0' * DIGITS + '\n',
    'nested too deeply': 'name: ' + '[' * 5000 + ']' * 5000 + '\n',
    'aliases without end': ALIASES + 'name: *g\n',
}
CASES = list(HOSTILE.values())
IDS = list(HOSTILE)
# Bytes that are not UTF-8, which ``read_text`` refuses with a UnicodeDecodeError.
NOT_UTF8 = b'name: caf\xe9\n'


def schema(models: int, columns: int) -> str:
    """A dbt schema file with no aliases: one test on every column."""
    lines = ['version: 2', 'models:']
    for m in range(models):
        lines += [f'  - name: model_{m}', '    columns:']
        for c in range(columns):
            lines += [f'      - name: column_{c}', '        data_tests: [not_null]']
    return '\n'.join(lines) + '\n'


@pytest.mark.parametrize('text', CASES, ids=IDS)
def test_load_raises_only_a_yaml_error(text: str) -> None:
    with pytest.raises(yaml.YAMLError):
        yamlio.load(text)


def test_load_reads_what_safe_load_reads() -> None:
    text = 'a: [1, 0x1f, 0b101, 1:30]\nb: &x {c: 2026-11-11, d: !!binary aGk=}\nd: *x\ne: "\\u00e9"\n'
    assert yamlio.load('') is None
    assert yamlio.load(text) == yaml.safe_load(text)
    assert yamlio.load(f'{ALIASES[: ALIASES.index("c:")]}z: *b\n')['z'][0] == ['x'] * 9
    assert yamlio.load('n: 1' + '0' * (DIGITS - 1)) == {'n': 10 ** (DIGITS - 1)}


def test_a_large_file_without_aliases_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limit is on what aliases add: a file of many values written out is read whole."""
    monkeypatch.setattr(yamlio, 'MAX_VALUES', 1_000)
    text = schema(40, 30)
    assert len(yamlio.load(text)['models']) == 40
    facts = _yaml_facts(text)
    assert len(facts['tests']) == 40 * 30
    change = compare_yaml('schema.yml', text, schema(41, 30))
    assert change.tests_removed == [] and len(change.tests_added) == 30


def test_aliases_are_refused_for_what_they_add(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yamlio, 'MAX_VALUES', 1_000)
    shared = 'shared: &s [' + ', '.join(['x'] * 99) + ']\n'
    # Ten copies of a list of 100 values add 1,000 values; eleven add more than the limit.
    assert len(yamlio.load(shared + ''.join(f'c{n}: *s\n' for n in range(10)))) == 11
    with pytest.raises(yaml.MarkedYAMLError) as raised:
        yamlio.load(shared + 'small: [1, 2]\n' + 'many: [' + ', '.join(['*s'] * 11) + ']\n')
    assert raised.value.problem == 'its aliases stand for more than 1,000 values'
    mark = raised.value.problem_mark
    assert mark is not None and (mark.line, mark.column) == (2, 7)


def test_a_problem_keeps_the_line_of_its_value() -> None:
    for text, line in [
        ('a: 1\nb: "\\U00110000"\n', 1),
        ('a: 1\nb: 2\nc: [x, !!bool maybe]\n', 2),
        ('a: 1\nb: "\\ud800"\nc: 3\n', 1),
    ]:
        with pytest.raises(yaml.MarkedYAMLError) as raised:
            yamlio.load(text)
        assert raised.value.problem_mark is not None and raised.value.problem_mark.line == line, text
    # PyYAML's reader gives a position, in bytes or characters, rather than a line.
    positions: list[tuple[str | bytes, int, int]] = [
        (b'a: 1\nb: 2\nc: caf\xe9\n', 2, 6),
        ('a: 1\nb: 2\nc: x\x07\n', 2, 4),
        (codecs.BOM_UTF8 + b'a: 1\r\nb: \x07\n', 1, 3),
        (codecs.BOM_UTF16_LE + 'a: 1\nb: \x07\n'.encode('utf-16-le'), 1, 3),
    ]
    for content, line, column in positions:
        with pytest.raises(yaml.MarkedYAMLError) as raised:
            yamlio.load(content)
        mark = raised.value.problem_mark
        assert mark is not None and (mark.line, mark.column) == (line, column), content
    errors = definitions.parse(
        'version: 1\nterms:\n  - name: X\n    approved: 2026-13-01\n    definition: d\n', 'definitions.yml'
    ).errors
    # Python 3.14 adds the value to the message: "month must be in 1..12, not 13".
    (error,) = errors
    assert error.startswith(
        "definitions.yml:4: not valid YAML: '2026-13-01' is not a valid timestamp: month must be in 1..12"
    )


def test_an_escaped_surrogate_pair_is_its_character(tmp_path: Path) -> None:
    """JSON writes a character past the Basic Multilingual Plane as an escaped surrogate pair;
    only a half left unpaired is refused."""
    text = 'a: &x "Orders \\ud83d\\udce6"\nb: *x\n"\\ud83d\\udce6": 1\n'
    assert yamlio.load(text) == {'a': 'Orders \N{PACKAGE}', 'b': 'Orders \N{PACKAGE}', '\N{PACKAGE}': 1}
    assert yamlio.load(json.dumps({'name': 'Orders \N{PACKAGE}'})) == {'name': 'Orders \N{PACKAGE}'}
    (tmp_path / 'schema.yml').write_text(text)
    assert hook._unreadable(tmp_path / 'schema.yml', []) == []
    for half in ('\\ud83d', '\\udce6', '\\udce6\\ud83d', '\\ud83d\\ud83d\\udce6'):
        with pytest.raises(yaml.MarkedYAMLError, match='a lone surrogate'):
            yamlio.load(f'a: "{half}"\n')


def test_read_passes_bytes_to_the_loader(tmp_path: Path) -> None:
    path = tmp_path / 'a.yml'
    path.write_bytes(NOT_UTF8)
    with pytest.raises(yaml.YAMLError):
        yamlio.read(path)
    path.write_bytes(codecs.BOM_UTF16_LE + 'name: café\n'.encode('utf-16-le'))
    assert yamlio.read(path) == {'name': 'café'}
    path.write_bytes(codecs.BOM_UTF8 + 'name: café\n'.encode())
    assert yamlio.read(path) == {'name': 'café'}
    with pytest.raises(OSError):
        yamlio.read(tmp_path / 'missing.yml')


@pytest.mark.parametrize('text', CASES, ids=IDS)
def test_definitions_and_ossie_list_a_problem(text: str) -> None:
    assert definitions.parse(text, 'definitions.yml').errors
    assert ossie.parse(text, 'model.ossie.yaml').errors


def test_definitions_and_ossie_files_that_are_not_utf8_list_a_problem(tmp_path: Path) -> None:
    (tmp_path / 'definitions.yml').write_bytes(NOT_UTF8)
    (tmp_path / 'model.ossie.yaml').write_bytes(NOT_UTF8)
    assert definitions.parse_file(tmp_path / 'definitions.yml', 'definitions.yml').errors
    assert ossie.parse_file(tmp_path / 'model.ossie.yaml', 'model.ossie.yaml').errors


def test_the_build_goes_on_past_definitions_it_cannot_read(tmp_path: Path) -> None:
    (tmp_path / 'definitions.yml').write_text('terms:\n  - name: X\n    definition: !!bool maybe\n')
    (tmp_path / 'model.ossie.yaml').write_bytes(NOT_UTF8)
    (tmp_path / 'x.sql').write_text('create table b as select a from t')
    warnings = build(load_config(tmp_path)).graph.warnings
    assert any(w.startswith('definitions: definitions.yml:3: not valid YAML: ') for w in warnings)
    assert any(w.startswith('ossie: model.ossie.yaml: not valid YAML: ') for w in warnings)


@pytest.mark.parametrize('text', CASES, ids=IDS)
@pytest.mark.parametrize(
    ('kind', 'path'),
    [
        ('dlt_schema', 'schemas/shop.schema.yaml'),
        ('meltano', 'meltano.yml'),
        ('airbyte_octavia', 'octavia/connections/app/configuration.yaml'),
        ('estuary', 'flow.yaml'),
        ('sling', 'replication.yaml'),
        ('lakeflow', 'databricks.yml'),
    ],
)
def test_ingestion_lists_a_problem(kind: str, path: str, text: str) -> None:
    reading = ingestion.read_files(kind, [(path, text)])
    assert reading.feeds == [] and [p for p in reading.problems if 'cannot read YAML' in p]


@pytest.mark.parametrize('content', [*(t.encode() for t in CASES), NOT_UTF8], ids=[*IDS, 'not UTF-8'])
def test_dbt_projects_profiles_and_diffs_read_nothing(tmp_path: Path, content: bytes) -> None:
    (tmp_path / 'dbt_project.yml').write_bytes(content)
    assert read_project(tmp_path) is None
    assert _read_target(tmp_path / 'dbt_project.yml', 'name', expand=False) is None
    if content != NOT_UTF8:
        with pytest.raises(yaml.YAMLError):
            _yaml_facts(content.decode())
        change = compare_yaml('schema.yml', 'models:\n  - name: m\n    data_tests: [t]\n', content.decode())
        assert not change.changed and change.note is not None


@pytest.mark.parametrize('content', [*(t.encode() for t in CASES), NOT_UTF8], ids=[*IDS, 'not UTF-8'])
def test_the_hook_says_the_file_is_not_valid_yaml(tmp_path: Path, content: bytes) -> None:
    (tmp_path / 'dbt_project.yml').write_bytes(content)
    (tmp_path / 'models').mkdir()
    assert hook.is_data_file(tmp_path / 'models' / 'schema.yml', tmp_path)
    (problem,) = hook._unreadable(tmp_path / 'dbt_project.yml', [])
    assert problem.startswith('dbt_project.yml is not valid YAML: ')
