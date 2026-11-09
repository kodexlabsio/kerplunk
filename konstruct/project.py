"""The Python API: one object for a project, its graph and every question Konstruct answers.

Example::

    import konstruct

    project = konstruct.Project('path/to/repo')
    project.build(update=True)
    walk = project.impact('orders.amount')
    print(walk.summary())
    for column in walk.columns:
        print(column.label, column.transform, column.path)
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from konstruct import query
from konstruct.graph import Graph

if TYPE_CHECKING:
    from konstruct.build import BuildResult
    from konstruct.config import Config
    from konstruct.conflicts import Conflicts
    from konstruct.diff import Diff
    from konstruct.explain import Explanation
    from konstruct.ontology import Ontology
    from konstruct.verify import Engine, Verification

OUT_DIR = 'konstruct-out'


def find_out_dir(start: Path) -> Path:
    """Find the output directory for ``start``.

    Args:
        start: A project root, or any directory inside one.

    Returns:
        The nearest ``konstruct-out`` at or above ``start`` that holds a ``graph.json``, otherwise
        ``start/konstruct-out``.
    """
    start = start.resolve()
    for directory in (start, *start.parents):
        if (directory / OUT_DIR / 'graph.json').is_file():
            return directory / OUT_DIR
        if directory.name == OUT_DIR and (directory / 'graph.json').is_file():
            return directory
    return start / OUT_DIR


class Project:
    """A repository Konstruct builds a graph for, and every question that can be asked of it.

    Creating a ``Project`` reads nothing. ``build()`` writes the graph; the question methods read it,
    rereading ``graph.json`` whenever a build changes it, and raise :class:`~konstruct.QueryError`
    when there is no graph or a name matches nothing (or several things). Names are accepted as
    the CLI accepts them: ``orders``, ``main.orders``, ``orders.amount``, a dbt source as
    ``source_name.table``, a task as ``dag_id.task_id``, or a node id.

    Every result is a dataclass: ``str(result)`` is the CLI's text and ``result.to_dict()`` the
    JSON that ``--json`` and the MCP server return.

    Args:
        path: The project root, or any directory inside one that has already been built.
        out_dir: Where the graph is written and read. By default, the ``konstruct-out`` directory
            found at or above ``path``, or ``<path>/konstruct-out``.

    Example::

        project = Project('analytics')
        if not project.has_graph() or project.changed_inputs():
            project.build(update=True)
        print(project.explain('orders.amount').composed)
    """

    def __init__(self, path: str | Path = '.', *, out_dir: str | Path | None = None) -> None:
        self.path = Path(path).resolve()
        if out_dir is not None:
            self.out_dir = Path(out_dir).resolve()
        else:
            found = find_out_dir(self.path)
            self.out_dir = found if (found / 'graph.json').is_file() else self.path / OUT_DIR
        self._graph: Graph | None = None
        self._stamp: tuple[float, int] | None = None

    def __repr__(self) -> str:
        return f'Project({str(self.path)!r})'

    @property
    def root(self) -> Path:
        """The directory the graph was built from (``path`` until a graph exists)."""
        graph_root = self.graph.meta.get('root') if self.has_graph() else None
        return Path(graph_root) if graph_root else self.path

    @property
    def graph_path(self) -> Path:
        """The path of ``graph.json`` in the output directory."""
        return self.out_dir / 'graph.json'

    def has_graph(self) -> bool:
        """Whether a graph has been built (``graph.json`` exists)."""
        return self.graph_path.is_file()

    def config(self) -> Config:
        """The project's configuration: ``konstruct.toml`` (or ``[tool.konstruct]`` in
        ``pyproject.toml``) over the defaults, with nothing discovered yet.

        Raises:
            konstruct.config.ConfigError: The configuration file is invalid.
        """
        from konstruct.config import load_config

        return load_config(self.path, self.out_dir)

    def build(
        self,
        *,
        update: bool = False,
        run_dbt: bool | None = None,
        warehouses: bool = True,
        dialect: str | None = None,
        config: Config | None = None,
    ) -> BuildResult:
        """Build the graph and write ``graph.json``, ``REPORT.md`` and ``graph.html``.

        Discovers dbt projects, warehouses, Airflow and Dagster files, SQL files and definitions
        (or reads what the configuration pins), analyses the SQL and writes the outputs. Problems
        with the inputs do not fail the build; they are recorded in ``result.graph.warnings``.

        Args:
            update: Reuse the cached analysis of anything unchanged since the last build.
            run_dbt: Run ``dbt docs generate`` when a project's artefacts are missing or stale.
                ``None`` follows the configuration (on by default).
            warehouses: Read warehouse metadata. ``False`` builds from files alone.
            dialect: The SQL dialect, overriding the one taken from dbt or the warehouse.
            config: A configuration to build with instead of reading ``konstruct.toml``.

        Returns:
            The graph, the files written, the time taken, cache use and counts of what was read.

        Example::

            result = project.build(update=True, warehouses=False)
            print(result.inputs, result.graph.warnings)
        """
        from konstruct.build import build

        config = config or self.config()
        config.override(run_dbt=run_dbt, warehouses=warehouses, dialect=dialect)
        result = build(config, update=update)
        self.out_dir = config.out_dir
        self._graph = result.graph
        self._stamp = self._file_stamp()
        return result

    def _file_stamp(self) -> tuple[float, int] | None:
        try:
            stat = self.graph_path.stat()
        except OSError:
            return None
        return (stat.st_mtime, stat.st_size)

    @property
    def graph(self) -> Graph:
        """The built graph, reread whenever ``graph.json`` changes on disk.

        Raises:
            konstruct.QueryError: No graph has been built.
        """
        stamp = self._file_stamp()
        if stamp is None:
            if self._graph is not None:
                return self._graph
            raise query.QueryError(f'no graph at {self.graph_path}; run `konstruct build` first')
        if self._graph is None or stamp != self._stamp:
            self._graph = Graph.read(self.graph_path)
            self._stamp = stamp
        return self._graph

    def changed_inputs(self) -> list[str]:
        """Input files that changed after the graph was built.

        Returns:
            Paths relative to the project root; empty when the graph is up to date or not built.
        """
        from konstruct.build import changed_inputs

        return changed_inputs(self.graph_path, self.graph) if self.has_graph() else []

    def impact(self, ref: str, max_depth: int | None = None) -> query.Walk:
        """Everything downstream of a table, model or column: what a change to it affects.

        For a column: the columns derived from it, the relations that join, filter or group on it,
        and the tests, exposures, orchestration tasks and business terms on anything reached. A
        join key reaching a relation does not mark every column of that relation as affected.

        Args:
            ref: A table, model or column, such as ``orders`` or ``stg_payments.amount``.
            max_depth: Stop after this many steps.

        Returns:
            The walk, grouped into columns, relations, tests, exposures, tasks and terms.

        Raises:
            konstruct.QueryError: ``ref`` matches nothing or several things.

        Example::

            walk = project.impact('stg_payments.amount')
            walk.summary()  # '7 columns, 3 relations, 5 tests, 1 exposure, 2 tasks and ...'
            [column.label for column in walk.columns if column.transform == 'direct']
        """
        return query.impact(self.graph, ref, max_depth)

    def trace(self, ref: str, max_depth: int | None = None) -> query.Walk:
        """Everything upstream of a table, model or column: where its data comes from.

        Each column step is marked ``direct`` (passed through or renamed) or ``transformed``. For
        a column, ``filtered_by`` lists the columns that join or filter its relation, and the tasks
        that build each relation on the way are included.

        Args:
            ref: A table, model or column.
            max_depth: Stop after this many steps.

        Returns:
            The walk upstream.

        Raises:
            konstruct.QueryError: ``ref`` matches nothing or several things.

        Example::

            for column in project.trace('customer_ltv.lifetime_value').columns:
                print(column.depth, column.label, column.transform, column.verified)
        """
        return query.trace(self.graph, ref, max_depth)

    def path_between(self, start: str, end: str) -> query.PathResult:
        """The shortest connection between two nodes, along data flow where there is one.

        Args:
            start: Where the path starts.
            end: Where it ends.

        Returns:
            The steps from ``start`` to ``end``; ``result.found`` is False when they are not
            connected.

        Raises:
            konstruct.QueryError: Either name matches nothing or several things.

        Example::

            path = project.path_between('raw_payments', 'customer_dashboard')
            [step.label for step in path.steps]
        """
        return query.path(self.graph, start, end)

    def search(self, text: str, limit: int = 20, kinds: list[str] | None = None) -> query.SearchResult:
        """Find nodes whose names, synonyms or descriptions match the words in ``text``.

        Words matching a name count for more than words in a description, rarer words for more
        than common ones, and business terms rank above columns with the same words.

        Args:
            text: The words to look for.
            limit: The most results to return.
            kinds: Only these node kinds, such as ``['column']`` or ``['model', 'table']``.

        Returns:
            Matching nodes, best first, each with a ``score``.

        Example::

            project.search('lifetime value', kinds=['column']).results[0].label
        """
        return query.search(self.graph, text, limit, kinds)

    def describe(self, ref: str) -> query.Description:
        """One node in detail.

        For a relation: its columns with types and descriptions, tests, the joins, filters and
        grouping that shape its rows, and its immediate neighbours. For a column: its expression
        and the business terms that define it. For a task: what it runs and the tasks around it.
        For a business term: its definition, owner, status and columns.

        Args:
            ref: Any node: a relation, column, task, DAG, test, exposure or term.

        Raises:
            konstruct.QueryError: ``ref`` matches nothing or several things.

        Example::

            orders = project.describe('orders')
            [(c.name, c.data_type) for c in orders.columns]
            orders.conditions  # ['GROUP BY: stg_payments.order_id', 'LEFT JOIN ON: ...']
        """
        return query.describe(self.graph, ref)

    def conflicts(self, limit: int | None = None, *, write: bool = False) -> Conflicts:
        """Columns that appear to be one business idea computed in different ways.

        Computed columns are grouped by what their names mean once qualifiers and synonyms are
        folded together, by business-term synonyms and by shared terms, then compared on their
        formulas in terms of sources, their source columns, and the filters and joins on their
        routes back to source. Pairs that agree, or where one is derived from the other, are left
        out.

        Args:
            limit: The most conflicts to return.
            write: Also write ``CONFLICTS.md`` and ``conflicts.json`` beside the graph.

        Returns:
            The conflicts, ranked by what depends on each side downstream.

        Example::

            for conflict in project.conflicts().conflicts:
                print(conflict.a.label, conflict.b.label)
                for difference in conflict.differences:
                    print(' ', difference.kind, difference.detail, difference.path)
        """
        from konstruct.conflicts import find_conflicts

        result = find_conflicts(self.graph, limit)
        if write:
            (self.out_dir / 'CONFLICTS.md').write_text(result.markdown(), encoding='utf-8')
            (self.out_dir / 'conflicts.json').write_text(
                json.dumps(result.to_dict(), indent=1) + '\n', encoding='utf-8'
            )
        return result

    def ontology(self) -> Ontology:
        """The ontology of the data stack, derived from the graph.

        Entities come from identifier-like columns with uniqueness tests, single-column primary
        keys and identifier fields, terms of kind ``entity`` and the grains of measures;
        measures are business terms with a formula, each bound column with its formula composed
        down to source and how many edges on the way are verified; relationships come from dbt
        ``relationships`` tests and from joins that match a column to an entity's key. The rules
        list what breaks the ontology now. ``konstruct build`` writes the same to ``ontology.yml``.

        Returns:
            The entities, measures, relationships and rules.

        Example::

            ontology = project.ontology()
            [(e.name, e.key, e.relations) for e in ontology.entities]
            ontology.measure('customer lifetime value').columns[0].formula
            ontology.violations  # [] when every rule holds
        """
        from konstruct.ontology import derive

        return derive(self.graph)

    def define(self, ref: str) -> query.Definitions:
        """The business definition of a term, or the terms that define a column.

        Looks ``ref`` up as a term name or synonym first (case and spacing ignored), then as a
        column or relation, returning the terms bound to it.

        Args:
            ref: A term (``lifetime value``), a synonym (``LTV``) or a column (``orders.amount``).

        Returns:
            The matching terms, approved first and retired last, with their definition, owner,
            status, grain, required filters and columns. ``terms`` is empty when a column is not
            bound to any term.

        Raises:
            konstruct.QueryError: Nothing matches ``ref``, or it names several columns.

        Example::

            term = project.define('lifetime value').terms[0]
            term.status, term.owner, [c.label for c in term.columns]
        """
        return query.define(self.graph, ref)

    def explain(self, ref: str) -> Explanation:
        """How a column is calculated.

        Returns the column's expression with CTEs, subqueries and aliases inlined, the same
        formula composed through every model upstream until only source columns remain, and step
        by step each column on the way with the joins, filters and grouping of its relation.

        Args:
            ref: A column, such as ``orders.credit_card_amount``.

        Returns:
            The explanation. ``composed`` is None for a source column, and ``complete`` is False
            when some column on the way has no recorded expression.

        Raises:
            konstruct.QueryError: ``ref`` is not a column, or matches nothing or several things.

        Example::

            explanation = project.explain('customer_ltv.lifetime_value')
            explanation.expression  # 'customers.customer_lifetime_value'
            explanation.composed    # 'SUM(raw_payments.amount / 100)'
            [step.conditions for step in explanation.steps]
        """
        from konstruct.explain import explain

        node = query.resolve_one(self.graph, ref)
        if node.kind != 'column':
            raise query.QueryError(
                f'{ref!r} is a {node.kind}; explain takes a column such as {self.graph.label(node)}.<column>'
            )
        return explain(self.graph, node)

    def export(
        self,
        fmt: str,
        *,
        select: str | None = None,
        columns: bool = False,
        namespace: str | None = None,
    ) -> str:
        """The graph in a format other tools read.

        Args:
            fmt: ``openlineage`` (JSON lines of static ``JobEvent`` objects with schema and column
                lineage facets), ``mermaid``, ``dot``, ``csv`` (one row per column edge) or ``ossie``
                (the ontology as an Apache Ossie semantic model).
            select: Only the lineage, both directions, of this table or column.
            columns: For ``mermaid`` and ``dot``, draw columns and column lineage.
            namespace: For ``openlineage``, the dataset namespace (default ``<dialect>://konstruct``).

        Returns:
            The export as text.

        Raises:
            ValueError: ``fmt`` is not one of the formats.
            konstruct.QueryError: ``select`` matches nothing or several things.

        Example::

            Path('lineage.jsonl').write_text(project.export('openlineage', namespace='duckdb://shop'))
            print(project.export('mermaid', select='orders.amount', columns=True))
        """
        from konstruct.export import export

        node = query.resolve_one(self.graph, select) if select else None
        return export(self.graph, fmt, select=node, columns=columns, namespace=namespace)

    def list(self, kinds: list[str] | None = None) -> query.Listing:
        """Every node of the given kinds.

        Args:
            kinds: Node kinds such as ``['model']`` or ``['task', 'dag']``. By default, tables,
                models, sources, seeds and snapshots.

        Example::

            [item.label for item in project.list(['model']).items]
        """
        return query.list_nodes(self.graph, kinds)

    def verify(
        self,
        *,
        warehouse: str | Path | None = None,
        seed: int = 1729,
        max_rows: int = 10000,
        models: Sequence[str] | None = None,
        write: bool = True,
    ) -> Verification:
        """Check the graph's column lineage against the data.

        For every dbt model and SQL-built table, copies its inputs (DuckDB), works inside a
        transaction that is rolled back (Postgres), or copies a sample of each input into an
        in-memory DuckDB database (Snowflake, BigQuery, Databricks), perturbs one input column at
        a time by shifting, nulling and shuffling it, reruns the model's SQL and records which
        outputs change. A claimed edge is confirmed when its output changes; an observed influence the
        graph does not claim is reported as unexplained.

        Args:
            warehouse: A DuckDB file, a ``postgresql://`` URL, or ``snowflake``, ``bigquery`` or
                ``databricks`` for the configured warehouse of that type, sampled. By default,
                the DuckDB or Postgres warehouse the build read.
            seed: The random seed, so a run can be repeated exactly.
            max_rows: The most rows sampled from any input.
            models: Only these models or tables.
            write: Write ``VERIFY.md`` and ``verify.json``, and mark verified edges in
                ``graph.json``, ``graph.html`` and ``ontology.yml``.

        Returns:
            One result per model, with ``totals()`` across them.

        Raises:
            konstruct.QueryError: No warehouse was found.

        Example::

            result = project.verify(seed=7)
            result.totals()  # {'claimed': 36, 'confirmed': 36, 'unexplained': 0, ...}
        """
        from konstruct.html import write_html
        from konstruct.ontology import derive as derive_ontology
        from konstruct.verify import REMOTE, annotate, verify

        graph = self.graph
        target: str | Engine
        if warehouse is not None and str(warehouse).lower() in REMOTE:
            target = self._remote_engine(str(warehouse).lower())
        elif warehouse:
            target = str(warehouse)
        else:
            target = self._verification_warehouse()
        result = verify(
            graph, self.root, target, seed=seed, max_rows=max_rows, only=list(models) if models else None
        )
        if isinstance(target, str):
            path = Path(target)
            if not result.warehouse.startswith('postgres') and path.resolve().is_relative_to(
                self.root.resolve()
            ):
                result.warehouse = str(path.resolve().relative_to(self.root.resolve()))
        if write:
            annotate(graph, result)
            graph.write(self.graph_path)
            self._stamp = self._file_stamp()
            if (self.out_dir / 'graph.html').is_file():
                write_html(graph, self.out_dir / 'graph.html')
            if (self.out_dir / 'ontology.yml').is_file():
                derive_ontology(graph).write(self.out_dir / 'ontology.yml')
            (self.out_dir / 'VERIFY.md').write_text(result.markdown(), encoding='utf-8')
            (self.out_dir / 'verify.json').write_text(
                json.dumps(result.to_dict(), indent=1) + '\n', encoding='utf-8'
            )
        return result

    def _remote_engine(self, kind: str) -> Engine:
        """A sampled engine for the configured Snowflake, BigQuery or Databricks warehouse."""
        from konstruct.config import discover_warehouses
        from konstruct.dbt import find_projects
        from konstruct.verify import sampled_engine

        config = self.config()
        for warehouse in discover_warehouses(config, find_projects(config.root)):
            if warehouse.type == kind:
                try:
                    return sampled_engine(warehouse.connector(config.root), self.graph)
                except Exception as error:  # a missing extra or a refused connection
                    raise query.QueryError(f'cannot connect to {kind}: {error}') from error
        raise query.QueryError(f'no {kind} warehouse is configured')

    def _verification_warehouse(self) -> str:
        """The DuckDB file the build read, or else the Postgres warehouse it connected to."""
        for source in self.graph.meta.get('sources', []):
            if source.get('input') == 'duckdb' and source.get('status') != 'failed':
                path = Path(str(source['name']).removeprefix('duckdb ').strip())
                return str(path if path.is_absolute() else self.root / path)
        from konstruct.config import discover_warehouses
        from konstruct.dbt import find_projects
        from konstruct.warehouse import PostgresConnector

        config = self.config()
        for warehouse in discover_warehouses(config, find_projects(config.root)):
            if warehouse.type in ('postgres', 'postgresql'):
                connector = warehouse.connector(config.root)
                if isinstance(connector, PostgresConnector):
                    return connector.dsn
        raise query.QueryError(
            'verification needs the models built in a DuckDB file or a Postgres database; none was '
            'found (pass --warehouse PATH or a postgresql:// URL, or --warehouse snowflake, bigquery '
            'or databricks to check a sample of that warehouse)'
        )

    def diff(self, base: str | None = None, head: str | None = None) -> Diff:
        """What a change does to the data stack, column by column.

        Compares each changed dbt model, SQL file, DAG, dbt YAML file and definitions file
        between ``base`` and ``head``. Columns are compared by their canonical expressions, so
        formatting and renamed CTEs do not count as changes. A removed column that something
        downstream still reads is a breaking change.

        Args:
            base: The commit to compare with. By default, the merge base with ``main``.
            head: The commit to compare. By default, the working tree.

        Returns:
            The changes, with ``breaking``, the downstream impact of each, and a column-aware
            ``dbt build --select`` in ``selection``.

        Raises:
            konstruct.diff.DiffError: The project is not in a git repository, or a commit is
                unknown.

        Example::

            change = project.diff(base='origin/main')
            for relation, column in change.breaking:
                print(relation, column.column, column.breaks)
            print(change.selection.command())
        """
        from konstruct.diff import diff

        return diff(self.graph, self.root, base=base, head=head)

    def query(self, question: str, depth: int = 1) -> query.QueryResult:
        """Answer a question in words with the nodes it is about and their connections.

        Ranks nodes by the question's words (there is no language model), returns the best
        matches with the edges around them, leads with the definitions of any business terms
        matched, and adds impact or lineage when the question asks what breaks or where
        something comes from.

        Args:
            question: The question, such as ``what breaks if stg_payments.amount changes``.
            depth: How many steps of connections around each match to include.

        Example::

            answer = project.query('what is lifetime value')
            answer.definitions[0].definition
        """
        return query.query(self.graph, question, depth=depth)
