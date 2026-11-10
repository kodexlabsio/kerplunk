"""Konstruct's native core (the Rust crate in src/): graph traversal, name resolution, search,
search by meaning, formula identity, topic routing and the server. ``konstruct.core.graph``,
``konstruct.analysis.query``, ``konstruct.analysis.semantic`` and ``konstruct.analysis.identity``
are its callers; nodes, edges and relations are passed in, and come back, as ordinals into the
lists given. Any string may hold lone surrogates (a file name decoded with ``surrogateescape``),
and an id comes back exactly as it was given. ``konstruct.interfaces.serve`` starts the server,
which reads the graph files itself and asks ``konstruct.interfaces.http_api`` for every answer."""

from collections.abc import Callable
from typing import Any, final

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
    def fed_by_columns_of(self, consumer: str, relation: str) -> bool:
        """Whether a column of ``relation`` feeds ``consumer``, which so records the columns it reads."""
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

@final
class VectorIndex:
    """Search by meaning over a graph's nodes (``konstruct.analysis.semantic``). Nodes are given
    as parallel lists; edges as node ordinals and kinds. ``previous`` is a saved index
    (``vectors.bin``), whose slots are reused where a node's vector is unchanged."""

    def __init__(
        self,
        ids: list[str],
        kinds: list[str],
        standings: list[int],
        names: list[str],
        qualifiers: list[str],
        descriptions: list[str],
        synonyms: list[list[str]],
        expressions: list[str | None],
        sources: list[int],
        targets: list[int],
        edge_kinds: list[str],
        previous: bytes | None,
    ) -> None: ...
    def search(self, text: str, limit: int, kinds: list[str]) -> list[tuple[int, float, str]]:
        """The nodes that best answer ``text``, best first, as (node, score, basis)."""
    def similar(self, ordinal: int, limit: int, kinds: list[str]) -> list[tuple[int, float, str]]:
        """The nodes most like node ``ordinal``, best first, as (node, score, basis)."""
    def hybrid(
        self, words: SearchIndex, text: str, lowered: str, needle: str, limit: int, kinds: list[str]
    ) -> list[tuple[int, float, str | None]]:
        """The word search and this search fused, best first, as (node, score, basis or None)."""
    def to_bytes(self) -> bytes:
        """The index as a saved index."""
    @property
    def update(self) -> tuple[int, int, int, bool]:
        """What reading the saved index did: (kept, added, removed, reused)."""
    @property
    def rows_read(self) -> bool:
        """True when every node's blocks were read from the saved index, made from the same
        graph, rather than computed."""
    def __len__(self) -> int: ...

def terms(text: str) -> list[str]:
    """The words of ``text`` as search by meaning reads them: split where a person reads a word
    boundary (``netSalesAmt``, ``net_sales_amt``), in lower case, abbreviations expanded
    (``amt`` is amount), singular, and stopwords left out."""

def concept(word: str) -> str | None:
    """The concept a word from :func:`terms` belongs to (``sale`` and ``turnover`` to
    ``revenue``), or None."""

@final
class Server:
    """``konstruct serve``: the viewer and a read-only JSON API over the graph in ``out_dir``."""

    def __init__(
        self,
        out_dir: str,
        host: str,
        port: int,
        page: str,
        token: str | None = None,
        api: Callable[[str, dict[str, Any]], tuple[str, str, str, str]] | None = None,
    ) -> None:
        """Load ``out_dir/graph.json`` and bind ``host:port`` (0 for any free port). With
        ``token``, every request must carry it. ``api`` answers every operation of the API
        (``konstruct.interfaces.http_api.Api``). Raises ``OSError`` when there is no graph or the
        port is taken."""
    @property
    def host(self) -> str:
        """The address bound."""
    @property
    def port(self) -> int:
        """The port bound."""
    def run(self) -> None:
        """Serve until interrupted (``KeyboardInterrupt``), without holding the GIL."""

def graph_version(data: bytes) -> str:
    """The version ``konstruct serve`` gives a ``graph.json`` with these bytes (FNV-1a 64, in
    hexadecimal); ``konstruct.interfaces.http_api.Api`` reports the graph each answer came from by it."""

def openapi() -> str:
    """The OpenAPI 3.1 document of ``konstruct serve``'s API, as it is served."""

@final
class Router:
    """A Kafka Connect routing transformation (``RegexRouter``, ``ByLogicalTableRouter``), run on
    finite automata over bytes, so in time linear in the topic, with ASCII classes and case as in
    Java. Raises ValueError, with the reason, for an
    expression it cannot run (a backreference, lookaround, a possessive repeat, one too large) or
    a replacement that is not Java's syntax (``$1``, ``${name}``, ``\\$``)."""

    def __init__(self, pattern: str, replacement: str) -> None: ...
    def route(self, topic: str) -> str | None:
        """The topic after routing: replaced when the whole expression matches it, else unchanged;
        None when the replacement splits a character, as an expression on bytes can."""
