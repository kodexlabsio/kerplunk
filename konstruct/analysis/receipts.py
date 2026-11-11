"""Answer receipts: what an answer about the data rests on, and whether it still holds.

When an agent, or a person, answers a question from the graph ("customer 3's lifetime value is
120"), ``konstruct receipts issue`` records what the answer rests on, so that ``konstruct receipts
check`` can say later whether that still holds. For each column the answer names (directly, or
through a relation, an exposure or a business term), a receipt keeps the pin the lineage lockfile
(:mod:`konstruct.analysis.lock`) makes: the formula composed down to source columns in canonical
form, the joins, filters and grouping that decide which rows feed it, the source columns it
reaches, its type, the business terms bound to it with their status, owner, approval date and a
version of their meaning, and the verification state of its lineage. It also keeps every term the
answer names and every term bound to the relations of its columns, as they were, and the build it
answered from (a SHA-256 of ``graph.json``, when it was built and by which Konstruct), the commit
checked out when it was issued, the inputs changed since the build, and the question and answer
when they are given.

``konstruct receipts check`` compares every receipt with the current graph as the lockfile check
compares pins: a receipt is stale when a column it rests on was removed, or its formula,
conditions, sources or type changed, or an approved term it rests on changed its meaning or status,
is no longer bound or is no longer defined; with git history, the change names the commit that
made it. A rebuild that changes none of these leaves a receipt holding, however much else changed.
What changed without making a receipt stale (a lost verification, a draft term reworded) is a note.
A graph older than its inputs cannot say a receipt holds: such a receipt is unchecked.

A receipt claims only what Konstruct recorded: a formula is derived from the SQL, not proven by
running it, and the lineage is marked confirmed only where ``konstruct verify`` confirmed it. That a
receipt holds says its basis is unchanged, not that the answer was right.

Receipts are JSON lines in ``konstruct-out/receipts/``, one file per day (UTC) of issue, so old ones
can be removed a day at a time. Each line is one receipt, with a ``format_version`` and a
``digest`` (see :func:`digest`): a record edited by hand, or damaged, no longer matches its digest
and fails the check as tampered. The digest is not a signature: whoever can write the file can
write a new record with a matching digest, and a removed line is not detected.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from konstruct.analysis.diff import run_git
from konstruct.analysis.lock import (
    CANONICAL,
    Attributor,
    Lockfile,
    Malformed,
    Pin,
    PinChange,
    PinnedTerm,
    Pinner,
    display_namer,
    expand_ref,
    find,
    optional_text,
    pinned_term,
    plural,
    relative_path,
    term_changes,
)
from konstruct.analysis.query import QueryError, resolve_one
from konstruct.core import jsonio
from konstruct.core.formats import FIELD, FIRST, FormatError, document
from konstruct.core.formats import check as check_format
from konstruct.core.graph import RELATION_KINDS, Graph, Node

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style

DIRECTORY = 'receipts'
"""The folder of ``konstruct-out`` that holds the receipts."""

Status = Literal['holds', 'stale', 'unchecked', 'tampered', 'unreadable']
STATUSES: tuple[Status, ...] = ('holds', 'stale', 'unchecked', 'tampered', 'unreadable')

_KINDS = 'a column, a relation, an exposure or a business term'
_VERIFIED = {
    'confirmed': 'lineage confirmed by konstruct verify',
    'partly confirmed': 'lineage partly confirmed by konstruct verify',
    'not exercised': 'lineage not exercised by konstruct verify',
}
_UNVERIFIED = 'lineage not checked against the data'
# What a stored pin and term may hold. A field a later release adds is ignored, as a reader of
# any Konstruct document ignores fields it does not know.
_PIN_KEYS = frozenset({f.name for f in fields(Pin)} - {'rows'} | {'relations'})
_TERM_KEYS = frozenset(f.name for f in fields(PinnedTerm))
# Open a file itself, never what a symbolic link names, and never wait on a FIFO. Windows has
# neither flag, nor symbolic links an unprivileged process can make; the lstat before each open
# refuses them there.
_NO_FOLLOW = getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)


class ReceiptError(QueryError):
    """A receipt that cannot be issued, found or read."""


@dataclass
class Build:
    """The build a receipt answered from, and the project when it was issued."""

    graph: str
    """``sha256:`` and the SHA-256 of ``graph.json``, which names the exact graph read."""
    generated: str | None = None
    """When the graph was built."""
    konstruct: str | None = None
    """The Konstruct release that built it."""
    commit: str | None = None
    """The commit checked out when the receipt was issued; None outside git."""
    uncommitted: bool | None = None
    """Whether tracked files had uncommitted changes then; None outside git."""
    changed_inputs: list[str] = field(default_factory=list)
    """Inputs the build read that had changed since it ran, when the receipt was issued: the
    answer came from a graph older than these."""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [])}

    @classmethod
    def from_dict(cls, data: Any) -> Build:
        if not isinstance(data, dict) or not isinstance(data.get('graph'), str):
            raise Malformed("'build' must be an object with the graph's 'graph' digest")
        changed = data.get('changed_inputs', [])
        uncommitted = data.get('uncommitted')
        if not isinstance(changed, list) or not all(isinstance(c, str) for c in changed):
            raise Malformed("'changed_inputs' must be a list of strings")
        if uncommitted is not None and not isinstance(uncommitted, bool):
            raise Malformed("'uncommitted' must be true or false")
        return cls(
            graph=data['graph'],
            generated=_string(data, 'generated'),
            konstruct=_string(data, 'konstruct'),
            commit=_string(data, 'commit'),
            uncommitted=uncommitted,
            changed_inputs=list(changed),
        )


@dataclass
class Receipt:
    """What one answer rests on, as it was when the receipt was issued."""

    id: str
    """``r-`` and 12 hex digits."""
    issued: str
    """When it was issued, in UTC, ISO 8601, to the second."""
    refs: list[str]
    """What the answer rests on, as named: columns, relations, exposures or business terms."""
    build: Build
    """The build it answered from."""
    citation: str
    """One paragraph to put under the answer: each column with its formula and verification
    state, each term with its status, and the receipt's id."""
    question: str | None = None
    """The question answered, when given."""
    answer: str | None = None
    """The answer given, when given."""
    canonical: str = CANONICAL
    """The canonical form the formulas and conditions were printed in, as the lockfile records it."""
    pins: list[Pin] = field(default_factory=list)
    """Each column the answer rests on, pinned as the lineage lockfile pins it, sorted by column."""
    terms: list[PinnedTerm] = field(default_factory=list)
    """Every business term named in ``refs``, and every term bound to the relation of a column the
    answer rests on, as they were, sorted by name."""
    digest: str = ''
    """``sha256:`` and the SHA-256 of the rest of the record (see :func:`digest`)."""

    def to_dict(self) -> dict[str, Any]:
        # Stored as a lockfile stores pins: each relation's conditions once, named by the pins.
        pins, relations = Lockfile(pins=self.pins, canonical=self.canonical).stored()
        data: dict[str, Any] = {
            'id': self.id,
            'issued': self.issued,
            'refs': self.refs,
            'question': self.question,
            'answer': self.answer,
            'citation': self.citation,
            'build': self.build.to_dict(),
            'canonical': self.canonical,
            'pins': [p.to_dict(references) for p, references in pins],
            'relations': [{'name': n, 'conditions': c} for n, c in sorted(relations.items())],
            'terms': [t.to_dict() for t in self.terms],
            'digest': self.digest,
        }
        return {k: v for k, v in data.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Receipt:
        """A receipt as it is stored. Fields it does not know, which a later release may add,
        are ignored.

        Raises:
            ReceiptError: ``data`` is not a receipt.
        """
        try:
            relations: dict[str, list[str]] = {}
            stored = data.get('relations', [])
            if not isinstance(stored, list) or not all(isinstance(r, dict) for r in stored):
                raise Malformed("'relations' must be a list of objects")
            for relation in stored:
                conditions = relation.get('conditions')
                if not isinstance(relation.get('name'), str) or not isinstance(conditions, list):
                    raise Malformed("a relation needs a 'name' and its 'conditions'")
                relations[relation['name']] = [str(c) for c in conditions]
            pins, terms, refs = data.get('pins', []), data.get('terms', []), data.get('refs')
            if not isinstance(pins, list) or not isinstance(terms, list):
                raise Malformed("'pins' and 'terms' must be lists")
            if not all(isinstance(p, dict) for p in pins):
                raise Malformed('a pin must be an object')
            if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
                raise Malformed("'refs' must be a list of strings")
            for key in ('id', 'issued', 'citation', 'digest'):
                if not isinstance(data.get(key), str):
                    raise Malformed(f'{key!r} must be a string')
            return cls(
                id=data['id'],
                issued=data['issued'],
                refs=list(refs),
                build=Build.from_dict(data.get('build')),
                citation=data['citation'],
                question=_string(data, 'question'),
                answer=_string(data, 'answer'),
                canonical=_string(data, 'canonical') or CANONICAL,
                pins=[Pin.from_dict(_pin(p), relations) for p in pins],
                terms=[PinnedTerm.from_dict(_known(t, _TERM_KEYS)) for t in terms],
                digest=data['digest'],
            )
        except Malformed as error:
            raise ReceiptError(f'not a receipt: {error}') from None

    def text(self, style: Style | None = None) -> str:
        """The receipt as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import receipt_lines, show

        return show(receipt_lines, self, style)

    def __str__(self) -> str:
        return self.text()


@dataclass
class CheckedReceipt:
    """One receipt, checked against the current graph."""

    id: str
    """The receipt's id, or for a line that is not a receipt, where it is."""
    status: Status
    """``holds`` (everything it rests on is as it was), ``stale`` (something changed),
    ``unchecked`` (the graph is older than its inputs, so it cannot say), ``tampered`` (the record
    does not match its digest) or ``unreadable``."""
    file: str
    """The file the receipt is in, relative to the project."""
    line: int
    """Its line in the file."""
    issued: str | None = None
    """When it was issued."""
    refs: list[str] = field(default_factory=list)
    """What it rests on, as named."""
    question: str | None = None
    """The question it answered, when it was given."""
    changes: list[PinChange] = field(default_factory=list)
    """Each column it rests on that changed, with what changed and, from git history, who
    changed it."""
    terms: list[str] = field(default_factory=list)
    """Each change to an approved term it rests on other than through a column's pin."""
    notes: list[str] = field(default_factory=list)
    """What changed without making it stale: a verification lost, a draft term reworded."""
    rebuilt: bool = False
    """Whether the graph is another build than the one it answered from."""
    problem: str | None = None
    """For a receipt that is unchecked, tampered or unreadable, why."""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            'id': self.id,
            'status': self.status,
            'file': self.file,
            'line': self.line,
            'issued': self.issued,
            'refs': self.refs,
            'question': self.question,
            'changes': [_change(c) for c in self.changes],
            'terms': self.terms,
            'notes': self.notes,
            'rebuilt': self.rebuilt,
            'problem': self.problem,
        }
        return {k: v for k, v in data.items() if v not in (None, [])}


@dataclass
class ReceiptCheck:
    """``konstruct receipts check``: whether what each answer rests on still holds."""

    directory: str
    """The folder of receipts, relative to the project."""
    receipts: list[CheckedReceipt] = field(default_factory=list)
    """Every receipt checked, in the order issued."""
    changed_inputs: list[str] = field(default_factory=list)
    """Inputs changed since the graph was built: while there are any, no receipt is said to
    hold."""

    @property
    def passed(self) -> bool:
        """True when every receipt checked holds."""
        return all(r.status == 'holds' for r in self.receipts)

    def counts(self) -> dict[str, int]:
        """How many receipts have each status."""
        found: dict[str, int] = dict.fromkeys(STATUSES, 0)
        for receipt in self.receipts:
            found[receipt.status] += 1
        return found

    def summary(self) -> str:
        if not self.receipts:
            return f'No receipts in {self.directory}.'
        counts = self.counts()
        parts = [f'{n} {status}' for status, n in counts.items() if n and status != 'holds']
        total = plural(len(self.receipts), 'receipt')
        if not parts:
            return f'{total} checked: every one holds.'
        held = f', {counts["holds"]} {"holds" if counts["holds"] == 1 else "hold"}' if counts['holds'] else ''
        return f'{total} checked: {", ".join(parts)}{held}.'

    def to_dict(self) -> dict[str, Any]:
        data = {
            'directory': self.directory,
            'passed': self.passed,
            'summary': self.summary(),
            'counts': self.counts(),
            'receipts': [r.to_dict() for r in self.receipts],
            'changed_inputs': self.changed_inputs,
        }
        return {k: v for k, v in data.items() if v != []}

    def text(self, style: Style | None = None) -> str:
        """The check as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import receipt_check_lines, show

        return show(receipt_check_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def issue(
    graph: Graph,
    root: Path,
    directory: Path,
    refs: list[str],
    *,
    graph_file: Path,
    question: str | None = None,
    answer: str | None = None,
    write: bool = True,
) -> Receipt:
    """A receipt for an answer that rests on ``refs``, appended to today's file in ``directory``.

    Args:
        graph: The graph the answer came from.
        root: The project root, for git and for the files that build columns with no formula.
        directory: The folder of receipts.
        refs: Columns, relations (each of their columns), exposures (the columns they read) or
            business terms (the columns they define, and the term itself).
        graph_file: The ``graph.json`` that ``graph`` was read from, whose digest names the build.
        question: The question answered, recorded as given.
        answer: The answer given, recorded as given.
        write: False to make the receipt without recording it.

    Raises:
        ReceiptError: ``refs`` is empty, names something an answer cannot rest on, or holds text
            that is not valid Unicode, or the receipt cannot be written.
        konstruct.QueryError: A name matches nothing or several things.
    """
    if not refs:
        raise ReceiptError('name what the answer rests on: columns, relations, exposures or terms')
    for what, value in (('question', question), ('answer', answer), *(('name', r) for r in refs)):
        if value is not None:
            _encode(value, what)
    root = Path(root)
    pinner = Pinner(graph, root, None)
    columns: dict[str, Node] = {}
    terms: dict[str, PinnedTerm] = {}
    for ref in refs:
        node = resolve_one(graph, ref)
        if node.kind not in RELATION_KINDS | {'column', 'exposure', 'term'}:
            raise ReceiptError(f'{ref!r} is a {node.kind}; an answer rests on {_KINDS}')
        if node.kind == 'term':
            terms.setdefault(node.name, pinned_term(node))
        for column in expand_ref(graph, ref):
            columns.setdefault(column.id, column)
    for column in columns.values():
        # A term bound to a column's relation, rather than to the column, is part of its meaning.
        owners = {o.id: o for o in (graph.parent_of(column), graph.parent_of(pinner.defining(column))) if o}
        for owner in owners.values():
            for term in _bound(graph, owner):
                terms.setdefault(term.name, pinned_term(term))
    pins = sorted((pinner.pin(c) for c in columns.values()), key=lambda p: p.column)
    receipt = Receipt(
        id=f'r-{secrets.token_hex(6)}',
        issued=dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        refs=list(refs),
        build=_build(graph, root, graph_file),
        citation='',
        question=question or None,
        answer=answer or None,
        pins=pins,
        terms=[terms[name] for name in sorted(terms)],
    )
    labels = {p.column: graph.label(columns[p.id]) for p in pins}
    receipt.citation = _citation(receipt, labels, display_namer(graph))
    data = document(receipt)
    receipt.digest = data['digest'] = digest(data)
    if write:
        _append(directory, f'{receipt.issued[:10]}.jsonl', _encode(json.dumps(data, **_JSON) + '\n'))
    return receipt


_JSON: dict[str, Any] = {'ensure_ascii': False, 'separators': (',', ':')}


def digest(data: dict[str, Any]) -> str:
    """The digest of a stored receipt: ``sha256:`` and the SHA-256 of the UTF-8 bytes of every
    field but ``digest`` as JSON with keys sorted, ``,`` and ``:`` as separators with no spaces,
    and every character written as itself rather than escaped.

    Raises:
        ReceiptError: The receipt holds text that is not valid Unicode (a lone surrogate).
    """
    body = {k: v for k, v in data.items() if k != 'digest'}
    return 'sha256:' + hashlib.sha256(_encode(json.dumps(body, sort_keys=True, **_JSON))).hexdigest()


def read(directory: Path, ids: list[str] | None = None, *, root: Path) -> list[Receipt]:
    """The receipts in ``directory`` that can be read and match their digests, in the order
    issued, or those ``ids`` name (each an id or the start of one). ``root`` is the project, which
    errors name files relative to.

    Raises:
        ReceiptError: An id names no receipt, or several, or a receipt it names does not match its
            digest or cannot be read.
    """
    records = _records(directory, root)
    if not ids:
        return [r.receipt for r in records if r.receipt is not None]
    found = []
    for record in _select(records, ids, directory, root):
        if record.receipt is None:
            where = relative_path(record.file, root)
            raise ReceiptError(
                f'{record.id} ({where}, line {record.line}) is {record.status}: {record.problem}'
            )
        found.append(record.receipt)
    return found


def check(
    graph: Graph,
    root: Path,
    directory: Path,
    ids: list[str] | None = None,
    *,
    graph_file: Path,
    history: bool = True,
    changed_inputs: list[str] | None = None,
) -> ReceiptCheck:
    """Compare every receipt in ``directory`` (or those ``ids`` name) with ``graph``.

    ``history`` False skips reading git history to say who changed what. ``changed_inputs`` are
    the inputs changed since ``graph`` was built: with any, a receipt nothing in the graph changed
    is unchecked rather than holding.

    Raises:
        ReceiptError: An id names no receipt, or several.
    """
    root = Path(root)
    records = _records(directory, root)
    if ids:
        records = _select(records, ids, directory, root, every=True)
    result = ReceiptCheck(directory=relative_path(directory, root), changed_inputs=list(changed_inputs or []))
    if not records:
        return result
    pinner = Pinner(graph, root, None)
    display = display_namer(graph)
    current = _graph_digest(graph_file)
    named = {n.name: n for n in graph.nodes.values() if n.kind == 'term'}
    # One attributor for every receipt, so the git history of a column is read once.
    attribute = Attributor(graph, root, pinner, None) if history else None
    for record in records:
        receipt = record.receipt
        checked = CheckedReceipt(
            id=record.id,
            status=record.status,
            file=relative_path(record.file, root),
            line=record.line,
            problem=record.problem,
        )
        result.receipts.append(checked)
        if receipt is None:
            continue
        checked.issued, checked.refs, checked.question = receipt.issued, receipt.refs, receipt.question
        checked.rebuilt = receipt.build.graph != current
        if attribute is not None:
            # Changes made since it was issued, or in its second by a commit other than the one
            # checked out then.
            attribute.since = (receipt.issued, receipt.build.commit or '')
        for old in receipt.pins:
            node = find(graph, old.column, old.id)
            change, _ = pinner.compare(old, node, display, checked.notes, when='issued')
            if change is None:
                continue
            if attribute is not None:
                change.changed_by = attribute(change, old, node)
            checked.changes.append(change)
        compared = {t.name for p in receipt.pins for t in p.terms}
        for term in receipt.terms:
            if term.name in compared:
                continue
            node = named.get(term.name)
            if node is not None:
                checked.terms += term_changes([term], [pinned_term(node)], '', checked.notes, 'issued')
            elif term.status == 'approved':
                checked.terms.append(f'{term.name!r} is no longer defined')
            else:
                checked.notes.append(f'{term.name!r} is no longer defined (not approved when issued)')
        if receipt.canonical != CANONICAL:
            _set_aside_printing(checked, receipt.canonical)
        if checked.changes or checked.terms:
            checked.status = 'stale'
        elif result.changed_inputs:
            checked.status = 'unchecked'
            shown = ', '.join(result.changed_inputs[:3]) + (
                ' and more' if len(result.changed_inputs) > 3 else ''
            )
            checked.problem = (
                f'the graph is older than {plural(len(result.changed_inputs), "changed input")} ({shown}); '
                'run `konstruct build -u`, or check without `--no-refresh`, to know whether this holds'
            )
    return result


def _set_aside_printing(checked: CheckedReceipt, canonical: str) -> None:
    """Under a canonical form this Konstruct does not print, a formula or condition that reads
    differently may differ only in how it is printed: those differences are set aside, as the
    lockfile check sets them aside, and the rest are compared."""
    printed = sum(1 for change in checked.changes if change.unprint())
    checked.changes = [c for c in checked.changes if c.reasons]
    if printed:
        checked.notes.append(
            f'issued with canonical form {canonical}, which this Konstruct does not print ({CANONICAL}): '
            f'{plural(printed, "column")} read differently in this form and {"was" if printed == 1 else "were"} '
            'not compared on formula or conditions'
        )


# Reading and writing


@dataclass
class _Record:
    file: Path
    line: int
    id: str
    status: Status
    receipt: Receipt | None = None
    problem: str | None = None


def _records(directory: Path, root: Path) -> list[_Record]:
    """Every line of every receipts file, read and checked against its digest. Only regular files
    in a real directory are read: a symbolic link, a FIFO or a device in their place is reported,
    not followed."""
    if directory.is_symlink():
        problem = 'it is a symbolic link, which is not followed'
        return [_Record(directory, 0, relative_path(directory, root), 'unreadable', problem=problem)]
    found: list[_Record] = []
    for path in sorted(directory.glob('*.jsonl')) if directory.is_dir() else []:
        content = _content(path)
        if isinstance(content, str):
            found.append(_Record(path, 0, path.name, 'unreadable', problem=content))
            continue
        # Lines end at a newline alone: U+2028, U+2029 and NEL in recorded text are text.
        for number, raw in enumerate(content.split(b'\n'), 1):
            if raw.strip():
                found.append(_record(path, number, raw))
    return found


def _content(path: Path) -> bytes | str:
    """The bytes of ``path`` when it is a regular file, or why it is not read."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return 'not a regular file, so not read'
        handle = os.open(path, os.O_RDONLY | _NO_FOLLOW)
    except OSError as error:
        return f'cannot be read: {error.strerror}'
    with os.fdopen(handle, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return 'not a regular file, so not read'
        return stream.read()


def _record(path: Path, number: int, raw: bytes) -> _Record:
    where = f'{path.name}:{number}'
    try:
        data = json.loads(raw.decode('utf-8'))
    except UnicodeDecodeError as error:
        return _Record(path, number, where, 'unreadable', problem=f'not UTF-8: {error.reason}')
    except ValueError as error:
        return _Record(path, number, where, 'unreadable', problem=f'not JSON: {error}')
    name = data.get('id') if isinstance(data, dict) else None
    shown = name if isinstance(name, str) and name else where
    try:
        check_format(data, FIRST, f'the receipt at {where}')
        matches = data.get('digest') == digest(data)
    except (FormatError, ReceiptError) as error:
        return _Record(path, number, shown, 'unreadable', problem=str(error))
    if not matches:
        problem = 'it does not match its digest: it was changed after it was issued'
        return _Record(path, number, shown, 'tampered', problem=problem)
    try:
        receipt = Receipt.from_dict({k: v for k, v in data.items() if k != FIELD})
    except ReceiptError as error:
        return _Record(path, number, shown, 'unreadable', problem=str(error))
    return _Record(path, number, receipt.id, 'holds', receipt=receipt)


def _append(directory: Path, name: str, line: bytes) -> None:
    """Add ``line`` to the file ``name`` in ``directory``, which is made a real directory, never
    writing through a symbolic link."""
    folder = jsonio.real_directory(directory.parent, directory.name)
    path = folder / name
    try:
        if path.is_symlink():
            # One committed or planted here would send receipts elsewhere; it is removed, as a
            # link in place of the folder is.
            path.unlink()
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NO_FOLLOW, 0o644)
    except OSError as error:
        raise ReceiptError(f'cannot write {name}: {error.strerror}') from None
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise ReceiptError(
                f'{name} in {directory.name} is not a regular file; remove it to record receipts'
            )
        # O_APPEND puts each write at the end of the file. On a local POSIX file system a line
        # written in one call is not interleaved with another process's; a short write (a full
        # disk, a signal) is finished here, and another writer's line may then fall between the
        # two parts, which a check reports as unreadable lines.
        view = memoryview(line)
        while view:
            written = os.write(handle, view)
            if written <= 0:
                raise ReceiptError(f'cannot write {name}: nothing was written')
            view = view[written:]
    except OSError as error:
        raise ReceiptError(f'cannot write {name}: {error.strerror}') from None
    finally:
        os.close(handle)


def _select(
    records: list[_Record], ids: list[str], directory: Path, root: Path, *, every: bool = False
) -> list[_Record]:
    """The records ``ids`` name, each an id or the start of one. Several records can carry one id
    (a copy edited by hand): with ``every`` all of them, else the one that can be read, so a forged
    copy does not hide the receipt it copies."""
    chosen: list[_Record] = []
    for wanted in ids:
        matches = [r for r in records if r.id == wanted] or [r for r in records if r.id.startswith(wanted)]
        if not matches:
            raise ReceiptError(f'no receipt {wanted!r} in {relative_path(directory, root)}')
        if len({r.id for r in matches}) > 1:
            shown = ', '.join(sorted({r.id for r in matches})[:5])
            raise ReceiptError(f'{wanted!r} is the start of several receipts: {shown}')
        if not every:
            matches = [r for r in matches if r.receipt is not None][:1] or matches[:1]
        chosen += [r for r in matches if r not in chosen]
    return chosen


def _build(graph: Graph, root: Path, graph_file: Path) -> Build:
    from konstruct.engine.build import changed_inputs

    commit = run_git(root, 'rev-parse', '--verify', '-q', 'HEAD', check=False).strip() or None
    status = run_git(root, 'status', '--porcelain', '--untracked-files=no', check=False) if commit else ''
    return Build(
        graph=_graph_digest(graph_file),
        generated=optional_text(graph.meta.get('generated_at')),
        konstruct=optional_text(graph.meta.get('konstruct_version')),
        commit=commit,
        uncommitted=bool(status.strip()) if commit else None,
        changed_inputs=changed_inputs(graph_file, graph),
    )


def _graph_digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            sha.update(block)
    return 'sha256:' + sha.hexdigest()


# What a receipt says


def verification(pin: Pin) -> str:
    """What ``konstruct verify`` found for a pinned column's lineage, in words."""
    return _VERIFIED.get(pin.verified or '', _UNVERIFIED)


def term_summary(term: PinnedTerm) -> str:
    """A term's name, status, approval and owner, as a citation gives them."""
    facts = [term.status + (f' {term.approved}' if term.approved and term.status == 'approved' else '')]
    if term.owner:
        facts.append(f'owner {term.owner}')
    if term.formula:
        facts.append(f'formula {term.formula}')
    return f'{term.name!r} ({", ".join(facts)})'


def _citation(receipt: Receipt, labels: dict[str, str], display: Any) -> str:
    # The verification state is said once when every column shares it, else for each column.
    states = {verification(p) for p in receipt.pins}
    parts = []
    for pin in receipt.pins:
        label = labels.get(pin.column, pin.column)
        shown = f'{label} = {display.text(pin.formula)}' if pin.formula else f'{label} (a source column)'
        facts = [verification(pin)] if len(states) > 1 else []
        if not pin.complete:
            facts.append('formula incomplete: a column on the way has no recorded expression')
        parts.append(f'{shown} ({"; ".join(facts)})' if facts else shown)
    terms = {t.name: t for p in receipt.pins for t in p.terms} | {t.name: t for t in receipt.terms}
    parts += [f'term {term_summary(terms[name])}' for name in sorted(terms)]
    shared = f' {states.pop().capitalize()}.' if len(states) == 1 else ''
    return f'Relied on {"; ".join(parts)}.{shared} Receipt {receipt.id}.'


def _bound(graph: Graph, owner: Node) -> list[Node]:
    """The terms bound to a relation, rather than read by it as a measure's input."""
    return [
        graph.nodes[e.source]
        for e in graph.in_edges(owner.id, 'defines')
        if e.source in graph.nodes and graph.nodes[e.source].kind == 'term' and e.attrs.get('role') != 'input'
    ]


def _change(change: PinChange) -> dict[str, Any]:
    """A changed column as a check reports it: what the lockfile check gives, without the
    commands that accept or revert a lockfile's change."""
    data = {k: v for k, v in change.__dict__.items() if k != 'changed_by' and v not in (None, [])}
    if change.changed_by:
        data['changed_by'] = [a.to_dict() for a in change.changed_by]
    return data


def _pin(data: dict[str, Any]) -> dict[str, Any]:
    pin: dict[str, Any] = _known(data, _PIN_KEYS)
    if isinstance(pin.get('terms'), list):
        pin['terms'] = [_known(t, _TERM_KEYS) for t in pin['terms']]
    return pin


def _known(data: Any, keys: frozenset[str]) -> Any:
    return {k: v for k, v in data.items() if k in keys} if isinstance(data, dict) else data


def _encode(text: str, what: str = 'receipt') -> bytes:
    """``text`` as UTF-8. A lone surrogate, which a command line or a JSON escape can carry, has
    no UTF-8 form."""
    try:
        return text.encode('utf-8')
    except UnicodeEncodeError as error:
        raise ReceiptError(
            f'the {what} is not valid text: it holds U+{ord(text[error.start]):04X}, half of a pair that '
            'makes one character'
        ) from None


def _string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise Malformed(f'{key!r} must be a string')
    return value or None


__all__ = [
    'DIRECTORY',
    'STATUSES',
    'Build',
    'CheckedReceipt',
    'Receipt',
    'ReceiptCheck',
    'ReceiptError',
    'check',
    'digest',
    'issue',
    'read',
    'term_summary',
    'verification',
]
