"""How the business's meaning changed, commit by commit: ``konstruct timeline``.

The world (:mod:`konstruct.semantics.world`) is built at each commit of a range and compared with
the world at the commit before it (:mod:`konstruct.semantics.world_facts`): the entities,
relationships, properties and metrics that were added, removed, renamed or changed, a property's or
a metric's composed formula, the joins, filters and grouping a metric is computed over, its
required filters, how far each is trusted (its confidence and what ``konstruct verify`` found) and
its approvals (status, owner and approval date), each with the commit's author, co-authors and date
and in plain English (:mod:`konstruct.analysis.english`). A commit that changes nothing of that,
such as one that only reformats SQL, has no entry. Commits are taken in the order of history, and an
approval is credited to a change only when the approving commit follows it, as
:mod:`konstruct.analysis.history` decides.

Each world is the build of the commit's own files (:mod:`konstruct.engine.snapshot`), with dbt's
manifest made from today's to match them (:mod:`konstruct.inputs.dbt_commit`). Nothing runs dbt,
connects to a warehouse or runs a plugin. What ``konstruct verify`` found of keys and relationships
in today's data holds at every commit. Worlds are kept in the user's cache, by commit, and the
builds share one analysis cache.

The timeline reads only git and files. :func:`effects` adds what each metric's change of formula or
rows does on today's data, through ``konstruct verify --effect`` (:mod:`konstruct.analysis.effect`).
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import logging
import secrets
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlglot.dialects.dialect import Dialect

from konstruct.analysis.diff import resolve_ref, run_git
from konstruct.analysis.english import (
    Condition,
    Names,
    Wording,
    column_keys,
    parse,
    unrepeated,
    word_condition,
)
from konstruct.analysis.history import Commit, Repository
from konstruct.analysis.release_notes import commit_range
from konstruct.core import jsonio
from konstruct.core.graph import Graph
from konstruct.engine import snapshot
from konstruct.inputs import dbt
from konstruct.semantics.world_facts import KINDS, Formulas, World, WorldChange, world_diff, world_of

if TYPE_CHECKING:
    from konstruct.analysis.verify import Engine
    from konstruct.engine.build import Cache
    from konstruct.semantics.ontology import Ontology

__all__ = [
    'KINDS',
    'Entry',
    'Timeline',
    'World',
    'WorldChange',
    'effects',
    'sentence',
    'timeline',
    'word',
    'world_diff',
    'world_of',
]

log = logging.getLogger('konstruct')

FORMAT_VERSION = 1
"""The version of the timeline's JSON document and of the worlds it caches."""
CACHE_BYTES = 1 << 30
"""How large the cache may grow: past it, the worlds built longest ago go first."""
ANALYSES = 4
"""How many projects' analysis caches it keeps."""
BACK = 20
"""How many commits back along first parents a commit is compared with when its parent's world
could not be built."""
_TRUST = {'inferred': 0, 'declared': 1, 'verified': 2}


# Plain English


def _plural(count: int, noun: str) -> str:
    return f'{count} {noun}' + ('' if count == 1 else 's')


def _quoted(value: Any) -> str:
    return 'none' if value in (None, '', []) else str(value)


def _trust(before: Any, after: Any) -> str:
    if before is None:
        return f'trusted as {after}'
    if after is None:
        return f'no longer trusted as {before}'
    direction = 'rose' if _TRUST.get(str(after), 0) > _TRUST.get(str(before), 0) else 'fell'
    return f'trust {direction} from {before} to {after}'


def _check(before: Any, after: Any) -> str:
    if after is None:
        return f'konstruct verify no longer checks it (it was {before})'
    if before is None:
        return f'konstruct verify {after} it'
    return f'konstruct verify now finds it {after} (it was {before})'


class _Wording:
    """The English of one change's formulas, filters and conditions, with one set of names for all
    of them."""

    def __init__(self, dialect: str | None, sql: Iterable[str], conditions: Iterable[str] = ()) -> None:
        self.name = dialect
        self.sql = list(sql)
        self.texts = list(conditions)
        self.wording: Wording | None = None
        self.formulas: Callable[[str], str | None] = lambda digest: None

    def _words(self) -> Wording:
        # Parsed only when something is worded: most of a first commit's many additions are not.
        if self.wording is None:
            self.dialect = Dialect.get_or_raise(self.name)
            self.trees = {text: parse(text, self.dialect) for text in self.sql}
            self.conditions = {text: Condition(text, self.dialect) for text in self.texts}
            found = [t for t in self.trees.values() if t is not None]
            found += [t for c in self.conditions.values() for t in c.trees]
            self.wording = Wording(self.name, Names(column_keys(found)))
        return self.wording

    def __call__(self, text: str) -> str:
        wording = self._words()
        tree = self.trees.get(text)
        return wording.sentence(tree) if tree is not None else f'`{text}`'

    def condition(self, text: str) -> str:
        """A condition (``WHERE: raw.orders.status <> 'void'``) in English."""
        wording = self._words()
        return word_condition(self.conditions.get(text) or Condition(text, self.dialect), wording)


def word(
    change: WorldChange, dialect: str | None, texts: Callable[[str], str | None] = lambda digest: None
) -> list[str]:
    """``change`` in plain English: one sentence for being added, removed or renamed, and one for
    each fact that changed. Also keeps the sentences by fact in ``change.worded``. ``texts`` gives
    the composed formula of a property's formula digest, where it is known."""
    facts = change.facts
    formulas = [str(v) for v in (*(change.fields.get('formula') or ()), facts.get('formula')) if v]
    formulas += [
        text
        for side in (change.before, change.after)
        if side
        for text in (texts(f['formula']) for f in side.get('formulas') or [])
        if text
    ]
    filters = [str(f) for side in (change.before, change.after) if side for f in side.get('filters') or []]
    rows = [str(r) for side in (change.before, change.after) if side for r in side.get('rows') or []]
    say = _Wording(dialect, formulas + filters, rows)
    say.formulas = texts
    out: list[str] = []
    if change.status == 'added':
        out.append(_added(change, say))
    elif change.status == 'removed':
        out.append(_removed(change))
    elif change.status == 'renamed':
        out.append(f'renamed from {change.renamed_from!r} to {change.name!r}')
    if change.status in ('renamed', 'changed'):
        for name, (before, after) in change.fields.items():
            change.worded[name] = _field(change.kind, name, before, after, say)
            out += change.worded[name]
        if 'filters' in change.fields:
            before, after = change.fields['filters']
            change.worded['note'] = [
                f'the required filter that keeps only rows where {say(str(f))} was removed'
                for f in before or []
                if f not in (after or [])
            ] + [
                f'a required filter that keeps only rows where {say(str(f))} was added'
                for f in after or []
                if f not in (before or [])
            ]
    return out


def _added(change: WorldChange, say: _Wording) -> str:
    facts = change.after or {}
    if change.kind == 'entity':
        return f'a new entity, identified by {facts.get("key")}, {_trust(None, facts.get("confidence"))}'
    if change.kind == 'relationship':
        cardinality = f', {facts["cardinality"]}' if facts.get('cardinality') else ''
        return (
            f'each {facts.get("source")} now refers to a {facts.get("target")} through {facts.get("via")}'
            f'{cardinality}, {_trust(None, facts.get("confidence"))}'
        )
    if change.kind == 'property':
        return f'{facts.get("entity")} has a new property, {facts.get("name")}'
    what = 'a proposed metric' if facts.get('proposed') else 'a new metric'
    text = f'{what}, {_trust(None, facts.get("confidence"))}'
    if facts.get('formula'):
        text += f': it is {say(str(facts["formula"]))}'
    for item in facts.get('filters') or []:
        text += f'; answers keep only rows where {say(str(item))}'
    return text


def _removed(change: WorldChange) -> str:
    facts = change.before or {}
    if change.kind == 'relationship':
        return f'each {facts.get("source")} no longer refers to a {facts.get("target")} through {facts.get("via")}'
    if change.kind == 'property':
        return f'{facts.get("entity")} no longer has the property {facts.get("name")}'
    return f'the {change.kind} is no longer found'


def _field(kind: str, name: str, before: Any, after: Any, say: _Wording) -> list[str]:
    if name == 'formula':
        if before and after:
            return [f'it was {say(str(before))}; it is now {say(str(after))}']
        return (
            [f'it is now {say(str(after))}']
            if after
            else ['it no longer has a formula it can be composed to']
        )
    if name == 'formulas':
        out = []
        olds = {f['column']: f['formula'] for f in before or []}
        news = {f['column']: f['formula'] for f in after or []}
        for column in sorted(set(olds) | set(news)):
            if olds.get(column) == news.get(column):
                continue
            was, now = (say.formulas(d) if d else None for d in (olds.get(column), news.get(column)))
            if column in olds and column in news and not (was and now):
                out.append(f'in {column}, the formula changed')  # one side was not composed
            elif was and now:
                out.append(f'in {column}, it was {say(str(was))}; it is now {say(str(now))}')
            elif now:
                out.append(f'in {column}, it is now {say(str(now))}')
            else:
                out.append(f'in {column}, it no longer has a formula it can be composed to')
        return out
    if name == 'filters':
        gone = [f for f in before or [] if f not in (after or [])]
        new = [f for f in after or [] if f not in (before or [])]
        return [f'answers no longer have to keep only rows where {say(str(f))}' for f in gone] + [
            f'answers now have to keep only rows where {say(str(f))}' for f in new
        ]
    if name == 'rows':
        gone = unrepeated(str(r) for r in before or [] if r not in (after or []))
        new = unrepeated(str(r) for r in after or [] if r not in (before or []))
        return [f'no longer: {say.condition(str(r))}' for r in gone] + [
            f'now: {say.condition(str(r))}' for r in new
        ]
    if name == 'confidence':
        return [_trust(before, after)]
    if name == 'check':
        return [_check(before, after)]
    if name == 'status':
        return [f'its status changed from {_quoted(before)} to {_quoted(after)}']
    if name == 'approved':
        return [f'approved on {after}' if after else f'its approval of {before} was withdrawn']
    if name == 'owner':
        return [f'now owned by {after}' if after else f'no longer owned by {before}']
    if name == 'additive':
        says = {True: 'adds up across groups', False: 'does not add up across groups'}
        return [
            f'it now {says[after]}'
            if after is not None
            else 'whether it adds up across groups is no longer known'
        ]
    if name == 'aggregation':
        return [f'it now aggregates with {after}' if after else f'it no longer aggregates with {before}']
    if name == 'refers_to':
        return [f'it now refers to {after}' if after else f'it no longer refers to {before}']
    if name == 'definition':
        return [f'its definition is now: {after}' if after else 'its definition was removed']
    if isinstance(before, list) or isinstance(after, list):
        gone = [str(v) for v in before or [] if v not in (after or [])]
        new = [str(v) for v in after or [] if v not in (before or [])]
        return [f'{name} no longer: {", ".join(gone)}'] * bool(gone) + [
            f'{name} now: {", ".join(new)}'
        ] * bool(new)
    return [f'{name} changed from {_quoted(before)} to {_quoted(after)}']


# The timeline


@dataclass
class Entry:
    """What one commit changed in the business's meaning."""

    commit: Commit
    """The commit, with its author, co-authors and dates."""
    changes: list[WorldChange] = field(default_factory=list)
    """What changed, by kind and then by name."""
    compared_with: Commit | None = None
    """The commit whose world it is compared with, when that is not its parent, whose world could
    not be built."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'commit': self.commit.to_dict(),
            'compared_with': self.compared_with.to_dict() if self.compared_with is not None else None,
            'changes': [c.to_dict() for c in self.changes],
        }


@dataclass
class Timeline:
    """How the business's meaning changed over a range of commits, oldest first."""

    since: str | None
    """Where the range starts: a commit, tag or date, or None for the whole history."""
    until: str
    """Where it ends."""
    paths: list[str] = field(default_factory=list)
    """Only commits that changed these files or directories, relative to the project."""
    entries: list[Entry] = field(default_factory=list)
    """The commits that changed something, in the order of history, oldest first."""
    commits: int = 0
    """Commits read in the range."""
    start: Commit | None = None
    """The commit whose world the first commit read is compared with, when it has a parent."""
    unknown: list[Commit] = field(default_factory=list)
    """Commits whose world could not be built."""
    notes: list[str] = field(default_factory=list)
    """What could not be read, and why."""
    warehouse: str | None = None
    """Where :func:`effects` ran the changes, when it did."""

    def changes(self, kind: str | None = None) -> list[tuple[Commit, WorldChange]]:
        """Every change with its commit, oldest first, or only those of one kind."""
        return [(e.commit, c) for e in self.entries for c in e.changes if kind is None or c.kind == kind]

    def summary(self) -> str:
        count = sum(len(e.changes) for e in self.entries)
        range_ = f'since {self.since}' if self.since else 'in the history read'
        return (
            f'{_plural(count, "change")} to the business in {len(self.entries)} of '
            f'{_plural(self.commits, "commit")} {range_}'
        )

    def release_notes(self) -> list[str]:
        """One sentence for each metric change, newest first, as a release note: what changed,
        in which commit and by whom, who approved it since, and what it does on today's data once
        :func:`effects` ran it."""
        return [sentence(commit, change) for commit, change in reversed(self.changes('metric'))]

    def to_dict(self) -> dict[str, Any]:
        return {
            'format_version': FORMAT_VERSION,
            'since': self.since,
            'until': self.until,
            'paths': self.paths,
            'summary': self.summary(),
            'commits': self.commits,
            'start': self.start.to_dict() if self.start is not None else None,
            'entries': [e.to_dict() for e in self.entries],
            'release_notes': self.release_notes(),
            'unknown': [c.to_dict() for c in self.unknown],
            'notes': self.notes,
            'warehouse': self.warehouse,
        }

    def markdown(self) -> str:
        """The timeline as Markdown, to post to a wiki, a chat channel or a pull request."""
        title = f'## Timeline {f"since {self.since}" if self.since else ""}'.rstrip()
        out = [title, '', self.summary() + '.', '']
        for entry in self.entries:
            commit = entry.commit
            out += [f'### {_day(commit)} `{commit.short}` {commit.by}: {commit.subject}', '']
            if entry.compared_with is not None:
                out += [
                    f'Compared with `{entry.compared_with.short}`, the last commit before it that built.',
                    '',
                ]
            for change in entry.changes:
                out.append(f'- **{change.headline()}**')
                out += [f'  - {line}' for line in change.english]
                if change.effect is not None:
                    out.append(f'  - {change.effect.summary()}')
            out.append('')
        notes = self.notes + [f'the world could not be built at {c.short}' for c in self.unknown]
        if notes:
            out += ['Notes:', '', *[f'- {n}' for n in notes], '']
        return '\n'.join(out).rstrip() + '\n'

    def text(self, style: Any = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import show, timeline_lines

        return show(timeline_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def _day(commit: Commit) -> str:
    return (commit.authored or commit.date)[:10]


def sentence(commit: Commit, change: WorldChange) -> str:
    """A release note for one metric change: what changed, in which commit and by whom, then whether
    it was approved since and what it does on today's data when that is known, as in ``Revenue: the
    required filter that keeps only rows where [status] has a value other than 'returned' was
    removed in a1b2c3d (Sam); approved in a commit by Priya on 2026-03-05 (e4f5a6b)``."""
    what, detail = _what(change)
    parts = [f'{change.name}: {what} in {commit.short} ({commit.by})', *detail]
    if change.approval is not None and change.approval.sha == commit.sha:
        if what != 'approved':
            parts.append('approved in the same commit')
    elif change.approval is not None:
        approver = change.approval
        parts.append(f'approved in a commit by {approver.by} on {_day(approver)} ({approver.short})')
    elif change.facts.get('status') == 'approved':
        parts.append('approved before this change and not since')
    elif change.facts.get('status') is not None:
        parts.append(f'{change.facts["status"]}, not approved since')
    if change.effect is not None:
        parts.append(change.effect.summary())
    return '; '.join(parts)


def _what(change: WorldChange) -> tuple[str, list[str]]:
    """The change to a metric in a few words, for its release note, and the sentences that say
    more of it."""
    if change.status == 'renamed':
        return f'renamed from {change.renamed_from!r}', []
    if change.status != 'changed':
        return change.status, []
    worded = change.worded
    if 'note' in worded:
        return ' and '.join(worded['note']), []
    if 'formula' in worded or 'rows' in worded:
        what = ' and '.join(
            w
            for k, w in (('formula', 'the formula'), ('rows', 'the rows it is computed over'))
            if k in worded
        )
        return f'{what} changed', [*worded.get('formula', []), *worded.get('rows', [])]
    if _approves(change):
        return 'approved', []
    return ', '.join(change.fields) + ' changed', [
        line for k, lines in worded.items() if k != 'note' for line in lines
    ]


def timeline(
    root: Path,
    *,
    since: str | None = None,
    until: str | None = None,
    paths: Sequence[str] = (),
    limit: int = 50,
    graph: Graph | None = None,
    cache: bool = True,
) -> Timeline:
    """How the business's meaning changed in each commit after ``since`` (a commit, tag or date;
    by default the whole history) up to ``until`` (default HEAD), in the order of history: each
    commit's world compared with its parent's, or, where that could not be built, with the last
    commit before it whose world could be. Only commits that changed the project, or with
    ``paths`` those files or directories in it, are read, at most the ``limit`` most recent.
    Merges are left out: what a merge brings is the commits it merges, which are read themselves.

    Each dbt project's manifest as it is now (``target/manifest.json``) is the start of that
    project's manifest at every commit, so build the project first. ``graph``, the project's graph
    now, gives what ``konstruct verify`` found of keys and relationships, which holds at every
    commit. With ``cache``, each commit's world is kept in the user's cache directory (none in CI),
    so a second run builds only new commits.

    Raises:
        konstruct.analysis.diff.DiffError: The project is not in a git repository, or ``since``
            or ``until`` is not a commit, tag or date.
        ValueError: ``limit`` is below 1.
    """
    if limit < 1:
        raise ValueError('limit must be at least 1')
    root = Path(root)
    top = Path(run_git(root, 'rev-parse', '--show-toplevel').strip())
    prefix = root.resolve().relative_to(top.resolve())
    end = resolve_ref(root, until) if until else 'HEAD'
    result = Timeline(since=since, until=until or 'HEAD', paths=list(paths))
    repository = Repository(top)
    scope = [(prefix / p).as_posix() for p in paths] if paths else [prefix.as_posix()]
    listed, more = repository.commits(*commit_range(root, since, end), paths=scope, limit=limit, check=True)
    if more:
        result.notes.append(f'only the last {limit} commits were read; raise the limit to read more')
    # The order of history up to the end of the range, which a rebase or a branch merged later
    # does not follow the dates of.
    order = {
        sha: i for i, sha in enumerate(run_git(top, 'rev-list', '--topo-order', '--reverse', end).split())
    }
    commits = sorted((c for c, _ in listed), key=lambda c: order.get(c.sha, -1))
    for commit in commits:
        commit.order = order.get(commit.sha, -1)
    result.commits = len(commits)
    if not commits:
        return result
    worlds = _Worlds(root, top, prefix, graph, cache, result.notes)
    try:
        first = _parent(repository, commits[0].sha)
        if first is not None:
            result.start = _commit(repository, first, order)
        elif repository.shallow:
            result.notes.append(
                f'history before {commits[0].short} is not in this shallow clone, so {commits[0].short} '
                'is compared with an empty project; fetch more history (git fetch --unshallow) to compare it'
            )
        noted: dict[str, list[str]] = {}
        for commit in commits:
            after = worlds.at(commit.sha)
            if after is None:
                result.unknown.append(commit)
                continue
            for note in after.notes:
                noted.setdefault(note, []).append(commit.short)
            base = worlds.before(repository, commit.sha)
            if base is None:
                result.notes.append(f'no commit before {commit.short} could be built to compare it with')
                continue
            sha, before = base
            changes = world_diff(before, after)
            for change in changes:
                change.english = word(change, after.dialect or before.dialect, worlds.texts(before, after))
            compared = (
                _commit(repository, sha, order) if sha and sha != _parent(repository, commit.sha) else None
            )
            if changes:
                result.entries.append(Entry(commit, changes, compared))
    finally:
        worlds.close()  # an interrupt too leaves no copy of the project behind
    result.notes += [f'{note} (at {", ".join(shorts)})' for note, shorts in noted.items()]
    _approvals(result)
    return result


def _parent(repository: Repository, sha: str) -> str | None:
    found = repository.git('rev-parse', '--verify', '--quiet', f'{sha}^').strip()
    return found or None


def _commit(repository: Repository, sha: str, order: dict[str, int]) -> Commit:
    found, _ = repository.commits(sha, limit=1)
    if found:
        commit = found[0][0]
        commit.order = order.get(sha, -1)
        return commit
    return Commit(sha, '', '', '', order.get(sha, -1), repository.top)


def _approvals(result: Timeline) -> None:
    """For each metric change, the first commit at or after it, by ancestry, that approved the
    metric, followed through renames: one that added it approved, made it approved, or set a new
    approval date while it stayed approved. A commit on a branch that does not contain the change
    approved something else."""
    identity: dict[str, int] = {}
    chains: list[list[tuple[Commit, WorldChange]]] = []
    for commit, change in result.changes('metric'):
        found = identity.pop(change.renamed_from, None) if change.renamed_from else identity.get(change.name)
        if found is None:
            found = len(chains)
            chains.append([])
        identity[change.name] = found
        chains[found].append((commit, change))
    for chain in chains:
        approvals = [c for c, change in chain if _approves(change)]
        for commit, change in chain:
            change.approval = next(
                (a for a in approvals if a.order >= commit.order and a.at_or_after(commit)), None
            )


def _approves(change: WorldChange) -> bool:
    approved = (change.after or {}).get('status') == 'approved'
    if change.status == 'added':
        return approved
    status, date = change.fields.get('status'), change.fields.get('approved')
    return (status is not None and status[1] == 'approved') or (
        approved and date is not None and bool(date[1])
    )


# Building the world at a commit


class _Worlds:
    """The world at each commit, built once each and kept in the user's cache by commit."""

    def __init__(
        self, root: Path, top: Path, prefix: Path, graph: Graph | None, cache: bool, notes: list[str]
    ) -> None:

        self.root = root
        self.top = top
        self.prefix = prefix
        self.notes = notes
        self.integrity = graph.meta.get('integrity') if graph is not None else None
        if self.integrity:
            notes.append(
                'what konstruct verify found of keys and relationships in the data now holds at every commit'
            )
        self.manifests = self._manifests(graph)
        self.extra = [
            str(FORMAT_VERSION),
            _digest([(where, _lasting(m), _lasting(c), i, p) for where, m, c, i, p in self.manifests]),
            _digest(self.integrity),
        ]
        self.directory = snapshot.user_cache('timeline') if cache else None
        if self.directory is not None and not snapshot.private_directory(self.directory):
            notes.append(f'{self.directory} is not private to you, so no world is cached')
            self.directory = None
        self.loaded: Cache | None = None
        self.made: str | None = None
        self.fresh = False
        self.saving = False
        self.worded = False
        """Whether a world built in this run worded a formula, which the cache then keeps."""
        self.memory: dict[str, World | None] = {}
        self.built = 0
        self.scratch = Path(tempfile.mkdtemp(prefix='konstruct-timeline-'))
        self.present: dict[str, bytes] | None = None
        """The files in the tree and their blobs, while the tree is whole."""
        self.last: tuple[str, World, Formulas] | None = None
        """The commit built last, its world and its formulas, which the next world built is worded
        against."""

    def _manifests(
        self, graph: Graph | None
    ) -> list[tuple[str, dict[str, Any], dict[str, Any] | None, dict[str, str], set[str]]]:
        """Each dbt project's manifest and catalog as they are now, by the project's directory in
        the project, with the files its compiled SQL follows from and its installed packages:
        what each commit's manifest is made from."""
        from konstruct.inputs.dbt_commit import compile_inputs, installed_packages

        found = []
        here = (self.top / self.prefix).resolve()
        for project in dbt.find_projects(here):
            try:
                manifest = jsonio.loads(project.manifest_path.read_bytes())
            except (OSError, ValueError):
                continue
            catalog: dict[str, Any] | None
            try:
                catalog = jsonio.loads(project.catalog_path.read_bytes())
            except (OSError, ValueError):
                catalog = None
            if not isinstance(manifest, dict):
                continue
            where = project.root.resolve().relative_to(here).as_posix()
            inputs = compile_inputs(project)
            packages = installed_packages(project)
            found.append((where, manifest, catalog if isinstance(catalog, dict) else None, inputs, packages))
        if (
            not found
            and graph is not None
            and any(n.kind == 'model' and 'dbt' in n.origins for n in graph.nodes.values())
        ):
            self.notes.append(
                'the dbt manifest (target/manifest.json) could not be read, so no dbt model is in any commit'
            )
        return found

    @property
    def analysis(self) -> Cache:
        """The analyses this timeline's builds share, read only once a world has to be built."""
        from konstruct.engine.build import Cache

        if self.loaded is not None:
            return self.loaded
        path = self._analysis_path()
        self.saving = path is not None and (not path.exists() or snapshot.private(path))
        self.loaded = Cache(path if self.saving and path else Path(), enabled=self.saving)
        return self.loaded

    def texts(self, *worlds: World) -> Callable[[str], str | None]:
        """The composed formula of a digest, from ``worlds`` or the texts this cache has kept."""

        def text(digest: str) -> str | None:
            for world in worlds:
                if digest in world.texts:
                    return world.texts[digest]
            found = self.analysis.entries.get(f'timeline text {digest}') if self.loaded is not None else None
            return found if isinstance(found, str) else None

        return text

    def at(self, sha: str) -> World | None:
        """The world at ``sha``, or None when it cannot be built."""
        if sha not in self.memory:
            self.memory[sha] = self._load(sha)
        return self.memory[sha]

    def before(self, repository: Repository, sha: str) -> tuple[str, World] | None:
        """The commit to compare ``sha`` with and its world: its parent, or the nearest commit
        before it along first parents whose world could be built; an empty world for a commit
        with no parent. None when none of the last commits before it could be built."""
        parent = _parent(repository, sha)
        if parent is None:
            return '', World()
        for candidate in run_git(
            self.top, 'rev-list', '--first-parent', f'--max-count={BACK}', parent
        ).split():
            world = self.at(candidate)
            if world is not None:
                return candidate, world
        return None

    def _analysis_path(self) -> Path | None:
        if self.directory is None:
            return None
        key = snapshot.repository_key(self.root, 'analysis')
        return self.directory / f'analysis-{key}.json' if key else None

    def generation(self) -> str:
        """Which analyses the worlds are built from: an id kept beside the analysis cache, made
        anew when there is none, so a world built from analyses that have since gone (removed, or
        not the user's own) is built again rather than read."""
        if self.made is None:
            path = self._analysis_path()
            marker = path.with_suffix('.id') if path is not None else None
            if (
                path is not None
                and marker is not None
                and snapshot.private(path)
                and snapshot.private(marker)
            ):
                with contextlib.suppress(OSError, UnicodeDecodeError):
                    self.made = marker.read_text(encoding='utf-8').strip() or None
            if self.made is None:
                self.made = secrets.token_hex(16)
                self.fresh = True
        return self.made

    def _path(self, sha: str) -> Path | None:
        if self.directory is None:
            return None
        found = snapshot.cache_name(self.root, sha, *self.extra, self.generation())
        return self.directory / f'world-{found[3]}.json' if found is not None and found[3] else None

    def _load(self, sha: str) -> World | None:
        path = self._path(sha)
        if path is not None and snapshot.private(path):
            with contextlib.suppress(OSError, ValueError):
                data = jsonio.loads(path.read_bytes())
                if isinstance(data, dict) and data.get('format_version') == FORMAT_VERSION:
                    return World.from_dict(data['world'])
        try:
            world = self._build(sha)
        except Exception as error:  # a commit that will not build is reported, not fatal
            log.warning('konstruct: the world at %s could not be built: %s', sha[:7], error)
            return None
        self._save(sha, world)
        return world

    def _save(self, sha: str, world: World) -> None:
        path = self._path(sha)
        if path is not None and (not path.exists() or snapshot.private(path)):
            with contextlib.suppress(OSError):
                jsonio.write_text(
                    path, jsonio.dumps({'format_version': FORMAT_VERSION, 'world': world.to_dict()})
                )
                path.chmod(0o600)

    def _build(self, sha: str) -> World:
        """The world at ``sha``, built from its files in this timeline's own tree, which holds
        the last commit built: only what differs is written, as most files stay the same."""
        tree = self.scratch / 'tree'
        written: list[Path] = []
        present, self.present = self.present, None  # until the tree is whole again
        try:
            if present is None:
                tree.mkdir()  # a new directory, so nothing in it is a link that leads elsewhere
                present = {}
            snapshot.write_tree(self.top, sha, self.prefix, tree, present)
            self.present = present
            root = tree / self.prefix
            if not root.is_dir():
                return World()  # the project did not exist yet
            world, formulas = self._world(root, written)
            self.last = (sha, world, formulas)
            return world
        except BaseException:
            self.present = None  # the tree is gone, so the next commit writes all of its own
            shutil.rmtree(tree, ignore_errors=True)
            raise
        finally:
            # The manifests written for this commit are not the next one's, even where the commit
            # has its own at that path, which the next commit then writes again.
            for path in written:
                path.unlink(missing_ok=True)
                if self.present is not None:
                    self.present.pop(path.relative_to(tree).as_posix(), None)

    def _world(self, root: Path, written: list[Path]) -> tuple[World, Formulas]:
        from konstruct.engine.build import build_graph
        from konstruct.engine.config import load_config
        from konstruct.inputs.dbt_commit import manifest_at

        config = load_config(root, out_dir=self.scratch / 'out', trusted=False)
        config.override(run_dbt=False, warehouses=False, plugins=False)
        config.discover_warehouses = False
        config.html = False
        notes: list[str] = []
        if config.repositories:
            notes.append('the other repositories konstruct.toml lists are not read at each commit')
            config.repositories = []
        inside = root.resolve()
        for where, manifest, catalog, inputs, packages in self.manifests:
            if not (root / where / 'dbt_project.yml').is_file():
                continue
            project = dbt.read_project(root / where)
            if project is None:
                continue
            outside = sorted(
                {
                    str(p.relative_to(root)) if p.is_relative_to(root) else str(p)
                    for p in (project.target_path, *project.source_paths)
                    if not p.resolve().is_relative_to(inside)
                }
            )
            if outside:
                notes.append(
                    f'the dbt project in {where} names paths outside the project ({", ".join(outside)}), '
                    'so its models are not read'
                )
                continue
            at = manifest_at(
                manifest, catalog, project, keep=config.inside, compiled_from=inputs, packages=packages
            )
            written.append(project.manifest_path)
            jsonio.write_text(project.manifest_path, jsonio.dumps(at.manifest))
            if at.catalog is not None:
                written.append(project.catalog_path)
                jsonio.write_text(project.catalog_path, jsonio.dumps(at.catalog))
            if at.package_macros:
                notes.append(
                    'the macros of installed dbt packages (dbt_packages) are not in git, so these models, '
                    f"rendered from the commit's files, are rendered without them: {', '.join(at.package_macros)}"
                )
            if at.uncatalogued:
                notes.append(
                    "the columns of models whose files differ from today's are read from their SQL, "
                    f"not dbt's catalog: {', '.join(at.uncatalogued)}"
                )
            if at.package_compiled:
                notes.append(
                    'the models of installed dbt packages are not in git, so these keep the SQL dbt '
                    "compiled today, with today's version of an ephemeral model the commit changed: "
                    f'{", ".join(at.package_compiled)}'
                )
            if at.moved:
                notes.append(
                    'these models are in another folder than today, and keep the configuration dbt gives '
                    "them today, not what dbt_project.yml configures for the commit's folder: "
                    f'{", ".join(at.moved)}'
                )
        graph, builder, _ = build_graph(config, cache=self.analysis)
        notes += [f'not read until the project is trusted: {u}' for u in builder.untrusted]
        notes += [
            f'{directory.relative_to(inside) if directory.is_relative_to(inside) else directory}: '
            f'did not read {", ".join(sorted(links))}, which lead outside the project'
            for directory, links in sorted(config.outside_links.items())
        ]
        if self.integrity is not None:
            graph.meta['integrity'] = copy.deepcopy(self.integrity)
        self.built += 1
        formulas = Formulas(graph)
        previous = self.last[1] if self.last is not None else None
        world = world_of(graph, root=root, previous=previous, formulas=formulas, every=False)
        world.notes = notes
        if self.last is not None:
            # The formulas that differ from the world built before are worded there too, as the
            # two are compared: that world's graph is kept until this one is built.
            sha, before, composer = self.last
            missing = [
                label
                for label, digest in world.digests.items()
                if before.digests.get(label) not in (None, digest)
                and before.digests[label] not in before.texts
            ]
            for label in missing:
                composer.text(label, before.texts)
            if missing:
                self._save(sha, before)
        # Kept with the analyses, so a later run words them without the graph.
        self.analysis.extend({f'timeline text {d}': t for d, t in world.texts.items()})
        for digest in world.texts:
            self.analysis.quiet().get_or_compute(f'timeline text {digest}', str)
        self.worded = self.worded or bool(world.texts)
        return world, formulas

    def close(self) -> None:
        """Remove the tree, save the analysis cache, and keep the cache to its size."""
        shutil.rmtree(self.scratch, ignore_errors=True)
        if self.directory is None:
            return
        if not self.built:
            _prune(self.directory)
            return
        with contextlib.suppress(OSError):
            if (
                self.loaded is not None
                and self.saving
                and (self.analysis.misses or self.worded or self.fresh)
            ):
                self.analysis.save()
                self.analysis.path.chmod(0o600)
                marker = self.analysis.path.with_suffix('.id')
                jsonio.write_text(marker, self.generation())
                marker.chmod(0o600)
        _prune(self.directory)


def _prune(directory: Path) -> None:
    """Keep the cache to its size: the analyses of the last few projects, and worlds, the oldest
    going first, until the whole is within :data:`CACHE_BYTES`."""
    with contextlib.suppress(OSError):
        analyses = sorted(directory.glob('analysis-*.json'), key=lambda p: p.stat().st_mtime)
        for old in analyses[: max(len(analyses) - ANALYSES, 0)]:
            old.unlink()
            old.with_suffix('.id').unlink(missing_ok=True)
        files = [(p, p.stat()) for p in directory.iterdir() if p.is_file()]
        total = sum(found.st_size for _, found in files)
        worlds = sorted((f for f in files if f[0].name.startswith('world-')), key=lambda f: f[1].st_mtime)
        for path, found in worlds:
            if total <= CACHE_BYTES:
                break
            path.unlink()
            total -= found.st_size


_RUN_FACTS = frozenset({'generated_at', 'invocation_id', 'invocation_started_at', 'created_at', 'stats'})
"""What a dbt artefact records of the run that wrote it rather than of the project: when it was
made, which invocation made it, and in the catalog, how much data each table held then (``stats``:
row counts, bytes, last modified), which every load of data changes."""


def _lasting(artefact: dict[str, Any] | None) -> Any:
    """A dbt manifest or catalog without what changes on every dbt run or load of data, so a run
    that changes nothing else keeps the worlds made from it: the metadata's, and each node's,
    whether listed by id (``nodes``, ``sources``) or in lists (``disabled``)."""
    if artefact is None:
        return None

    def node(value: Any) -> Any:
        return {k: v for k, v in value.items() if k not in _RUN_FACTS} if isinstance(value, dict) else value

    kept: dict[str, Any] = {}
    for key, value in artefact.items():
        if key == 'metadata':
            kept[key] = node(value)
        elif isinstance(value, dict):
            kept[key] = {
                k: [node(v) for v in item] if isinstance(item, list) else node(item)
                for k, item in value.items()
            }
        else:
            kept[key] = value
    return kept


def _digest(value: Any) -> str:
    return hashlib.sha256(jsonio.dumps(value, sort_keys=True, default=str).encode('utf-8')).hexdigest()


# What each change does on today's data


def effects(
    result: Timeline,
    graph: Graph,
    root: Path,
    warehouse: str | Path | Engine,
    *,
    max_rows: int = 1_000_000,
    ontology: Ontology | None = None,
) -> Timeline:
    """``result`` with what each metric's change of formula or rows does on today's data, run by
    ``konstruct verify --effect`` (:func:`konstruct.analysis.effect.effects`): the change in the
    same commit to one of its columns or relations, or to one upstream of them, run before and
    after on the same rows. Reads the warehouse and never writes to it; with no such change, no
    engine is opened."""
    from konstruct.analysis.effect import effects as run
    from konstruct.analysis.release_notes import release_notes

    wanted = [(c, w) for c, w in result.changes('metric') if {'formula', 'rows'} & set(w.fields)]
    if not wanted:
        return result
    notes = release_notes(
        graph,
        root,
        since=result.start.sha if result.start is not None else None,
        until=result.until,
        limit=max(result.commits, 1),
        whole_history=result.start is None,
    )
    upstream: dict[str, set[str]] = {}
    links = []
    for commit, change in wanted:
        columns = [str(c) for c in (change.after or {}).get('columns') or []]
        reach: set[str] = set()
        for label in columns:
            if label not in upstream:
                upstream[label] = _upstream(graph, label)
            reach |= upstream[label]
        relations = {r.id for r in (graph.parent_of(graph.nodes[i]) for i in reach) if r is not None}
        candidates = [
            n
            for n in notes.changes
            if n.commit.sha == commit.sha
            and n.node_id is not None
            and (
                (n.kind == 'formula' and n.node_id in reach) or (n.kind == 'rows' and n.node_id in relations)
            )
        ]
        own = [
            n
            for n in candidates
            if n.column in columns or n.relation in {c.rpartition('.')[0] for c in columns}
        ]
        chosen = (own or candidates)[:1]
        if chosen:
            links.append((change, chosen[0]))
    notes.changes = [n for _, n in links]
    run(graph, root, notes, warehouse, max_rows=max_rows, ontology=ontology)
    result.warehouse = notes.warehouse
    for change, note in links:
        change.effect = note.effect
        change.effect_column = note.column or note.relation
    return result


def _upstream(graph: Graph, label: str) -> set[str]:
    """The ids of the column called ``label`` and every column upstream of it."""
    from konstruct.analysis.query import QueryError, resolve_one

    try:
        node = resolve_one(graph, label)
    except QueryError:
        return set()
    return {node.id, *(s.node.id for s in graph.trace(node) if s.node.kind == 'column')}
