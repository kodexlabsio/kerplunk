"""The terminal's visual language: styled text, tables, trees, key and value blocks and SQL.

Everything here builds lines of :class:`Text`, runs of characters with a role each, and a
:class:`Style` turns them into a string. The same lines render three ways:

* **plain**, for pipes, files and ``str(result)``: no escape codes, ASCII connectors, and no
  truncation or wrapping, so output is stable for ``grep`` and diffs;
* **unicode without colour**, on a terminal with ``NO_COLOR`` or ``--no-color``;
* **colour**, on a terminal: a small semantic palette of the sixteen ANSI colours, which every
  terminal theme maps to shades that read on its own background, light or dark.

On a terminal, tables and trees fit the window: the column marked flexible is cut with an
ellipsis, and paragraphs and formulas wrap with a hanging indent. Widths are display widths, so
East Asian wide characters count as two columns.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

# Roles, not colours: renderers say what a piece of text is, and the palette says how it looks.
# Only the base ANSI colours are used, so the terminal's theme picks shades that suit its
# background; bold and faint carry hierarchy without colour.
PALETTE = {
    'title': '1',
    'subject': '1;36',
    'strong': '1',
    'accent': '36',
    'dim': '2',
    'ok': '32',
    'warn': '33',
    'error': '31',
    'bad': '1;31',
    'good': '1;32',
    'keyword': '35',
    'literal': '32',
}

_GLYPHS = {
    'branch': ('├─ ', '|- '),
    'last': ('└─ ', '`- '),
    'pipe': ('│  ', '|  '),
    'blank': ('   ', '   '),
    'ellipsis': ('…', '...'),
    'arrow': ('→', '->'),
    'down': ('↓', 'v'),
    'up': ('↑', '^'),
    'bar': ('━', '#'),
    'bar_empty': ('─', '.'),
    'dot': ('·', '-'),
    'continue': (' \\', ' \\'),
}


def char_width(char: str) -> int:
    """The columns one character takes in a terminal: two for East Asian wide and full-width
    characters, none for combining marks and controls, one otherwise."""
    if unicodedata.east_asian_width(char) in ('W', 'F'):
        return 2
    if unicodedata.combining(char) or unicodedata.category(char) in ('Cc', 'Cf', 'Mn', 'Me'):
        return 0
    return 1


def width_of(value: str) -> int:
    """The columns ``value`` takes in a terminal."""
    if value.isascii():
        return len(value)
    return sum(char_width(c) for c in value)


def cut(value: str, width: int) -> str:
    """The longest start of ``value`` that takes at most ``width`` columns."""
    if value.isascii():
        return value[:width]
    taken = 0
    for index, char in enumerate(value):
        taken += char_width(char)
        if taken > width:
            return value[:index]
    return value


def pad(value: str, width: int) -> str:
    """``value`` padded with spaces to ``width`` columns."""
    return value + ' ' * max(width - width_of(value), 0)


Part = tuple[str, str]


class Text:
    """A line, or part of one, as runs of text that each have a role (``''`` for none). Its
    display width is kept as runs are added, so measuring it costs nothing."""

    __slots__ = ('parts', 'whole', 'width')

    def __init__(self, *parts: str | Part | Text, whole: bool = False) -> None:
        self.parts: list[Part] = []
        self.width = 0
        self.whole = whole
        """Never cut to the width: a command to copy, say, which is broken another way."""
        for part in parts:
            self.append(part)

    @classmethod
    def joined(cls, parts: list[Part], width: int) -> Text:
        """Text from runs whose display width is already known."""
        made = cls.__new__(cls)
        made.parts = parts
        made.width = width
        made.whole = False
        return made

    @classmethod
    def of(cls, value: str) -> Text:
        """Unstyled text, made without the checks of the general constructor."""
        made = cls.__new__(cls)
        made.parts = [(value, '')] if value else []
        made.width = len(value) if value.isascii() else width_of(value)
        made.whole = False
        return made

    def append(self, part: str | Part | Text, role: str = '') -> Text:
        if isinstance(part, str):
            if part:
                self.parts.append((part, role))
                self.width += width_of(part)
        elif isinstance(part, tuple):
            if part[0]:
                self.parts.append(part)
                self.width += width_of(part[0])
        else:
            self.parts.extend(part.parts)
            self.width += part.width
        return self

    def __add__(self, other: str | Part | Text) -> Text:
        return Text(self, other)

    def __len__(self) -> int:
        return self.width

    def __bool__(self) -> bool:
        return self.width > 0 or any(value for value, _ in self.parts)

    def __repr__(self) -> str:
        return f'Text({self.plain!r})'

    @property
    def plain(self) -> str:
        return ''.join(value for value, _ in self.parts)

    def truncate(self, width: int, ellipsis: str) -> Text:
        """At most ``width`` columns, ending in ``ellipsis`` when anything was cut."""
        if self.width <= width:
            return self
        keep = max(width - width_of(ellipsis), 0)
        out = Text()
        for value, role in self.parts:
            if keep <= 0:
                break
            piece = cut(value, keep)
            out.append((piece, role))
            keep -= width_of(piece)
        last_role = out.parts[-1][1] if out.parts else ''
        return out.append((ellipsis, last_role or 'dim'))

    def words(self) -> list[list[Part]]:
        """The text as words and spaces for wrapping. A word is every run of non-space
        characters between spaces, whatever roles it spans, so a line never breaks inside a word
        where its colour changes; a space is a one-item list whose text is blank."""
        out: list[list[Part]] = []
        joining = False
        for value, role in self.parts:
            for piece in re.split(r'(\s+)', value):
                if not piece:
                    continue
                if piece.isspace():
                    # Spaces are kept as written; a line break inside a run counts as one space.
                    out.append([(' ' if '\n' in piece else piece, role)])
                    joining = False
                elif joining:
                    out[-1].append((piece, role))
                else:
                    out.append([(piece, role)])
                    joining = True
        return out


Cell = str | Part | Text | None
"""A table cell: a string, a ``(text, role)`` pair, a :class:`Text`, or None for an empty cell."""


def text(value: Cell) -> Text:
    """Any cell as text."""
    if isinstance(value, Text):
        return value
    if isinstance(value, tuple):
        return Text(value)
    return Text(_one_line(value or ''))


def plain_of(value: Cell) -> str:
    """Any cell as a plain string."""
    if isinstance(value, Text):
        return value.plain
    if isinstance(value, tuple):
        return _one_line(value[0])
    return _one_line(value or '')


def _one_line(value: str) -> str:
    # A cell is one line: newlines in a description would break the columns.
    return ' '.join(value.split()) if '\n' in value else value


@dataclass
class Style:
    """How lines become a string: whether to colour, whether to draw with Unicode, and the
    width to fit (None for no limit)."""

    colour: bool = False
    unicode: bool = False
    width: int | None = None

    @classmethod
    def plain(cls) -> Style:
        return cls()

    def glyph(self, name: str) -> str:
        fancy, ascii_ = _GLYPHS[name]
        return fancy if self.unicode else ascii_

    def paint(self, value: str, role: str) -> str:
        if not self.colour or not role or not value:
            return value
        codes = ';'.join(PALETTE[r] for r in role.split('+'))
        return f'\x1b[{codes}m{value}\x1b[0m'

    def line(self, line: Text) -> str:
        if not self.colour:
            return line.plain.rstrip()
        return ''.join(self.paint(value, role) for value, role in line.parts).rstrip()

    def render(self, lines: Iterable[Text]) -> str:
        """The lines as one string. On a terminal, a line still wider than the window is cut, so
        nothing wraps into the next line's columns."""
        if self.width is None:
            return '\n'.join(self.line(line) for line in lines)
        return '\n'.join(self.line(line if line.whole else self.fit(line)) for line in lines)

    def fit(self, line: Text) -> Text:
        """``line`` cut to the width, when there is one."""
        if self.width is None:
            return line
        return line.truncate(self.width, self.glyph('ellipsis'))


# Blocks


def blank() -> Text:
    return Text()


def header(title: str, subject: str | Text, *meta: str | None) -> Text:
    """The first line of a result: what it is, about what, and a few facts in parentheses."""
    line = Text((title, 'title'), ' ') if title else Text()
    line.append(subject if isinstance(subject, Text) else (subject, 'subject'))
    facts = [m for m in meta if m]
    if facts:
        line.append((f' ({", ".join(facts)})', 'dim'))
    return line


def section(title: str, count: int | None = None) -> Text:
    line = Text((title, 'title'))
    if count is not None:
        line.append((f' ({count})', 'dim'))
    return line


def paragraph(
    style: Style, value: str | Text, *, indent: int = 2, hanging: int | None = None, role: str = ''
) -> list[Text]:
    """A paragraph, wrapped to the width on a terminal and kept on one line otherwise."""
    body = value if isinstance(value, Text) else Text((' '.join(value.split()), role))
    return wrap(style, body, indent=indent, hanging=indent if hanging is None else hanging)


def wrap(
    style: Style, body: Text, *, indent: int = 0, hanging: int = 0, first: Text | None = None
) -> list[Text]:
    """``body`` after ``indent`` spaces (or after ``first``), broken at spaces to fit the width,
    with later lines indented by ``hanging``."""
    lead = first if first is not None else Text(' ' * indent)
    if style.width is None or lead.width + body.width <= style.width:
        return [Text(lead, body)]
    room = max(style.width, lead.width + 20)
    lines: list[Text] = []
    current = Text(lead)
    empty = True
    for word in body.words():
        if word[0][0].isspace():
            if not empty:
                current.append(word[0])
            continue
        size = sum(width_of(value) for value, _ in word)
        if not empty and current.width + size > room:
            lines.append(_rstrip(current))
            current = Text(' ' * hanging)
            empty = True
        for part in word:
            current.append(part)
        empty = False
    lines.append(_rstrip(current))
    return lines


def _rstrip(line: Text) -> Text:
    while line.parts and not line.parts[-1][0].strip():
        value, _ = line.parts.pop()
        line.width -= width_of(value)
    if line.parts:
        value, role = line.parts[-1]
        stripped = value.rstrip()
        line.parts[-1] = (stripped, role)
        line.width -= width_of(value) - width_of(stripped)
    return line


def table(
    style: Style,
    rows: Sequence[Sequence[Cell]],
    *,
    indent: int = 2,
    gap: int = 2,
    right: Iterable[int] = (),
    flex: int | None = None,
    cap: int | None = 48,
    headers: Sequence[str] | None = None,
    drop: Sequence[int] = (),
) -> list[Text]:
    """Rows as aligned columns.

    ``right`` lists the columns aligned right (numbers). On a terminal too narrow for the
    table, the columns in ``drop`` are left out first, in that order, and then the ``flex``
    column (by default the last) is cut to fit. A cell longer than ``cap`` does not widen its
    column; it overflows instead, so one long name does not push every row to the right.
    """
    if not rows:
        return []
    if style.width is None and not style.colour:
        return _plain_table(rows, indent=indent, gap=gap, right=set(right), cap=cap, headers=headers)
    cells = [[_runs(c) for c in row] for row in rows]
    if headers is not None:
        cells.insert(0, [([(h, 'dim')], width_of(h)) for h in headers])
    count = max(len(row) for row in cells)
    empty: tuple[list[Part], int] = ([], 0)
    for row in cells:
        row.extend(empty for _ in range(count - len(row)))
    sizes = [[row[i][1] for row in cells] for i in range(count)]
    # Columns that are empty in every row are left out, so optional facts leave no gaps.
    keep = [i for i in range(count) if any(sizes[i])]
    if not keep:
        return [Text() for _ in cells]
    flex = flex if flex is not None and flex in keep else keep[-1]
    aligned = set(right)

    def measure(columns: list[int]) -> list[int]:
        # The flexible column is cut to the room left, not to the cap.
        return [_column_width(sizes[i], cap, i in aligned or i == flex) for i in columns]

    widths = measure(keep)
    if style.width is not None:
        for column in drop:
            # Columns go only while the flexible one would be cut to less than a useful width.
            at = keep.index(flex)
            room = style.width - indent - sum(w + gap for j, w in enumerate(widths) if j != at)
            if room >= min(widths[at], 28):
                break
            if column in keep and column != flex:
                keep.remove(column)
                widths = measure(keep)
        at = keep.index(flex)
        fixed = indent + sum(w + gap for j, w in enumerate(widths) if j != at)
        widths[at] = max(min(widths[at], style.width - fixed), 8)
    flex_at = keep.index(flex)
    right_at = {j for j, i in enumerate(keep) if i in aligned}
    ellipsis = style.glyph('ellipsis')
    lead = (' ' * indent, '')
    space = (' ' * gap, '')
    lines = []
    for full in cells:
        row = [full[i] for i in keep]
        last = max((j for j, (_, size) in enumerate(row) if size), default=-1)
        parts: list[Part] = [lead]
        total = indent
        for j in range(last + 1):
            runs, size = row[j]
            if style.width is not None and j == flex_at and size > widths[j]:
                cut_text = Text(*runs).truncate(widths[j], ellipsis)
                runs, size = cut_text.parts, cut_text.width
            missing = max(widths[j] - size, 0)
            if j in right_at and missing:
                parts.append((' ' * missing, ''))
                total += missing
            parts.extend(runs)
            total += size
            if j < last:
                if j not in right_at and missing:
                    parts.append((' ' * missing, ''))
                    total += missing
                parts.append(space)
                total += gap
        lines.append(style.fit(Text.joined(parts, total)))
    return lines


def _runs(cell: Cell) -> tuple[list[Part], int]:
    """A cell as its styled runs and display width."""
    if isinstance(cell, Text):
        return cell.parts, cell.width
    if isinstance(cell, tuple):
        value, role = cell
    else:
        value, role = cell or '', ''
    if '\n' in value:
        value = _one_line(value)
    return ([(value, role)] if value else []), width_of(value)


def _column_width(sizes: list[int], cap: int | None, uncapped: bool) -> int:
    fitting = [n for n in sizes if cap is None or uncapped or n <= cap]
    return max(fitting) if fitting else (cap or 0)


def _plain_table(
    rows: Sequence[Sequence[Cell]],
    *,
    indent: int,
    gap: int,
    right: set[int],
    cap: int | None,
    headers: Sequence[str] | None,
) -> list[Text]:
    """A table as plain strings, for pipes: the same layout as :func:`table` with no width to
    fit, built without styled runs because nothing is coloured. It runs for every piped result,
    some tens of thousands of lines long, so it works a column at a time with the string
    methods rather than a cell at a time."""
    table_rows: list[Sequence[Cell]] = [headers] if headers is not None else []
    table_rows.extend(rows)
    padded: list[list[str]] = []
    for index, column in enumerate(itertools.zip_longest(*table_rows)):
        values = [
            c if c.__class__ is str else c[0] if c.__class__ is tuple else '' if c is None else c.plain  # type: ignore[union-attr]
            for c in column
        ]
        joined = ''.join(values)
        if not joined:
            continue
        if '\n' in joined:
            values = [_one_line(v) for v in values]
        ascii_ = joined.isascii()
        sizes = list(map(len, values)) if ascii_ else [width_of(v) for v in values]
        width = _column_width(sizes, cap, index in right)
        if ascii_:
            padded.append(
                list(map(str.rjust if index in right else str.ljust, values, itertools.repeat(width)))
            )
        elif index in right:
            padded.append([' ' * (width - n) + v for v, n in zip(values, sizes, strict=True)])
        else:
            padded.append([v + ' ' * (width - n) for v, n in zip(values, sizes, strict=True)])
    lead = ' ' * indent
    space = ' ' * gap
    if not padded:
        return [Text() for _ in table_rows]
    # Every kept column is padded and each line right-stripped, which leaves the same text as
    # stopping at the last filled cell.
    lines = (space.join(cells).rstrip() for cells in zip(*padded, strict=True))
    return [Text.of(lead + line if line else '') for line in lines]


def pairs(
    style: Style, rows: Sequence[tuple[str, Cell]], *, indent: int = 2, width: int | None = None
) -> list[Text]:
    """Keys and values: keys dim and aligned, values wrapped under themselves on a terminal."""
    shown = [(k, text(v)) for k, v in rows if v]
    if not shown:
        return []
    key_width = width or max(width_of(k) for k, _ in shown)
    lines = []
    for key, value in shown:
        lead = Text(' ' * indent, (pad(key, key_width), 'dim'), '  ')
        lines += wrap(style, value, first=lead, hanging=indent + key_width + 2)
    return lines


@dataclass
class Branch:
    """One node of a tree: its label, the facts shown beside it, and its children."""

    label: Cell
    facts: list[Cell] = field(default_factory=list)
    children: list[Branch] = field(default_factory=list)


def tree(
    style: Style, root: Branch, *, indent: int = 2, flex: int | None = None, drop: Sequence[int] = ()
) -> list[Text]:
    """A tree with box-drawing connectors (ASCII ones in plain text), its facts aligned."""
    rows: list[list[Cell]] = [[root.label, *root.facts]]
    branch, last, pipe, blank_ = (style.glyph(g) for g in ('branch', 'last', 'pipe', 'blank'))
    plain = style.width is None and not style.colour
    # Depth first with a stack rather than recursion, so a deep lineage cannot hit the limit.
    pending = [(child, '', i == len(root.children) - 1) for i, child in enumerate(root.children)]
    pending.reverse()
    while pending:
        node, prefix, is_last = pending.pop()
        connector = prefix + (last if is_last else branch)
        label: Cell = (
            connector + plain_of(node.label) if plain else Text((connector, 'dim'), text(node.label))
        )
        rows.append([label, *node.facts])
        below = prefix + (blank_ if is_last else pipe)
        count = len(node.children)
        pending.extend((node.children[i], below, i == count - 1) for i in range(count - 1, -1, -1))
    return table(style, rows, indent=indent, flex=flex, cap=56, drop=drop)


def bar(style: Style, share: float, width: int = 20) -> Text:
    """A coverage bar, drawn only on a colour terminal where it reads at a glance."""
    if not (style.colour and style.unicode):
        return Text()
    filled = round(max(0.0, min(share, 1.0)) * width)
    role = 'ok' if share >= 0.9 else 'warn' if share >= 0.6 else 'error'
    return Text((style.glyph('bar') * filled, role), (style.glyph('bar_empty') * (width - filled), 'dim'))


def command(style: Style, value: str, *, indent: int = 2) -> list[Text]:
    """A shell command to copy. Plain, it is one line. On a terminal too narrow for it, it breaks
    between words with a trailing backslash, so it still runs when pasted and no word is split
    across lines."""
    if style.width is None or indent + width_of(value) <= style.width:
        return [Text(' ' * indent, value, whole=True)]
    lines: list[Text] = []
    current = ' ' * indent
    room = style.width - width_of(style.glyph('continue'))
    for word in value.split(' '):
        if current.strip() and width_of(current) + 1 + width_of(word) > room:
            lines.append(Text(current, (style.glyph('continue'), 'dim'), whole=True))
            current = ' ' * (indent + 4) + word
        else:
            current += (' ' if current.strip() else '') + word
    lines.append(Text(current, whole=True))
    return lines


# SQL


_KEYWORDS = frozenset(
    """
    ALL AND ANY AS ASC BETWEEN BY CASE CAST CROSS CURRENT DESC DISTINCT ELSE END EXCEPT EXISTS FALSE
    FILTER FOLLOWING FROM FULL GROUP HAVING IF ILIKE IN INNER INTERSECT INTERVAL IS JOIN LATERAL
    LEFT LIKE LIMIT NOT NULL NULLS ON OR ORDER OUTER OVER PARTITION PRECEDING QUALIFY RANGE
    RIGHT ROW ROWS SELECT THEN TRUE UNBOUNDED UNION USING WHEN WHERE WINDOW WITH WITHIN
    """.split()
)
_SQL_TOKEN = re.compile(
    r"""(?P<string>'(?:[^']|'')*'?)"""
    r'|(?P<quoted>"(?:[^"]|"")*"?|`[^`]*`?)'
    r'|(?P<name>[A-Za-z_][A-Za-z0-9_$]*(?:\.(?:[A-Za-z_][A-Za-z0-9_$]*|"[^"]*"|\*))*)'
    r'|(?P<number>\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+)'
    r'|(?P<other>[^\'"`A-Za-z_0-9.]+|.)',
    re.DOTALL,
)


def sql(value: str) -> Text:
    """SQL with light highlighting: keywords and functions, and literals. Identifiers stay
    plain, as do words in lower case, so a column called ``date`` is not taken for a keyword."""
    runs: list[Part] = []
    pieces: list[str] = []
    current = ''
    for match in _SQL_TOKEN.finditer(value):
        kind = match.lastgroup
        token = match.group()
        role = ''
        if kind in ('string', 'number'):
            role = 'literal'
        elif (
            kind == 'name'
            and token.isupper()
            and '.' not in token
            and (token in _KEYWORDS or value[match.end() : match.end() + 1] == '(')
        ):
            role = 'literal' if token in ('TRUE', 'FALSE', 'NULL') else 'keyword'
        if role != current and pieces:
            runs.append((''.join(pieces), current))
            pieces = []
        current = role
        pieces.append(token)
    if pieces:
        runs.append((''.join(pieces), current))
    return Text(*runs)


def formula(style: Style, value: str, *, indent: int = 2, lead: str = '= ') -> list[Text]:
    """``= <sql>``, highlighted, wrapped under itself on a terminal."""
    first = Text(' ' * indent, (lead, 'dim'))
    return wrap(style, sql(value), first=first, hanging=indent + len(lead))
