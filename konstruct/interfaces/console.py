"""Terminal output: what the terminal can show, results on stdout, and progress, warnings and
errors on stderr.

Colour follows the usual conventions: on when a stream is a terminal, off with ``NO_COLOR``,
``--no-color`` or ``TERM=dumb``, and on regardless with ``FORCE_COLOR``. Unicode connectors and
fitting to the window are for terminals only; anything piped gets plain ASCII text with no
escape codes, truncation or animation, so it stays stable for ``grep``, files and CI logs.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import sys
import threading
import time
from types import TracebackType
from typing import Any, TextIO

from konstruct.interfaces.layout import Style


def _terminal(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty()) and os.environ.get('TERM') != 'dumb'
    except (AttributeError, ValueError):
        return False


def colour_enabled(stream: TextIO, override: bool | None = None) -> bool:
    """Colour when writing to a terminal, unless NO_COLOR is set; FORCE_COLOR forces it on."""
    if override is not None:
        return override
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    return _terminal(stream)


def unicode_enabled(stream: TextIO) -> bool:
    """Box-drawing characters on a terminal whose encoding has them; ASCII everywhere else."""
    encoding = (getattr(stream, 'encoding', None) or '').lower().replace('-', '')
    return encoding in ('utf8', 'utf8sig') and (_terminal(stream) or bool(os.environ.get('FORCE_COLOR')))


def terminal_width(stream: TextIO) -> int | None:
    """The window's width on a terminal (``COLUMNS`` overrides it); None when piped."""
    if not _terminal(stream):
        return None
    return max(shutil.get_terminal_size((100, 24)).columns, 40)


def style_for(stream: TextIO, colour: bool | None = None) -> Style:
    """How to draw on ``stream``: its colour, character set and width."""
    return Style(
        colour=colour_enabled(stream, colour), unicode=unicode_enabled(stream), width=terminal_width(stream)
    )


_COMMAND = re.compile(r'`([^`]+)`')


class Console:
    def __init__(self, colour: bool | None = None, quiet: bool = False, verbose: bool = False) -> None:
        self.out = sys.stdout
        self.err = sys.stderr
        self.quiet = quiet
        self.verbose = verbose
        self.ui = style_for(self.out, colour)
        """How results are drawn on stdout."""
        self.err_ui = style_for(self.err, colour)
        """How progress and messages are drawn on stderr."""

    def print(self, text: str = '') -> None:
        print(text, file=self.out)

    def _paint(self, text: str, role: str) -> str:
        return self.err_ui.paint(text, role)

    def _commands(self, text: str) -> str:
        """Commands in backticks, picked out in the accent colour on a terminal."""
        if not self.err_ui.colour:
            return text
        return _COMMAND.sub(lambda m: self._paint(m.group(1), 'accent'), text)

    def info(self, text: str) -> None:
        """Progress and hints on stderr, so stdout stays clean for --json and pipes."""
        if not self.quiet:
            print(f'{self._paint("konstruct:", "dim")} {self._commands(text)}', file=self.err)

    def warn(self, text: str, *details: str) -> None:
        print(f'{self._paint("konstruct:", "warn")} {self._commands(text)}', file=self.err)
        for detail in details:
            print(f'  {self._commands(detail)}', file=self.err)

    def error(self, text: str, *, hint: str | None = None, suggestions: list[str] | None = None) -> None:
        """What went wrong on the first line; why, and how to fix it, on the lines below.

        A message in the form ``what; why or fix`` (the form Konstruct's errors take) is split
        there, so the first line stays short."""
        what, *rest = text.split('; ')
        print(f'{self._paint("konstruct: error:", "error+strong")} {what}', file=self.err)
        for part in rest:
            if suggestions and part.startswith('did you mean'):
                continue
            print(f'  {self._commands(part)}', file=self.err)
        if suggestions:
            shown = ', '.join(self._paint(s, 'accent') for s in suggestions)
            print(f'  {self._paint("did you mean", "dim")} {shown}', file=self.err)
        if hint:
            print(f'  {self._paint("try", "dim")} {self._commands(hint)}', file=self.err)

    def progress(self, text: str) -> Progress:
        """A spinner on stderr while a long step runs, on a terminal only."""
        enabled = not self.quiet and not self.verbose and _terminal(self.err) and not os.environ.get('CI')
        return Progress(self, text, enabled=enabled)


def _exit_on_signal(number: int, frame: Any) -> None:
    raise SystemExit(128 + number)


_FRAMES = ('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')
_ASCII_FRAMES = ('-', '\\', '|', '/')
_ESCAPE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


class Progress(logging.Handler):
    """A one-line spinner on stderr, updated from the build's progress log records.

    It appears only after a short delay, so a quick step shows nothing, and it is erased when
    the step ends: the result that follows is the record of what happened. Records from the
    ``konstruct`` loggers update its text; with ``-v`` they are printed as lines instead.
    """

    DELAY = 0.15
    INTERVAL = 0.08

    def __init__(self, console: Console, text: str, *, enabled: bool) -> None:
        super().__init__(logging.DEBUG)
        self.console = console
        self.text = text
        self.detail = ''
        self.enabled = enabled
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._shown = False
        self._started = 0.0
        self._saved: tuple[int, bool] | None = None
        self._terminate: Any = None

    def __enter__(self) -> Progress:
        if not self.enabled:
            return self
        self._started = time.monotonic()
        logger = logging.getLogger('konstruct')
        self._saved = (logger.level, logger.propagate)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(self)
        if threading.current_thread() is threading.main_thread():
            # SIGTERM would kill the process with the cursor still hidden; as an exit instead, the
            # spinner is erased on the way out like any other.
            self._terminate = signal.signal(signal.SIGTERM, _exit_on_signal)
        self._thread = threading.Thread(target=self._spin, name='konstruct-progress', daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        if not self.enabled or self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        logger = logging.getLogger('konstruct')
        logger.removeHandler(self)
        if self._saved is not None:
            logger.setLevel(self._saved[0])
            logger.propagate = self._saved[1]
        if self._terminate is not None:
            signal.signal(signal.SIGTERM, self._terminate)
        with self._lock:
            self._clear()

    def update(self, text: str, detail: str = '') -> None:
        with self._lock:
            self.text = text
            self.detail = detail

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            with self._lock:
                self._clear()
                self.console.warn(message)
            return
        if record.name == 'konstruct.progress':
            with self._lock:
                self.detail = message
        elif record.levelno >= logging.INFO:
            self.update(message[:1].upper() + message[1:])

    def frame(self, index: int) -> str:
        """The spinner's line at ``index``: the frame, the step, its count and the time taken."""
        ui = self.console.err_ui
        frames = _FRAMES if ui.unicode else _ASCII_FRAMES
        line = f'{ui.paint(frames[index % len(frames)], "accent")} {self.text}'
        if self.detail:
            line += '  ' + ui.paint(self.detail, 'dim')
        elapsed = time.monotonic() - self._started
        if elapsed >= 1:
            line += '  ' + ui.paint(f'{elapsed:.0f}s', 'dim')
        width = (ui.width or 80) - 1
        visible = _ESCAPE.sub('', line)
        if len(visible) > width:
            line = visible[: width - 1] + ui.glyph('ellipsis')
        return line

    def _clear(self) -> None:
        if self._shown:
            self.console.err.write('\r\x1b[2K\x1b[?25h')
            self.console.err.flush()
            self._shown = False

    def _spin(self) -> None:
        if self._stop.wait(self.DELAY):
            return
        index = 0
        while not self._stop.is_set():
            with self._lock:
                self.console.err.write(('' if self._shown else '\x1b[?25l') + '\r\x1b[2K' + self.frame(index))
                self.console.err.flush()
                self._shown = True
            index += 1
            self._stop.wait(self.INTERVAL)
