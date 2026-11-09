"""A worker process that analyses SQL for a build, so models are analysed on every CPU.

The build starts ``python -m konstruct.engine.worker`` once per worker and, for each batch, writes one
line of JSON (a list of jobs) to its standard input and reads one line of JSON (their results)
from its standard output, until it closes the input. Workers are separate interpreters, started
fresh rather than forked, so they hold none of the build's threads or state and never import
the program that started the build.
"""

from __future__ import annotations

import signal
import sys
from typing import Any

import orjson

from konstruct.core.sql import analyse


def run(job: list[Any]) -> list[dict[str, Any]]:
    """One model's lineage as plain data."""
    sql, dialect, schema, catalog, database, target = job
    return [
        s.to_dict()
        for s in analyse(
            sql, dialect=dialect, schema=schema, default_catalog=catalog, default_db=database, target=target
        )
    ]


def main() -> None:
    # The build stops its workers itself; Ctrl-C reaches the whole process group.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    for line in sys.stdin.buffer:
        if line.strip():
            sys.stdout.buffer.write(orjson.dumps([run(job) for job in orjson.loads(line)]) + b'\n')
            sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
