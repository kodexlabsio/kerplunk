"""Fuzz the readers of definitions and Apache Ossie files: for any text, each returns what it
could read with its problems listed, and never raises. The dictionary beside this file, used
unless another is given, spells the tags and escapes that reach the constructors of the core's
YAML reader (src/yaml/), whose code the fuzzer cannot see: only what the Python surface does
with the reader's answer is instrumented.

Run with ``uv run --with atheris python tests/fuzz_targets/fuzz_yaml.py -max_total_time=60``; see
docs/development.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import atheris

with atheris.instrument_imports(include=['konstruct']):
    from konstruct.semantics import definitions, ossie


def test_one_input(data: bytes) -> None:
    text = atheris.FuzzedDataProvider(data).ConsumeUnicodeNoSurrogates(len(data))
    definitions.parse(text, 'definitions.yml')
    ossie.parse(text, 'model.ossie.yaml')


if __name__ == '__main__':
    if not any(arg.startswith('-dict=') for arg in sys.argv):
        sys.argv.append(f'-dict={Path(__file__).with_name("yaml.dict")}')
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
