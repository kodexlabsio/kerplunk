"""Fuzz the readers of definitions and Apache Ossie files: for any text, each returns what it
could read with its problems listed, and never raises.

Run with ``uv run --with atheris python tests/fuzz_targets/fuzz_yaml.py -max_total_time=60``; see
docs/development.md.
"""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports(include=['konstruct', 'yaml']):
    from konstruct.semantics import definitions, ossie


def test_one_input(data: bytes) -> None:
    text = atheris.FuzzedDataProvider(data).ConsumeUnicodeNoSurrogates(len(data))
    definitions.parse(text, 'definitions.yml')
    ossie.parse(text, 'model.ossie.yaml')


if __name__ == '__main__':
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
