"""Fuzz the readers of loader configuration files (dlt, Meltano, Singer, Airbyte, Terraform, Kafka
Connect, Estuary, Sling, Lakeflow Connect, DMS) and of the schemas dlt keeps in the warehouse: for
any text, discovery names a format or none, and each reader returns the feeds it could read with
its problems listed, and never raises or runs on: YAML aliases that would expand without end are
refused, and routing expressions and wildcards are matched in linear time.

Run with ``uv run --with atheris==3.1.0 python tests/fuzz_targets/fuzz_ingestion.py -max_total_time=60``; see
docs/development.md.
"""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports(include=['konstruct', 'yaml']):
    from konstruct.inputs import ingestion

# A path for each format, where discovery would find it; the grouped formats' files sit in the
# directories they are read by.
PATHS = {
    'dlt': 'pipelines/shop.py',
    'dlt_schema': 'schemas/shop.schema.yaml',
    'meltano': 'meltano.yml',
    'singer_catalog': 'extract/tap-postgres.catalog.json',
    'singer_state': 'state/tap-postgres.state.json',
    'airbyte_api': 'airbyte/connection.json',
    'airbyte_octavia': 'octavia/connections/app/configuration.yaml',
    'terraform': 'terraform/main.tf',
    'kafka_connect': 'connect/sink.json',
    'estuary': 'flow.yaml',
    'sling': 'replication.yaml',
    'lakeflow': 'databricks.yml',
    'dms': 'dms/mappings.json',
}
KINDS = sorted(PATHS)
assert set(KINDS) == set(ingestion.READERS) | set(ingestion.GROUP_READERS)
# Inputs a mutating fuzzer is unlikely to build: YAML aliases that stand for 387 million values,
# and 200,000 unary minus signs, which exhaust Python's parser. Each is read once at start, in
# every format, and must come back as a problem within the fuzzer's timeout.
ALIASES = ''.join(
    f'{level}: &{level} [' + ', '.join([f'*{chr(ord(level) - 1)}' if level > 'a' else 'x'] * 9) + ']\n'
    for level in 'abcdefghi'
)
HOSTILE = [
    ALIASES + 'z: *i\n',
    'import dlt\np = dlt.pipeline("a")\np.run(' + '-' * 200_000 + 'x)\n',
]


def read(kind: str, text: str) -> None:
    path = PATHS[kind]
    ingestion.kind_of(path, text)
    ingestion.read_files(kind, [(path, text)])
    if kind == 'kafka_connect':
        ingestion.read_files(kind, [('connect/worker.properties', text)])
    ingestion.dlt_version_feeds([('shop', 1, text)], ('warehouse', 'shop'), 'warehouse.shop._dlt_version')


def test_one_input(data: bytes) -> None:
    provider = atheris.FuzzedDataProvider(data)
    kind = provider.PickValueInList(KINDS)
    read(kind, provider.ConsumeUnicodeNoSurrogates(len(data)))


if __name__ == '__main__':
    for text in HOSTILE:
        for kind in KINDS:
            read(kind, text)
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
