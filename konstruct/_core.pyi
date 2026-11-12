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

from collections.abc import Callable, Iterator, Sequence
from dataclasses import Field
from os import PathLike
from pathlib import Path
from typing import Any, ClassVar, Self, TypeVar, final

from konstruct.core.graph import EdgeKind, IndexBasis, NodeKind

_Index = TypeVar('_Index')

StrPath = str | PathLike[str]

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

ASSET_DECORATORS: frozenset[str]
SQL_METHODS: frozenset[str]
SENSOR_DECORATORS: frozenset[str]
OP_WRAPPERS: frozenset[str]
"""The tables the Dagster reader is built from."""

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

class AssetInfo:
    """A Dagster asset, or a dbt selection that builds many (``dbt_select`` is set for it)."""

    key: str
    function: str
    line: int
    group: str | None
    description: str | None
    deps: list[str]
    sql: str | None
    kinds: list[str]
    dbt_select: list[str] | None
    dbt_exclude: list[str]
    def __init__(
        self,
        key: str,
        function: str,
        line: int,
        group: str | None = None,
        description: str | None = None,
        deps: list[str] | None = None,
        sql: str | None = None,
        kinds: list[str] | None = None,
        dbt_select: list[str] | None = None,
        dbt_exclude: list[str] | None = None,
    ) -> None: ...
    def __eq__(self, other: object) -> bool: ...
    def __copy__(self) -> Self: ...
    def __deepcopy__(self, memo: dict[int, Any]) -> Self: ...
    def __reduce__(self) -> tuple[Any, ...]: ...

class OpInfo:
    """An ``@op`` definition. It runs once per invocation in a job; see ``OpCall``."""

    name: str
    function: str
    line: int
    description: str | None
    sql: str | None
    kinds: list[str]
    dbt_commands: list[str]
    def __init__(
        self,
        name: str,
        function: str,
        line: int,
        description: str | None = None,
        sql: str | None = None,
        kinds: list[str] | None = None,
        dbt_commands: list[str] | None = None,
    ) -> None: ...
    def __eq__(self, other: object) -> bool: ...
    def __copy__(self) -> Self: ...
    def __deepcopy__(self, memo: dict[int, Any]) -> Self: ...
    def __reduce__(self) -> tuple[Any, ...]: ...

class OpCall:
    """One invocation of an op in a job, by its handle, and the handles it takes inputs from."""

    handle: str
    op: str
    upstream: list[str]
    def __init__(self, handle: str, op: str, upstream: list[str] | None = None) -> None: ...
    def __eq__(self, other: object) -> bool: ...
    def __copy__(self) -> Self: ...
    def __deepcopy__(self, memo: dict[int, Any]) -> Self: ...
    def __reduce__(self) -> tuple[Any, ...]: ...

class JobInfo:
    """A Dagster job: the assets it selects, or (``ops``) the op invocations it runs."""

    name: str
    line: int
    selection: list[str]
    schedule: str | None
    description: str | None
    ops: list[OpCall]
    def __init__(
        self,
        name: str,
        line: int,
        selection: list[str] | None = None,
        schedule: str | None = None,
        description: str | None = None,
        ops: list[OpCall] | None = None,
    ) -> None: ...
    def __eq__(self, other: object) -> bool: ...
    def __copy__(self) -> Self: ...
    def __deepcopy__(self, memo: dict[int, Any]) -> Self: ...
    def __reduce__(self) -> tuple[Any, ...]: ...

class SensorInfo:
    """A Dagster sensor: the jobs it starts, the assets it watches and, for run status sensors,
    the jobs whose runs it watches."""

    name: str
    kind: str
    line: int
    jobs: list[str]
    assets: list[str]
    monitored_jobs: list[str]
    status: str | None
    description: str | None
    def __init__(
        self,
        name: str,
        kind: str,
        line: int,
        jobs: list[str] | None = None,
        assets: list[str] | None = None,
        monitored_jobs: list[str] | None = None,
        status: str | None = None,
        description: str | None = None,
    ) -> None: ...
    def __eq__(self, other: object) -> bool: ...
    def __copy__(self) -> Self: ...
    def __deepcopy__(self, memo: dict[int, Any]) -> Self: ...
    def __reduce__(self) -> tuple[Any, ...]: ...

class DagsterFile:
    """What one Dagster file declares."""

    path: str
    assets: list[AssetInfo]
    jobs: list[JobInfo]
    warnings: list[str]
    ops: list[OpInfo]
    sensors: list[SensorInfo]
    def __init__(
        self,
        path: str,
        assets: list[AssetInfo] | None = None,
        jobs: list[JobInfo] | None = None,
        warnings: list[str] | None = None,
        ops: list[OpInfo] | None = None,
        sensors: list[SensorInfo] | None = None,
    ) -> None: ...
    def to_dict(self) -> dict[str, Any]:
        """The record as plain dicts and lists."""
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DagsterFile:
        """The record ``to_dict`` describes."""
    def __eq__(self, other: object) -> bool: ...
    def __copy__(self) -> Self: ...
    def __deepcopy__(self, memo: dict[int, Any]) -> Self: ...
    def __reduce__(self) -> tuple[Any, ...]: ...

def looks_like_dagster_file(text: str) -> bool:
    """Whether ``text`` looks like the file of Dagster definitions: it mentions Dagster and one of
    the decorators or calls that declare them."""

def parse_dagster_file(path: str | PathLike[str]) -> DagsterFile:
    """The assets, jobs, ops and sensors of a Dagster file, read without importing or running it.
    Raises ``SyntaxError`` for text that is not Python, or is nested too deeply to read."""

def parse_dagster_source(path: str | PathLike[str], source: str) -> DagsterFile:
    """The definitions of Python ``source``, read as if it were the file at ``path``."""

def scan_python_files(paths: Sequence[str | PathLike[str]]) -> list[tuple[bool, bool]]:
    """For each path, whether the file looks like an Airflow DAG file and whether it looks like a
    Dagster file (``looks_like_dag_file`` and ``looks_like_dagster_file`` of its bytes), with the
    files read in parallel. A file that cannot be read is neither."""

@final
class Tree:
    """The files under a root, walked once on every CPU: directories in the order ``os.walk``
    yields them, each with its files in name order. A directory that is a link, hidden (a leading
    ``.``) or named in ``skip`` is not entered."""

    def __init__(
        self, root: StrPath, skip: list[str], fold_paths: bool = False, enter_hidden: bool = False
    ) -> None:
        """With ``fold_paths``, ``exclude`` patterns ignore case, as ``fnmatch`` does where file
        names do; with ``enter_hidden``, a directory with a leading ``.`` is entered."""
    def directories(self) -> list[str]:
        """The directories, as paths."""
    def files(
        self,
        *,
        suffixes: list[str] | None = None,
        patterns: list[str] | None = None,
        fold: bool = False,
        exclude: list[str] = ...,
        skip: StrPath | None = None,
    ) -> list[tuple[str, bool]]:
        """The files whose names end with one of ``suffixes`` (whatever the case with ``fold``)
        or match one of ``patterns`` as ``fnmatch.fnmatchcase`` reads them, each with whether it
        is a link, except those directly in the directory ``skip`` (from the root, ``''`` for the
        root) and those whose path from the root matches one of the ``exclude`` patterns."""

def name_accepts(
    name: StrPath, *, suffixes: list[str] | None = None, patterns: list[str] | None = None, fold: bool = False
) -> bool:
    """Whether a file name ends with one of ``suffixes`` (whatever the case with ``fold``) or
    matches one of ``patterns``: the rule :meth:`Tree.files` applies, for a file met on its own."""

def file_identity(path: StrPath) -> list[int] | None:
    """What says a file changed: size, modification and change times in nanoseconds, device and
    inode; None when it is not a regular file."""

def file_digest(found: list[int]) -> str:
    """An identity in 16 hexadecimal digits: BLAKE2b with an 8-byte digest of the fields in
    decimal separated by spaces."""

def folder_files(folder: StrPath, extension: str) -> tuple[str, list[tuple[str, list[int], str]]]:
    """The regular files in ``folder`` whose names end with ``extension``, in the order the folder
    lists them, each with its identity and the digest of it; and a digest of all of them (BLAKE2b
    with an 8-byte digest of the names joined by NUL, then each identity as 64-bit integers in the
    machine's byte order). Raises ``OSError`` when the folder cannot be listed."""

def folder_digest(folder: StrPath, extension: str) -> str:
    """The first of what :func:`folder_files` gives, alone."""

def files_under(folder: StrPath) -> list[tuple[str, list[int]]]:
    """Every regular file under ``folder`` at any depth, with its path from ``folder`` and its
    identity."""

def write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a new file in the same directory that is then renamed
    over it, so a reader sees the old file or the new one, never half of one. A symbolic link at
    ``path`` is replaced, not followed, and a regular file keeps its mode."""

def real_directory(base: Path, *parts: str) -> Path:
    """``base`` joined with ``parts``, made as real directories: a symbolic link in the way is
    removed first, so one committed inside the output directory cannot send outputs elsewhere."""

RELATION_KINDS: frozenset[str]
"""The node kinds that are relations: ``source``, ``table``, ``model``, ``seed`` and ``snapshot``."""
RELATION_PRIORITY: dict[str, int]
"""When several nodes describe one physical relation (a seed and the dbt source that reads it),
SQL that names the relation resolves to the kind with the lowest number."""

def column_id(parent_id: str, column: str) -> str: ...
def reading_key(key: str, reading: str = '') -> str:
    """How canonical text (a relation's conditions, a column's formula) names one reading of a
    relation that a query reads more than once: the relation key with each ``#`` in it doubled,
    then ``#`` and the reading (``db.main.orders#2``). A relation read once is its key, escaped
    the same way, so a relation named ``events#2`` is never taken for a reading of ``events``."""

def split_reading(name: str) -> tuple[str, str]:
    """The relation key and the reading a name in canonical text stands for (see
    :func:`reading_key`): ``db.main.orders#2`` is reading ``2`` of ``db.main.orders``, and
    ``main.events##2`` is the relation ``main.events#2``."""

def relation_key(*parts: str | None) -> str:
    """Canonical, case-insensitive key for a warehouse relation, e.g. ``jaffle.main.orders``."""

class Node:
    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]
    __match_args__: ClassVar[tuple[str, ...]] = (
        'id',
        'kind',
        'name',
        'parent',
        'relation',
        'description',
        'data_type',
        'path',
        'origins',
        'attrs',
    )
    """A node of the graph."""

    id: str
    """A unique id, such as ``model.<package>.<name>``, ``table.<relation>``, ``<relation id>#<column>``, ``task.<dag>.<task>``, ``looker.view.<view>``, ``powerbi.table.<model>.<table>`` or ``term.<slug>``."""
    kind: NodeKind
    """What the node is."""
    name: str
    """Its own name."""
    parent: str | None
    """For a column, its relation's id."""
    relation: str | None
    """For a relation, its lower-case warehouse name, such as ``shop.main.orders``."""
    description: str | None
    """Description from dbt, the warehouse or definitions.yml."""
    data_type: str | None
    """Column type."""
    path: str | None
    """The file that defines it, relative to the project."""
    origins: list[str]
    """Inputs it came from."""
    attrs: dict[str, Any]
    """Everything else, such as expressions, conditions, schedules, usage and term fields."""

    def __replace__(self, **changes: Any) -> Self: ...
    def __init__(
        self,
        id: str,
        kind: NodeKind,
        name: str,
        parent: str | None = None,
        relation: str | None = None,
        description: str | None = None,
        data_type: str | None = None,
        path: str | None = None,
        origins: list[str] | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> None: ...
    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node: ...

class Edge:
    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]
    __match_args__: ClassVar[tuple[str, ...]] = ('source', 'target', 'kind', 'attrs')
    """An edge of the graph; ``konstruct.core.graph`` gives each kind's direction."""

    source: str
    """Source node id."""
    target: str
    """Target node id."""
    kind: EdgeKind
    """Edge kind."""
    attrs: dict[str, Any]
    """Details such as ``transform``, ``role``, ``verified`` and ``via``."""

    def __replace__(self, **changes: Any) -> Self: ...
    def __init__(
        self, source: str, target: str, kind: EdgeKind, attrs: dict[str, Any] | None = None
    ) -> None: ...
    @property
    def key(self) -> tuple[str, str, str]: ...
    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Edge: ...

class Step:
    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]
    __match_args__: ClassVar[tuple[str, ...]] = ('node', 'depth', 'via', 'previous')
    """One node reached by a traversal, with the edge that reached it."""

    node: Node
    """The node reached."""
    depth: int
    """Steps from the start."""
    via: Edge | None
    """The edge that reached it."""
    previous: str | None
    """The id of the node it was reached from."""

    def __replace__(self, **changes: Any) -> Self: ...
    def __init__(self, node: Node, depth: int, via: Edge | None, previous: str | None) -> None: ...

class Graph:
    """The nodes and edges of a project and the questions asked of them."""

    nodes: dict[str, Node]
    warnings: list[str]
    meta: dict[str, Any]

    @property
    def _versions(self) -> dict[IndexBasis, int]:
        """For each basis an index can have, a version bumped whenever that part of the graph
        changes; each cached index records the version it was made from."""
    @property
    def _indexes(self) -> dict[str, tuple[int, Any]]:
        """The indexes made so far, each with the version it was made from."""
    @property
    def _in_file(self) -> bool:
        """Whether the edges are still in the file the graph was read from, not yet Python objects."""

    def __init__(self) -> None: ...
    def add_node(self, node: Node) -> Node:
        """Add a node, or merge it into an existing node with the same id."""
    def add_column(
        self,
        parent: Node,
        name: str,
        *,
        data_type: str | None = None,
        description: str | None = None,
        origin: str | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> Node: ...
    def add_edge(self, source: str, target: str, kind: EdgeKind, **attrs: Any) -> Edge | None:
        """Add an edge, or merge ``attrs`` into an existing one of the same kind between the same nodes. Self-loops are ignored and return None."""
    def remove_edge(self, edge: Edge) -> None: ...
    def changed(self) -> None:
        """Note that nodes or edges were edited in place (a name, a relation key, an edge's
        ``role``), so the indexes queries use are made again."""
    def indexed(self, name: str, build: Callable[[Graph], _Index], *, basis: IndexBasis = 'graph') -> _Index:
        """The index ``name`` that ``build`` makes from the graph, kept until what it is made from
        changes: anything in the graph (``'graph'``), a node (``'nodes'``, for an index that reads
        no edges) or a relation (``'relations'``)."""
    def warn(self, message: str) -> None: ...
    @property
    def edges(self) -> list[Edge]: ...
    def out_edges(self, node_id: str, kind: str | None = None) -> list[Edge]:
        """Edges leaving ``node_id``, optionally only of one kind."""
    def in_edges(self, node_id: str, kind: str | None = None) -> list[Edge]:
        """Edges arriving at ``node_id``, optionally only of one kind."""
    def node_for_relation(self, key: str) -> Node | None:
        """The node SQL naming this relation refers to (see RELATION_PRIORITY)."""
    def nodes_for_relation(self, key: str) -> list[Node]:
        """Every node that describes this relation, the preferred one first."""
    def node_for_reading(self, name: str) -> Node | None:
        """The relation a name in canonical text stands for, whether the relation itself or one
        reading of it (see :func:`split_reading`)."""
    def shared_relations(self) -> Iterator[tuple[str, list[Node]]]: ...
    def relations(self) -> Iterator[Node]: ...
    def guessed_relations(self) -> list[str]:
        """The relations whose columns in the graph may not be all they have: the build marks a
        relation ``columns_guessed`` unless the warehouse or a dbt catalog lists its columns, or SQL
        that writes it selects them without a star it cannot expand."""
    def built(self, relation: Node) -> bool:
        """Whether something in the graph builds ``relation``: a model, seed or snapshot, a file of
        its own, a task, or SQL that gives its columns expressions. A source, or a table the graph
        only reads, is not built."""
    def columns_of(self, node_id: str) -> list[Node]:
        """The columns of a relation, in their order."""
    def column(self, parent_id: str, name: str) -> Node | None:
        """The column ``name`` of the relation ``parent_id``, or None."""
    def parent_of(self, node: Node) -> Node | None:
        """A column's relation, or None for anything else."""
    def label(self, node: Node) -> str:
        """Short human label: ``orders.amount`` for a column, ``orders`` for a relation."""
    def resolve(self, ref: str) -> list[Node]:
        """Find the nodes a user reference such as ``orders.amount`` or ``stg_orders`` names."""
    def downstream_edges(self, node_id: str) -> Iterator[tuple[Edge, str]]:
        """Edges along which a change to ``node_id`` propagates, with the node they reach:
        ``feeds`` forwards, and ``derived_from``, ``tests`` and ``runs`` backwards."""
    def upstream_edges(self, node_id: str) -> Iterator[tuple[Edge, str]]:
        """Edges along which ``node_id`` depends on something, with the node they reach:
        ``feeds`` and ``runs`` backwards, and ``derived_from`` forwards."""
    def expand(self, node: Node) -> list[str]:
        """A relation stands for itself and all its columns; anything else for itself."""
    def impact(self, node: Node, max_depth: int | None = None) -> list[Step]:
        """Everything downstream of ``node``.

        A column reached through a join, filter or group-by (``role = "reference"``) is reported,
        but the walk does not continue from the whole relation it reached, so one join key does
        not mark every downstream column as affected. For a column, the exposures that use and
        the tasks that read or run each affected relation are reported too, and so are the tasks
        whose SQL an edge on the way came from (an Airflow SQL task, a Dagster asset). Tests and
        the tasks that run a node are reported, not walked through.
        """
    def trace(self, node: Node, max_depth: int | None = None) -> list[Step]:
        """Everything upstream of ``node``. For a column, the tasks that build each relation on the
        way (including the column's own) are reported too."""
    def fed_by_columns_of(self, consumer: str, relation: str) -> bool:
        """Whether ``consumer`` (a Looker view, say) records which of ``relation``'s columns it
        reads, so a column reaches it only through its own edge, and walks do not pass it every
        column of ``relation``."""
    def path(self, start: Node, end: Node) -> list[Step]:
        """Shortest path from ``start`` to ``end``, following data flow in either direction: downstream
        if there is a path that way, else upstream, else either way through ``contains`` and
        ``precedes`` too. Of several paths of the same length, each step takes the neighbour whose
        id sorts first, so the same path is found whatever order the build added the edges in."""
    def to_dict(self) -> dict[str, Any]:
        """The graph as plain data: ``format_version``, ``meta``, ``nodes``, ``edges`` and ``warnings``."""
    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str = 'the graph') -> Graph:
        """The graph in ``data``, as ``to_dict`` writes it in this or any earlier format.

        Raises:
            konstruct.FormatError: A newer Konstruct wrote ``data`` (``source`` names it).
        """
    def write(self, path: Path) -> None:
        """Write the graph to ``path`` as JSON (``graph.json``)."""
    @classmethod
    def read(cls, path: Path) -> Graph:
        """Load a graph from a ``graph.json`` file, written by this or any earlier Konstruct.

        Raises:
            json.JSONDecodeError: The file is not JSON; the message says where and why.
            konstruct.FormatError: A newer Konstruct wrote it, it is not a graph, or it is larger
                than 2 GiB.
        """
    @classmethod
    def from_json(cls, data: bytes, source: str = 'the graph') -> Graph:
        """The graph in ``data``, the bytes of a ``graph.json``, as ``read`` reads a file.

        Raises:
            json.JSONDecodeError: ``data`` is not JSON; the message says where and why.
            konstruct.FormatError: A newer Konstruct wrote it, it is not a graph, or it is larger
                than 2 GiB (``source`` names it).
        """
    def stats(self) -> dict[str, int]: ...

class YamlFailure(Exception):
    """A YAML text that cannot be read. ``args`` is ``(stage, context, context_mark, problem,
    problem_mark)``: the stage that refused it (``reader``, ``scanner``, ``parser``,
    ``composer``, ``constructor``, ``unreadable`` or ``limit``), what it was doing and where that began, the
    problem and where it is; a mark is ``(index, line, column)``. ``konstruct.core.yamlio`` raises
    the PyYAML exception of the same stage from it."""

    args: tuple[str, str | None, tuple[int, int, int] | None, str, tuple[int, int, int] | None]

def yaml_load(
    source: str | bytes,
    lines: bool = False,
    max_values: int = 1_000_000,
    max_depth: int = 250,
    digits: int = 4300,
) -> Any:
    """The value of the YAML document ``source``, read as PyYAML's ``safe_load`` reads it (YAML
    1.1), None for an empty one. Raises :class:`YamlFailure` for text that cannot be read: not
    YAML, not UTF-8 or UTF-16, a value its tag cannot make, an unpaired surrogate, an integer of
    more than ``digits`` digits, nesting past ``max_depth`` (aliases expanded), or aliases that add
    more than ``max_values`` values. With ``lines`` every mapping is a dict of string keys with
    the line it starts on, from one, as ``__line__``."""

def yaml_load_all(
    source: str | bytes,
    lines: bool = False,
    max_values: int = 1_000_000,
    max_depth: int = 250,
    digits: int = 4300,
) -> list[Any]:
    """The value of each document of the stream ``source``, an empty document left out, each read
    as :func:`yaml_load` reads one. The values aliases add are one budget of ``max_values`` for
    the whole stream."""
