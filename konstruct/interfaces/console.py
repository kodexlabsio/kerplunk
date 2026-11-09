"""Terminal output: optional colour, aligned key-value rows and warnings on stderr."""

from __future__ import annotations

import os
import sys
from typing import TextIO

_CODES = {'bold': '1', 'dim': '2', 'red': '31', 'green': '32', 'yellow': '33', 'cyan': '36'}


def colour_enabled(stream: TextIO, override: bool | None = None) -> bool:
    """Colour when writing to a terminal, unless NO_COLOR is set; FORCE_COLOR forces it on."""
    if override is not None:
        return override
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    return hasattr(stream, 'isatty') and stream.isatty() and os.environ.get('TERM') != 'dumb'


class Console:
    def __init__(self, colour: bool | None = None, quiet: bool = False) -> None:
        self.out = sys.stdout
        self.err = sys.stderr
        self.colour = colour_enabled(self.out, colour)
        self.err_colour = colour_enabled(self.err, colour)
        self.quiet = quiet

    def style(self, text: str, *styles: str, stream: TextIO | None = None) -> str:
        enabled = self.err_colour if stream is self.err else self.colour
        if not enabled or not styles:
            return text
        codes = ';'.join(_CODES[s] for s in styles)
        return f'\x1b[{codes}m{text}\x1b[0m'

    def print(self, text: str = '') -> None:
        print(text, file=self.out)

    def row(self, key: str, value: str, width: int = 11, *, style: tuple[str, ...] = ()) -> None:
        label = key.ljust(width) if len(key) < width else key + ' '
        self.print(f'  {self.style(label, "dim")}{self.style(value, *style) if style else value}')

    def info(self, text: str) -> None:
        """Progress and hints on stderr, so stdout stays clean for --json and pipes."""
        if not self.quiet:
            print(f'{self.style("konstruct:", "dim", stream=self.err)} {text}', file=self.err)

    def warn(self, text: str) -> None:
        print(f'{self.style("konstruct:", "yellow", stream=self.err)} {text}', file=self.err)

    def error(self, text: str) -> None:
        print(f'{self.style("konstruct: error:", "red", "bold", stream=self.err)} {text}', file=self.err)
