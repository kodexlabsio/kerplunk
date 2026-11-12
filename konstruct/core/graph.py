"""The Konstruct graph: nodes, edges, traversal and (de)serialisation.

Edge directions
---------------
Every edge is stored as ``source -> target`` with these meanings:

``contains``      relation -> column
``feeds``         upstream -> downstream (relation to relation, task to the table it writes,
                  table to the task that reads it, or column to the relation that filters,
                  joins or groups on it: ``role = "reference"``)
``derived_from``  downstream column -> upstream column
``runs``          DAG -> task, and task -> the dbt node or table it executes
``precedes``      upstream task -> downstream task (Airflow ``>>``)
``tests``         test -> the relation or column it tests
``defines``       business term -> the column (or relation) that holds it
``relates_to``    business term -> a related term

`downstream_edges()` and `upstream_edges()` hide those directions so callers think only in data
flow.

The graph is the native core's (``konstruct._core``), and this module gives its names: ``Graph``,
``Node``, ``Edge`` and ``Step``. Walks, shortest paths and name resolution run over an index of the
graph, made on first use and again after the graph changes through ``add_node``, ``add_edge`` or
``remove_edge``. Code that edits a node's or an edge's fields in place calls ``changed()``.

``Graph.read`` parses ``graph.json``, which also makes the index walks use from the file. The nodes
become Python objects at once; each edge only when something reads it (``out_edges``, a walk's
steps), and all of them when the graph changes or every edge is asked for (``edges``, ``to_dict``).
"""

from __future__ import annotations

from typing import Any, Literal  # noqa: F401  (named by the annotations get_type_hints reads)

from konstruct._core import (
    GRAPH_FORMAT_VERSION,
    RELATION_KINDS,
    RELATION_PRIORITY,
    Edge,
    Graph,
    Node,
    Step,
    column_id,
    reading_key,
    relation_key,
    split_reading,
)

NodeKind = Literal[
    'source', 'table', 'column', 'model', 'seed', 'snapshot', 'dag', 'task', 'test', 'exposure', 'term'
]
EdgeKind = Literal['contains', 'feeds', 'derived_from', 'runs', 'precedes', 'tests', 'defines', 'relates_to']
IndexBasis = Literal['graph', 'nodes', 'relations']
"""What an index is made from, so what makes it stale (see ``Graph.indexed``)."""

__all__ = [
    'GRAPH_FORMAT_VERSION',
    'RELATION_KINDS',
    'RELATION_PRIORITY',
    'Edge',
    'EdgeKind',
    'Graph',
    'IndexBasis',
    'Node',
    'NodeKind',
    'Step',
    'column_id',
    'reading_key',
    'relation_key',
    'split_reading',
]
