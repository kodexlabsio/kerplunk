"""When a column's formula changed, and who changed the business terms bound to it.

``konstruct history COLUMN`` reads the git history of the file that defines the column (a dbt
model, a SQL file, or the SQL file a DAG task runs) and works out the column's formula at each
commit and at its parent, with the same analysis the build uses, so a commit that only reformats
the SQL, or a merge, is not a change. It does the same for every ``definitions.yml`` that binds a
term to the column, recording each change to the term's status, approval date, owner and
definition with the commit's author and date: who approved the formula, and when. Uncommitted
edits are the last entry. Commits are ordered by ancestry, not by their dates, so a rebased
change still comes after the approval it followed.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from konstruct.analysis.diff import DiffError, _Context, _git, file_at
from konstruct.analysis.explain import Explainer
from konstruct.core.graph import Graph, Node
from konstruct.core.sql import strip_templating
from konstruct.semantics import definitions

_SEPARATOR = '\x1f'
_UNKNOWN = '?'


@dataclass
class Commit:
    sha: str
    """Empty for uncommitted changes."""
    author: str
    date: str
    """When the commit was made (its committer date), in UTC, ISO 8601."""
    subject: str
    order: int = 0
    """Its place in a topological order of history, oldest first, for display."""
    repository: Path | None = field(default=None, repr=False, compare=False)
    """The repository it is in, to ask git about ancestry."""

    def follows(self, other: Commit) -> bool:
        """Whether this commit comes after ``other`` in history: ``other`` is one of its ancestors,
        or this is the working tree. Commits on parallel branches follow neither way."""
        if not self.sha:
            return True
        if not other.sha or self.sha == other.sha:
            return False
        if self.repository is None or self.repository != other.repository:
            return self.order > other.order
        result = subprocess.run(
            ['git', '-C', str(self.repository), 'merge-base', '--is-ancestor', other.sha, self.sha],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def at_or_after(self, other: Commit) -> bool:
        return self.sha == other.sha or self.follows(other)

    @property
    def short(self) -> str:
        return self.sha[:7] if self.sha else 'working tree'

    def to_dict(self) -> dict[str, Any]:
        return {'sha': self.sha or None, 'author': self.author, 'date': self.date, 'subject': self.subject}


def _working_tree(order: int, repository: Path | None = None) -> Commit:
    return Commit('', '', '', 'uncommitted changes', order, repository)


@dataclass
class FormulaChange:
    commit: Commit
    path: str
    """The file at that commit, relative to its repository."""
    before: str | None
    """The formula before this commit, or None where the column was added."""
    after: str | None
    """The formula after it, or None where the column was removed."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'commit': self.commit.to_dict(),
            'path': self.path,
            'before': self.before,
            'after': self.after,
        }


@dataclass
class TermChange:
    commit: Commit
    term: str
    path: str
    """The definitions file at that commit, relative to its repository."""
    changes: dict[str, tuple[Any, Any]]
    """Each changed field (status, approved, owner, definition) with its value before and after."""
    status: str | None = None
    """The term's status after this commit (None where it is gone)."""

    @property
    def approved(self) -> bool:
        """Whether this commit approved the term: made an existing term approved, or, while it
        stayed approved, set a new approval date. A term first seen already approved, such as a
        renamed one, is not approved by this commit."""
        status = self.changes.get('status')
        if status is not None:
            return status[0] is not None and status[1] == 'approved'
        approved = self.changes.get('approved')
        return self.status == 'approved' and approved is not None and approved[1] is not None

    @property
    def leaves_approved(self) -> bool:
        """Whether the term is approved after this commit."""
        return self.status == 'approved'

    def to_dict(self) -> dict[str, Any]:
        return {
            'commit': self.commit.to_dict(),
            'term': self.term,
            'path': self.path,
            'approved': self.approved,
            'changes': {k: {'before': b, 'after': a} for k, (b, a) in self.changes.items()},
        }


@dataclass
class History:
    column: str
    label: str
    formula: str | None
    """The formula now, as the graph has it."""
    path: str | None
    """The file that defines the column, relative to the project."""
    formulas: list[FormulaChange] = field(default_factory=list)
    """In history order, oldest first."""
    terms: list[TermChange] = field(default_factory=list)
    """In history order, oldest first."""
    read_formulas: bool = False
    """Whether the formula's history was read (it is defined by SQL in a git repository)."""
    unknown: list[Commit] = field(default_factory=list)
    """Commits at which the formula could not be worked out."""
    window: Commit | None = None
    """When the formula's history was cut short (a shallow clone or the limit), the oldest commit
    read: what came before it is not known."""
    notes: list[str] = field(default_factory=list)

    @property
    def last_approval(self) -> TermChange | None:
        return max((t for t in self.terms if t.approved), key=lambda t: t.commit.order, default=None)

    @property
    def approved_since_last_change(self) -> bool | None:
        """Whether a bound term was approved after every formula change, by ancestry.

        True when one approval follows every change read (and, if the history was cut short, the
        oldest commit read); False when a term was approved at or before a change that no later
        approval follows; None when the formula's history was not read, nothing settles it, or a
        commit after the last approval could not be worked out."""
        if not self.read_formulas:
            return None
        approvals = [t for t in self.terms if t.approved]
        last = self.last_approval
        if last is not None and any(c.at_or_after(last.commit) for c in self.unknown):
            return None
        changes = [f.commit for f in self.formulas]
        for change in changes:
            # An approval in the same commit as the change (a squash-merged pull request) covers it.
            approved_before = any(t.leaves_approved and change.follows(t.commit) for t in self.terms)
            if approved_before and not any(a.commit.at_or_after(change) for a in approvals):
                return False
        # A commit whose formula could not be worked out may have changed it, so an approval has
        # to follow those too.
        unsettled = changes + self.unknown
        for approval in approvals:
            if all(approval.commit.at_or_after(c) for c in unsettled) and (
                self.window is None or approval.commit.at_or_after(self.window)
            ):
                return True
        if not unsettled and self.window is None and any(t.leaves_approved for t in self.terms):
            return True
        return None

    def summary(self) -> str:
        changes = len(self.formulas)
        text = f'{self.label}: {changes} formula change{"s" if changes != 1 else ""}'
        if not self.read_formulas:
            text = f'{self.label}: the formula history was not read'
        approval = self.last_approval
        if approval is not None:
            by = (
                f'by {approval.commit.author} in {approval.commit.short}'
                if approval.commit.sha
                else 'in the working tree'
            )
            text += f'; {approval.term!r} last approved {by}'
            if self.approved_since_last_change is False:
                text += ', before the latest formula change'
        elif self.approved_since_last_change is False:
            text += '; a bound term was approved before the latest formula change and not since'
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            'column': self.column,
            'label': self.label,
            'summary': self.summary(),
            'formula': self.formula,
            'path': self.path,
            'formulas': [c.to_dict() for c in self.formulas],
            'terms': [t.to_dict() for t in self.terms],
            'read_formulas': self.read_formulas,
            'unknown': [c.to_dict() for c in self.unknown],
            'approved_since_last_change': self.approved_since_last_change,
            'notes': self.notes,
        }

    def __str__(self) -> str:
        from konstruct.interfaces.render import plain

        return plain(self)


def _heading(commit: Commit) -> str:
    if not commit.sha:
        return '  working tree (uncommitted)'
    return f'  {commit.date[:10]} {commit.short} {commit.author}: {commit.subject}'


def _shown(value: Any) -> str:
    return 'none' if value in (None, '') else str(value)


def _utc(date: str) -> str:
    try:
        return dt.datetime.fromisoformat(date).astimezone(dt.UTC).isoformat()
    except ValueError:
        return date


class _Repository:
    """One git repository: its commits in ancestry order, and files at a commit or its parent."""

    def __init__(self, top: Path) -> None:
        self.top = top
        output = _git(top, 'rev-list', '--topo-order', '--reverse', 'HEAD', check=False)
        self.order = {sha: index for index, sha in enumerate(output.split())}
        self.shallow = _git(top, 'rev-parse', '--is-shallow-repository', check=False).strip() == 'true'

    def git(self, *args: str) -> str:
        return _git(self.top, '-c', 'core.quotePath=false', *args, check=False)

    def log(self, path: str, limit: int) -> tuple[list[tuple[Commit, str]], bool]:
        """The commits that changed ``path``, oldest first, each with the file's path then (renames
        are followed), and whether the log stopped at ``limit``."""
        output = self.git(
            'log',
            '--follow',
            '--no-merges',
            f'--max-count={limit}',
            '--name-only',
            f'--format=%x1e%H{_SEPARATOR}%an{_SEPARATOR}%cI{_SEPARATOR}%s',
            '--',
            path,
        )
        found = []
        for block in output.split('\x1e'):
            lines = [line for line in block.strip().split('\n') if line.strip()]
            if not lines:
                continue
            sha, author, date, subject = [*lines[0].split(_SEPARATOR), '', '', ''][:4]
            commit = Commit(sha, author, _utc(date), subject, self.order.get(sha, -1), self.top)
            found.append((commit, lines[-1] if len(lines) > 1 else path))
        return list(reversed(found)), len(found) >= limit

    def has_parent(self, sha: str) -> bool:
        return bool(self.git('rev-parse', '--verify', '--quiet', f'{sha}^').strip())

    def file(self, ref: str | None, path: str) -> str | None:
        return file_at(self.top, ref, path)


def _repository_of(path: Path) -> tuple[_Repository, str] | None:
    """The repository holding ``path``, and the path relative to it."""
    folder = path if path.is_dir() else path.parent
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    try:
        top = Path(_git(folder, 'rev-parse', '--show-toplevel').strip())
    except (DiffError, OSError, subprocess.SubprocessError):
        return None
    return _Repository(top), path.resolve().relative_to(top.resolve()).as_posix()


def _versions(
    repository: _Repository,
    path: str,
    limit: int,
    notes: list[str],
    what: str,
    cut: list[Commit] | None = None,
) -> list[tuple[Commit, str, str | None, str | None]]:
    """For each commit that changed ``path``, the file before (at its parent) and after; then the
    working tree against HEAD, if it differs. A commit whose parent is not in the clone is a
    starting point, not a change."""
    commits, stopped = repository.log(path, limit)
    versions = []
    for index, (commit, at) in enumerate(commits):
        after = repository.file(commit.sha, at)
        if repository.has_parent(commit.sha):
            before_path = commits[index - 1][1] if index else at
            before = repository.file(f'{commit.sha}^', at)
            if before is None and before_path != at:
                before = repository.file(f'{commit.sha}^', before_path)
        elif repository.shallow and index == 0:
            notes.append(f'{what}: history before {commit.short} is not in this shallow clone')
            if cut is not None:
                cut.append(commit)
            versions.append((commit, at, after, after))
            continue
        else:
            before = None
        versions.append((commit, at, before, after))
    if stopped and commits:
        if cut is not None:
            cut.append(commits[0][0])
        counted = (
            f'{limit} commits that changed {path} were' if limit != 1 else f'commit that changed {path} was'
        )
        notes.append(f'{what}: only the last {counted} read')
    head = repository.file('HEAD', path)
    current = repository.file(None, path)
    if current != head:
        versions.append((_working_tree(len(repository.order) + 1, repository.top), path, head, current))
    return versions


def history(graph: Graph, root: Path, column: Node, *, limit: int = 50) -> History:
    """The changes to ``column``'s formula, and to the terms bound to it, in git history."""
    if limit < 1:
        raise ValueError('limit must be at least 1')
    explainer = Explainer(graph)
    relation = graph.nodes.get(column.parent or '')
    raw = column.attrs.get('expression')
    result = History(
        column=column.id,
        label=graph.label(column),
        formula=explainer.namer.text(raw) if isinstance(raw, str) else None,
        path=relation.path if relation is not None else None,
    )
    context = _Context(graph, root)
    _formula_history(graph, root, relation, column, explainer, context, limit, result)
    _term_history(graph, root, column, limit, result)
    return result


def _defining_file(graph: Graph, relation: Node, notes: list[str]) -> tuple[str, bool] | None:
    """The file whose SQL defines ``relation``, and whether it is a dbt model."""
    if relation.path and not relation.path.endswith(('.yml', '.yaml', '.json')):
        return relation.path, relation.kind in ('model', 'snapshot')
    tasks = [
        graph.nodes[e.source]
        for e in graph.in_edges(relation.id, 'feeds')
        if e.source in graph.nodes and graph.nodes[e.source].kind == 'task'
    ]
    # A task that runs a SQL file is preferred, since that file's history can be read.
    for task in sorted(tasks, key=lambda t: not t.attrs.get('sql_path')):
        if task.attrs.get('sql_path'):
            return str(task.attrs['sql_path']), False
        notes.append(
            f'{graph.label(relation)} is written by the SQL inside task {graph.label(task)}, '
            'whose history is not read'
        )
        return None
    notes.append(f'{graph.label(relation)} is not defined by SQL in this repository')
    return None


def _formula_history(
    graph: Graph,
    root: Path,
    relation: Node | None,
    column: Node,
    explainer: Explainer,
    context: _Context,
    limit: int,
    result: History,
) -> None:
    if relation is None:
        return
    found = _defining_file(graph, relation, result.notes)
    if found is None:
        return
    path, is_model = found
    result.path = result.path or path
    located = _repository_of(Path(root) / path)
    if located is None:
        result.notes.append('not a git repository, so there is no history')
        return
    repository, repository_path = located
    if is_model:
        result.notes.append(
            'dbt macros and refs are rendered as they are now, so a change to a macro is not listed'
        )

    def formula_at(text: str | None) -> str | None:
        if text is None:
            return None
        if is_model:
            sql, catalog, schema = context.model_sql(relation, text)
            statements = [
                s
                for s in context.lineage(sql, catalog, schema, relation.relation)
                if s.kind in ('select', 'unparsed')
            ]
            statement = statements[-1] if statements else None
        else:
            # The last statement that writes the relation with columns, as the build and diff take.
            statement = next(
                (
                    s
                    for s in reversed(context.lineage(strip_templating(text), None, None, None))
                    if s.target == relation.relation and s.columns
                ),
                None,
            )
        if statement is None or statement.errors:
            return _UNKNOWN
        found = statement.expressions.get(column.name)
        if found is None:
            return None
        text = explainer.namer.text(found)
        return _UNKNOWN if '?' in text else text

    result.read_formulas = True
    known: str | None = None
    cut: list[Commit] = []
    for commit, at, before, after in _versions(
        repository, repository_path, limit, result.notes, result.label, cut
    ):
        old, new = formula_at(before), formula_at(after)
        if new == _UNKNOWN:
            result.unknown.append(commit)
            continue
        if old == _UNKNOWN:
            # Compare with the last formula that could be worked out; with none, this commit
            # cannot be said to have changed it.
            result.unknown.append(commit)
            if known is None:
                known = new
                continue
            old = known
        if old != new:
            result.formulas.append(FormulaChange(commit, at, old, new))
        known = new
    result.window = cut[0] if cut else None
    if result.unknown:
        hint = (
            ' (build with the dbt artefacts, target/manifest.json, to resolve refs)'
            if is_model and not context.manifests
            else ''
        )
        shown = ', '.join(c.short for c in result.unknown)
        result.notes.append(f'the formula could not be worked out at {shown}{hint}')


def _term_history(graph: Graph, root: Path, column: Node, limit: int, result: History) -> None:
    for edge in graph.in_edges(column.id, 'defines'):
        term = graph.nodes.get(edge.source)
        if term is None or term.kind != 'term' or not term.path or edge.attrs.get('role') == 'input':
            continue
        located = _repository_of(Path(root) / term.path)
        if located is None:
            continue
        repository, repository_path = located

        def facts(text: str | None, at: str, name: str = term.name) -> dict[str, Any]:
            parsed = definitions.parse(text, at) if text is not None else None
            entry = next((t for t in parsed.terms if t.name == name), None) if parsed else None
            if entry is None:
                return {}
            return {
                'status': entry.status,
                'approved': entry.approved,
                'owner': entry.owner.name if entry.owner else None,
                'definition': entry.definition,
            }

        for commit, at, before_text, after_text in _versions(
            repository, repository_path, limit, result.notes, f'term {term.name!r}'
        ):
            before, after = facts(before_text, at), facts(after_text, at)
            changed = {
                k: (before.get(k), after.get(k))
                for k in sorted(set(before) | set(after))
                if before.get(k) != after.get(k)
            }
            if changed:
                result.terms.append(TermChange(commit, term.name, at, changed, after.get('status')))
    result.terms.sort(key=lambda t: t.commit.order)
