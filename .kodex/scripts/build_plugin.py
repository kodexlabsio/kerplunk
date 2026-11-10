"""Write the Claude Code plugin in claude-code-plugin/ from the toolkit texts in the package.

The plugin's skills, commands, subagent, hook and MCP configuration are rendered from
konstruct/interfaces/toolkit, the same texts `konstruct install` writes, so the two never drift.
Run it after changing a toolkit text and commit the result; tests/test_toolkit.py fails while
claude-code-plugin/ is out of date.

    uv run .kodex/scripts/build_plugin.py           # write the plugin
    uv run .kodex/scripts/build_plugin.py --check   # exit 1 if it is out of date
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from konstruct.interfaces.toolkit import plugin_files

PLUGIN = Path(__file__).resolve().parents[2] / 'claude-code-plugin'


def stale(directory: Path = PLUGIN) -> list[str]:
    """Files of the plugin that differ from what the toolkit renders, or should not be there."""
    expected = plugin_files()
    found = {p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()}
    wrong = [
        path
        for path, text in expected.items()
        if not (directory / path).is_file() or (directory / path).read_text(encoding='utf-8') != text
    ]
    return sorted(set(wrong) | (found - set(expected)))


def write(directory: Path = PLUGIN) -> None:
    expected = plugin_files()
    for path in stale(directory):
        target = directory / path
        if path in expected:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected[path], encoding='utf-8')
        else:
            target.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n', 1)[0])
    parser.add_argument('--check', action='store_true', help='exit 1 if the plugin is out of date')
    args = parser.parse_args(argv)
    if args.check:
        out_of_date = stale()
        for path in out_of_date:
            print(
                f'claude-code-plugin/{path} is out of date; run .kodex/scripts/build_plugin.py',
                file=sys.stderr,
            )
        return 1 if out_of_date else 0
    write()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
