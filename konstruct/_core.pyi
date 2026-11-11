"""Konstruct's native core (the Rust crate in src/): graph traversal, name resolution, search,
search by meaning, formula identity, topic routing, the readers of Airflow and Dagster files and
the server. ``konstruct.core.graph``, ``konstruct.analysis.query``,
``konstruct.analysis.semantic`` and ``konstruct.analysis.identity`` are the callers of the
indexes; nodes, edges and relations are passed in, and come back, as ordinals into the lists
given. Any string may hold lone surrogates (a file name decoded with ``surrogateescape``), and an
id comes back exactly as it was given. ``konstruct.interfaces.serve`` starts the server, which
reads the graph files itself and asks ``konstruct.interfaces.http_api`` for every answer.
``konstruct.inputs.airflow`` and ``konstruct.inputs.dagster`` re-export the readers of the
orchestrators' files, whose records here are the ones the build and the plugins read."""

from collections.abc import Callable, Sequence
from os import PathLike
from typing import Any, Self, final

GRAPH_FORMAT_VERSION: int
"""The newest graph.json format this Konstruct reads and writes."""

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
class GraphFile:
    """A parsed ``graph.json``: its nodes are made Python objects by ``load``, its edges one at a
    time, when first asked for."""

    def __init__(self, data: bytes) -> None:
        """Raises ``ValueError(message, offset)`` when ``data`` is not JSON, and ``OverflowError``
        when it is larger than 2 GiB."""
    def document(self) -> Any:
        """The whole document as Python values, for one that ``load`` refuses."""
    def header(self) -> Any:
        """The document's members other than ``nodes`` and ``edges``, or the document itself when
        it is not an object."""
    def load(self, node_type: type, edge_type: type) -> tuple[dict[str, Any], list[tuple[int, Any]]]:
        """The first node of each id, by id, and the later ones with an id that came before, each
        with the number of first nodes before it. Raises ``ValueError(message)`` when the
        document is not a graph as Konstruct writes one."""
    def index(self) -> GraphIndex:
        """The index of the edges, numbered as this file numbers them."""
    def __len__(self) -> int: ...
    def __getitem__(self, ordinal: int, /) -> Any:
        """The edge with this ordinal."""
    def edges(self) -> list[Any]:
        """Every edge, in order."""
    def out_edges(self, node: str, kind: str | None = None) -> list[Any]:
        """The edges leaving ``node``, of kind ``kind`` if given."""
    def in_edges(self, node: str, kind: str | None = None) -> list[Any]:
        """The edges arriving at ``node``, of kind ``kind`` if given."""

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
        api: Callable[[str, dict[str, Any], str, bytes], tuple[str, str, str, str]] | None = None,
    ) -> None:
        """Load ``out_dir/graph.json`` and bind ``host:port`` (0 for any free port). With
        ``token``, every request must carry it. ``api`` answers every operation of the API
        (``konstruct.interfaces.http_api.Api``), given the version and bytes of the graph the
        server serves. Raises ``OSError`` when there is no graph or the
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

DAG_CLASSES: frozenset[str]
GROUP_CLASSES: frozenset[str]
COSMOS_CLASSES: frozenset[str]
SQL_KWARGS: tuple[str, ...]
WRITE_KWARGS: tuple[str, ...]
READ_KWARGS: tuple[str, ...]
DBT_COMMANDS: frozenset[str]
UNRESOLVED: str
GROUP: str
"""The tables the Airflow reader is built from: the classes it takes for a DAG, a task group and
a Cosmos task, the keyword arguments that hold SQL or name tables, the dbt commands it reads, and
the marks it puts in ids."""

class DbtInvocation:
    """One ``dbt <command>`` that an Airflow task runs, and what it selects."""

    command: str
    selectors: list[str]
    excludes: list[str]
    project_dir: str | None
    unsupported: list[str]
    def __init__(
        self,
        command: str,
        selectors: list[str] | None = None,
        excludes: list[str] | None = None,
        project_dir: str | None = None,
        unsupported: list[str] | None = None,
    ) -> None: ...
    def __eq__(self, other: object) -> bool: ...
    def __copy__(self) -> Self: ...
    def __deepcopy__(self, memo: dict[int, Any]) -> Self: ...
    def __reduce__(self) -> tuple[Any, ...]: ...

class TaskInfo:
    """One task of an Airflow DAG."""

    task_id: str
    operator: str
    line: int
    doc: str | None
    bash_command: str | None
    sql: str | None
    sql_path: str | None
    dbt: list[DbtInvocation]
    reads: list[str]
    writes: list[str]
    inlets: list[str]
    outlets: list[str]
    def __init__(
        self,
        task_id: str,
        operator: str,
        line: int,
        doc: str | None = None,
        bash_command: str | None = None,
        sql: str | None = None,
        sql_path: str | None = None,
        dbt: list[DbtInvocation] | None = None,
        reads: list[str] | None = None,
        writes: list[str] | None = None,
        inlets: list[str] | None = None,
        outlets: list[str] | None = None,
    ) -> None: ...
    def __eq__(self, other: object) -> bool: ...
    def __copy__(self) -> Self: ...
    def __deepcopy__(self, memo: dict[int, Any]) -> Self: ...
    def __reduce__(self) -> tuple[Any, ...]: ...

class DagInfo:
    """One DAG of an Airflow file: its tasks by id and the dependencies between them."""

    dag_id: str
    path: str
    line: int
    schedule: str | None
    description: str | None
    tags: list[str]
    tasks: dict[str, TaskInfo]
    dependencies: list[tuple[str, str]]
    warnings: list[str]
    def __init__(
        self,
        dag_id: str,
        path: str,
        line: int,
        schedule: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        tasks: dict[str, TaskInfo] | None = None,
        dependencies: list[tuple[str, str]] | None = None,
        warnings: list[str] | None = None,
    ) -> None: ...
    def to_dict(self) -> dict[str, Any]:
        """The record as plain dicts, lists and tuples."""
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DagInfo:
        """The record ``to_dict`` describes."""
    def __eq__(self, other: object) -> bool: ...
    def __copy__(self) -> Self: ...
    def __deepcopy__(self, memo: dict[int, Any]) -> Self: ...
    def __reduce__(self) -> tuple[Any, ...]: ...

def looks_like_dag_file(text: str) -> bool:
    """Whether ``text`` looks like the file of an Airflow DAG: it mentions Airflow and a DAG."""

def parse_dag_file(path: str | PathLike[str]) -> list[DagInfo]:
    """The DAGs of an Airflow file, read without importing or running it. Raises ``SyntaxError``
    for text that is not Python, or is nested too deeply to read."""

def parse_dag_source(path: str | PathLike[str], source: str) -> list[DagInfo]:
    """The DAGs of Python ``source``, read as if it were the file at ``path``."""

def parse_dbt_commands(command: str) -> list[DbtInvocation]:
    """The ``dbt <command>`` invocations in a shell command, with their selection flags."""

def scan_python_files(paths: Sequence[str | PathLike[str]]) -> list[tuple[bool, bool]]:
    """For each path, whether the file looks like an Airflow DAG file and whether it looks like a
    Dagster file (``looks_like_dag_file`` and ``looks_like_dagster_file`` of its bytes), with the
    files read in parallel. A file that cannot be read is neither."""
