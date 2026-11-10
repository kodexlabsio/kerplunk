"""Konstruct's native core (the Rust crate in src/): graph traversal, name resolution, search and
formula identity. ``konstruct.core.graph``, ``konstruct.analysis.query`` and
``konstruct.analysis.identity`` are its callers; nodes, edges and relations are passed in, and come
back, as ordinals into the lists given. Any string may hold lone surrogates (a file name decoded
with ``surrogateescape``), and an id comes back exactly as it was given."""

from typing import final

@final
class GraphIndex:
    """The edges of a graph, indexed for walks and shortest paths."""

    def __init__(
        self,
        nodes: list[str],
        node_kinds: list[str],
        parents: list[str | None],
        sources: list[str],
        targets: list[str],
        kinds: list[str],
        references: list[bool],
        through: list[tuple[int, str]],
    ) -> None: ...
    def impact(
        self, starts: list[str], max_depth: int | None, column: bool
    ) -> list[tuple[int, int, int | None, int | None]]:
        """Each node reached as (node, depth, edge that reached it, node it was reached from)."""
    def trace(
        self, starts: list[str], max_depth: int | None, column: bool
    ) -> list[tuple[int, int, int | None, int | None]]:
        """Each node reached upstream, as ``impact`` reports them."""
    def id(self, ordinal: int) -> str:
        """The id of a node or edge endpoint."""
    def path(self, start: str, end: str) -> list[tuple[int, int, int | None, int | None]]:
        """The shortest path's nodes, as ``impact`` reports them, start first; empty when there is none."""
    def neighbours(self, node: str, downstream: bool) -> list[int]:
        """The edges a walk follows from ``node``."""

@final
class RelationNames:
    """Relations by every name they answer to."""

    def __init__(
        self, names: list[str], ids: list[str], relations: list[str | None], outside_warehouse: list[bool]
    ) -> None: ...
    def find(self, needle: str) -> list[int]:
        """The relations ``needle`` (in lower case) names."""

@final
class SearchIndex:
    """Ranked word search over a graph's nodes. ``standings``: 0 other, 1 term, 2 relation, 3 test."""

    def __init__(
        self,
        ids: list[str],
        kinds: list[str],
        standings: list[int],
        names: list[str],
        labels: list[str],
        words: list[str],
        descriptions: list[str],
        synonyms: list[list[str]],
    ) -> None: ...
    def search(self, text: str, needle: str, limit: int | None, kinds: list[str]) -> list[tuple[int, float]]:
        """The nodes matching ``text`` (in lower case), best first, as (node, score)."""

# An input of a witness: name, then the definition and label of the expression it is (None for a
# column), then its value (None for NULL).
_Input = tuple[str, int | None, int | None, str | None]
# A function's name, and the definition and label of where it is applied.
_Mention = tuple[str, int, int | None]
# kind, inputs, rows (None when nothing aggregates), the two results, functions, and the
# expressions only in each formula.
_Witness = tuple[
    str,
    list[_Input],
    list[list[_Input]] | None,
    tuple[str | None, str | None] | None,
    list[str],
    list[_Mention],
    list[_Mention],
]

@final
class FormulaSet:
    """Formulas written as definitions of a straight-line program, to fingerprint and compare."""

    def __init__(self, key: bytes | None = None) -> None:
        """Formulas under ``key``, or under the fixed key fingerprints use when it is None."""
    def __len__(self) -> int: ...
    def add(self, text: str, context: str) -> int:
        """Adds a definition whose aggregates run under ``context``; raises ValueError if malformed."""
    def fingerprints(self, definitions: list[int]) -> list[int]:
        """The 128-bit fingerprint of each definition."""
    def compare(self, a: int, b: int) -> tuple[bool, float, bool, _Witness | None]:
        """Whether two definitions are shown to be the same, a bound on the error, whether they
        are without NULLs, and why they are not."""
