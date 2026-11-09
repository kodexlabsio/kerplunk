"""`konstruct mcp`: a stdio MCP server over the graph in konstruct-out.

The server rereads graph.json whenever it changes, so `konstruct build --update` in another
terminal is picked up without restarting the agent.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from konstruct import __version__
from konstruct.project import Project
from konstruct.query import QueryError

INSTRUCTIONS = """\
Konstruct answers questions about this repository's data stack: dbt models, sources, tests and
exposures; warehouse and Iceberg tables; Airflow DAGs and tasks; and column-level lineage.
Refer to tables as `name` or `schema.name` and to columns as `table.column`.
Use `impact` before changing a model, table or column, `trace` to find where a value comes from,
`define` for what a business term means and which columns hold it, `explain` for how a column is calculated, `describe` for one node's columns and tests, `search` when unsure of a name, and `path` to see how
two nodes connect. If a tool says there is no graph, ask the user to run `konstruct build`."""


def build_server(root: Path) -> MCPServer:
    project = Project(root)
    server = MCPServer('konstruct', version=__version__, instructions=INSTRUCTIONS)

    def run(func: Callable[..., Any], *args: Any) -> dict[str, Any]:
        if not project.has_graph():
            raise ToolError(f'no graph at {project.graph_path}; run `konstruct build` in the project first')
        try:
            result: dict[str, Any] = func(*args).to_dict()
        except QueryError as error:
            raise ToolError(str(error)) from error
        return result

    @server.tool()
    def impact(ref: str, max_depth: int | None = None) -> dict[str, Any]:
        """Everything downstream of a table, model or column (e.g. `orders` or `orders.amount`):
        derived columns, relations, models that join or filter on it, tests, exposures and the
        orchestration tasks that run them. Use before renaming, dropping or changing it."""
        return run(project.impact, ref, max_depth)

    @server.tool()
    def trace(ref: str, max_depth: int | None = None) -> dict[str, Any]:
        """Everything upstream of a table, model or column: the source columns it is derived from,
        each marked direct (passed through or renamed) or transformed, and the tasks that build it."""
        return run(project.trace, ref, max_depth)

    @server.tool()
    def search(text: str, limit: int = 20, kinds: list[str] | None = None) -> dict[str, Any]:
        """Find nodes whose names or descriptions match the words in `text`. `kinds` limits the
        results to node kinds: source, table, column, model, seed, snapshot, dag, task, test, exposure."""
        return run(project.search, text, limit, kinds)

    @server.tool()
    def describe(ref: str) -> dict[str, Any]:
        """One node in detail: description, path, columns with types and descriptions, tests,
        immediate upstream and downstream neighbours, and for tasks what they run."""
        return run(project.describe, ref)

    @server.tool()
    def conflicts(limit: int = 20) -> dict[str, Any]:
        """Columns that appear to measure the same business idea (by name, synonyms or a shared
        business term) but are computed differently: each pair with its formula in terms of
        sources, and exactly what differs (source columns, filters, joins, aggregation) with
        the file each lives in, ranked by how much depends on them downstream."""
        return run(project.conflicts, limit)

    @server.tool()
    def define(ref: str) -> dict[str, Any]:
        """The business definition of a term, by name or synonym (e.g. `lifetime value`), or the
        terms that define a column (e.g. `customers.customer_lifetime_value`): the agreed wording,
        owner, status (draft, agreed, approved, retired), grain, required filters, unit and the
        columns that hold it. Approved terms come first. Use before answering a business question,
        so the answer follows the approved definition and its required filters."""
        return run(project.define, ref)

    @server.tool()
    def explain(ref: str) -> dict[str, Any]:
        """What a column is, as SQL: its expression with CTEs and aliases inlined, the same formula
        composed down to source columns, and step by step the expression of each column on the
        way with the joins, filters and grouping that shape its rows. Use to answer "how is this
        number calculated?" or before changing a calculation."""
        return run(project.explain, ref)

    @server.tool()
    def path(start: str, end: str) -> dict[str, Any]:
        """The shortest connection between two nodes, following data flow where possible."""
        return run(project.path_between, start, end)

    @server.tool()
    def diff(base: str | None = None, head: str | None = None) -> dict[str, Any]:
        """What the current branch changes, column by column, compared with `base` (a commit, or a
        date such as 2026-10-01 for the last commit before its start; default: the merge base with
        main): columns added, removed or changed with their expressions before and after, changed
        join and filter conditions, removed columns still read downstream (breaking), changed DAG
        tasks, and the downstream impact of each change. Use before proposing a merge."""
        from konstruct.diff import DiffError

        try:
            return run(project.diff, base, head)
        except DiffError as error:
            raise ToolError(str(error)) from error

    @server.tool()
    def query(question: str) -> dict[str, Any]:
        """A free-text question: the best-matching nodes and their connections, plus impact or
        lineage when the question asks for it. Prefer the specific tools when you know the name."""
        return run(project.query, question)

    return server


def serve(root: Path) -> None:
    build_server(root).run('stdio')
