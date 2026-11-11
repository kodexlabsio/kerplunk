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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from konstruct.analysis import query
from konstruct.core import jsonio
from konstruct.core.formats import document
from konstruct.core.graph import Graph

if TYPE_CHECKING:
    from konstruct.analysis.checksql import HistoryCheck, SqlCheck
    from konstruct.analysis.conflicts import Conflicts
    from konstruct.analysis.diff import Diff
    from konstruct.analysis.english import English
    from konstruct.analysis.explain import Explanation
    from konstruct.analysis.gate import Gate
    from konstruct.analysis.history import History
    from konstruct.analysis.mutation import MutationTesting
    from konstruct.analysis.receipts import Receipt, ReceiptCheck
    from konstruct.analysis.release_notes import ReleaseNotes
    from konstruct.analysis.verify import Engine, Verification
    from konstruct.analysis.why import Why
    from konstruct.engine.build import BuildResult
    from konstruct.engine.config import Config
    from konstruct.engine.trust import Trust
    from konstruct.plugins.registry import Plugins
    from konstruct.semantics.ontology import Ontology

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
    JSON that ``--json`` and the MCP server return, which add its ``format_version``. A
    ``graph.json`` written by a newer Konstruct raises :class:`~konstruct.FormatError` rather than
    being misread.

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

    def __init__(
        self, path: str | Path = '.', *, out_dir: str | Path | None = None, plugins: bool = True
    ) -> None:
        self.path = Path(path).resolve()
        self.use_plugins = plugins
        """Whether the plugins ``konstruct.toml`` enables run; ``False`` is ``--no-plugins``."""
        self._plugins: Plugins | None = None
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

    def trust(self, *, revoke: bool = False) -> Trust:
        """Trust the project's own connections and paths, as they are now, and let the build run dbt.

        A repository's ``konstruct.toml`` and ``profiles.yml`` can name warehouses, read
        environment variables into them and point outside the project, and dbt runs the
        repository's macros and models. The build uses those settings, and runs dbt, only once
        the project is trusted, and any change to the settings withdraws the trust until they
        are trusted again. The record does not cover the dbt project's code. It is kept outside
        the repository, in the user's configuration directory.

        Args:
            revoke: Remove the record instead.

        Returns:
            Whether the project is now trusted, the files the trust covers, the environment
            variables their connections read and the dbt projects it lets the build run dbt in.

        Example::

            print(project.trust())  # after reviewing konstruct.toml and profiles.yml
        """
        from konstruct.engine import trust

        return trust.revoke(self.path) if revoke else trust.trust(self.path)

    def config(self) -> Config:
        """The project's configuration: ``konstruct.toml`` (or ``[tool.konstruct]`` in
        ``pyproject.toml``) over the defaults, with nothing discovered yet.

        Raises:
            konstruct.engine.config.ConfigError: The configuration file is invalid.
        """
        from konstruct.engine.config import load_config

        config = load_config(self.path, self.out_dir)
        config.override(plugins=self.use_plugins)
        return config

    def plugins(self) -> Plugins:
        """The plugin registry for this project: Konstruct's own inputs, export formats, gate checks
        and ontology packs, and every installed plugin with whether it runs here and why.

        Example::

            for plugin in project.plugins().installed():
                print(plugin.distribution, plugin.group, plugin.name, plugin.enabled, plugin.reason)
        """
        from konstruct.plugins.registry import Plugins

        if self._plugins is None:
            self._plugins = Plugins.for_config(self.config())
        return self._plugins

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
        from konstruct.engine.build import build

        config = config or self.config()
        config.override(run_dbt=run_dbt, warehouses=warehouses, dialect=dialect, plugins=self.use_plugins)
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
        from konstruct.engine.build import changed_inputs

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

    def search(
        self,
        text: str,
        limit: int = 20,
        kinds: list[str] | None = None,
        mode: query.SearchMode = 'keyword',
    ) -> query.SearchResult:
        """Find nodes whose names, synonyms or descriptions match the words in ``text``, or, with
        ``mode='semantic'``, that match them by meaning.

        Words matching a name count for more than words in a description, rarer words for more
        than common ones, and business terms rank above columns with the same words. Search by
        meaning also finds a node through the abbreviations and synonyms of data work, its
        formula and its lineage (see :func:`konstruct.analysis.semantic.search`), with exact
        names still first; its index is made on first use and saved as ``vectors.bin`` in the
        output directory.

        Args:
            text: The words to look for.
            limit: The most results to return.
            kinds: Only these node kinds, such as ``['column']`` or ``['model', 'table']``.
            mode: ``keyword`` (the default) or ``semantic``.

        Returns:
            Matching nodes, best first, each with a ``score``, and in a search by meaning what
            the match rests on (``matched``).

        Example::

            project.search('lifetime value', kinds=['column']).results[0].label
            project.search('revenue', mode='semantic').results[0].matched
        """
        return query.search(self.graph, text, limit, kinds, mode, self.out_dir)

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
            write: Also write ``CONFLICTS.md`` and ``conflicts.json`` beside the graph, and show the
                conflicts in ``graph.html``.

        Returns:
            The conflicts, ranked by what depends on each side downstream.

        Example::

            for conflict in project.conflicts().conflicts:
                print(conflict.a.label, conflict.b.label)
                for difference in conflict.differences:
                    print(' ', difference.kind, difference.detail, difference.path)
        """
        from konstruct.analysis.conflicts import find_conflicts

        result = find_conflicts(self.graph, limit)
        if write:
            from konstruct.outputs.html import graph_fingerprint, viewer_data, write_html, write_viewer_data

            jsonio.write_text(self.out_dir / 'CONFLICTS.md', result.markdown())
            # The fingerprint says which graph these are true of, so graph.html shows them only
            # while the graph is unchanged.
            data = {**document(result), 'graph': graph_fingerprint(self.graph)}
            jsonio.write_text(self.out_dir / 'conflicts.json', json.dumps(data, indent=1) + '\n')
            page, explain = self.out_dir / 'graph.html', self.out_dir / 'explain.json'
            if page.is_file() or explain.is_file():
                # explain.json too: `konstruct serve` reloads it when it changes.
                shown = viewer_data(self.graph, self.out_dir)
                if explain.is_file():
                    write_viewer_data(shown, explain)
                if page.is_file():
                    write_html(self.graph, page, data=shown)
        return result

    def check_sql(self, sql: str) -> SqlCheck:
        """Check SQL against the project's definitions, keys and relationships, without running it.

        Each statement is read in the graph's dialect. Every output column that aggregates is a
        measure: its formula is composed down to source columns, with the filters on the way, and
        compared with the business term it is named for or the column of the project that already
        holds it. The checks find a measure that leaves out a filter its definition requires or
        computes it differently, a column of a retired term, a sum, count or average over a join
        that repeats the rows it aggregates, a measure of a coarser grain repeated on the rows of a
        finer one, a join off the columns of a relationship, and a relation or column the project
        does not have. Each finding carries its fix, and ``corrected`` holds the SQL with every fix
        that changes nothing else applied.

        Args:
            sql: One or more statements.

        Returns:
            The findings, errors first. ``passed`` is True when every statement was analysed
            (``checked``) and nothing breaks an approved definition.

        Example::

            result = project.check_sql('select region, sum(amount) as revenue from orders group by 1')
            result.passed
            [(f.rule, f.fix) for f in result.findings]
            result.corrected
        """
        from konstruct.analysis.checksql import check_sql

        return check_sql(self.graph, sql)

    def check_history(
        self, *, days: int | None = None, limit: int = 5000, pseudonymise: bool = False
    ) -> HistoryCheck:
        """Run :meth:`check_sql` over the query history of the project's Snowflake, BigQuery and
        Databricks warehouses, and gather the queries that compute a measure differently from its
        definition (shadow metrics), with how many queries, runs and people each has.

        Reads history metadata only, over the project's own connections, so the project must be
        trusted (``konstruct trust``).

        Args:
            days: How far back to read, by default ``[usage] days`` in ``konstruct.toml``.
            limit: The most distinct query texts to read and check, those run most often first.
            pseudonymise: Count people without naming them, and leave the literals out of the
                example queries.

        Returns:
            The shadow metrics, approved terms first, and how often each term is read through its
            own columns.

        Raises:
            konstruct.QueryError: ``days`` or ``limit`` is below 1, no such warehouse is configured,
                the project is not trusted, or the warehouse refuses the history.

        Example::

            history = project.check_history(days=30, pseudonymise=True)
            [(s.term, s.detail, s.queries, s.users) for s in history.shadow]
        """
        from konstruct.analysis.checksql import check_history
        from konstruct.engine.config import ConfigError
        from konstruct.inputs.usage import HistoryError, MissingDriver, NoHistory, project_history

        if limit < 1:
            raise query.QueryError('the limit must be at least 1 query')
        if days is not None and days < 1:
            raise query.QueryError('the history must cover at least 1 day')
        try:
            history = project_history(self.config(), days=days, max_queries=limit)
        except NoHistory as error:
            raise query.QueryError(str(error)) from error
        except (ConfigError, MissingDriver, HistoryError) as error:
            # An untrusted project, a missing extra, or a connection or query the warehouse refused.
            raise query.QueryError(f'cannot read query history: {error}') from error
        return check_history(self.graph, history, pseudonymise=pseudonymise, limit=limit)

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
        from konstruct.semantics.ontology import derive

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

    def history(self, ref: str, *, limit: int = 50) -> History:
        """When a column's formula changed, and who changed the business terms bound to it.

        Reads the git history of the file that defines the column and of each ``definitions.yml``
        that binds a term to it. The formula is worked out at each commit, so a commit that only
        reformats the SQL is not a change; uncommitted edits are the last entry.

        Args:
            ref: A column, such as ``customers.lifetime_value``.
            limit: The most commits to read for each file.

        Returns:
            The formula changes and term changes, oldest first, each with its commit's author and
            date, and whether a bound term was approved after the latest formula change.

        Raises:
            konstruct.QueryError: ``ref`` is not a column, or matches nothing or several things,
                or ``limit`` is below 1.

        Example::

            history = project.history('customers.lifetime_value')
            [(c.commit.author, c.before, c.after) for c in history.formulas]
            history.last_approval.commit.author if history.last_approval else None
        """
        from konstruct.analysis.history import history

        if limit < 1:
            raise query.QueryError('the limit must be at least 1 commit')
        node = query.resolve_one(self.graph, ref)
        if node.kind != 'column':
            raise query.QueryError(
                f'{ref!r} is a {node.kind}; history takes a column such as {self.graph.label(node)}.<column>'
            )
        return history(self.graph, self.root, node, limit=limit)

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
        from konstruct.analysis.explain import explain

        node = query.resolve_one(self.graph, ref)
        if node.kind != 'column':
            raise query.QueryError(
                f'{ref!r} is a {node.kind}; explain takes a column such as {self.graph.label(node)}.<column>'
            )
        return explain(self.graph, node)

    def english(self, ref: str) -> English:
        """A column's formula in plain English, worded from the formula by a fixed grammar.

        The formula is composed down to source columns, as :meth:`explain` gives it, and each kind
        of expression has one wording, with brackets where the SQL's grouping matters, so the
        sentence says what the SQL says and nothing more. A part with no wording is quoted as SQL
        and listed in ``verbatim``. The joins, filters and grouping of every relation on the way,
        the grain of the column's relation and the required filters of each bound business term
        are worded the same way.

        Args:
            ref: A column, such as ``orders.credit_card_amount``.

        Returns:
            The sentence, the conditions of each relation in English, the grain, the bound terms and
            the column each wording names.

        Raises:
            konstruct.QueryError: ``ref`` is not a column, or matches nothing or several things.

        Example::

            result = project.english('orders.credit_card_amount')
            result.english
            # "the sum of ([amount] divided by 100 if [payment method] is 'credit_card', else 0)"
            [(n.wording, n.column) for n in result.names]  # [('[amount]', 'raw_payments.amount'), ...]
        """
        from konstruct.analysis.english import english

        node = query.resolve_one(self.graph, ref)
        if node.kind != 'column':
            raise query.QueryError(
                f'{ref!r} is a {node.kind}; explain takes a column such as {self.graph.label(node)}.<column>'
            )
        return english(self.graph, node, ontology=self.ontology())

    def receipt(
        self,
        refs: Sequence[str],
        *,
        question: str | None = None,
        answer: str | None = None,
        write: bool = True,
    ) -> Receipt:
        """A receipt for an answer: what it rests on, recorded so that :meth:`check_receipts` can
        say later whether that still holds.

        For each column ``refs`` names (directly, or through a relation, an exposure or a business
        term), the receipt records its formula composed down to source columns, the joins,
        filters and grouping that decide its rows, its source columns, its type, the terms bound
        to it with their status and owner, and whether ``konstruct verify`` confirmed its
        lineage; and the build it answered from. It is appended to
        ``konstruct-out/receipts/<date>.jsonl`` with a digest of its contents.

        Args:
            refs: What the answer rests on: columns, relations, exposures or business terms.
            question: The question answered, recorded as given.
            answer: The answer given, recorded as given.
            write: False to make the receipt without recording it.

        Returns:
            The receipt, with ``citation``, a paragraph to put under the answer.

        Raises:
            konstruct.QueryError: A name matches nothing or several things, or names something
                an answer cannot rest on, or ``refs`` is empty.

        Example::

            receipt = project.receipt(['customers.customer_lifetime_value'], question='LTV of 3?')
            print(receipt.citation)
        """
        from konstruct.analysis.receipts import DIRECTORY, issue

        return issue(
            self.graph,
            self.root,
            self.out_dir / DIRECTORY,
            list(refs),
            graph_file=self.graph_path,
            question=question,
            answer=answer,
            write=write,
        )

    def receipts(self, ids: Sequence[str] | None = None) -> list[Receipt]:
        """The receipts issued for this project, in the order issued, or those ``ids`` name
        (each an id, or the start of one, such as ``r-7f3a``).

        Raises:
            konstruct.QueryError: An id names no receipt, or several, or a receipt it names does
                not match its digest or cannot be read.
        """
        from konstruct.analysis.receipts import DIRECTORY, read

        return read(self.out_dir / DIRECTORY, list(ids) if ids else None, root=self.root)

    def check_receipts(
        self, ids: Sequence[str] | None = None, *, history: bool = True, refresh: bool = True
    ) -> ReceiptCheck:
        """Whether what each receipt's answer rests on still holds in the current graph.

        A receipt is stale when a column it rests on was removed, or its formula, row conditions,
        sources or type changed, or an approved term it rests on changed its meaning or status;
        tampered when its record does not match its digest. A rebuild that changes none of these
        leaves it holding. A graph older than its inputs cannot say a receipt holds, so the graph
        is brought up to date first, as ``build(update=True)`` does; with ``refresh`` False, a
        receipt it would say holds is unchecked instead.

        Args:
            ids: Only these receipts (each an id or the start of one); by default, every one.
            history: Read git history to say which commit changed what.
            refresh: Update the graph first when inputs changed since it was built.

        Returns:
            Each receipt with its status, what changed and who changed it; ``passed`` is True
            when every one holds.

        Raises:
            konstruct.QueryError: An id names no receipt, or several.

        Example::

            result = project.check_receipts()
            [(r.id, r.status, [c.reasons for c in r.changes]) for r in result.receipts]
        """
        from konstruct.analysis.receipts import DIRECTORY, check

        changed = self.changed_inputs()
        if changed and refresh:
            self.build(update=True)
            changed = self.changed_inputs()
        return check(
            self.graph,
            self.root,
            self.out_dir / DIRECTORY,
            list(ids) if ids else None,
            graph_file=self.graph_path,
            history=history,
            changed_inputs=changed,
        )

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
                lineage facets), ``mermaid``, ``dot``, ``csv`` (one row per column edge), ``ossie``
                (the ontology as an Apache Ossie semantic model), or a format a plugin the project
                enables adds.
            select: Only the lineage, both directions, of this table or column.
            columns: For ``mermaid`` and ``dot``, draw columns and column lineage.
            namespace: For ``openlineage``, the dataset namespace (default ``<dialect>://konstruct``).

        Returns:
            The export as text.

        Raises:
            konstruct.outputs.export.ExportError: ``fmt`` is not one of the formats, ``select`` is given
                with ``ossie``, the ontology has no entities or measures to export as Ossie, or a
                plugin's format failed. It is a ``ValueError``.
            konstruct.QueryError: ``select`` matches nothing or several things.

        Example::

            Path('lineage.jsonl').write_text(project.export('openlineage', namespace='duckdb://shop'))
            print(project.export('mermaid', select='orders.amount', columns=True))
        """
        from konstruct.outputs.export import export

        node = query.resolve_one(self.graph, select) if select else None
        return export(
            self.graph, fmt, select=node, columns=columns, namespace=namespace, plugins=self.plugins()
        )

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
        in-memory DuckDB database (Snowflake, BigQuery, Databricks, Trino), perturbs one input
        column at a time by shifting, nulling and shuffling it, reruns the model's SQL and records
        which outputs change. A claimed edge is confirmed when its output changes; an observed influence the
        graph does not claim is reported as unexplained.

        Args:
            warehouse: A DuckDB file, a ``postgresql://`` URL, or ``snowflake``, ``bigquery``,
                ``databricks`` or ``trino`` for the configured warehouse of that type, sampled. By default,
                the DuckDB or Postgres warehouse the build read.
            seed: The random seed, so a run can be repeated exactly.
            max_rows: The most rows sampled from any input.
            models: Only these models or tables.
            write: Write ``VERIFY.md`` and ``verify.json``, and mark verified edges in
                ``graph.json``, ``graph.html``, ``explain.json`` and ``ontology.yml``.

        Returns:
            One result per model, with ``totals()`` across them.

        Raises:
            konstruct.QueryError: No warehouse was found.

        Example::

            result = project.verify(seed=7)
            result.totals()  # {'claimed': 36, 'confirmed': 36, 'unexplained': 0, ...}
        """
        from konstruct.analysis.verify import annotate, verify
        from konstruct.outputs.html import viewer_data, write_html, write_viewer_data
        from konstruct.semantics.ontology import derive as derive_ontology

        graph = self.graph
        target = self._target(warehouse)
        result = verify(
            graph, self.root, target, seed=seed, max_rows=max_rows, only=list(models) if models else None
        )
        result.warehouse = self._shown(target, result.warehouse)
        if write:
            annotate(graph, result)
            page, explain = self.out_dir / 'graph.html', self.out_dir / 'explain.json'
            shown = page.is_file() or explain.is_file()
            ontology = derive_ontology(graph) if shown or (self.out_dir / 'ontology.yml').is_file() else None
            data = viewer_data(graph, self.out_dir, ontology=ontology) if shown else None
            if data is not None:
                # Before graph.json, as the build writes it: `konstruct serve` reloads explain.json
                # when graph.json changes.
                write_viewer_data(data, explain)
            graph.write(self.graph_path)
            self._stamp = self._file_stamp()
            if data is not None and page.is_file():
                write_html(graph, page, data=data)
            if ontology is not None and (self.out_dir / 'ontology.yml').is_file():
                ontology.write(self.out_dir / 'ontology.yml')
            jsonio.write_text(self.out_dir / 'VERIFY.md', result.markdown())
            jsonio.write_text(self.out_dir / 'verify.json', json.dumps(document(result), indent=1) + '\n')
        return result

    def mutate(
        self,
        *,
        warehouse: str | Path | None = None,
        seed: int = 1729,
        max_rows: int = 1000,
        max_mutants: int = 30,
        operators: Sequence[str] | None = None,
        models: Sequence[str] | None = None,
        suggest: bool = True,
        write: bool = True,
    ) -> MutationTesting:
        """Mutation test the project's dbt tests: would they fail if a model were wrong?

        For each dbt model, makes small semantic mutants of its compiled SQL (an aggregate swapped,
        a filter or join condition dropped or negated, a join type, a comparison or a date moved, a
        COALESCE removed, a CASE branch dropped), runs each in verify's read-only engines on a
        sample of the model's inputs, and runs the dbt data tests and unit tests that read the model
        against it. A mutant is killed when a test fails on it. For each survivor, a test that
        would have killed it is suggested.

        Args:
            warehouse: As for :meth:`verify`.
            seed: The seed of the mutant sample, so a run can be repeated exactly.
            max_rows: The most rows sampled from any input.
            max_mutants: The most mutants run per model.
            operators: The kinds of mutation to make (by default all of
                :data:`konstruct.core.mutate.OPERATORS`).
            models: Only these models.
            suggest: Find, for each survivor, a test that would kill it.
            write: Write ``MUTATION.md`` and ``mutation.json``.

        Returns:
            One result per model, with ``totals()`` and ``score`` across them.

        Raises:
            konstruct.QueryError: No warehouse was found, or an operator is unknown.

        Example::

            result = project.mutate(models=['orders'], max_mutants=10)
            result.score  # killed / (killed + survived)
        """
        from konstruct.analysis.mutation import mutate
        from konstruct.core.mutate import OPERATORS

        chosen = tuple(operators) if operators else OPERATORS
        unknown = sorted(set(chosen) - set(OPERATORS))
        if unknown:
            raise query.QueryError(
                f'unknown mutation operator {unknown[0]!r}; choose from {", ".join(OPERATORS)}'
            )
        target = self._target(warehouse)
        result = mutate(
            self.graph,
            self.root,
            target,
            seed=seed,
            max_rows=max_rows,
            max_mutants=max_mutants,
            operators=chosen,
            only=list(models) if models else None,
            suggest=suggest,
        )
        result.warehouse = self._shown(target, result.warehouse)
        if write:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            jsonio.write_text(self.out_dir / 'MUTATION.md', result.markdown())
            jsonio.write_text(self.out_dir / 'mutation.json', json.dumps(document(result), indent=1) + '\n')
        return result

    def why(
        self,
        column: str,
        where: Mapping[str, str],
        *,
        warehouse: str | Path | None = None,
        limit: int = 50,
        max_rows: int = 1_000_000,
    ) -> Why:
        """The source rows behind one value of a column, each with its share, and the related rows
        a filter or a join left out, with the reason.

        The value is the row of the column's model that ``where`` picks (``{'customer_id': '42'}``).
        Runs read-only in verify's engines: the model's SQL is composed down to source tables, each
        source is copied for the key alone where that is exact, and the computing step is rerun with
        each row carrying the ids of its source rows.

        Args:
            column: A column, such as ``customer_ltv.lifetime_value``.
            where: Output columns of the model and the values that pick one row.
            warehouse: As for :meth:`verify`.
            limit: The most rows listed in each table.
            max_rows: The most rows copied from any source.

        Returns:
            The value, its contributing rows with their shares, and the rows left out.

        Raises:
            konstruct.QueryError: The column is not built by SQL that verify can rerun, the key does
                not lead to the rows that compute it, or the SQL is of a shape whose rows cannot be
                traced.

        Example::

            result = project.why('customer_ltv.lifetime_value', {'customer_id': '3'})
            result.value, [c.values for c in result.contributions]
        """
        from konstruct.analysis.why import why

        target = self._target(warehouse)
        result = why(self.graph, self.root, target, column, dict(where), limit=limit, max_rows=max_rows)
        result.warehouse = self._shown(target, result.warehouse)
        return result

    def release_notes(
        self, since: str | None = None, *, until: str | None = None, limit: int = 200
    ) -> ReleaseNotes:
        """The changes to formulas, rows and business terms in a range of commits, newest first.

        Each dbt model and SQL file a commit changed is compared with its parent by canonical
        expressions, as :meth:`diff` compares a branch, so reformatting is not a change. Each changed
        column, and each relation whose joins, filters or grouping changed, is an entry with the
        formula or conditions before and after as SQL and in plain English, the commit's author and
        date, what it affects downstream (columns, exposures and their owners, business terms), and
        for each business term bound to it or downstream of it the commit that approved the term
        after the change, if one has. Changes to the business terms themselves are entries too.
        Reads only git and the graph.

        Args:
            since: A commit, tag or date (``2026-10-01``) the range starts after; by default the
                latest tag, or the whole history when there is none.
            until: A commit, tag or date the range ends at (default HEAD).
            limit: The most commits read.

        Returns:
            The entries, newest commit first. ``markdown()`` gives them as Markdown to post.

        Raises:
            konstruct.QueryError: ``since`` or ``until`` is not a commit, tag or date, the project is
                not in a git repository, or ``limit`` is below 1.

        Example::

            notes = project.release_notes('v1.4')
            [(c.commit.author, c.headline(), c.after_english) for c in notes.changes]
            print(notes.markdown())
        """
        from konstruct.analysis.diff import DiffError
        from konstruct.analysis.release_notes import release_notes

        if limit < 1:
            raise query.QueryError('the limit must be at least 1 commit')
        try:
            return release_notes(self.graph, self.root, since=since, until=until, limit=limit)
        except DiffError as error:
            raise query.QueryError(str(error)) from error

    def effect(
        self,
        since: str | None = None,
        *,
        until: str | None = None,
        warehouse: str | Path | None = None,
        models: Sequence[str] | None = None,
        limit: int = 200,
        max_rows: int = 1_000_000,
    ) -> ReleaseNotes:
        """The release notes, with what each formula and row change does on today's data.

        For each change, the changed model's SQL as it was before the commit and as it was after
        are run on the same data, the warehouse as it is now, so the difference is the definition's
        alone. Runs read-only in verify's engines: each version must be a single query that only
        reads, the tables it reads are copied, and nothing is written to the warehouse.

        Args:
            since: As for :meth:`release_notes`.
            until: As for :meth:`release_notes`.
            warehouse: As for :meth:`verify`.
            models: Only changes to these models or tables, by name or label.
            limit: As for :meth:`release_notes`.
            max_rows: The most rows copied from any table.

        Returns:
            The release notes, each formula and row change with its ``effect``: the rows each version
            returns and, for the changed column (or for a change to the rows, each column), its
            sum in each and, with a key, the rows whose value changed. A version whose templating
            cannot be rendered as it would be run is not run, and its effect says why.

        Raises:
            konstruct.QueryError: As for :meth:`release_notes`, or no warehouse is found.

        Example::

            notes = project.effect('2026-10-01')
            [(c.headline(), c.effect.summary()) for c in notes.changes if c.effect]
        """
        from konstruct.analysis.effect import effects, runnable

        notes = self.release_notes(since, until=until, limit=limit)
        if models:
            notes.changes = [
                c
                for c in notes.changes
                if c.kind == 'term'
                or any(m in (c.relation, (c.relation or '').rsplit('.', 1)[-1]) for m in models)
            ]
        if not runnable(notes):
            return notes  # nothing to run, so no warehouse is opened
        target = self._target(warehouse)
        effects(self.graph, self.root, notes, target, max_rows=max_rows, ontology=self.ontology())
        notes.warehouse = self._shown(target, notes.warehouse or '')
        return notes

    def _target(self, warehouse: str | Path | None) -> str | Engine:
        """The warehouse verify's modes run in: a named remote one sampled, a DuckDB file or
        Postgres URL as given, or else the one the build read."""
        from konstruct.analysis.verify import REMOTE

        if warehouse is not None and str(warehouse).lower() in REMOTE:
            return self._remote_engine(str(warehouse).lower())
        if warehouse:
            return str(warehouse)
        return self._verification_warehouse()

    def _shown(self, target: str | Engine, label: str) -> str:
        """A DuckDB file inside the project, named relative to it."""
        if isinstance(target, str) and not label.startswith('postgres'):
            path = Path(target).resolve()
            # The project's own directory first: a graph copied with its project keeps the old root.
            for base in (self.path, self.root.resolve()):
                if path.is_relative_to(base):
                    return str(path.relative_to(base))
        return label

    def _remote_engine(self, kind: str) -> Engine:
        """A sampled engine for the configured Snowflake, BigQuery, Databricks or Trino warehouse."""
        from konstruct.analysis.verify import sampled_engine
        from konstruct.engine.config import UntrustedConfig, dbt_projects, discover_warehouses

        config = self.config()
        for warehouse in discover_warehouses(config, dbt_projects(config)):
            if warehouse.type == kind:
                try:
                    return sampled_engine(warehouse.connector(config.root), self.graph)
                except UntrustedConfig as error:
                    raise query.QueryError(f'cannot verify against {kind}: {error}') from error
                except Exception as error:  # a missing extra or a refused connection
                    raise query.QueryError(f'cannot connect to {kind}: {error}') from error
        raise query.QueryError(f'no {kind} warehouse is configured')

    def _verification_warehouse(self) -> str:
        """The DuckDB file the build read, or else the Postgres warehouse it read.

        graph.json only says which DuckDB file the build read; the file is opened only when the
        configuration, with trust as it is recorded now, still allows it, since a graph.json can
        be committed to the repository.
        """
        from konstruct.engine.config import UntrustedConfig, dbt_projects, discover_warehouses
        from konstruct.inputs.warehouse import DuckDBConnector, PostgresConnector

        config = self.config()
        warehouses = discover_warehouses(config, dbt_projects(config))
        refused: list[str] = []
        allowed: set[Path] = set()
        for warehouse in warehouses:
            if warehouse.type == 'duckdb':
                try:
                    connector = warehouse.connector(config.root)
                except UntrustedConfig as error:
                    refused.append(str(error))
                    continue
                if isinstance(connector, DuckDBConnector):
                    allowed.add(connector.path.resolve())
        for source in self.graph.meta.get('sources', []):
            # A source the build read has no status; 'skipped' and 'failed' were not read.
            if source.get('input') == 'duckdb' and 'status' not in source and 'repository' not in source:
                path = Path(str(source['name']).removeprefix('duckdb ').strip())
                path = (path if path.is_absolute() else config.root / path).resolve()
                if path in allowed:
                    return str(path)
        read = {
            source['name']
            for source in self.graph.meta.get('sources', [])
            if source.get('input') in ('postgres', 'postgresql')
            and 'status' not in source
            and 'repository' not in source
        }
        for warehouse in warehouses:
            # As for DuckDB, only a connection the last build read, still allowed now.
            if warehouse.type in ('postgres', 'postgresql'):
                try:
                    connector = warehouse.connector(config.root)
                except UntrustedConfig as error:
                    refused.append(str(error))
                    continue
                if isinstance(connector, PostgresConnector) and warehouse.describe(config.root) in read:
                    return connector.dsn
        reason = f' ({"; ".join(refused)})' if refused else ''
        raise query.QueryError(
            'verification needs the models built in a DuckDB file or a Postgres database; none was '
            f'found{reason} (pass --warehouse PATH or a postgresql:// URL, or --warehouse snowflake, '
            'bigquery, databricks or trino to check a sample of that warehouse)'
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
            konstruct.analysis.diff.DiffError: The project is not in a git repository, or a commit is
                unknown.

        Example::

            change = project.diff(base='origin/main')
            for relation, column in change.breaking:
                print(relation, column.column, column.breaks)
            print(change.selection.command())
        """
        from konstruct.analysis.diff import diff

        return diff(self.graph, self.root, base=base, head=head)

    def gate(
        self,
        base: str | None = None,
        *,
        update: bool = True,
        run_dbt: bool | None = None,
        warehouses: bool = True,
    ) -> Gate:
        """The check a change must pass before it is proposed.

        Brings the graph up to date with the working tree, compares with ``base`` as
        :meth:`diff` does, and reports what stops the change: a removed column something still
        reads, an approved term whose meaning changes (in definitions.yml or through a column it
        names), and a column the change alters that computes a business idea differently from
        another (as :meth:`conflicts` finds), and the findings of the gate checks of the plugins
        the project enables. The plugins enabled at ``base`` run as well as those enabled now,
        in the update and in the checks, so a change cannot turn off a plugin that judges it.
        Each finding says what to do about it.

        Args:
            base: The commit to compare with. By default, the merge base with ``main``.
            update: Bring the graph up to date first. ``False`` uses it as it is, as when it was
                just built.
            run_dbt: For that build, as for :meth:`build`.
            warehouses: For that build, as for :meth:`build`.

        Example::

            result = project.gate()
            if not result.passed:
                for finding in result.findings:
                    print(finding.rule, finding.message, finding.fix)
        """
        from konstruct.analysis.gate import gate, plugins_at
        from konstruct.plugins.registry import Plugins

        # The plugins the base enables run too, its inputs in the update and its checks in the
        # gate: a change that takes a plugin out of konstruct.toml is still judged by it.
        config = self.config()
        head = config.plugins
        config.plugins = list(dict.fromkeys([*head, *plugins_at(self.root, base)]))
        if update:
            self.build(update=True, run_dbt=run_dbt, warehouses=warehouses, config=config)
        checks = self.plugins() if config.plugins == head else Plugins.for_config(config)
        return gate(self.graph, self.diff(base=base), self.conflicts(), plugins=checks)

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
