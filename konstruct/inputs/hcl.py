"""A reader for the part of HCL (Terraform's configuration language) that resource arguments use.

Blocks, attributes, strings, numbers, booleans, lists, objects and heredocs are read as values.
Anything that Terraform would evaluate (a reference such as ``fivetran_connector.pg.id``, a
function call, a conditional, a ``for`` expression) is kept as a :class:`Ref` holding its text, so
nothing is evaluated and an unusual expression never stops the file from being read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


class HclError(ValueError):
    """The text is not HCL that this reader understands."""

    def __init__(self, message: str, line: int) -> None:
        super().__init__(f'line {line}: {message}')
        self.line = line


@dataclass(frozen=True)
class Ref:
    """An expression Terraform evaluates, kept as its text."""

    text: str


@dataclass
class Block:
    """A block such as ``resource "fivetran_connector" "pg" { ... }``."""

    type: str
    labels: list[str]
    line: int
    arguments: dict[str, Any] = field(default_factory=dict)
    """Its arguments written as attributes (``service = "postgres"``)."""
    blocks: list[Block] = field(default_factory=list)

    def block(self, type: str) -> Block | None:
        """The first nested block of a type."""
        return next((b for b in self.blocks if b.type == type), None)


_TOKEN = re.compile(
    r"""
    (?P<newline>\n)
  | (?P<space>[ \t\r]+)
  | (?P<comment>\#[^\n]*|//[^\n]*|/\*.*?\*/)
  | (?P<heredoc><<-?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)[ \t]*\n)
  | (?P<string>")
  | (?P<number>-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_-]*)
  | (?P<punct>==|!=|<=|>=|&&|\|\||=>|\.\.\.|[=:{}\[\](),.?!<>+\-*/%])
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    line: int
    value: Any = None
    start: int = 0
    end: int = 0


def _string(text: str, start: int, line: int) -> tuple[str, int]:
    """A quoted string from the character after its opening quote, with ``${...}`` kept as text
    (``$${`` is a literal ``${``); returns the string and the position after its closing quote."""
    out: list[str] = []
    i = start
    while i < len(text):
        char = text[i]
        if char == '"':
            return ''.join(out), i + 1
        if char == '\\' and i + 1 < len(text):
            escaped = text[i + 1]
            out.append({'n': '\n', 't': '\t', 'r': '\r'}.get(escaped, escaped))
            i += 2
            continue
        if text.startswith('$${', i) or text.startswith('%%{', i):
            out.append(text[i + 1 : i + 3])
            i += 3
            continue
        if text.startswith('${', i) or text.startswith('%{', i):
            depth, j = 0, i + 1
            while j < len(text):
                if text[j] == '{':
                    depth += 1
                elif text[j] == '}':
                    depth -= 1
                    if depth == 0:
                        break
                elif text[j] == '"':
                    _, j = _string(text, j + 1, line)
                    continue
                j += 1
            out.append(text[i : j + 1])
            i = j + 1
            continue
        if char == '\n':
            raise HclError('unterminated string', line)
        out.append(char)
        i += 1
    raise HclError('unterminated string', line)


def _tokens(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    position, line = 0, 1
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise HclError(f'unexpected {text[position]!r}', line)
        kind = match.lastgroup or ''
        if kind == 'tag':
            kind = 'heredoc'
        if kind == 'string':
            value, end = _string(text, match.end(), line)
            tokens.append(_Token('string', text[position:end], line, value, position, end))
            position = end
            continue
        if kind == 'heredoc':
            tag = match.group('tag')
            closing = re.compile(rf'^[ \t]*{re.escape(tag)}[ \t]*$', re.MULTILINE)
            found = closing.search(text, match.end())
            if found is None:
                raise HclError(f'heredoc {tag} is not closed', line)
            body = text[match.end() : found.start()]
            tokens.append(_Token('string', text[position : found.end()], line, body, position, found.end()))
            line += text.count('\n', position, found.end())
            position = found.end()
            continue
        token_text = match.group(0)
        if kind == 'comment':
            line += token_text.count('\n')
        elif kind != 'space':
            tokens.append(_Token(kind, token_text, line, None, position, match.end()))
            line += kind == 'newline'
        position = match.end()
    tokens.append(_Token('end', '', line, None, len(text), len(text)))
    return tokens


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _tokens(text)
        self.i = 0

    def peek(self, skip_newlines: bool = False) -> _Token:
        if skip_newlines:
            while self.tokens[self.i].kind == 'newline':
                self.i += 1
        return self.tokens[self.i]

    def take(self) -> _Token:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def expect(self, text: str) -> _Token:
        token = self.take()
        if token.text != text:
            raise HclError(f'expected {text!r}, found {token.text or "the end"!r}', token.line)
        return token

    def body(self, closing: str | None) -> tuple[dict[str, Any], list[Block]]:
        attrs: dict[str, Any] = {}
        blocks: list[Block] = []
        while True:
            token = self.peek(skip_newlines=True)
            if token.kind == 'end' or (closing and token.text == closing):
                if closing:
                    if token.kind == 'end':
                        raise HclError(f'expected {closing!r}', token.line)
                    self.take()
                return attrs, blocks
            if token.kind not in ('name', 'string'):
                raise HclError(f'unexpected {token.text!r}', token.line)
            self.take()
            name = token.value if token.kind == 'string' else token.text
            following = self.peek()
            if following.text == '=':
                self.take()
                attrs[name] = self.expression(stop_at_newline=True)
                continue
            labels: list[str] = []
            while self.peek().kind in ('string', 'name'):
                label = self.take()
                labels.append(label.value if label.kind == 'string' else label.text)
            self.expect('{')
            inner_attrs, inner_blocks = self.body('}')
            blocks.append(Block(name, labels, token.line, inner_attrs, inner_blocks))

    def expression(self, stop_at_newline: bool) -> Any:
        """One expression; an operator after a literal makes the whole expression a Ref."""
        start = self.i
        value = self.primary()
        following = self.peek()
        ends = {'newline', 'end'} if stop_at_newline else {'end'}
        if (
            following.kind in ends
            or following.text in (',', '}', ']', ')')
            or (not stop_at_newline and following.kind == 'newline')
        ):
            return value
        self.skip_to_end(stop_at_newline)
        return Ref(self.source(start))

    def skip_to_end(self, stop_at_newline: bool) -> None:
        depth = 0
        while True:
            token = self.peek()
            if token.kind == 'end':
                return
            if depth == 0 and (
                token.text in (',', '}', ']', ')') or (token.kind == 'newline' and stop_at_newline)
            ):
                return
            if token.text in ('(', '[', '{'):
                depth += 1
            elif token.text in (')', ']', '}'):
                depth -= 1
            self.take()

    def source(self, start: int) -> str:
        if self.i <= start:
            return ''
        return self.text[self.tokens[start].start : self.tokens[self.i - 1].end].strip()

    def primary(self) -> Any:
        token = self.peek(skip_newlines=True)
        if token.kind == 'string':
            self.take()
            return token.value
        if token.kind == 'number':
            self.take()
            return float(token.text) if any(c in token.text for c in '.eE') else int(token.text)
        if token.text == '[':
            return self.sequence()
        if token.text == '{':
            return self.mapping()
        if token.text in ('(', '-', '!'):
            start = self.i
            self.take()
            self.skip_to_end(stop_at_newline=False)
            if self.peek().text == ')':
                self.take()
            return Ref(self.source(start))
        if token.kind == 'name':
            if token.text in ('true', 'false'):
                self.take()
                return token.text == 'true'
            if token.text == 'null':
                self.take()
                return None
            start = self.i
            self.take()
            if token.text == 'jsonencode' and self.peek().text == '(':
                # The value a resource encodes as JSON is still a literal worth reading.
                self.take()
                value = self.expression(stop_at_newline=False)
                if self.peek(skip_newlines=True).text == ')':
                    self.take()
                    return value
                self.i = start + 1
            while True:
                following = self.peek()
                if following.text == '.':
                    self.take()
                    if self.peek().kind == 'end':
                        raise HclError('expected a name after .', following.line)
                    self.take()
                elif following.text in ('[', '('):
                    self.group()
                else:
                    break
            return Ref(self.source(start))
        raise HclError(f'unexpected {token.text or "end of file"!r}', token.line)

    def group(self) -> None:
        """Skip a bracketed group (a call's arguments or an index)."""
        depth = 0
        while True:
            token = self.take()
            if token.kind == 'end':
                raise HclError('unclosed bracket', token.line)
            if token.text in ('(', '[', '{'):
                depth += 1
            elif token.text in (')', ']', '}'):
                depth -= 1
                if depth == 0:
                    return

    def sequence(self) -> Any:
        start = self.i
        self.expect('[')
        if self.peek(skip_newlines=True).text == 'for':
            self.i = start
            self.group()
            return Ref(self.source(start))
        items: list[Any] = []
        while True:
            token = self.peek(skip_newlines=True)
            if token.text == ']':
                self.take()
                return items
            items.append(self.expression(stop_at_newline=False))
            separator = self.peek(skip_newlines=True)
            if separator.text == ',':
                self.take()
            elif separator.text != ']':
                raise HclError(f'expected , or ] in a list, found {separator.text!r}', separator.line)

    def mapping(self) -> Any:
        start = self.i
        self.expect('{')
        if self.peek(skip_newlines=True).text == 'for':
            self.i = start
            self.group()
            return Ref(self.source(start))
        items: dict[str, Any] = {}
        while True:
            token = self.peek(skip_newlines=True)
            if token.text == '}':
                self.take()
                return items
            if token.kind == 'string':
                key = str(token.value)
            elif token.kind in ('name', 'number'):
                key = token.text
            else:
                raise HclError(f'unexpected {token.text!r} as an object key', token.line)
            self.take()
            separator = self.take()
            if separator.text not in ('=', ':'):
                raise HclError(f'expected = after {key!r}', separator.line)
            items[key] = self.expression(stop_at_newline=True)
            following = self.peek()
            if following.text == ',':
                self.take()


def parse(text: str) -> list[Block]:
    """The top-level blocks of an HCL file.

    Raises:
        HclError: The text is not HCL this reader understands.
    """
    _, blocks = _Parser(text).body(None)
    return blocks
