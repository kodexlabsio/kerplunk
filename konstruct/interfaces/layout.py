"""The terminal's visual language: styled text, tables, trees, key and value blocks and SQL.

Everything here builds lines of :class:`Text`, runs of characters with a role each, and a
:class:`Style` turns them into a string. The same lines render three ways:

* **plain**, for pipes, files and ``str(result)``: no escape codes, ASCII connectors, and no
  truncation or wrapping, so output is stable for ``grep`` and diffs;
* **unicode without colour**, on a terminal with ``NO_COLOR`` or ``--no-color``;
* **colour**, on a terminal: a small semantic palette of the sixteen ANSI colours, which every
  terminal theme maps to shades that read on its own background, light or dark.

On a terminal, tables and trees fit the window: the column marked flexible is cut with an
ellipsis, and paragraphs and formulas wrap with a hanging indent.
"""

from __future__ import annotations

import re
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
    'rule': ('─', '-'),
    'bar': ('━', '#'),
    'bar_empty': ('─', '.'),
    'dot': ('·', '-'),
}


class Text:
    """A line, or part of one, as runs of text that each have a role (``''`` for none)."""

    __slots__ = ('parts', 'whole')

    def __init__(self, *parts: str | tuple[str, str] | Text, whole: bool = False) -> None:
        self.parts: list[tuple[str, str]] = []
        self.whole = whole
        """Never cut to the width: a command to copy, say, which the terminal may wrap instead."""
        for part in parts:
            self.append(part)

    def append(self, part: str | tuple[str, str] | Text, role: str = '') -> Text:
        if isinstance(part, Text):
            self.parts.extend(part.parts)
        elif isinstance(part, tuple):
            if part[0]:
                self.parts.append(part)
        elif part:
            self.parts.append((part, role))
        return self

    def __add__(self, other: str | tuple[str, str] | Text) -> Text:
        return Text(self, other)

    def __len__(self) -> int:
        return sum(len(text) for text, _ in self.parts)

    def __bool__(self) -> bool:
        return any(text for text, _ in self.parts)

    def __repr__(self) -> str:
        return f'Text({self.plain!r})'

    @property
    def plain(self) -> str:
        return ''.join(text for text, _ in self.parts)

    def truncate(self, width: int, ellipsis: str) -> Text:
        """At most ``width`` characters, ending in ``ellipsis`` when anything was cut."""
        if len(self) <= width:
            return self
        keep = max(width - len(ellipsis), 0)
        cut = Text()
        for text, role in self.parts:
            if keep <= 0:
                break
            cut.append((text[:keep], role))
            keep -= len(text[:keep])
        last_role = cut.parts[-1][1] if cut.parts else ''
        return cut.append((ellipsis, last_role or 'dim'))

    def words(self) -> list[tuple[str, str]]:
        """The runs split at spaces, each space its own run, for wrapping."""
        out: list[tuple[str, str]] = []
        for text, role in self.parts:
            out.extend(
                # Spaces are kept as written; a line break inside a run counts as one space.
                (' ' if '\n' in piece else piece, role)
                for piece in re.split(r'(\s+)', text)
                if piece
            )
        return out


def text(value: object, role: str = '') -> Text:
    """Any cell as text: ``None`` as nothing, anything else by its ``str``."""
    if isinstance(value, Text):
        return value
    shown = '' if value is None else str(value)
    # A cell is one line: newlines in a description would break the columns.
    return Text((' '.join(shown.split()) if '\n' in shown else shown, role))


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
        return ''.join(self.paint(value, role) for value, role in line.parts).rstrip()

    def render(self, lines: Iterable[Text]) -> str:
        """The lines as one string. On a terminal, a line still wider than the window is cut, so
        nothing wraps into the next line's columns."""
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
    if style.width is None or len(lead) + len(body) <= style.width:
        return [Text(lead, body)]
    room = max(style.width, len(lead) + 20)
    lines: list[Text] = []
    current = Text(lead)
    empty = True
    for piece, role in body.words():
        if piece.isspace():
            if not empty:
                current.append((piece, role))
            continue
        if not empty and len(current) + len(piece) > room:
            lines.append(_rstrip(current))
            current = Text(' ' * hanging)
            empty = True
        current.append((piece, role))
        empty = False
    lines.append(_rstrip(current))
    return lines


def _rstrip(line: Text) -> Text:
    while line.parts and not line.parts[-1][0].strip():
        line.parts.pop()
    if line.parts:
        value, role = line.parts[-1]
        line.parts[-1] = (value.rstrip(), role)
    return line


Cell = object
"""A table cell: a :class:`Text`, a string, or None for an empty cell."""


def table(
    style: Style,
    rows: Sequence[Sequence[object]],
    *,
    indent: int = 2,
    gap: int = 2,
    right: Iterable[int] = (),
    flex: int | None = None,
    cap: int | None = 48,
    headers: Sequence[str] | None = None,
    drop: Sequence[int] = (),
    overflow: str = 'cut',
) -> list[Text]:
    """Rows as aligned columns.

    ``right`` lists the columns aligned right (numbers). On a terminal too narrow for the
    table, the columns in ``drop`` are left out first, in that order, and then the ``flex``
    column (by default the last) is cut to fit. A cell longer than ``cap`` does not widen its
    column; it overflows instead, so one long name does not push every row to the right. With
    ``overflow='wrap'`` a last column too long for the terminal wraps under itself instead.
    """
    if not rows:
        return []
    cells = [[text(c) for c in row] for row in rows]
    if headers is not None:
        cells.insert(0, [Text((h, 'dim')) for h in headers])
    count = max(len(row) for row in cells)
    for row in cells:
        row.extend(Text() for _ in range(count - len(row)))
    # Columns that are empty in every row are left out, so optional facts leave no gaps.
    keep = [i for i in range(count) if any(row[i] for row in cells)]
    flex = flex if flex is not None and flex in keep else keep[-1]

    def measure(columns: list[int]) -> list[int]:
        widths = []
        for i in columns:
            lengths = [len(row[i]) for row in cells]
            # The flexible column is cut to the room left, not to the cap.
            fitting = [n for n in lengths if cap is None or n <= cap or i in right or i == flex]
            widths.append(max(fitting) if fitting else (cap or 0))
        return widths

    right = set(right)
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
        flex_at = keep.index(flex)
        fixed = indent + sum(w + gap for j, w in enumerate(widths) if j != flex_at)
        widths[flex_at] = max(min(widths[flex_at], style.width - fixed), 8)
    cells = [[row[i] for i in keep] for row in cells]
    right = {keep.index(i) for i in right if i in keep}
    flex_at = keep.index(flex)
    lines = []
    ellipsis = style.glyph('ellipsis')
    for row in cells:
        line = Text(' ' * indent)
        last = max((j for j, cell in enumerate(row) if cell), default=-1)
        for j, cell in enumerate(row[: last + 1]):
            if style.width is not None and j == flex_at and j == last and overflow == 'wrap':
                lines += wrap(style, cell, first=line, hanging=len(line))
                break
            if style.width is not None and j == flex_at:
                cell = cell.truncate(widths[j], ellipsis)
            pad = max(widths[j] - len(cell), 0)
            if j in right:
                line.append(' ' * pad).append(cell)
            elif j < last:
                line.append(cell).append(' ' * pad)
            else:
                line.append(cell)
            if j < last:
                line.append(' ' * gap)
        else:
            lines.append(style.fit(line))
    return lines


def pairs(
    style: Style, rows: Sequence[tuple[str, Cell]], *, indent: int = 2, width: int | None = None
) -> list[Text]:
    """Keys and values: keys dim and aligned, values wrapped under themselves on a terminal."""
    shown = [(k, text(v)) for k, v in rows if v]
    if not shown:
        return []
    key_width = width or max(len(k) for k, _ in shown)
    lines = []
    for key, value in shown:
        lead = Text(' ' * indent, (key.ljust(key_width), 'dim'), '  ')
        lines += wrap(style, value, first=lead, hanging=indent + key_width + 2)
    return lines


@dataclass
class Branch:
    """One node of a tree: its label, the facts shown beside it, and its children."""

    label: Text
    facts: list[Cell] = field(default_factory=list)
    children: list[Branch] = field(default_factory=list)


def tree(
    style: Style, root: Branch, *, indent: int = 2, flex: int | None = None, drop: Sequence[int] = ()
) -> list[Text]:
    """A tree with box-drawing connectors (ASCII ones in plain text), its facts aligned."""
    rows: list[list[Cell]] = [[root.label, *root.facts]]

    def visit(branch: Branch, prefix: str) -> None:
        for index, child in enumerate(branch.children):
            last = index == len(branch.children) - 1
            connector = style.glyph('last' if last else 'branch')
            rows.append([Text((prefix + connector, 'dim'), child.label), *child.facts])
            visit(child, prefix + style.glyph('blank' if last else 'pipe'))

    visit(root, '')
    return table(style, rows, indent=indent, flex=flex, cap=56, drop=drop)


def bar(style: Style, share: float, width: int = 20) -> Text:
    """A coverage bar, drawn only on a colour terminal where it reads at a glance."""
    if not (style.colour and style.unicode):
        return Text()
    filled = round(max(0.0, min(share, 1.0)) * width)
    role = 'ok' if share >= 0.9 else 'warn' if share >= 0.6 else 'error'
    return Text((style.glyph('bar') * filled, role), (style.glyph('bar_empty') * (width - filled), 'dim'))


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
    r'|(?P<other>.)',
    re.DOTALL,
)


def sql(value: str) -> Text:
    """SQL with light highlighting: keywords and functions, and literals. Identifiers stay
    plain, as do words in lower case, so a column called ``date`` is not taken for a keyword."""
    out = Text()
    for match in _SQL_TOKEN.finditer(value):
        token = match.group()
        kind = match.lastgroup
        role = ''
        if kind in ('string', 'number'):
            role = 'literal'
        elif kind == 'name' and '.' not in token and token.isupper():
            following = value[match.end() : match.end() + 1]
            if token in _KEYWORDS or following == '(':
                role = 'literal' if token in ('TRUE', 'FALSE', 'NULL') else 'keyword'
        if out.parts and out.parts[-1][1] == role:
            out.parts[-1] = (out.parts[-1][0] + token, role)
        else:
            out.append((token, role))
    return out


def formula(style: Style, value: str, *, indent: int = 2, lead: str = '= ') -> list[Text]:
    """``= <sql>``, highlighted, wrapped under itself on a terminal."""
    first = Text(' ' * indent, (lead, 'dim'))
    return wrap(style, sql(value), first=first, hanging=indent + len(lead))
