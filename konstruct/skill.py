"""`konstruct install`: the /konstruct skill for Claude Code and Cursor, and optional MCP registration.

Both agents load Agent Skills from ``SKILL.md`` files. Cursor also reads ``.claude/skills``, so
installing for both writes one skill under ``.claude/skills`` (or ``~/.claude/skills``) rather
than two copies that would show up twice in Cursor.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from konstruct import __version__


class InstallError(Exception):
    pass


SKILL_BODY = """\
---
name: konstruct
description: >-
  Answer questions about the data stack in this repository with konstruct's graph of dbt models,
  sources, warehouse tables, Airflow DAGs and column-level lineage. Use it before changing a dbt
  model, SQL file, table or column (what breaks downstream?), when asked where a column comes from,
  which tests or DAG tasks cover a model, or how two tables are connected.
argument-hint: "[question, or table.column]"
allowed-tools: Bash(konstruct *)
---

# Konstruct

Konstruct keeps a graph of this data stack in `konstruct-out/`: dbt models, sources, seeds, tests and
exposures; warehouse and Iceberg tables with their columns; Airflow DAGs and tasks; and column-level
lineage parsed from the SQL. Query it instead of reading every model by hand.

## Steps

1. Query commands build the graph if `konstruct-out/` does not exist, and warn when inputs changed
   since the last build. Add `--refresh` to bring it up to date first; it reuses earlier work and
   takes seconds. `konstruct status` shows what Konstruct found and whether the graph is current.
2. Answer the request with the narrowest command:
   - `konstruct impact <table.column>` (or `<table>`): everything downstream. Run this before
     renaming, dropping, retyping or changing the logic of a column or model.
   - `konstruct trace <table.column>`: everything upstream; where a value comes from.
   - `konstruct describe <name>`: columns, types, descriptions, tests, neighbours of one node.
   - `konstruct define <term>`: the approved business definition of a term (or the terms that
     define a column): wording, owner, status, grain, required filters and bound columns. Use it
     before answering a business question, and follow the required filters.
   - `konstruct ontology`: the business objects (entities and their keys), the measures with
     their formulas composed to source, and how the entities relate. Read it before writing a
     query that joins or aggregates, so joins follow the relationships and grains are right.
   - `konstruct conflicts`: columns that compute the same business idea in different ways, with
     the filters, joins and sources that differ. Check it before adding a new measure.
   - `konstruct explain <table.column>`: how a column is calculated, as SQL composed down to its
     source columns, with the joins and filters on the way.
   - `konstruct path <a> <b>`: how two tables or columns are connected.
   - `konstruct search <words>`: find nodes when you do not know the exact name.
   - `konstruct list models` (or tables, sources, tasks, ...): what exists.
   - `konstruct diff`: after editing models, SQL or DAGs, what the change does column by column
     compared with main, including removed columns that downstream models still read (breaking).
     Run it before you finish a change and fix anything it reports as breaking.
   - `konstruct gate`: the check to pass before you open a pull request. It fails on a removed
     column something still reads, an approved definition whose meaning changed, or a changed
     column that computes a business idea differently from an existing one, and says how to fix
     each. Fix what it reports and run it again until it passes; the pull request check runs it too.
   - `konstruct verify`: test the lineage against the data (DuckDB, Postgres, or a sample of
     Snowflake, BigQuery or Databricks); edges it confirms are marked `confirmed` in impact and trace.
   - `konstruct query "<question>"`: free-text questions; returns matching nodes and their
     connections, plus impact or lineage when the question asks for it.
   Add `--json` for structured output.
3. Report what the graph shows: start from the one-line summary, then the affected models,
   columns, tests, exposures and DAG tasks, with file paths. Then read only the files you need.

## Reading the output

- `[model, transformed]` means the column is computed from the upstream one; `direct` means it is
  passed through or renamed.
- "Relations that join, filter or group on it" lists models that use the column in a JOIN, WHERE,
  GROUP BY, HAVING or QUALIFY. Changing it changes which rows those models return, even though no
  output column is derived from it.
- Lineage is static: dynamic SQL, Python models and macros that Konstruct could not expand are
  listed under Warnings in `konstruct-out/REPORT.md`. Say so when a gap matters to the answer.

## Request

$ARGUMENTS
"""

_MARKER = re.compile(r'\n<!-- generated by konstruct [^;]+; sha256:([0-9a-f]{64}) -->\n?$')


def render_skill() -> str:
    digest = hashlib.sha256(SKILL_BODY.encode('utf-8')).hexdigest()
    return f'{SKILL_BODY}\n<!-- generated by konstruct {__version__}; sha256:{digest} -->\n'


def _edited_by_hand(text: str) -> bool:
    match = _MARKER.search(text)
    if not match:
        return True
    body = text[: match.start()]
    return hashlib.sha256(body.encode('utf-8')).hexdigest() != match.group(1)


def write_skill(directory: Path, force: bool) -> tuple[Path, str]:
    path = directory / 'konstruct' / 'SKILL.md'
    content = render_skill()
    if path.is_file():
        existing = path.read_text(encoding='utf-8')
        if existing == content:
            return path, 'unchanged'
        if _edited_by_hand(existing) and not force:
            raise InstallError(f'{path} was edited by hand; rerun with --force to replace it')
        path.write_text(content, encoding='utf-8')
        return path, 'updated'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path, 'installed'


def _merge_mcp(path: Path, server: dict[str, Any]) -> str:
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding='utf-8') or '{}')
        except ValueError as error:
            raise InstallError(f'{path} is not valid JSON: {error}') from error
        if not isinstance(data, dict):
            raise InstallError(f'{path} does not contain a JSON object')
    servers = data.setdefault('mcpServers', {})
    if servers.get('konstruct') == server:
        return 'unchanged'
    state = 'updated' if 'konstruct' in servers else 'registered'
    servers['konstruct'] = server
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    return state


def install(
    *,
    agents: str = 'both',
    project: Path | None = None,
    mcp: bool = False,
    force: bool = False,
    home: Path | None = None,
) -> list[str]:
    """Install the skill (and optionally the MCP server); return what was done, one line each."""
    base = project or home or Path.home()
    messages = []
    if agents in ('claude', 'both'):
        path, state = write_skill(base / '.claude' / 'skills', force)
        readers = 'Claude Code and Cursor' if agents == 'both' else 'Claude Code'
        messages.append(f'{state} /konstruct skill for {readers}: {path}')
    if agents == 'cursor':
        path, state = write_skill(base / '.cursor' / 'skills', force)
        messages.append(f'{state} /konstruct skill for Cursor: {path}')
    if mcp:
        if project is None:
            messages.append(
                'MCP: register it per project with `konstruct install --project . --mcp`, or for Claude '
                'Code everywhere with `claude mcp add konstruct -- konstruct mcp`'
            )
        else:
            if agents in ('claude', 'both'):
                state = _merge_mcp(
                    project / '.mcp.json', {'command': 'konstruct', 'args': ['mcp', '--path', '.']}
                )
                messages.append(f'{state} MCP server for Claude Code: {project / ".mcp.json"}')
            if agents in ('cursor', 'both'):
                server = {
                    'type': 'stdio',
                    'command': 'konstruct',
                    'args': ['mcp', '--path', '${workspaceFolder}'],
                }
                state = _merge_mcp(project / '.cursor' / 'mcp.json', server)
                messages.append(f'{state} MCP server for Cursor: {project / ".cursor" / "mcp.json"}')
    return messages
