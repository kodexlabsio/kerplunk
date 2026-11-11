"""Release notes for the measures of a data stack: ``konstruct release-notes --since v1.4``.

For every commit in a range, each dbt model and SQL file it changed is compared with its parent the
way ``konstruct diff`` compares a branch: by canonical expressions, so a commit that only reformats
SQL or renames a CTE changes nothing. Each changed column, and each relation whose joins, filters or
grouping changed, becomes one entry: the formula or conditions before and after, as SQL and in
plain English (:mod:`konstruct.analysis.english`), the commit with its author and date, what it
affects downstream (columns, dashboards and their owners, business terms), and for each business
term bound to it or downstream of it whether the term was approved at or after the commit, and by
whom. Changes to the business terms themselves (status, approval, owner, definition) in the range
are entries too.

The notes read only git and the graph. ``konstruct verify --effect`` adds what each change does on
today's data (:mod:`konstruct.analysis.effect`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlglot.dialects.dialect import Dialect

from konstruct.analysis.diff import (
    DATE,
    Context,
    DiffError,
    check_ref,
    compare_model,
    compare_sql_file,
    file_at,
    project_path,
    resolve_ref,
    run_git,
)
from konstruct.analysis.english import Condition, Names, Wording, column_keys, parse, word_condition
from konstruct.analysis.explain import Explainer
from konstruct.analysis.history import Commit, Repository, TermChange, term_history, utc
from konstruct.core.graph import Graph, Node
from konstruct.core.sql import PLACEHOLDER
from konstruct.inputs import dbt

if TYPE_CHECKING:
    from konstruct.analysis.effect import Effect
    from konstruct.interfaces.layout import Style

_SEPARATOR = '\x1f'


@dataclass
class Approval:
    """A business term bound to a changed column, or downstream of it, and whether it was approved
    after the change."""

    term: str
    """The term's name."""
    status: str | None
    """Its status now."""
    owner: str | None
    """Who owns it now."""
    approved: Commit | None = None
    """The first commit at or after the change that approved it; None when none has."""
    bound: bool = True
    """Whether the term is bound to the changed column itself (for a change to a relation's rows,
    to one of its columns), rather than downstream of it."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'term': self.term,
            'status': self.status,
            'owner': self.owner,
            'approved': self.approved.to_dict() if self.approved else None,
            'bound': self.bound,
        }

    def text(self) -> str:
        """A sentence on the approval: by whom and when, or that there has been none since."""
        where = 'bound to it' if self.bound else 'downstream'
        if self.approved is not None:
            when = (
                f'{self.approved.date[:10]} ({self.approved.short})'
                if self.approved.sha
                else 'the working tree'
            )
            return f'{self.term!r} ({where}) was approved in a commit by {self.approved.author} on {when}, after this change'
        if self.status == 'approved':
            return f'{self.term!r} ({where}) is approved, but not since this change'
        status = f'is {self.status} and ' if self.status else ''
        return f'{self.term!r} ({where}) {status}has not been approved since this change'


@dataclass
class Affected:
    """What is downstream of a change."""

    columns: int = 0
    """Columns downstream."""
    relations: int = 0
    """Tables and models downstream, including those that join or filter on it."""
    exposures: list[str] = field(default_factory=list)
    """Dashboards and other dbt exposures downstream, with their owners."""
    terms: list[str] = field(default_factory=list)
    """Business terms bound to the column or downstream of it."""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def text(self) -> str | None:
        parts = []
        if self.columns:
            parts.append(f'{self.columns} column{"s" if self.columns != 1 else ""}')
        if self.relations:
            parts.append(f'{self.relations} relation{"s" if self.relations != 1 else ""}')
        if self.exposures:
            parts.append(f'the exposure{"s" if len(self.exposures) != 1 else ""} {", ".join(self.exposures)}')
        return f'affects {", ".join(parts)} downstream' if parts else None


@dataclass
class Change:
    """One entry of the notes: a column whose formula changed, a relation whose rows changed, or a
    business term whose definition changed, in one commit."""

    commit: Commit
    """The commit."""
    path: str
    """The file it changed, relative to the project."""
    kind: str
    """``formula`` (a column added, removed or changed), ``rows`` (joins, filters or grouping) or
    ``term`` (a business term's status, approval, owner or definition)."""
    status: str
    """For a formula, ``added``, ``removed`` or ``changed``; for rows and terms, ``changed``."""
    relation: str | None = None
    """The relation, by label; None for a term."""
    column: str | None = None
    """The column, by label, for a formula."""
    term: str | None = None
    """The term's name, for a term."""
    node_id: str | None = None
    """The relation's or column's graph id, when the graph has it."""
    previous_path: str | None = None
    """Where the file was before the commit, relative to the project, when the commit moved it."""
    before: str | None = None
    """The formula before, as SQL with short names."""
    after: str | None = None
    """The formula after."""
    before_english: str | None = None
    """``before`` in plain English."""
    after_english: str | None = None
    """``after`` in plain English."""
    added: list[str] = field(default_factory=list)
    """For rows, the conditions only after the commit, as SQL."""
    removed: list[str] = field(default_factory=list)
    """For rows, the conditions only before it."""
    added_english: list[str] = field(default_factory=list)
    """``added`` in plain English."""
    removed_english: list[str] = field(default_factory=list)
    """``removed`` in plain English."""
    fields: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    """For a term, each changed field with its value before and after."""
    affected: Affected = field(default_factory=Affected)
    """What is downstream."""
    approvals: list[Approval] = field(default_factory=list)
    """The business terms bound to the column or downstream of it, and whether each was approved
    after this change."""
    effect: Effect | None = None
    """What the change does on today's data, from ``konstruct verify --effect``."""

    @property
    def subject(self) -> str:
        return self.column or self.term or self.relation or self.path

    def headline(self) -> str:
        """What changed, in a few words."""
        if self.kind == 'term':
            return f'term {self.term!r}: ' + ', '.join(sorted(self.fields)) + ' changed'
        if self.kind == 'rows':
            return f'{self.relation}: joins, filters or grouping changed'
        return f'{self.column}: formula {self.status}'

    def lines(self) -> list[str]:
        """The entry as sentences, for text and markdown."""
        out = []
        if self.kind == 'formula':
            if self.before_english is not None and self.after_english is not None:
                out.append(f'it was {self.before_english}; it is now {self.after_english}')
            elif self.after_english is not None:
                out.append(f'it is {self.after_english}')
            elif self.before_english is not None:
                out.append(f'it was {self.before_english}')
        out += [f'no longer: {t}' for t in self.removed_english]
        out += [f'now: {t}' for t in self.added_english]
        for name, (before, after) in self.fields.items():
            out.append(f'{name}: {_shown(before)} to {_shown(after)}')
        affected = self.affected.text()
        if affected:
            out.append(affected)
        out += [a.text() for a in self.approvals]
        if self.effect is not None:
            out.append(self.effect.summary())
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            'commit': self.commit.to_dict(),
            'path': self.path,
            'kind': self.kind,
            'status': self.status,
            'relation': self.relation,
            'column': self.column,
            'term': self.term,
            'node_id': self.node_id,
            'previous_path': self.previous_path,
            'headline': self.headline(),
            'before': self.before,
            'after': self.after,
            'before_english': self.before_english,
            'after_english': self.after_english,
            'added': self.added,
            'removed': self.removed,
            'added_english': self.added_english,
            'removed_english': self.removed_english,
            'fields': {k: {'before': b, 'after': a} for k, (b, a) in self.fields.items()},
            'affected': self.affected.to_dict(),
            'approvals': [a.to_dict() for a in self.approvals],
            'effect': self.effect.to_dict() if self.effect is not None else None,
        }


@dataclass
class ReleaseNotes:
    """The changes to formulas, rows and business terms in a range of commits, newest first."""

    since: str | None
    """Where the range starts: a commit, tag or date, or None for the whole history."""
    until: str
    """Where it ends."""
    changes: list[Change] = field(default_factory=list)
    """The entries, newest commit first."""
    commits: int = 0
    """Commits read in the range."""
    notes: list[str] = field(default_factory=list)
    """What could not be read, and why."""
    warehouse: str | None = None
    """Where ``konstruct verify --effect`` ran the changes, when it did."""

    def summary(self) -> str:
        count = len(self.changes)
        shown = {c.commit.sha for c in self.changes}
        range_ = f'since {self.since}' if self.since else 'in the whole history'
        return (
            f'{count} change{"s" if count != 1 else ""} in {len(shown)} of {self.commits} '
            f'commit{"s" if self.commits != 1 else ""} {range_}'
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'since': self.since,
            'until': self.until,
            'summary': self.summary(),
            'commits': self.commits,
            'changes': [c.to_dict() for c in self.changes],
            'notes': self.notes,
            'warehouse': self.warehouse,
        }

    def markdown(self) -> str:
        """The notes as Markdown, to post to a wiki, a chat channel or a pull request."""
        out = [
            f'## Release notes {f"since {self.since}" if self.since else ""}'.rstrip(),
            '',
            self.summary() + '.',
            '',
        ]
        for commit, changes in self.by_commit():
            when = f'{commit.date[:10]} `{commit.short}`' if commit.sha else 'working tree'
            out += [f'### {when} {commit.author}: {commit.subject}', '']
            for change in changes:
                out.append(f'- **{change.headline()}**')
                out += [f'  - {line}' for line in change.lines()]
            out.append('')
        if self.notes:
            out += ['Notes:', '', *[f'- {n}' for n in self.notes], '']
        return '\n'.join(out).rstrip() + '\n'

    def by_commit(self) -> list[tuple[Commit, list[Change]]]:
        """The changes grouped by commit, newest first."""
        groups: dict[str, tuple[Commit, list[Change]]] = {}
        for change in self.changes:
            groups.setdefault(change.commit.sha, (change.commit, []))[1].append(change)
        return list(groups.values())

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import release_notes_lines, show

        return show(release_notes_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def _shown(value: Any) -> str:
    return 'none' if value in (None, '') else str(value)


# Reading the range


def latest_tag(root: Path, until: str = 'HEAD') -> str | None:
    """The most recent tag before ``until``, reachable from its parent, where the notes start by
    default: so the notes up to a release's own tag cover that release."""
    found = run_git(root, 'describe', '--tags', '--abbrev=0', f'{until}^', check=False).strip()
    return found or None


def _range(root: Path, since: str | None, end: str) -> list[str]:
    """``git log`` arguments for the commits after ``since`` up to the commit ``end``."""
    if since is None:
        return [end]
    check_ref(since)
    is_ref = bool(
        run_git(root, 'rev-parse', '--verify', '--quiet', f'{since}^{{commit}}', check=False).strip()
    )
    if is_ref:
        return [f'{since}..{end}']
    if not DATE.match(since):
        raise DiffError(f'{since!r} is not a commit, branch, tag or date')
    return [f'--since={since}', end]


@dataclass
class _File:
    """One file a commit changed: where it was before the commit and after it, relative to the
    repository (the same path unless the commit moved it), and where it is now in the project."""

    before: str | None
    after: str | None
    now: str


def release_notes(
    graph: Graph,
    root: Path,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
) -> ReleaseNotes:
    """The changes to formulas, rows and business terms between ``since`` (a commit, tag or date;
    by default the latest tag reachable from ``until``, or the whole history when there is none)
    and ``until`` (default HEAD), newest first. At most ``limit`` commits are read. A file a commit
    moves is compared with itself at its old path."""
    if limit < 1:
        raise ValueError('limit must be at least 1')
    top = Path(run_git(root, 'rev-parse', '--show-toplevel').strip())
    end = resolve_ref(root, until) if until else 'HEAD'
    given = since
    if since is None:
        since = latest_tag(root, end)
    result = ReleaseNotes(since=since, until=until or 'HEAD')
    if given is None and since is not None:
        result.notes.append(f'from the latest tag, {since}; pass --since for another start')
    prefix = Path(root).resolve().relative_to(top.resolve())
    output = run_git(
        top,
        '-c',
        'core.quotePath=false',
        'log',
        '--no-merges',
        f'--max-count={limit}',
        '--name-status',
        '-M',
        f'--format=%x1e%H{_SEPARATOR}%an{_SEPARATOR}%cI{_SEPARATOR}%s',
        *_range(root, since, end),
        '--',
        str(prefix) if str(prefix) != '.' else '.',
    )
    repository = Repository(top)
    commits: list[tuple[Commit, list[_File]]] = []
    # Newest first, as git lists them, so each file's path now is known from the moves after it.
    now: dict[str, str] = {}
    for block in output.split('\x1e'):
        lines = [line for line in block.strip().split('\n') if line.strip()]
        if not lines:
            continue
        sha, author, date, subject = [*lines[0].split(_SEPARATOR), '', '', ''][:4]
        commit = Commit(sha, author, utc(date), subject, repository.order.get(sha, -1), top)
        files = []
        moves = {}
        for line in lines[1:]:
            status, *paths = line.split('\t')
            if status.startswith('R') and len(paths) == 2:
                old, new = paths
                files.append(_File(old, new, now.get(new, new)))
                moves[old] = now.get(new, new)
            elif paths:
                path = paths[-1]
                files.append(
                    _File(
                        None if status.startswith('A') else path,
                        None if status.startswith('D') else path,
                        now.get(path, path),
                    )
                )
        now.update(moves)
        commits.append((commit, files))
    commits.reverse()
    if len(commits) >= limit:
        result.notes.append(f'only the last {limit} commits were read; raise the limit to read more')
    if repository.shallow and commits and not repository.has_parent(commits[0][0].sha):
        first = commits.pop(0)[0]
        result.notes.append(
            f'history before {first.short} is not in this shallow clone, so {first.short} could not be '
            'compared with its parent; fetch more history (git fetch --unshallow) to include it'
        )
    result.commits = len(commits)
    _Reader(graph, root, top, prefix, result, limit).read(commits)
    return result


class _Reader:
    """Turns the commits of a range into entries."""

    def __init__(
        self, graph: Graph, root: Path, top: Path, prefix: Path, result: ReleaseNotes, limit: int
    ) -> None:
        self.graph = graph
        self.root = root
        self.top = top
        self.prefix = prefix
        self.result = result
        self.limit = limit
        self.context = Context(graph, root)
        self.explainer = Explainer(graph)
        dialect = graph.meta.get('dialect')
        self.dialect_name = dialect if isinstance(dialect, str) else None
        self.dialect = Dialect.get_or_raise(self.dialect_name)
        self.by_path: dict[str, list[Node]] = {}
        for node in graph.nodes.values():
            if node.path:
                self.by_path.setdefault(node.path, []).append(node)
            sql_path = node.attrs.get('sql_path')
            if sql_path:
                self.by_path.setdefault(str(sql_path), []).append(node)
        self.terms: dict[str, list[TermChange]] = {}

    def read(self, commits: list[tuple[Commit, list[_File]]]) -> None:
        in_range = {c.sha for c, _ in commits}
        term_files: set[str] = set()
        for commit, files in commits:
            for changed in self._moves(commit, files):
                now = project_path(changed.now, self.prefix)
                if not now or set(Path(now).parts) & dbt.SKIP_DIRS:
                    continue
                if Path(now).name in ('definitions.yml', 'definitions.yaml'):
                    term_files.add(now)
                    continue
                self._file(commit, changed, now)
        for term in self.graph.nodes.values():
            if term.kind == 'term' and term.path in term_files:
                for change in self._term_changes(term):
                    if change.commit.sha in in_range:
                        self.result.changes.append(
                            Change(
                                commit=change.commit,
                                path=change.path,
                                kind='term',
                                status='changed',
                                term=change.term,
                                node_id=term.id,
                                fields=dict(change.changes),
                            )
                        )
        self.result.changes.sort(key=lambda c: (-c.commit.order, c.kind != 'rows', c.subject))

    def _moves(self, commit: Commit, files: list[_File]) -> list[_File]:
        """``files`` with a SQL file deleted and one added that build the same relations taken as
        one file moved: git pairs a move only when the text is mostly the same, and a small file
        moved and edited in one commit is not."""
        added = [f for f in files if f.before is None and f.after and f.after.endswith('.sql')]
        deleted = [f for f in files if f.after is None and f.before and f.before.endswith('.sql')]
        if not added or not deleted:
            return files

        def builds(path: str, ref: str) -> frozenset[str]:
            text = file_at(self.top, ref, path)
            nodes = self.by_path.get(project_path(path, self.prefix), [])
            if text is None or any(n.kind in ('model', 'snapshot') and 'dbt' in n.origins for n in nodes):
                return frozenset()  # a dbt model is named after its file, so a new file is a new model
            return frozenset(r.label for r in compare_sql_file(self.context, path, None, text))

        sources = {id(f): builds(f.before or '', f'{commit.sha}^') for f in deleted}
        kept = [f for f in files if f not in added and f not in deleted]
        for new in added:
            targets = builds(new.after or '', commit.sha)
            old = next((f for f in deleted if targets and sources[id(f)] == targets), None)
            if old is None:
                kept.append(new)
                continue
            deleted.remove(old)
            kept.append(_File(old.before, new.after, new.now))
        return kept + deleted

    def _file(self, commit: Commit, changed: _File, now: str) -> None:
        """The entries for one file ``commit`` changed, known in the graph at ``now``."""
        nodes = self.by_path.get(now, [])
        models = [n for n in nodes if n.kind in ('model', 'snapshot') and 'dbt' in n.origins]
        if not models and not now.endswith('.sql'):
            return
        before = file_at(self.top, f'{commit.sha}^', changed.before) if changed.before else None
        after = file_at(self.top, commit.sha, changed.after) if changed.after else None
        if before is None and after is None:
            return
        in_project = project_path(changed.after or changed.before or changed.now, self.prefix)
        moved = (
            project_path(changed.before, self.prefix)
            if changed.before and changed.after and changed.before != changed.after
            else None
        )
        relations = (
            [compare_model(self.context, m, in_project, before, after) for m in models]
            if models
            else compare_sql_file(self.context, in_project, before, after)
        )
        if PLACEHOLDER in (before or '') + (after or '') or any(
            PLACEHOLDER in c for r in relations for c in r.conditions_added + r.conditions_removed
        ):
            self.result.notes.append(
                f'{in_project} in {commit.short} is templated where it shows {PLACEHOLDER!r}: '
                'Konstruct could not render that part'
            )
        for relation in relations:
            if relation.note:
                self.result.notes.append(f'{relation.label} in {commit.short}: {relation.note}')
            node = self.graph.nodes.get(relation.node_id or '')
            for column in relation.columns:
                target = self.graph.column(node.id, column.column) if node is not None else None
                change = Change(
                    commit=commit,
                    path=in_project,
                    previous_path=moved,
                    kind='formula',
                    status=column.change,
                    relation=relation.label,
                    column=f'{relation.label}.{column.column}',
                    node_id=target.id if target is not None else None,
                    before=self.explainer.namer.text(column.before) if column.before else None,
                    after=self.explainer.namer.text(column.after) if column.after else None,
                )
                self._word(change)
                self._downstream(change, column.impact, [target] if target is not None else [])
                self.result.changes.append(change)
            if relation.conditions_added or relation.conditions_removed:
                change = Change(
                    commit=commit,
                    path=in_project,
                    previous_path=moved,
                    kind='rows',
                    status='changed',
                    relation=relation.label,
                    node_id=relation.node_id,
                    added=[self.explainer.namer.condition(c) for c in relation.conditions_added],
                    removed=[self.explainer.namer.condition(c) for c in relation.conditions_removed],
                )
                self._word(change)
                self._downstream(
                    change, relation.rows_impact, self.graph.columns_of(node.id) if node is not None else []
                )
                self.result.changes.append(change)

    def _word(self, change: Change) -> None:
        """The formulas and conditions of ``change`` in English, with one set of names for all."""
        formulas = {
            k: parse(v, self.dialect) for k, v in (('before', change.before), ('after', change.after)) if v
        }
        added = [Condition(c, self.dialect) for c in change.added]
        removed = [Condition(c, self.dialect) for c in change.removed]
        trees = [t for t in formulas.values() if t is not None]
        trees += [t for c in added + removed for t in c.trees]
        wording = Wording(self.dialect_name, Names(column_keys(trees)))
        for side, tree in formulas.items():
            text = wording.sentence(tree) if tree is not None else f'`{getattr(change, side)}`'
            setattr(change, f'{side}_english', text)
        change.added_english = [word_condition(c, wording) for c in added]
        change.removed_english = [word_condition(c, wording) for c in removed]

    def _downstream(self, change: Change, impact: dict[str, Any] | None, columns: list[Node]) -> None:
        bound = [
            self.graph.nodes[e.source]
            for column in columns
            for e in self.graph.in_edges(column.id, 'defines')
            if e.source in self.graph.nodes
            and self.graph.nodes[e.source].kind == 'term'
            and e.attrs.get('role') != 'input'
        ]
        downstream = []
        if impact:
            change.affected.columns = len(impact.get('columns', []))
            change.affected.relations = len(impact.get('relations', [])) + len(
                impact.get('joins_and_filters', [])
            )
            for item in impact.get('exposures', []):
                node = self.graph.nodes.get(item['id'])
                owner = node.attrs.get('owner') if node is not None else None
                change.affected.exposures.append(
                    f'{item["label"]} (owner {owner})' if owner else item['label']
                )
            downstream = [
                self.graph.nodes[i['id']] for i in impact.get('terms', []) if i['id'] in self.graph.nodes
            ]
        seen: set[str] = set()
        for term, is_bound in [*((t, True) for t in bound), *((t, False) for t in downstream)]:
            if term.id in seen or term.attrs.get('registry'):
                continue
            seen.add(term.id)
            change.affected.terms.append(term.name)
            owner = term.attrs.get('owner')
            approval = Approval(
                term=term.name,
                status=term.attrs.get('status'),
                owner=owner.get('name') if isinstance(owner, dict) else None,
                bound=is_bound,
            )
            approval.approved = next(
                (
                    t.commit
                    for t in self._term_changes(term)
                    if t.approved and t.commit.at_or_after(change.commit)
                ),
                None,
            )
            change.approvals.append(approval)

    def _term_changes(self, term: Node) -> list[TermChange]:
        if term.id not in self.terms:
            notes: list[str] = []
            self.terms[term.id] = term_history(self.root, term, self.limit, notes)
            self.result.notes.extend(n for n in notes if n not in self.result.notes)
        return self.terms[term.id]
