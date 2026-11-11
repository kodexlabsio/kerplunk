"""Every result as text: the CLI's output on a terminal, and ``str(result)`` everywhere else.

Each renderer turns one result type into lines of :class:`~konstruct.interfaces.layout.Text`
with the same shape: a header line naming what the result is about, a one-line summary, then
sections of aligned tables, trees and key and value blocks. Each result's ``text(style)`` calls
:func:`show` with its renderer: with no style it gives the stable ASCII text that pipes,
``grep``, coding agents and the golden files read, and with a terminal's style the drawn output.

This module imports no result type at run time, so loading it loads no analysis.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from konstruct.interfaces.layout import (
    Branch,
    Cell,
    Part,
    Style,
    Text,
    bar,
    blank,
    command,
    formula,
    header,
    pad,
    pairs,
    paragraph,
    section,
    sql,
    table,
    tree,
    width_of,
    wrap,
)

if TYPE_CHECKING:
    from konstruct.analysis.checksql import HistoryCheck, SqlCheck
    from konstruct.analysis.conflicts import Conflicts
    from konstruct.analysis.diff import Diff
    from konstruct.analysis.english import English
    from konstruct.analysis.explain import Explanation
    from konstruct.analysis.gate import Gate
    from konstruct.analysis.history import History
    from konstruct.analysis.integrity import KeyCheck, LinkCheck
    from konstruct.analysis.laws import Laws
    from konstruct.analysis.lock import LockCheck, LockResult
    from konstruct.analysis.mutation import MutationTesting
    from konstruct.analysis.query import (
        Definitions,
        Description,
        EdgeItem,
        Item,
        Listing,
        PathResult,
        QueryResult,
        SearchResult,
        TermDefinition,
        Walk,
    )
    from konstruct.analysis.receipts import Receipt, ReceiptCheck
    from konstruct.analysis.release_notes import ReleaseNotes
    from konstruct.analysis.semantic import SimilarResult
    from konstruct.analysis.verify import Verification
    from konstruct.analysis.why import Why
    from konstruct.engine.trust import Trust
    from konstruct.interfaces.status import Status
    from konstruct.semantics.ontology import Ontology
    from konstruct.semantics.world import Connection, EntityDetail, Metric, Metrics

Lines = list[Text]

STATUS_ROLES = {'approved': 'ok', 'agreed': 'accent', 'draft': 'warn', 'retired': 'dim'}
CONFIDENCE_ROLES = {'verified': 'ok', 'declared': '', 'inferred': 'dim'}
EDGE_ROLES = {'transformed': 'accent', 'direct': 'dim', 'reference': '', 'run': 'dim', 'seed': 'dim'}


Result = TypeVar('Result')


def show(lines: Callable[[Result, Style], Lines], result: Result, style: Style | None = None) -> str:
    """``result`` drawn by its renderer ``lines``: plain text with no style, else for a terminal."""
    style = style or Style()
    return style.render(lines(result, style))


# Shared pieces


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f'{count} {word if count == 1 else plural or word + "s"}'


def _note(style: Style, value: str | None, *, indent: int = 2) -> Lines:
    """A description under a heading: dim, on one line in plain text and wrapped on a terminal."""
    if not value:
        return []
    return paragraph(style, Text((' '.join(value.split()), 'dim')), indent=indent)


def _summary(style: Style, value: str | Text) -> Lines:
    """A summary sentence, wrapped on a terminal rather than cut."""
    return paragraph(style, value, indent=0)


def _commands(value: str, role: str = '') -> Text:
    """Text in ``role`` with each command in backticks picked out in the accent colour."""
    if value.count('`') % 2:
        return Text((value, role))
    out = Text()
    for index, piece in enumerate(value.split('`')):
        out.append(('`' + piece + '`', 'accent') if index % 2 else (piece, role))
    return out


def _status(value: str | None) -> Part:
    return (value or '', STATUS_ROLES.get(value or '', ''))


def _confidence(value: str, check: str | None = None) -> Text:
    """A fact's confidence, and a failed check of it beside it, so a failure is never missed."""
    text = Text((value, CONFIDENCE_ROLES.get(value, '')))
    if check == 'failed':
        text.append(('  check failed', 'error'))
    return text


def _edge(entry: Item) -> Cell:
    word = entry.transform or entry.role or entry.command or ''
    return (word, EDGE_ROLES.get(word, ''))


def _place(entry: Item) -> Cell:
    """Where a node lives: its file, and the task whose SQL made the edge."""
    if entry.through:
        return (f'{entry.path}  via {entry.through}' if entry.path else f'via {entry.through}', 'dim')
    return (entry.path or '', 'dim')


def _facts(entry: Item, style: Style) -> list[Cell]:
    """How a node was reached and what it is: edge, verification, kind, type and place. One call
    per node, as a walk can reach tens of thousands of them, and plain strings when nothing is
    coloured."""
    edge = entry.transform or entry.role or entry.command or ''
    verified = entry.verified or ''
    through = entry.through
    path = entry.path or ''
    place = (f'{path}  via {through}' if path else f'via {through}') if through else path
    if not style.colour:
        return [edge, verified, entry.relation_kind or entry.kind, entry.data_type or '', place]
    return [
        (edge, EDGE_ROLES.get(edge, '')),
        (verified, 'ok' if verified == 'confirmed' else 'warn'),
        (entry.relation_kind or entry.kind, 'dim'),
        (entry.data_type or '', 'dim'),
        (place, 'dim'),
    ]


def _verified(entry: Item) -> Cell:
    return (entry.verified or '', 'ok' if entry.verified == 'confirmed' else 'warn')


def _kind(entry: Item) -> Cell:
    return (entry.relation_kind or entry.kind, 'dim')


def _type(entry: Item) -> Cell:
    return (entry.data_type or '', 'dim')


def _conditions(style: Style, values: Sequence[str], *, indent: int = 2) -> Lines:
    """Join, filter and grouping conditions, the clause aligned and the SQL highlighted."""
    from konstruct.analysis.explain import split_condition

    split = [split_condition(v) for v in values]
    width = max((len(p) for p, _ in split), default=0)
    lines: Lines = []
    for clause, rest in split:
        lead = Text(' ' * indent, (clause.ljust(width), 'keyword'), '  ' if clause else '')
        lines += wrap(style, sql(rest), first=lead, hanging=indent + width + 2)
    return lines


def _items(style: Style, entries: Sequence[Item], *, depth: bool = True) -> Lines:
    rows: list[list[Cell]] = []
    for entry in entries:
        hop: Cell = (str(entry.depth), 'dim') if depth and entry.depth is not None else None
        extra = (entry.command or entry.status or '', STATUS_ROLES.get(entry.status or '', 'dim'))
        rows.append([hop, entry.label, extra, _place(entry)])
    return table(style, rows, right=[0], flex=3)


def item_line(entry: Item) -> str:
    """One node on one line, as the tables show it: hops, name, edge, kind, type and file."""
    cells = [
        str(entry.depth) if entry.depth is not None else '',
        entry.label,
        entry.transform or entry.role or '',
        entry.verified or '',
        entry.relation_kind or entry.kind,
        entry.data_type or '',
        entry.status or entry.command or '',
        _place(entry)[0],  # type: ignore[index]
    ]
    return '  '.join(c for c in cells if c)


def edge_line(edge: EdgeItem) -> str:
    """One edge on one line, as the Connections section shows it."""
    extra = edge.transform or edge.role
    return f'{edge.source_label}  {edge.kind} ->  {edge.target_label}' + (f'  {extra}' if extra else '')


# Impact and trace


def walk_lines(walk: Walk, style: Style) -> Lines:
    target = walk.target
    title = 'Downstream of' if walk.direction == 'downstream' else 'Upstream of'
    lines = [header(title, target.label, target.relation_kind or target.kind, target.data_type)]
    lines += _note(style, target.description)
    lines += _summary(style, walk.summary())
    lineage = _lineage(walk, style)
    if lineage:
        lines += [blank(), section('Lineage'), *lineage]
    column = target.kind == 'column'
    implied = [r for r in walk.relations if r.depth is None]
    if column and implied:
        lines += [blank(), section('Relations', len(implied))]
        lines += table(style, [[r.label, _kind(r), _place(r)] for r in implied], flex=2)
    if not column and walk.columns:
        lines += [blank(), section('Columns', len(walk.columns))]
        rows = [[(str(c.depth), 'dim'), c.label, *_facts(c, style)] for c in walk.columns]
        lines += table(style, rows, right=[0], flex=6, drop=[4, 5, 3])
    if walk.filtered_by:
        lines += [blank(), section('Columns that filter or join its relation', len(walk.filtered_by))]
        rows = [[c.label, _kind(c), _type(c), _place(c)] for c in walk.filtered_by]
        lines += table(style, rows, flex=3)
    for key, heading in (('tests', 'Tests'), ('exposures', 'Exposures'), ('tasks', 'Orchestration')):
        entries: list[Item] = getattr(walk, key)
        if entries:
            lines += [blank(), section(heading, len(entries)), *_items(style, entries)]
    if walk.terms:
        lines += [blank(), section('Business terms', len(walk.terms))]
        lines += table(style, [[t.label, _status(t.status), _place(t)] for t in walk.terms], flex=2)
    if walk.entities:
        lines += [blank(), section('Business entities', len(walk.entities))]
        rows = [[name, (', '.join(relations), 'dim')] for name, relations in walk.entities.items()]
        lines += table(style, rows, flex=1)
    return lines


def _lineage(walk: Walk, style: Style) -> Lines:
    """The walk as a tree from the target: columns for a column, relations for a relation."""
    target = walk.target
    if target.kind == 'column':
        shown = [*walk.columns, *walk.joins_and_filters, *(r for r in walk.relations if r.depth is not None)]
    else:
        shown = [r for r in (*walk.relations, *walk.joins_and_filters) if r.depth is not None]
    if not shown:
        return []
    shown.sort(key=lambda i: (i.depth or 0, i.label))
    ids = {i.id for i in shown}
    start = set(walk.start)
    branches = {i.id: Branch(i.label, _facts(i, style)) for i in shown}
    root = Branch((target.label, 'subject'), ['', '', _kind(target), _type(target), _place(target)])

    def parent(node_id: str) -> str | None:
        seen: set[str] = set()
        current = walk.parents.get(node_id)
        while current is not None and current not in seen:
            seen.add(current)
            if current in ids:
                return current
            if current in start:
                return None
            owner = walk.owners.get(current)
            if target.kind != 'column' and owner is not None and owner in ids:
                return owner
            current = walk.parents.get(current)
        return None

    for entry in shown:
        above = parent(entry.id)
        (branches[above] if above is not None else root).children.append(branches[entry.id])
    return tree(style, root, flex=5, drop=[3, 4, 2])


# Path, search, list and describe


def path_lines(result: PathResult, style: Style) -> Lines:
    if not result.found:
        return [Text(f'No path between {result.start.label} and {result.end.label}.')]
    steps = len(result.steps) - 1
    lines = [
        header(
            'Path from',
            Text((result.start.label, 'subject'), ' to ', (result.end.label, 'subject')),
            _plural(steps, 'step'),
        )
    ]
    rows = table(style, [[s.label, _kind(s), _place(s)] for s in result.steps], flex=2)
    for index, (step, row) in enumerate(zip(result.steps, rows, strict=True)):
        if index:
            arrow = style.glyph('down' if step.direction == 'forward' else 'up')
            lines.append(Text('  ', (f'{arrow} {step.via}', 'dim')))
        lines.append(row)
    return lines


def search_lines(result: SearchResult, style: Style) -> Lines:
    if not result.results:
        return [Text(f'Nothing matches {result.query!r}.')]
    return _matches(style, result.results, indent=0)


def _basis(entry: Item) -> Cell:
    """What a match by meaning rests on, when it is more than the node's own words."""
    return (f'by {entry.matched}', 'accent') if entry.matched not in (None, 'words') else ''


def _matches(style: Style, entries: Sequence[Item], *, indent: int) -> Lines:
    # A search by meaning says what each match rests on; a word search has no such column.
    by_meaning = any(_basis(entry) for entry in entries)
    rows: list[list[Cell]] = []
    for entry in entries:
        kind = entry.relation_kind or entry.kind
        facts = (f'{kind} {entry.data_type}' if entry.data_type else kind, 'dim')
        cells: list[Cell] = [entry.label, facts, _status(entry.status)]
        if by_meaning:
            cells.append(_basis(entry))
        rows.append([*cells, _place(entry)])
    lines: Lines = []
    for entry, row in zip(entries, table(style, rows, indent=indent, flex=3), strict=True):
        lines.append(row)
        lines += _note(style, entry.description, indent=indent + 4)
    return lines


def similar_lines(result: SimilarResult, style: Style) -> Lines:
    if not result.results:
        return [Text(f'Nothing is like {result.node.label}.')]
    lines = [header('Like', result.node.label, _plural(len(result.results), 'node'))]
    return lines + _matches(style, result.results, indent=2)


def listing_lines(result: Listing, style: Style) -> Lines:
    if not result.items:
        return [Text(f'No {" or ".join(result.kinds)} nodes.')]
    rows: list[list[Cell]] = [[i.label, (i.kind, 'dim'), (i.path or '', 'dim')] for i in result.items]
    # Column headings only on a terminal: piped, every line is a record.
    headings = ['name', 'kind', 'path'] if style.width is not None else None
    return table(style, rows, indent=0, flex=2, headers=headings)


HIDDEN_ATTRS = frozenset(
    {
        'fqn',
        'dbt_name',
        'checksum',
        'view_sql',
        'expression',
        'conditions',
        'description',
        'nullable',
        'read_before',
        'star_reads',
    }
)


def description_lines(result: Description, style: Style) -> Lines:
    from konstruct.analysis.query import _attr_text

    node = result.node
    lines = [header('', node.label, node.relation_kind or node.kind, node.data_type)]
    lines += _note(style, node.description)
    facts: list[tuple[str, Cell]] = [
        ('id', Text((node.id, 'dim'))),
        ('relation', result.relation),
        ('path', node.path),
        ('formula', sql(result.expression) if result.expression else None),
        ('from', ', '.join(result.origins)),
    ]
    for key, value in result.attrs.items():
        if key not in HIDDEN_ATTRS and value not in (None, '', [], {}):
            facts.append((key.replace('_', ' '), _attr_text(value)))
    lines += [blank(), *pairs(style, facts)]
    if result.columns:
        lines += [blank(), section('Columns', len(result.columns))]
        rows = [[c.name, _type(c), (' '.join((c.description or '').split()), 'dim')] for c in result.columns]
        lines += table(style, rows, flex=2)
    for key, heading in result.LISTS:
        values: list[str] = getattr(result, key)
        if not values:
            continue
        lines += [blank(), section(heading, len(values))]
        if key == 'conditions':
            lines += _conditions(style, values)
        elif key == 'terms':
            lines += table(style, [[t.name, _status(t.status)] for t in result.neighbours[key]], flex=1)
        elif key in ('upstream', 'downstream'):
            lines += table(style, [[n.label, (n.kind, 'dim')] for n in result.neighbours[key]], flex=1)
        else:
            lines += table(style, [[v] for v in values])
    return lines


# Definitions


def term_lines(term: TermDefinition, style: Style, *, indent: int = 0) -> Lines:
    from konstruct.analysis.query import _attr_text, _where

    lines = [Text(' ' * indent, (term.name, 'subject'), '  ', _status(term.status))]
    if term.definition:
        lines += [*paragraph(style, term.definition, indent=indent + 2), blank()]
    columns = [c.label for c in term.columns]
    width = max((width_of(c) for c in columns), default=0)
    facts: list[tuple[str, Cell]] = [
        (
            'owner',
            _attr_text(term.owner)
            if term.owner
            else Text(('no owner', 'warn' if term.status == 'approved' else 'dim')),
        ),
        ('approved', term.approved),
        ('also called', ', '.join(term.synonyms)),
        ('grain', term.grain),
        ('unit', ' '.join(v for v in (term.unit, term.currency) if v)),
        ('required filters', sql('; '.join(term.filters)) if term.filters else None),
        ('exceptions', term.exceptions),
        ('related', ', '.join(term.related)),
        ('a kind of', ', '.join(term.broader)),
        ('kinds', ', '.join(term.narrower)),
        ('defined in', _where(term.path, term.line, term.iri)),
    ]
    if term.columns:
        for index, column in enumerate(term.columns):
            facts.append(
                (
                    'columns' if index == 0 else '',
                    Text(pad(column.label, width), '  ', (column.data_type or '', 'dim')),
                )
            )
    else:
        facts.append(('columns', Text(('none bound', 'warn'))))
    key_width = max(len(k) for k, v in facts if v)
    return [*lines, *pairs(style, facts, indent=indent + 2, width=key_width)]


def definitions_lines(result: Definitions, style: Style) -> Lines:
    if not result.terms:
        target = result.column.label if result.column else repr(result.query)
        return [Text(f'No business term defines {target}. Add one to definitions.yml.')]
    lines: Lines = []
    if result.column:
        lines += [header('Terms defining', result.column.label), blank()]
    for index, term in enumerate(result.terms):
        if index:
            lines.append(blank())
        lines += term_lines(term, style)
    return lines


# Free-text query


def query_lines(result: QueryResult, style: Style) -> Lines:
    if not result.matches:
        return [
            Text(
                f'Nothing in the graph matches {result.question!r}. Try `konstruct search` with a table or column name.'
            )
        ]
    lines: Lines = []
    if result.definitions:
        lines.append(section('Definitions'))
        for index, term in enumerate(result.definitions):
            if index:
                lines.append(blank())
            lines += term_lines(term, style, indent=2)
        lines.append(blank())
    lines += [section('Best matches'), *_matches(style, result.matches, indent=2)]
    if result.edges:
        shown = result.edges[: result.EDGE_LIMIT]
        arrow = style.glyph('arrow')
        rows: list[list[Cell]] = [
            [
                e.source_label,
                Text((f'{e.kind} {arrow}', 'dim')),
                e.target_label,
                Text((e.transform or e.role or '', EDGE_ROLES.get(e.transform or e.role or '', ''))),
            ]
            for e in shown
        ]
        lines += [blank(), section('Connections', len(result.edges)), *table(style, rows, flex=2)]
        if len(result.edges) > len(shown):
            lines.append(Text((f'  and {len(result.edges) - len(shown)} more', 'dim')))
    for walk in (result.impact, result.lineage):
        if walk is not None:
            lines += [blank(), *walk_lines(walk, style)]
    return lines


# Formulas


def explanation_lines(result: Explanation, style: Style) -> Lines:
    lines = [header('Formula of', result.label, result.data_type)]
    lines += _note(style, result.description)
    if result.expression is None:
        lines += [
            blank(),
            *paragraph(
                style,
                'No expression recorded: this column is read from a source, or the SQL that builds it '
                'could not be analysed.',
            ),
        ]
        return lines
    lines += [blank(), *formula(style, result.expression)]
    if result.composed and result.composed != result.expression:
        lines += [blank(), section('In terms of sources'), *formula(style, result.composed)]
    if not result.complete:
        lines.append(Text(('  partial: some columns on the way have no recorded expression', 'warn')))
    if result.sources:
        lines += [
            blank(),
            section('Sources', len(result.sources)),
            *table(style, [[s] for s in result.sources]),
        ]
    derived = [s for s in result.steps if s.expression is not None]
    if derived:
        lines += [blank(), section('Step by step', len(derived))]
        for step in derived:
            checked = bool(step.verified) and all(v == 'confirmed' for v in step.verified)
            lead = Text('  ', (step.label, 'strong'), (' = ', 'dim'))
            body = sql(step.expression or '')
            if checked:
                body.append('  ').append(('verified', 'ok'))
            lines += wrap(style, body, first=lead, hanging=6)
            lines += _conditions(style, step.conditions, indent=6)
    return lines


def english_lines(result: English, style: Style) -> Lines:
    lines = [header('In plain English', result.label)]
    if result.formula:
        lines += formula(style, result.formula)
    return [*lines, *_english(result, style)]


def english_section(result: English, style: Style) -> Lines:
    """The English under an explanation, as ``explain --english`` prints it."""
    return [section('In plain English'), *_english(result, style)]


def _english(result: English, style: Style) -> Lines:
    """The sentence, the rows each relation keeps, the grain, the terms and the names."""
    if result.sentence:
        lines = paragraph(style, result.sentence)
    elif result.english:
        lines = paragraph(style, f'{result.label} is {result.english}.')
    else:
        lines = paragraph(style, f'{result.label} is read from a source: it has no formula.')
    if not result.complete:
        lines.append(Text(('  partial: some columns on the way have no recorded expression', 'warn')))
    for rows in result.rows:
        lines += [blank(), Text('  ', ('In ', 'dim'), (rows.relation, 'strong'))]
        for text in rows.english:
            lines += wrap(style, Text(text), first=Text('    - '), hanging=6)
    if result.grain:
        lines += [blank(), *pairs(style, [('grain', result.grain)])]
    for term in result.terms:
        facts = [term.status or '', f'owner {term.owner}' if term.owner else '']
        if term.unit:
            facts.append(f'unit {term.unit}' + (f' ({term.currency})' if term.currency else ''))
        lead = Text('  ', ('term ', 'dim'), (repr(term.name), 'strong'))
        lines += [blank(), lead.append(('  ' + ', '.join(f for f in facts if f), 'dim'))]
        for text in term.english:
            lines += wrap(style, Text(('requires ', 'dim'), text), first=Text('    - '), hanging=6)
    if result.names:
        lines += [blank(), section('Names', len(result.names))]
        lines += table(style, [[n.wording, Text((n.column, 'dim'))] for n in result.names])
    if result.verbatim:
        lines += [blank(), section('Quoted as SQL', len(result.verbatim))]
        lines += _note(style, 'These parts have no wording in the grammar, so they are given as SQL.')
    return lines


# Changes


_CHANGE_ROLES = {'added': 'ok', 'removed': 'error', 'changed': 'warn'}


def diff_lines(result: Diff, style: Style) -> Lines:
    from konstruct.analysis.explain import split_condition

    files = len(result.source_files)
    lines = [
        header('Diff', f'{result.shown_base}..{result.shown_head}', _plural(files, 'file') + ' changed'),
        *_summary(style, result.summary()),
    ]
    width = 9

    def label(word: str, role: str = '') -> Text:
        return Text('  ', (word.ljust(width), role))

    def impact(data: dict[str, Any] | None) -> Lines:
        shown = result._impact_line(data)
        if not shown:
            return []
        return wrap(style, Text(('affects ', 'dim'), shown), first=label(''), hanging=width + 2)

    for relation in result.relations:
        if not relation.changed and not relation.note:
            continue
        lines += [
            blank(),
            Text(
                (relation.path, 'strong'), (f'  ({relation.kind} {relation.label}, {relation.status})', 'dim')
            ),
        ]
        if relation.note:
            lines += paragraph(style, Text((relation.note, 'warn')))
        for change in relation.columns:
            lines.append(
                Text(label(change.change, _CHANGE_ROLES.get(change.change, '')), (change.column, 'strong'))
            )
            if change.change == 'changed':
                for word, value in (('was', change.before), ('now', change.after)):
                    lines += formula(style, value or '', indent=width + 4, lead=f'{word}  ')
            for consumer in change.breaks:
                lines.append(Text(label(''), ('BREAKS', 'bad'), f' {consumer}, which still reads it'))
            lines += impact(change.impact)
        for sign, conditions in (('+', relation.conditions_added), ('-', relation.conditions_removed)):
            for condition in conditions:
                clause, rest = split_condition(condition)
                lead = Text(label('rows', 'accent'), (f'{sign} ', 'ok' if sign == '+' else 'error'))
                if clause:
                    lead.append((clause, 'keyword')).append(' ')
                lines += wrap(style, sql(rest), first=lead, hanging=width + 4)
        if relation.conditions_added or relation.conditions_removed:
            lines += impact(relation.rows_impact)
    for dag in result.dags:
        lines += [blank(), Text((dag.path, 'strong'), (f'  (DAG {dag.dag}, {dag.status})', 'dim'))]
        for word, entries, role in (
            ('task +', dag.tasks_added, 'ok'),
            ('task -', dag.tasks_removed, 'error'),
            ('after +', dag.dependencies_added, 'ok'),
            ('after -', dag.dependencies_removed, 'error'),
            ('runs', dag.runs_changed, 'warn'),
            ('sql', dag.sql_changed, 'warn'),
        ):
            lines += [label(word, role) + entry for entry in entries]
    for yaml_change in result.yaml:
        lines += [blank(), Text((yaml_change.path, 'strong'), ('  (dbt YAML)', 'dim'))]
        lines += [Text('  ', line) for line in yaml_change.lines()]
    for definitions_change in result.definitions:
        lines += [blank(), Text((definitions_change.path, 'strong'), ('  (business definitions)', 'dim'))]
        lines += [Text('  ', line) for line in definitions_change.lines()]
    if result.selection.modified_plus:
        lines += [blank(), Text(('dbt', 'title'), '  ', result.selection.summary())]
        lines += command(style, result.selection.command())
    if result.other:
        lines += [
            blank(),
            *paragraph(style, Text(('Not analysed: ', 'dim'), ', '.join(result.other)), indent=0),
        ]
    return lines


def gate_lines(result: Gate, style: Style) -> Lines:
    lines = _summary(
        style, Text((result.verdict, 'good' if result.passed else 'bad'), f': {result.detail()}')
    )
    for finding in result.findings:
        role = 'error' if finding.rule == 'breaking' else 'warn'
        lines += [
            blank(),
            *wrap(style, Text(finding.message), first=Text((finding.rule, role), ': '), hanging=2),
        ]
        facts: list[tuple[str, Cell]] = [('file', finding.path), ('fix', finding.fix)]
        lines += pairs(style, facts, width=4)
    if result.laws_from:
        lines += [blank(), *paragraph(style, f'Data laws: {result.laws_from}.', indent=0, role='dim')]
    return lines


def verification_lines(result: Verification, style: Style) -> Lines:
    facts = [f'seed {result.seed}', ', '.join(result.strategies), f'up to {result.max_rows} rows per input']
    lines = [header('Verified against', result.warehouse, *facts)]
    for line in result.summary().splitlines():
        lines += _summary(style, line)
    rows: list[list[Cell]] = []
    for m in result.models:
        if m.skipped:
            rows.append([m.label, Text((f'skipped: {m.skipped}', 'warn'))])
            continue
        confirmed = len(m.confirmed)
        rows.append(
            [
                m.label,
                str(m.claimed),
                Text((str(confirmed), 'ok' if confirmed == m.claimed else '')),
                _count(len(m.not_exercised), 'warn'),
                _count(len(m.through_references), ''),
                _count(len(m.unexplained), 'error'),
            ]
        )
    headings = ['model', 'claimed', 'confirmed', 'not exercised', 'via joins/filters', 'unexplained']
    lines += [blank(), *table(style, rows, right=[1, 2, 3, 4, 5], headers=headings, cap=None)]
    details: list[list[Cell]] = []
    for m in result.checked:
        for kind, entries in (('unexplained', m.unexplained), ('not exercised', m.not_exercised)):
            for output, source in entries:
                arrow = '<-' if not style.unicode else '←'
                details.append(
                    [
                        f'{m.label}.{output}',
                        Text((arrow, 'dim')),
                        source,
                        Text((kind, 'error' if kind == 'unexplained' else 'warn')),
                    ]
                )
    if details:
        lines += [blank(), *table(style, details[:40])]
        if len(details) > 40:
            lines.append(Text((f'  and {len(details) - 40} more in VERIFY.md', 'dim')))
    every: list[KeyCheck | LinkCheck] = [*result.keys, *result.links]
    checks = [c for c in every if c.state != 'confirmed']
    if checks:
        roles = {'failed': 'error', 'skipped': 'warn'}
        lines += [blank(), *table(style, [[Text((c.state, roles[c.state])), c.detail] for c in checks[:40]])]
        if len(checks) > 40:
            lines.append(Text((f'  and {len(checks) - 40} more in VERIFY.md', 'dim')))
    return lines


def mutation_lines(result: MutationTesting, style: Style) -> Lines:
    from konstruct.analysis.mutation import suggestion_name, where

    facts = [
        f'seed {result.seed}',
        f'up to {result.max_mutants} mutants per model',
        f'up to {result.max_rows} rows per input',
    ]
    lines = [header('Mutation testing against', result.warehouse, *facts)]
    for line in result.summary().splitlines():
        lines += _summary(style, line)
    rows: list[list[Cell]] = []
    for m in result.models:
        if m.skipped:
            rows.append([m.label, Text((f'skipped: {m.skipped}', 'warn'))])
            continue
        score = '-' if m.score is None else f'{100 * m.score:.0f}%'
        rows.append(
            [
                m.label,
                str(len(m.mutants)),
                _count(m.count('killed'), 'ok'),
                _count(m.count('survived'), 'warn'),
                _count(m.count('equivalent'), ''),
                _count(m.count('indistinguishable'), ''),
                _count(m.count('invalid'), 'error'),
                score,
            ]
        )
    headings = [
        'model',
        'mutants',
        'killed',
        'survived',
        'equivalent',
        'indistinguishable',
        'invalid',
        'score',
    ]
    if rows:
        lines += [blank(), *table(style, rows, right=[1, 2, 3, 4, 5, 6, 7], headers=headings, cap=None)]
    survivors = sorted(((m, s) for m in result.checked for s in m.survivors), key=lambda pair: -pair[1].reach)
    if survivors:
        lines += [blank(), section('Survivors, by downstream reach', len(survivors))]
        for model, survivor in survivors[:20]:
            changes = ', '.join(survivor.changed)
            lines += wrap(
                style,
                Text(f'{where(survivor.mutant)}; changes {changes}'),
                first=Text(f'  {model.label}: '),
                hanging=4,
            )
            if survivor.suggestions:
                best = survivor.suggestions[0]
                lines += _note(style, f'would be killed by {suggestion_name(best)}: {best.reason}', indent=4)
        if len(survivors) > 20:
            lines.append(Text((f'  and {len(survivors) - 20} more in MUTATION.md', 'dim')))
    unused = [(m, t) for m in result.checked for t in m.tests if t.state != 'used']
    if unused:
        lines += [blank(), section('Tests not used', len(unused))]
        for model, test in unused[:20]:
            reason = f': {test.reason}' if test.reason else ''
            lines += wrap(
                style,
                Text((f'({test.state}{reason})', 'dim')),
                first=Text(f'  {model.label}: {test.name} '),
                hanging=4,
            )
        if len(unused) > 20:
            lines.append(Text((f'  and {len(unused) - 20} more in MUTATION.md', 'dim')))
    return lines


def why_lines(result: Why, style: Style) -> Lines:
    from konstruct.analysis.why import key_text, show_value

    head = Text((f'{result.label} where {key_text(result.where)}', 'subject'), ': ')
    head.append((show_value(result.value), 'accent') if result.matches else ('no such row', 'warn'))
    lines = [head]
    if result.expression:
        lines += formula(style, result.expression)
    if result.composed and result.composed != result.expression:
        lines += formula(style, result.composed)
        lines[-1].append(('  (in terms of sources)', 'dim'))
    lines.append(blank())
    for line in result.explanation():
        lines += _summary(style, line)
    if result.contributions:
        heading = '/'.join(a.function for a in result.aggregates) or 'value'
        rows: list[list[Cell]] = [
            [
                ('* ' if c.chosen else '') + ', '.join(show_value(v) for v in c.values),
                '; '.join(r.text() for r in c.rows),
            ]
            for c in result.contributions
        ]
        lines += [blank(), section('Contributing rows', result.contributors)]
        lines += table(style, rows, right=[0], headers=[heading, 'rows'], cap=None)
        if result.contributors > len(result.contributions):
            shown = (
                f'  showing {len(result.contributions)} of {result.contributors}; raise the limit to see more'
            )
            lines.append(Text((shown, 'dim')))
    if result.excluded:
        left: list[list[Cell]] = [[e.row.text(), Text((e.reason, 'warn'))] for e in result.excluded]
        lines += [blank(), section('Left out', result.exclusions), *table(style, left, cap=None)]
        if result.exclusions > len(result.excluded):
            shown = f'  showing {len(result.excluded)} of {result.exclusions}; raise the limit to see more'
            lines.append(Text((shown, 'dim')))
    lines += [blank(), Text((f'Run read-only against {result.warehouse} in {result.seconds:.2f}s.', 'dim'))]
    return lines


# Receipts

_RECEIPT_ROLES = {
    'holds': 'good',
    'stale': 'bad',
    'unchecked': 'warn',
    'tampered': 'bad',
    'unreadable': 'warn',
}


def receipt_lines(result: Receipt, style: Style) -> Lines:
    from konstruct.analysis.receipts import term_summary, verification

    build = result.build
    facts = [f'issued {result.issued}', f'commit {build.commit[:7]}' if build.commit else 'outside git']
    lines = [header('Receipt', result.id, *facts, f'graph {build.graph[:19]}')]
    lines += pairs(style, [('question', result.question or ''), ('answer', result.answer or '')], indent=2)
    lines += [blank(), *_summary(style, result.citation)]
    for pin in result.pins:
        lines += [blank(), Text((pin.column, 'strong'))]
        if pin.formula:
            lines += formula(style, pin.formula)
        facts_: list[tuple[str, Cell]] = [('rows', c) for c in pin.conditions]
        facts_ += [
            ('sources', ', '.join(pin.sources)),
            ('type', pin.type or ''),
            ('verified', verification(pin)),
        ]
        if not pin.complete:
            facts_.append(
                ('formula', Text(('incomplete: a column on the way has no recorded expression', 'warn')))
            )
        facts_ += [('term', term_summary(t)) for t in pin.terms]
        lines += pairs(style, facts_, width=7)
    for term in result.terms:
        lines += [blank(), Text(('term ', 'dim'), (term_summary(term), 'strong'))]
        lines += _note(style, term.definition)
    notes = []
    if build.changed_inputs:
        notes.append(
            f'The graph was older than {_plural(len(build.changed_inputs), "changed input")} when it was '
            f'issued: {", ".join(build.changed_inputs[:5])}.'
        )
    if build.uncommitted:
        notes.append('Tracked files had uncommitted changes when it was issued.')
    if notes:
        lines.append(blank())
        for note in notes:
            lines += _summary(style, Text((note, 'warn')))
    return lines


def receipt_check_lines(result: ReceiptCheck, style: Style) -> Lines:
    lines = _summary(style, Text((result.summary(), 'good' if result.passed else 'bad')))
    for receipt in result.receipts:
        lead = Text((receipt.id, 'strong'), '  ', (receipt.status, _RECEIPT_ROLES[receipt.status]))
        if receipt.problem:
            lines += [blank(), *wrap(style, Text((receipt.problem, 'dim')), first=lead + '  ', hanging=4)]
            where = f'{receipt.file}, line {receipt.line}' if receipt.line else receipt.file
            lines.append(Text('    ', (where, 'dim')))
            continue
        about = [f'issued {receipt.issued}', 'graph rebuilt since' if receipt.rebuilt else 'same graph']
        lines += [blank(), lead + Text((f'  ({", ".join(about)})', 'dim'))]
        rows: list[tuple[str, Cell]] = [
            ('question', receipt.question or ''),
            ('rests on', ', '.join(receipt.refs)),
        ]
        lines += pairs(style, rows, indent=4)
        for change in receipt.changes:
            lines += [Text('    ', (change.label, 'accent'), (f'  {", ".join(change.reasons)}', 'warn'))]
            if change.removed:
                lines += paragraph(style, Text(('removed  ', 'dim'), change.removed), indent=6)
            for word, value in (('was', change.formula_before), ('now', change.formula_after)):
                if value and ('formula' in change.reasons or word == 'was'):
                    lines += formula(style, value, indent=6, lead=f'{word}  ')
            facts: list[tuple[str, Cell]] = [('condition +', c) for c in change.conditions_added]
            facts += [('condition -', c) for c in change.conditions_removed]
            facts += [('source +', c) for c in change.sources_added]
            facts += [('source -', c) for c in change.sources_removed]
            facts += [
                ('file', f'{f} changed, and its columns have no formula to compare') for f in change.files
            ]
            if change.type_before:
                facts.append(('type', f'{change.type_before} -> {change.type_after}'))
            facts += [('definition', t) for t in change.terms]
            facts += [('changed by', str(a)) for a in change.changed_by]
            lines += pairs(style, facts, indent=6, width=11)
        lines += pairs(style, [('definition', t) for t in receipt.terms], indent=4, width=11)
        for note in receipt.notes:
            lines += paragraph(style, Text(('note: ', 'dim'), note), indent=4, hanging=6)
    return lines


_LAW_ROLES = {'holds': 'ok', 'broken': 'error', 'unchecked': 'warn', 'proposed': 'dim'}


def laws_lines(result: Laws, style: Style) -> Lines:
    if result.warehouse is None:
        lines = [header('Data laws', 'proposed from the SQL', 'not checked')]
    else:
        lines = [header('Data laws checked against', result.warehouse, f'tolerance {result.tolerance:g}')]
    lines += _summary(style, result.summary())
    if result.laws:
        lines.append(blank())
    for law in result.laws:
        body = Text(law.statement)
        if law.detail:
            body.append((f'  ({law.detail})', 'dim'))
        lead = Text('  ', (law.status.ljust(9), _LAW_ROLES.get(law.status, '')), ' ')
        lines += wrap(style, body, first=lead, hanging=12)
        lines += [Text(' ' * 12, (failure.text(), 'dim')) for failure in law.failures[:3]]
        if law.kind == 'additivity' and law.evidence and law.status == 'broken':
            lines += paragraph(style, law.evidence, indent=12, role='dim')
    blocked = [b for b in result.blocked if b.measure and b.column]
    if blocked:
        lines += [blank(), section('Measures with no law', len(blocked))]
        rows: list[list[Cell]] = [
            [f'{b.model.rsplit(".", 1)[-1]}.{b.column}', Text((b.reason, 'dim'))] for b in blocked[:20]
        ]
        lines += table(style, rows)
        if len(blocked) > 20:
            lines.append(Text((f'  and {len(blocked) - 20} more; the JSON lists them all', 'dim')))
    return lines


def lock_lines(result: LockResult, style: Style) -> Lines:
    lines = _summary(style, result.summary())
    lines += [Text('  ', ('+', 'ok'), f' {c}') for c in result.added]
    lines += [Text('  ', ('~', 'warn'), f' {c}', (' (change accepted)', 'dim')) for c in result.updated]
    lines += [Text('  ', ('-', 'error'), f' {c}') for c in result.removed]
    lines += [
        Text('  ', ('!', 'warn'), f' {c} is read, but no longer built where it was') for c in result.missing
    ]
    if result.stale:
        lines += _summary(style, _commands(result.stale_advice(), 'warn'))
    return lines


def lock_check_lines(result: LockCheck, style: Style) -> Lines:
    lines = _summary(style, Text((result.summary(), 'good' if result.passed else 'bad')))
    lines += [Text(line) for line in result.relock_lines()]
    for change in result.changes:
        head, *rest = change.lines()
        lines += [blank(), Text((head, 'subject')), *(Text(line) for line in rest)]
    if result.notes:
        lines.append(blank())
        for note in result.notes:
            lines += paragraph(style, Text(('note:', 'dim'), ' ', _commands(note)), indent=0, hanging=6)
    if len(result.changes) > 1:
        lines += [blank(), Text('Accept every change: ', ('konstruct lock --update', 'accent'))]
    return lines


def _count(value: int, role: str) -> Text:
    return Text((str(value), role if value else 'dim'))


def history_lines(result: History, style: Style) -> Lines:
    from konstruct.analysis.history import Commit

    summary = result.findings()
    lines = [
        header('History of', result.label, result.path),
        *_summary(style, summary[:1].upper() + summary[1:] + '.'),
    ]
    events: dict[tuple[int, str], tuple[Commit, Lines]] = {}
    for change in result.formulas:
        body: Lines = []
        if change.before is None:
            body += formula(style, change.after or '', indent=4, lead='added  ')
        elif change.after is None:
            body += formula(style, change.before, indent=4, lead='removed  ')
        else:
            body += formula(style, change.before, indent=4, lead='was  ')
            body += formula(style, change.after, indent=4, lead='now  ')
        events.setdefault((change.commit.order, change.commit.sha), (change.commit, []))[1].extend(body)
    for term in result.terms:
        rows = [
            [
                Text(('term', 'dim')) if index == 0 else None,
                repr(term.term) if index == 0 else None,
                Text((key, 'dim')),
                Text(_shown(b), (f' {style.glyph("arrow")} ', 'dim'), _shown(a)),
            ]
            for index, (key, (b, a)) in enumerate(term.changes.items())
        ]
        events.setdefault((term.commit.order, term.commit.sha), (term.commit, []))[1].extend(
            table(style, rows, indent=4, flex=3)
        )
    for key in sorted(events):
        commit, body = events[key]
        lines += [blank(), style.fit(_commit(commit)), *body]
    if result.notes:
        lines.append(blank())
        for note in result.notes:
            lines += wrap(style, Text((note, 'dim')), first=Text('  ', ('note: ', 'dim')), hanging=8)
    return lines


def release_notes_lines(result: ReleaseNotes, style: Style) -> Lines:
    lines = [
        header(
            'Release notes', f'since {result.since}' if result.since else 'of the whole history', result.until
        ),
        *_summary(style, result.summary()[:1].upper() + result.summary()[1:] + '.'),
    ]
    for commit, changes in result.by_commit():
        lines += [blank(), style.fit(_commit(commit))]
        for change in changes:
            lines.append(Text('    ', (change.headline(), 'strong')))
            if change.kind == 'formula':
                if change.before is not None:
                    lines += formula(style, change.before, indent=6, lead='was  ')
                if change.after is not None:
                    lines += formula(style, change.after, indent=6, lead='now  ')
            for line in change.lines():
                lines += wrap(style, Text(line), first=Text('      - '), hanging=8)
    if result.warehouse:
        lines += [blank(), Text((f'Effects run read-only against {result.warehouse}.', 'dim'))]
    if result.notes:
        lines.append(blank())
        for note in result.notes:
            lines += wrap(style, Text((note, 'dim')), first=Text('  ', ('note: ', 'dim')), hanging=8)
    return lines


def _commit(commit: Any) -> Text:
    if not commit.sha:
        return Text('  ', ('working tree', 'accent'), (' (uncommitted)', 'dim'))
    return Text(
        '  ',
        (commit.date[:10], 'dim'),
        '  ',
        (commit.short, 'accent'),
        '  ',
        commit.author,
        ('  ', ''),
        (commit.subject, 'strong'),
    )


def _shown(value: Any) -> str:
    return 'none' if value in (None, '') else str(value)


def conflicts_lines(result: Conflicts, style: Style) -> Lines:
    from konstruct.analysis.conflicts import _reach_text
    from konstruct.analysis.explain import split_condition

    lines = _summary(style, Text((result.summary(), 'ok' if not result.conflicts else '')))
    current = None
    for conflict in result.conflicts:
        if conflict.idea != current:
            current = conflict.idea
            lines += [blank(), section(conflict.idea)]
        else:
            lines.append(blank())
        pair = Text('  ', (conflict.a.label, 'strong'), ('  vs  ', 'dim'), (conflict.b.label, 'strong'))
        lines += wrap(style, pair, hanging=4)
        why = f'Compared because they {conflict.reason}; downstream weight {conflict.score}.'
        lines += paragraph(style, Text((why, 'dim')), indent=4)
        for side, name in ((conflict.a, 'a'), (conflict.b, 'b')):
            formula_text = side.composed or side.expression
            lead = Text('    ', (name, 'accent'), '  ', side.label, (' = ', 'dim'))
            body = sql(formula_text) if formula_text else Text(('no recorded formula', 'warn'))
            lines += wrap(style, body, first=lead, hanging=7)
            lines.append(Text('       ', (f'in {side.path or "?"}; feeds {_reach_text(side.reach)}', 'dim')))
            if side.carried_into:
                lines.append(Text('       ', (f'carried into {", ".join(side.carried_into)}', 'dim')))
        for difference in conflict.differences:
            who = ''
            if difference.side:
                who = f' only in {conflict.a.label if difference.side == "a" else conflict.b.label}'
            lead = Text('    ', ('- ', 'dim'), (difference.kind, 'warn'), f'{who}: ')
            clause, rest = split_condition(difference.detail)
            body = Text((clause, 'keyword'), ' ', sql(rest)) if clause else Text(difference.detail)
            if difference.path:
                body.append((f'  ({difference.path})', 'dim'))
            lines += wrap(style, body, first=lead, hanging=6)
            if difference.witness is not None:
                # Values at which the two give different results, or the function they apply
                # differently.
                lines += paragraph(style, Text((str(difference.witness), 'dim')), indent=8)
    return lines


_SEVERITY_ROLES = {'error': 'error', 'warning': 'warn', 'info': 'dim'}


def sql_check_lines(result: SqlCheck, style: Style) -> Lines:
    role = 'bad' if not result.passed else 'ok' if not result.findings else ''
    lines = _summary(style, Text((result.summary(), role)))
    for finding in result.findings:
        lead = Text(
            (pad(finding.severity, 7), _SEVERITY_ROLES.get(finding.severity, '')),
            '  ',
            (finding.rule, 'strong'),
            '  ',
        )
        # Most messages begin with the column they are about; the others are led by it.
        named = finding.column and not finding.message.startswith(finding.column)
        where = Text((f'{finding.column}: ', 'accent')) if named else Text()
        lines += [blank(), *wrap(style, Text(where, finding.message), first=lead, hanging=9)]
        facts: list[tuple[str, Cell]] = [('fix', finding.fix), ('term', finding.term or '')]
        facts += [('evidence', item) for item in finding.evidence]
        lines += pairs(style, facts, indent=9, width=8)
    if result.corrected:
        lines += [blank(), section('Corrected SQL')]
        lines += [Text('  ', sql(line), whole=True) for line in result.corrected.splitlines()]
    return lines


def history_check_lines(result: HistoryCheck, style: Style) -> Lines:
    from konstruct.analysis.checksql import shadow_text

    lines = _summary(style, Text((result.summary(), 'ok' if not result.shadow else '')))
    lines += [line for note in result.notes for line in _note(style, note, indent=0)]
    if result.shadow:
        lines += [blank(), section('Shadow metrics', len(result.shadow))]
    for metric in result.shadow:
        verb = 'computes' if metric.queries == 1 else 'compute'
        body = Text(
            f'{_plural(metric.queries, "query", "queries")} by {_plural(metric.users, "person", "people")} '
            f'{verb} ',
            (repr(metric.term), 'strong'),
        )
        if metric.status:
            body.append(' (').append(_status(metric.status)).append(')')
        body.append(f' {shadow_text(metric)}')
        lines += [blank(), *wrap(style, body, indent=2, hanging=4)]
        facts: list[tuple[str, Cell]] = [
            ('rule', metric.rule),
            ('fix', metric.fix),
            ('people', ', '.join(metric.people)),
            ('e.g.', sql(metric.example) if metric.example else ''),
        ]
        lines += pairs(style, facts, indent=4)
    if result.adoption:
        lines += [blank(), section('Read through their own columns', len(result.adoption))]
        rows: list[list[Cell]] = [
            [item.term, str(item.queries), str(item.runs), str(item.users)] for item in result.adoption
        ]
        lines += table(style, rows, right=[1, 2, 3], headers=['term', 'queries', 'runs', 'people'])
    return lines


# The ontology


def ontology_lines(result: Ontology, style: Style) -> Lines:
    counts = [
        _plural(len(result.entities), 'entity', 'entities'),
        _plural(len(result.measures), 'measure'),
        _plural(len(result.relationships), 'relationship'),
        _plural(len(result.violations), 'rule violation'),
    ]
    violations = len(result.violations)
    summary = f'{", ".join(counts)}.' if violations else f'{", ".join(counts[:3])} and no rule violations.'
    lines = _summary(style, Text(('Ontology:', 'title'), ' ', summary))
    if result.entities:
        lines += [blank(), section('Entities', len(result.entities))]
        rows: list[list[Cell]] = [
            [
                Text((e.name, 'strong')),
                Text(('key ', 'dim'), e.key),
                Text((', '.join(e.relations), 'dim')),
                _confidence(e.confidence, e.check),
                Text((f'term {e.term!r}', 'dim')) if e.term else None,
            ]
            for e in result.entities
        ]
        lines += table(style, rows, flex=2)
    if result.measures:
        lines += [blank(), section('Measures', len(result.measures))]
        for measure in result.measures:
            line = Text('  ', (measure.name, 'strong'), '  ', _status(measure.status), '  ')
            line.append(_confidence(measure.confidence))
            about = ', '.join(x for x in (measure.owner, measure.grain) if x)
            if about:
                line.append((f'  {about}', 'dim'))
            lines.append(line)
            if measure.expression:
                reads = ', '.join(c.column for c in measure.columns) or 'no column'
                body = Text(sql(measure.expression), (f'  reads {reads}', 'dim'))
                lines += wrap(style, body, first=Text('    ', ('= ', 'dim')), hanging=6)
            rows = []
            # A metric's columns are its inputs, already listed with its expression.
            for column in [] if measure.expression else measure.columns:
                checked = Text()
                if column.edges:
                    role = 'ok' if column.verified == column.edges else 'dim'
                    checked = Text((f'verified {column.verified}/{column.edges}', role))
                if not column.complete:
                    checked.append(('  partial', 'warn'))
                rows.append([column.column, Text(('= ', 'dim'), sql(column.formula or '?')), checked])
            lines += table(style, rows, indent=4, flex=1)
    if result.relationships:
        lines += [blank(), section('Relationships', len(result.relationships))]
        arrow = style.glyph('arrow')
        rows = [
            [
                Text((r.source, 'strong'), (f' {arrow} ', 'dim'), (r.target, 'strong')),
                Text((r.cardinality or 'cardinality unknown', '' if r.cardinality else 'dim')),
                sql(r.via),
                _confidence(r.confidence, r.check),
                Text(('tested', 'ok')) if r.tested else None,
            ]
            for r in result.relationships
        ]
        lines += table(style, rows, flex=2)
    concepts = [c for c in result.concepts if c.broader]
    if concepts:
        lines += [blank(), section('Concepts', len(concepts))]
        lines += table(
            style, [[c.name, Text(('a kind of ', 'dim'), ', '.join(c.broader))] for c in concepts], flex=1
        )
    broken = [rule for rule in result.rules if rule.violations]
    if broken:
        lines += [blank(), section('Rule violations', violations)]
        for rule in broken:
            for violation in rule.violations:
                lines += wrap(style, Text(violation), first=Text('  ', (rule.name, 'error'), ': '), hanging=4)
    return lines


# The world model: one entity, metrics, and how two entities connect


def _additivity(aggregation: str, additive: bool | None) -> str:
    if additive is None:
        return f'{aggregation}, additivity unknown'
    return aggregation if additive else f'{aggregation}, not additive'


def metric_lines(metric: Metric, style: Style, *, indent: int = 0) -> Lines:
    marker = Text(('proposed', 'warn')) if metric.proposed else Text(_status(metric.status))
    head = Text(' ' * indent, (metric.name, 'subject'), '  ')
    if marker:
        head.append(marker).append('  ')
    head.append(_confidence(metric.confidence))
    lines = [head]
    if metric.definition:
        lines += _note(style, metric.definition, indent=indent + 2)
    facts: list[tuple[str, Cell]] = [
        ('formula', sql(metric.formula) if metric.formula else None),
        ('aggregates', metric.aggregation and _additivity(metric.aggregation, metric.additive)),
        ('additive', 'unknown' if metric.additive is None and not metric.aggregation else None),
        (
            'grain',
            metric.grain and f'{metric.grain}{", inferred" if metric.grain_confidence == "inferred" else ""}',
        ),
        ('owner', metric.owner),
        ('approved', metric.approved),
        ('unit', ' '.join(v for v in (metric.unit, metric.currency) if v)),
        ('required filters', sql('; '.join(metric.filters)) if metric.filters else None),
        ('source', metric.source),
        ('defined in', metric.path),
    ]
    for index, column in enumerate(metric.columns):
        checked = (
            Text(
                (
                    f'  verified {column.verified}/{column.edges}',
                    'ok' if column.verified == column.edges else 'dim',
                )
            )
            if column.edges
            else Text()
        )
        facts.append(('columns' if index == 0 else '', Text(column.column, checked)))
    for index, item in enumerate(metric.evidence):
        facts.append(('evidence' if index == 0 else '', Text((item, 'dim'))))
    return [*lines, *pairs(style, facts, indent=indent + 2, width=16)]


def metrics_lines(result: Metrics, style: Style) -> Lines:
    defined = [m for m in result.metrics if not m.proposed]
    proposed = len(result.metrics) - len(defined)
    summary = f'{_plural(len(defined), "defined metric")} and {_plural(proposed, "proposal")}'
    if proposed:
        summary += ', inferred from the code and approved by no one'
    if result.total > len(result.metrics):
        summary += f', of {result.total} found'
    lines = _summary(style, Text(('Metrics:', 'title'), ' ', f'{summary}.'))
    if result.query is None:
        rows: list[list[Cell]] = [
            [
                Text((m.name, 'strong')),
                Text(('proposed', 'warn')) if m.proposed else _status(m.status or m.source),
                _confidence(m.confidence),
                Text((m.grain or '', 'dim')),
                sql(m.formula) if m.formula else None,
            ]
            for m in result.metrics
        ]
        return [*lines, blank(), *table(style, rows, flex=4)]
    for metric in result.metrics:
        lines += [blank(), *metric_lines(metric, style)]
    return lines


def entity_lines(result: EntityDetail, style: Style) -> Lines:
    found = result.entity
    lines = [header('Entity', found.name, f'term {found.term!r}' if found.term else None)]
    lines += _note(style, found.description)
    facts: list[tuple[str, Cell]] = [
        ('key', found.key),
        ('held by', ', '.join(found.relations)),
        ('confidence', _confidence(found.confidence, found.check)),
    ]
    for index, item in enumerate(found.evidence):
        facts.append(('evidence' if index == 0 else '', Text((item, 'dim'))))
    lines += [blank(), *pairs(style, facts, width=10)]
    if result.properties:
        rows: list[list[Cell]] = [
            [
                Text((p.name, 'strong')),
                Text((p.data_type or '', 'dim')),
                Text(('refers to ', 'dim'), p.refers_to) if p.refers_to else None,
                Text((f'term {", ".join(p.terms)}', 'dim')) if p.terms else None,
                Text((p.description or '', 'dim')),
            ]
            for p in result.properties
        ]
        lines += [blank(), section('Properties', len(result.properties)), *table(style, rows, flex=4)]
    if result.relationships:
        arrow = style.glyph('arrow')
        rows = [
            [
                Text((r.source, 'strong'), (f' {arrow} ', 'dim'), (r.target, 'strong')),
                Text((r.cardinality or 'cardinality unknown', '' if r.cardinality else 'dim')),
                sql(r.via),
                _confidence(r.confidence, r.check),
            ]
            for r in result.relationships
        ]
        lines += [blank(), section('Relationships', len(result.relationships)), *table(style, rows, flex=2)]
    if result.metrics:
        rows = [
            [
                Text((m.name, 'strong')),
                Text(('proposed', 'warn')) if m.proposed else _status(m.status or m.source),
                _confidence(m.confidence),
                sql(m.formula) if m.formula else None,
            ]
            for m in result.metrics
        ]
        lines += [blank(), section('Metrics', len(result.metrics)), *table(style, rows, flex=3)]
    return lines


def connection_lines(result: Connection, style: Style) -> Lines:
    arrow = style.glyph('arrow')
    subject = Text((result.start, 'subject'), (f' {arrow} ', 'dim'), (result.end, 'subject'))
    if not result.found:
        return [
            header('No relationship connects', subject),
            Text(('  `konstruct path` shows how their relations connect in the lineage', 'dim')),
        ]
    shared = sum(h.same_row for h in result.hops)
    counted = [_plural(len(result.hops) - shared, 'relationship')]
    counted += [_plural(shared, 'shared row')] if shared else []
    lines = [header('How', subject, ', '.join(counted))]
    facts: list[tuple[str, Cell]] = [
        ('cardinality', result.cardinality or Text(('unknown', 'dim'))),
        ('confidence', _confidence(result.confidence or 'inferred', result.check)),
    ]
    lines += [blank(), *pairs(style, facts, width=11)]
    if result.check == 'failed':
        lines += _note(
            style,
            'konstruct verify found the data contradicting a relationship on the way: the joins can '
            'lose or repeat rows.',
        )
    if result.cardinality in ('one-to-many', 'many-to-many'):
        lines += _note(
            style, f'One {result.start} can match several {result.end} rows: aggregate them before joining.'
        )
    rows: list[list[Cell]] = [
        [
            Text((h.start, 'strong'), (f' {arrow} ', 'dim'), (h.end, 'strong')),
            Text((h.cardinality or 'cardinality unknown', '' if h.cardinality else 'dim')),
            sql(h.via),
            _confidence(h.confidence, h.check),
        ]
        for h in result.hops
    ]
    lines += [blank(), section('Path', len(result.hops)), *table(style, rows, flex=2)]
    if result.join:
        lines += [blank(), section('Join'), *[Text('  ', sql(line)) for line in result.join.splitlines()]]
    return lines


# Status


def status_lines(result: Status, style: Style) -> Lines:
    roles = {'ok': 'ok', 'attention': 'warn'}
    rows: list[tuple[str, Cell]] = [
        (key, _commands(value, roles.get(state, ''))) for key, value, state in result.rows()
    ]
    return [header('Status of', str(result.root)), blank(), *pairs(style, rows)]


def trust_lines(result: Trust, style: Style) -> Lines:
    advice = 'review it and run `konstruct trust`'
    summary, role = {
        'trusted': ('Trusted', 'good'),
        'environment': (f'Trusted by {result.environment_variable}', 'good'),
        'changed': (f'Not trusted: its settings changed since it was trusted; {advice}', 'bad'),
        'untrusted': (f'Not trusted; {advice}', 'bad'),
    }[result.state]
    if result.action == 'recorded':
        summary = 'Trusted: the record is saved'
    elif result.action == 'unchanged' and result.trusted:
        summary = 'Already trusted: the record is unchanged'
    elif result.action == 'revoked':
        summary = 'Trust revoked: the record is removed'
    elif result.action == 'unchanged':
        summary = 'Not trusted: there was no record to remove'
    covers = ', '.join(result.files) if result.files else 'nothing (no connections or paths)'
    projects = ', '.join(result.dbt_projects)
    rows: list[tuple[str, Cell]] = [
        ('covers', covers),
        ('reads', f'{", ".join(result.variables)} from the environment' if result.variables else ''),
        (
            'runs dbt',
            (f'in {projects}' if result.trusted else f'in {projects}, once trusted') if projects else '',
        ),
        ('sha256', result.sha256),
        ('record', str(result.record)),
    ]
    return [
        header('Trust of', str(result.root)),
        *_summary(style, _commands(summary, role)),
        blank(),
        *pairs(style, rows),
    ]


def build_lines(
    style: Style,
    root: str,
    sources: list[dict[str, str]],
    stats: dict[str, int],
    covered: int,
    total: int,
    reused: tuple[int, int] | None,
    warnings: list[str],
    wrote: str,
    report: str,
) -> Lines:
    """The summary `konstruct build` prints: what it read, what the graph holds, what it wrote."""
    keys = [s.get('input', '') for s in sources] + ['warnings']
    width = max(width_of(k) for k in keys)
    lines = [header('Built', root), blank()]
    rows: list[list[Cell]] = []
    if not sources:
        none = 'none found: no dbt project, DAG, SQL file or warehouse'
        rows.append([Text(('inputs'.ljust(width), 'dim')), Text((none, 'warn'))])
    names = []
    for source in sources:
        name = source.get('name') or source.get('path', '')
        if source.get('input') == 'dbt':
            name = f'{source["name"]} ({source["path"]})'
        names.append(name)
    name_width = max((width_of(n) for n in names if width_of(n) <= 40), default=0)
    for source, name in zip(sources, names, strict=True):
        failed = source.get('status') == 'failed'
        detail = Text((source.get('detail', ''), 'error' if failed else ''))
        note = Text((f'({source["note"]})', 'dim')) if source.get('note') else Text()
        key = Text((source.get('input', '').ljust(width), 'dim'))
        row: list[Cell] = [key, Text((name, 'error' if failed else 'strong'))]
        # A note that would not fit beside the detail goes on its own line under it, whole.
        room = style.width is None or 6 + width + name_width + len(detail) + 2 + len(note) <= style.width
        if note and room:
            rows.append([*row, Text(detail, '  ' if detail else '', note)])
        else:
            rows.append([*row, detail])
            if note:
                rows.append(['', '', note])
    lines += table(style, rows, flex=2, cap=40)
    counts = ', '.join(
        f'{count} {kind}{"s" if count != 1 else ""}'
        for kind, count in sorted(stats.items())
        if not kind.startswith('edge:') and kind != 'column'
    )
    facts: list[tuple[str, Cell]] = [('graph', f'{counts or "empty"}; {stats.get("column", 0)} columns')]
    if total:
        share = covered / total
        coverage = Text(f'{covered} of {total} columns of models and built tables ({100 * share:.0f}%)')
        drawn = bar(style, share)
        if drawn:
            coverage = Text(drawn, '  ', coverage)
        facts.append(('lineage', coverage))
    if reused is not None:
        facts.append(('reused', f'{reused[0]} of {reused[1]} cached analyses'))
    facts.append(('warnings', Text((str(len(warnings)), 'warn')) if warnings else Text(('none', 'ok'))))
    lines += [blank(), *pairs(style, facts[:-1], width=width)]
    lines += pairs(style, facts[-1:], width=width)
    for warning in warnings[:5]:
        lines += wrap(
            style,
            Text(warning),
            first=Text(' ' * (width + 4), (style.glyph('dot') + ' ', 'warn')),
            hanging=width + 6,
        )
    if len(warnings) > 5:
        lines.append(Text(' ' * (width + 4), (f'and {len(warnings) - 5} more in {report}', 'dim')))
    lines += pairs(style, [('wrote', wrote)], width=width)
    return lines
