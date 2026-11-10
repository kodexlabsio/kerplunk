"""Search by meaning: find a column, model, table, business term or formula by what it is, not
only by what it is called.

``search(graph, 'revenue')`` finds a column named ``net_sales_amt``: its words expand
(``amt`` is amount, sales is a kind of revenue), its formula reads columns whose names say
revenue, and the business terms and upstream columns around it in the lineage say the same.
The vectors, the index and the ranking are in the native core (``konstruct._core``); see
``src/vector/``. Nothing is downloaded or sent anywhere: every vector comes from the graph, a
table of the abbreviations and synonyms of data work, and hashing.

The ranking fuses the word search (:func:`konstruct.analysis.query.search`) with the vector
search by reciprocal rank, so a node named exactly what was asked for still comes first.

The index is made the first time a search by meaning needs it, so a build pays nothing for it,
and saved as ``vectors.bin`` beside ``graph.json``: its graph, every node's blocks and a digest
of the graph they were computed from. A process whose graph has that digest reads the blocks
rather than computing them; after a build changes the graph, the blocks are computed again,
nodes whose vector changed are reinserted, and the file is saved again, so a file left by an
earlier build is never wrong, only slower to read.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from konstruct import _core
from konstruct.analysis.query import Item, SearchResult, item, resolve_one
from konstruct.core.graph import RELATION_KINDS, Graph, Node

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style

VECTOR_FILE = 'vectors.bin'
"""The saved index's file name, in the output directory."""

# Attributes whose words describe a node, in the order they are read.
_DESCRIBING = ('grain', 'unit', 'currency', 'label', 'fields', 'measures')


def _standing(node: Node) -> int:
    """How the core weighs a node's kind: 0 other, 1 business term, 2 relation, 3 test."""
    if node.kind == 'term':
        return 1
    if node.kind in RELATION_KINDS:
        return 2
    return 3 if node.kind == 'test' else 0


def _description(node: Node) -> str:
    """The node's description and the attributes that say what it is (a term's grain and unit,
    a dashboard's fields)."""
    parts = [node.description or '']
    for key in _DESCRIBING:
        value = node.attrs.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(v) for v in value if isinstance(v, str))
    return ' '.join(p for p in parts if p)


class VectorIndex:
    """A graph's nodes in the native vector index, numbered in the graph's node order."""

    def __init__(self, graph: Graph, previous: bytes | None = None) -> None:
        self.nodes = list(graph.nodes.values())
        self.ordinal = {node.id: i for i, node in enumerate(self.nodes)}
        """Each node's position in :attr:`nodes`, by id."""
        ordinal = self.ordinal
        qualifiers = []
        for node in self.nodes:
            if node.kind == 'column':
                parent = graph.parent_of(node)
                qualifiers.append(graph.label(parent) if parent is not None else '')
            else:
                qualifiers.append(node.relation or '')
        edges = [e for e in graph.edges if e.source in ordinal and e.target in ordinal]
        self.core = _core.VectorIndex(
            [n.id for n in self.nodes],
            [n.kind for n in self.nodes],
            [_standing(n) for n in self.nodes],
            [n.name for n in self.nodes],
            qualifiers,
            [_description(n) for n in self.nodes],
            [[str(s) for s in n.attrs.get('synonyms', []) if isinstance(s, str)] for n in self.nodes],
            [e if isinstance(e := n.attrs.get('expression'), str) else None for n in self.nodes],
            [ordinal[e.source] for e in edges],
            [ordinal[e.target] for e in edges],
            [e.kind for e in edges],
            previous,
        )

    @property
    def update(self) -> tuple[int, int, int, bool]:
        """What reading a saved index did: nodes kept, added and removed, and whether it was read."""
        return self.core.update


def _read(out_dir: Path | None) -> bytes | None:
    if out_dir is None:
        return None
    with contextlib.suppress(OSError):
        return (out_dir / VECTOR_FILE).read_bytes()
    return None


def _save(index: VectorIndex, out_dir: Path) -> None:
    """Write the index as ``vectors.bin`` in ``out_dir``, unless it was read whole from the file
    there, which then already holds it.
    The file is replaced whole, so a reader never sees half of one; an output directory that
    cannot be written to only means the next search builds the index again."""
    if index.core.rows_read:
        return
    path = out_dir / VECTOR_FILE
    partial = path.with_name(f'.{VECTOR_FILE}.{os.getpid()}')
    try:
        partial.write_bytes(index.core.to_bytes())
        partial.replace(path)
    except OSError:
        partial.unlink(missing_ok=True)


def _made(graph: Graph, out_dir: Path | None) -> VectorIndex:
    index = VectorIndex(graph, _read(out_dir))
    if out_dir is not None:
        _save(index, out_dir)
    return index


def vector_index(graph: Graph, out_dir: Path | None = None) -> VectorIndex:
    """The graph's vector index, made on first use and kept until the graph changes. With
    ``out_dir``, the index saved there is read and brought up to date rather than built anew,
    and saved there again when that changed it."""
    return graph.indexed('vectors', lambda g: _made(g, out_dir))


def search(
    graph: Graph,
    text: str,
    limit: int = 20,
    kinds: list[str] | None = None,
    out_dir: Path | None = None,
) -> SearchResult:
    """Nodes that match ``text`` by meaning or by name, best first: the word search and the
    vector search fused, an exact name first. Each result's ``matched`` says what a match by
    meaning rests on most: the node's own ``words``, its ``lineage``, its ``formula`` or its
    ``structure``."""
    if limit <= 0:
        return SearchResult(text, [])
    from konstruct.analysis.query import _SearchIndex

    words = graph.indexed('search', _SearchIndex)
    vectors = vector_index(graph, out_dir)
    found = vectors.core.hybrid(words.core, text, text.lower(), text.strip().lower(), limit, kinds or [])
    return SearchResult(
        text,
        [item(graph, vectors.nodes[i], score=round(score, 3), matched=basis) for i, score, basis in found],
    )


@dataclass
class SimilarResult:
    """The nodes most like one node, by words, lineage, formula and structure."""

    node: Item
    """The node compared."""
    results: list[Item]
    """The most similar nodes, best first, each with a ``score`` and what it ``matched`` on."""

    def to_dict(self) -> dict[str, Any]:
        return {'node': self.node.to_dict(), 'results': [r.to_dict() for r in self.results]}

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI would print it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import show, similar_lines

        return show(similar_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def similar(
    graph: Graph,
    ref: str,
    limit: int = 10,
    kinds: list[str] | None = None,
    out_dir: Path | None = None,
) -> SimilarResult:
    """The nodes most like ``ref``: named alike, near it in the lineage, computed by the same
    formula, or built from the same sources by the same path."""
    node = resolve_one(graph, ref)
    vectors = vector_index(graph, out_dir)
    found = vectors.core.similar(vectors.ordinal[node.id], max(limit, 0), kinds or [])
    return SimilarResult(
        item(graph, node),
        [item(graph, vectors.nodes[i], score=round(score, 3), matched=basis) for i, score, basis in found],
    )
