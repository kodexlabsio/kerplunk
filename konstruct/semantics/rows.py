"""Whether a column's formula, composed down to source columns, still counts the rows the column does.

Composing keeps a formula's expression but not the query around it. ``SUM(amount)`` composed down
to ``SUM(raw_orders.amount)`` aggregates every source row once; the column it came from may have
aggregated fewer rows (a filter, an inner join that drops unmatched rows) or more (a join that
matches several rows, a cross join, a union). Each relation on the way down is checked:

- a ``WHERE``, ``HAVING``, ``QUALIFY``, ``LIMIT``, ``OFFSET`` or ``DISTINCT``, an anti join, or a
  ``WHERE`` on the nullable side of an outer join changes the rows;
- a ``UNION`` that removes duplicates, or any ``INTERSECT`` or ``EXCEPT``, changes the rows;
- a join from a table the column reads is safe only when the other side's join column is known to
  be unique (:func:`konstruct.semantics.ontology.unique_columns`), so that it matches at most one
  row. An inner or semi join also drops no row only when a ``not_null`` test and a
  ``relationships`` test on that exact foreign key column, targeting the joined column, vouch that
  every row matches; so does an outer join when the column's table is read only on its nullable
  side (the relation's ``nullable`` attribute), or is joined through such a table, since its rows
  then reach the result only where they match;
- a self join is safe only when both of its columns are unique, and a join between two tables the
  column reads is checked both ways;
- a table read with no join condition that links it so (a cross join, a comma join) changes the
  rows, and so does a source that makes rows of its own (``ROWS FROM``: a table function, VALUES,
  UNNEST, LATERAL or LATERAL VIEW).

The rows a PIVOT or UNPIVOT makes, or a recursive CTE adds, are not recorded, so are not checked.
"""

from __future__ import annotations

from konstruct.core.graph import RELATION_KINDS, Graph, Node
from konstruct.semantics.ontology import join_columns, unique_columns

# The conditions that decide which rows a relation keeps, by their label without what follows it.
# UNION, INTERSECT and EXCEPT are recorded only when they remove rows (UNION DISTINCT, any INTERSECT
# or EXCEPT); a UNION ALL is in each column's formula instead.
# ROWS FROM is a source that makes rows: a table function, VALUES, an UNNEST or a LATERAL.
_KEEPS = (
    'WHERE', 'HAVING', 'QUALIFY', 'LIMIT', 'OFFSET', 'DISTINCT', 'UNION', 'INTERSECT', 'EXCEPT', 'ROWS FROM',
)  # fmt: skip


class Rows:
    """Answers :meth:`changed` for the columns of one graph."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self.unique = unique_columns(graph)
        self.not_null: set[str] = set()
        self.references: set[tuple[str, str]] = set()  # (foreign key, the column it refers to)
        self.memo: dict[tuple[str, frozenset[str]], str | None] = {}  # (relation, ours) -> change
        for node in graph.nodes.values():
            if node.kind != 'test':
                continue
            edges = graph.out_edges(node.id, 'tests')
            columns = [e.target for e in edges if e.attrs.get('role') is None]
            if node.attrs.get('test') == 'not_null':
                self.not_null.update(columns)
            elif node.attrs.get('test') == 'relationships':
                targets = [e.target for e in edges if e.attrs.get('role') == 'relationship target']
                self.references.update((c, t) for c in columns for t in targets)

    def changed(self, start: Node) -> str | None:
        """How the rows of a relation on the way from ``start`` down to source differ from the
        source rows its composed formula aggregates (``WHERE in main.completed``), or None when
        every relation on the way keeps them one for one."""
        graph = self.graph
        seen, stack = {start.id}, [start]
        while stack:
            column = stack.pop()
            inputs = [e.target for e in graph.out_edges(column.id, 'derived_from') if e.target in graph.nodes]
            owner = graph.parent_of(column)
            # A column with inputs is computed by its relation's query, so that query decides its rows.
            if inputs and owner is not None:
                ours = frozenset(str(p) for p in {graph.nodes[i].parent for i in inputs} - {None})
                if (owner.id, ours) not in self.memo:
                    self.memo[owner.id, ours] = self.relation(owner, ours)
                found = self.memo[owner.id, ours]
                if found is not None:
                    return found
            for target in inputs:
                if target not in seen:
                    seen.add(target)
                    stack.append(graph.nodes[target])
        return None

    def relation(self, relation: Node, ours: frozenset[str]) -> str | None:
        """How ``relation``'s query changes the rows of the tables ``ours`` (by id), or None."""
        graph = self.graph
        where = relation.relation or graph.label(relation)
        joins: list[tuple[str, list[tuple[Node, Node]]]] = []
        for condition in relation.attrs.get('conditions', []) or []:
            label, sep, _ = str(condition).partition(': ')
            base = label.partition(' BEFORE ')[0].upper()
            if not sep:
                continue
            if base.startswith(_KEEPS) or base.endswith('JOIN WHERE') or base.startswith('ANTI'):
                return f'{label.partition(" BEFORE ")[0]} in {where}'
            if base.endswith('JOIN ON'):
                joins.append((base, join_columns(graph, str(condition))))
        # The tables read only on the nullable side of an outer join: their rows reach the result
        # only where they match, as through an inner join, and so do the rows joined through them.
        nullable = set()
        for name in relation.attrs.get('nullable') or {}:
            node = graph.node_for_reading(str(name))
            if node is not None:
                nullable.add(node.id)
        reached = set(ours)
        fragile = set(ours) & nullable
        # A join between two tables the column reads repeats or drops the rows of each, so it is
        # checked both ways before the joins out from them.
        for base, pairs in joins:
            outer = base.startswith(('LEFT', 'RIGHT', 'FULL'))
            both = [*pairs, *((b, a) for a, b in pairs)]
            for a, b in both:
                if a.parent != b.parent and a.parent in ours and b.parent in ours:
                    found = self.join(where, outer, a, b, both, fragile)
                    if found is not None:
                        return found
        progress = True
        while progress:  # follow the joins out from our tables, one table further each time
            progress = False
            for base, pairs in joins:
                both = [*pairs, *((b, a) for a, b in pairs)]
                outer = base.startswith(('LEFT', 'RIGHT', 'FULL'))
                for a, b in both:
                    if a.parent == b.parent and a.parent in reached:
                        # A self join: a copy of our table matches each row, so both columns must be
                        # unique for it to match one.
                        if not (a.id in self.unique and b.id in self.unique):
                            return f'a self join in {where} on {graph.label(a)} = {graph.label(b)}'
                        continue
                    if a.parent not in reached or b.parent in reached:
                        continue
                    found = self.join(where, outer, a, b, both, fragile)
                    if found is not None:
                        return found
                    reached.add(str(b.parent))
                    if a.parent in fragile:
                        fragile.add(str(b.parent))
                    progress = True
        unlinked = sorted(
            graph.label(graph.nodes[i]) for i in self.inputs(relation) - reached if i in graph.nodes
        )
        if unlinked:
            return f'{", ".join(unlinked)} read in {where} with no join condition on a unique key'
        return None

    def join(
        self, where: str, outer: bool, a: Node, b: Node, both: list[tuple[Node, Node]], fragile: set[str]
    ) -> str | None:
        """How joining ``b``'s table to ``a``'s, which the rows come from, changes them, or None."""
        graph = self.graph
        # Every pair of the condition that joins these two tables, ours first.
        keys = [(x, y) for x, y in both if (x.parent, y.parent) == (a.parent, b.parent)]
        if not any(y.id in self.unique for _, y in keys):
            return f'a join in {where} to {graph.label(b)}, which is not known to be unique'
        drops = not outer or a.parent in fragile
        if drops and not any(x.id in self.not_null and (x.id, y.id) in self.references for x, y in keys):
            how = (
                f'an inner join in {where} on {graph.label(a)}'
                if not outer
                else f'an outer join in {where} that keeps {graph.label(a)} only where it matches'
            )
            return f'{how}, which no not_null and relationships tests vouch for'
        return None

    def inputs(self, relation: Node) -> set[str]:
        """The ids of the relations ``relation``'s query reads."""
        graph = self.graph
        found = set()
        for edge in graph.in_edges(relation.id, 'feeds'):
            source = graph.nodes.get(edge.source)
            if source is None:
                continue
            owner = source if source.kind in RELATION_KINDS else graph.parent_of(source)
            if owner is not None and owner.id != relation.id:
                found.add(owner.id)
        return found
