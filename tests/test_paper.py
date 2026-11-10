"""The paper stays in step with the code: it names every command, MCP tool and export format, and
types no number that the code should have measured.

``paper/generate.py`` writes the measurements as macros in ``paper/generated/``, which CI builds
and the repository does not keep. A number in the paper is therefore either a macro or a figure
from a source the sentence cites; any other digit was typed by hand and can drift from the code.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PAPER = ROOT / 'paper'
GENERATED = PAPER / 'generated'


def _uncomment(text: str) -> str:
    return '\n'.join(re.sub(r'(?<!\\)%.*', '', line) for line in text.splitlines())


def paper_text() -> str:
    """The paper's own text, without comments or the preamble: the main file's body and its
    sections, not the generated files."""
    main = _uncomment((PAPER / 'konstruct.tex').read_text(encoding='utf-8'))
    body = main.split(r'\begin{document}', 1)[1]
    sections = [_uncomment(p.read_text(encoding='utf-8')) for p in sorted((PAPER / 'sections').glob('*.tex'))]
    return '\n'.join([body, *sections])


# Markup that holds digits without stating a number: cross-references and labels, environment
# arguments (an algorithm's line numbering, a table's columns, a plot axis's options) and lengths.
# Everything inside an environment, a figure's drawing included, is still read.
_MARKUP = re.compile(
    r'\\(?:label|cref|Cref|ref|eqref|input|bibliography|bibliographystyle)\{[^{}]*\}'
    r'|\\begin\{[a-zA-Z*]+\}(?:\[[^\]]*\])?(?:\{[^{}]*\})?'
    r'|\\end\{[a-zA-Z*]+\}'
    r'|\\setlength\{[^{}]*\}\{[^{}]*\}'
    r'|\\\\\[[^\]]*\]',
)
_NUMBER = re.compile(r'(?<![A-Za-z_\d.])\d+(?:[.,]\d+)*(?![A-Za-z_\d])')
_UNITS = (
    'zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|'
    'sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|'
    'hundred|thousand|million|billion|dozen'
)
_NUMBER_WORDS = re.compile(
    rf'\b(?:(?:{_UNITS})s?|(?:{_UNITS})fold|twice|double|triple|half|halves|quarters?|thirds?|'
    r'second|fourth|fifth|sixth|seventh|eighth|ninth|tenth|percent)\b',
    re.IGNORECASE,
)
# Number words that state structure rather than a measurement, reviewed one by one.
STRUCTURAL = (
    'two relations are at the heart',
    'two columns are compared',
    'rests on two assumptions',
    'when the two runs',
    'keeping the two apart',
    'between two nodes',
    'two versions of a model',
    'the second condition',
    'unless the two are bound',
    'uses all three to test',
    'on $D$ twice',
    'the two results differ',
    'two inputs change together',
    'second argument',
    'second, the engine',
    'three rules shape it',
    'three workloads',
    'three ways',
    'in two arms',
    'comparison with zero',
    'this half of the experiment',
)
_CITE = re.compile(r'\\cite[a-z]*\{')
# Generated macros are named in camel case starting with a lower-case letter (\\jsClaimed).
_GENERATED = re.compile(r'\\[a-z]+[A-Z][a-zA-Z]*')


def sentences(text: str) -> list[str]:
    """The paper's prose split into sentences at every full stop followed by a space, whatever
    follows it, and at blank lines and table rows."""
    text = _MARKUP.sub(' ', text)
    parts = re.split(r'(?<=[.!?])\s+|\n\s*\n|\\\\', text)
    return [p.strip() for p in parts if p.strip()]


def hand_typed(sentence: str) -> list[str]:
    """The numbers typed by hand in ``sentence``: every digit and number word, except in a clause
    that cites the source of a third-party figure and is not about Konstruct, and except the
    reviewed structural phrases."""
    found: list[str] = []
    for clause in _MARKUP.sub(' ', sentence).split(';'):
        if _CITE.search(clause) and 'Konstruct' not in clause:
            continue
        found += _NUMBER.findall(clause)
        words = re.sub(r'\s+', ' ', clause)
        for phrase in STRUCTURAL:
            words = re.sub(re.escape(phrase), ' ', words, flags=re.IGNORECASE)
        found += [w.lower() for w in _NUMBER_WORDS.findall(words)]
    return found


def test_the_paper_types_no_number_by_hand() -> None:
    found = {s: hand_typed(s) for s in sentences(paper_text())}
    assert {s: n for s, n in found.items() if n} == {}


@pytest.mark.parametrize(
    ('text', 'numbers'),
    [
        ('The command line has 22 commands.', ['22']),
        (r'A build of 2,000 models takes \perfLargestCold{}~s.', ['2,000']),
        (r'All \jsConfirmed{} of 87 claimed edges are confirmed.', ['87']),
        (r'It took 2.5~s, on \texttt{1000} rows and $n = 3$.', ['2.5', '1000', '3']),
        (r'The median of five runs is \perfLargestImpact{}~ms.', ['five']),
        (r'Two columns are compared, one at a time.', []),
        (r'\jsConfirmed{} of \jsClaimed{} edges (\cref{tab:verify}) at M0 with SHA256.', []),
        (r'In 2023, 67\% said so~\citep{precisely2024}.', []),
        (r'In 2023, 67\% said so~\citep{precisely2024}; Konstruct confirmed 36.', ['36']),
        (r'\begin{algorithmic}[1] \State $x$ \label{eq:2}', []),
        # The reviewer's probes.
        (
            r'Konstruct confirms 87 edges, using the definition of dependency of~\citet{cheney2007dependency}.',
            ['87'],
        ),
        (r'A cold build starts eight workers and takes seventeen seconds.', ['eight', 'seventeen']),
        (r'On \machineCores{} CPUs a cold build takes seventeen seconds.', ['seventeen']),
        (
            "The bundled example has 3 DAGs and 12 models. jaffle-shop is dbt Labs' sandbox "
            r'project~\citep{jaffleshop}.',
            ['3', '12'],
        ),
        (
            r'\begin{tikzpicture}\node at (0,0) {Cold build: 4.2 s on 2,000 models};\end{tikzpicture}',
            ['0,0', '4.2', '2,000'],
        ),
        (
            r'About half of the \jsClaimed{} edges, and a quarter of the references, are direct.',
            ['half', 'quarter'],
        ),
        (r'\caption{Konstruct builds 5,000 models in 41 s, as in~\citep{sqlglot}.}', ['5,000', '41']),
        (
            r'Konstruct is the first tool, and its third release confirms a hundredfold more edges.',
            ['third', 'hundredfold'],
        ),
    ],
)
def test_hand_typed_numbers_are_found(text: str, numbers: list[str]) -> None:
    assert [n for sentence in sentences(text) for n in hand_typed(sentence)] == numbers


def _generated_macros() -> set[str]:
    files = sorted(GENERATED.glob('*.tex')) if GENERATED.is_dir() else []
    if not files:
        if os.environ.get('KONSTRUCT_REQUIRE_PAPER_NUMBERS'):
            pytest.fail('paper/generated/ is empty; run paper/generate.py first')
        pytest.skip('paper/generated/ is built by paper/generate.py in CI')
    text = '\n'.join(path.read_text(encoding='utf-8') for path in files)
    return set(re.findall(r'\\newcommand\{\\([a-zA-Z]+)\}', text))


def test_every_structural_phrase_is_in_the_paper() -> None:
    """The allowlist of number words names only phrases the paper still uses."""
    text = re.sub(r'\s+', ' ', paper_text()).lower()
    assert [p for p in STRUCTURAL if p.lower() not in text] == []


def test_every_generated_macro_the_paper_uses_exists() -> None:
    text = paper_text()
    preamble = (PAPER / 'konstruct.tex').read_text(encoding='utf-8')
    declared = re.findall(r'\\(?:newcommand|DeclareMathOperator)\{?\\([a-zA-Z]+)', preamble)
    defined = _generated_macros() | set(declared)
    used = {m[1:] for m in _GENERATED.findall(text)}
    assert used - defined == set()


def test_the_paper_names_every_command() -> None:
    from konstruct.interfaces.cli import parser

    action = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))
    text = paper_text()
    missing = [c for c in sorted(action.choices) if not re.search(rf'\\cmd\{{konstruct\s+{c}[\s}}]', text)]
    assert missing == []


def test_the_paper_names_every_mcp_tool() -> None:
    tree = ast.parse((ROOT / 'konstruct' / 'interfaces' / 'mcp_server.py').read_text())
    build = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'build_server')
    tools = [
        node.name
        for node in build.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(d, ast.Call) and getattr(d.func, 'attr', '') == 'tool' for d in node.decorator_list
        )
    ]
    text = paper_text()
    assert tools
    assert [t for t in tools if f'\\cmd{{{t}}}' not in text] == []


def test_the_paper_names_every_export_format() -> None:
    from konstruct.outputs.export import FORMATS

    text = paper_text()
    assert [f for f in FORMATS if f'\\cmd{{{f}}}' not in text] == []


@pytest.mark.parametrize(
    'path',
    sorted([PAPER / 'konstruct.tex', *(PAPER / 'sections').glob('*.tex')]),
    ids=lambda p: str(p.relative_to(PAPER)),
)
def test_the_paper_has_no_dashes(path: Path) -> None:
    """The writing style has no em or en dashes, which LaTeX writes as -- and ---."""
    text = _uncomment(path.read_text(encoding='utf-8'))
    assert '--' not in re.sub(r'\\cmd\{[^}]*\}', '', text)
    assert '\u2013' not in text and '\u2014' not in text
