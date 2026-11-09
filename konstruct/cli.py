"""The Konstruct command line.

Exit status: 0 on success, 1 on an error (or, with ``build --strict``, on warnings), 2 on a usage
error, 3 when ``path`` finds no connection.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from konstruct import __version__
from konstruct.console import Console
from konstruct.project import OUT_DIR, Project, find_out_dir

__all__ = ['OUT_DIR', 'find_out_dir', 'main', 'parser']

NODE_KINDS = ('source', 'table', 'column', 'model', 'seed', 'snapshot', 'dag', 'task', 'test', 'exposure')


def _project(args: argparse.Namespace) -> Project:
    if getattr(args, 'graph', None):
        graph = Path(args.graph).resolve()
        return Project(graph.parent.parent, out_dir=graph.parent)
    return Project(args.project)


def _ready(
    project: Project, args: argparse.Namespace, console: Console, *, warn_stale: bool = True
) -> Project:
    """Make sure a query command has a graph: build one if there is none, and refresh or warn
    when inputs changed since the last build."""
    if not project.has_graph():
        if args.no_build:
            from konstruct.query import QueryError

            raise QueryError(f'no graph at {project.graph_path}; run `konstruct build` first')
        console.info(f'no graph yet; building {project.path}')
        project.build()
        return project
    changed = project.changed_inputs()
    if changed and (warn_stale or args.refresh):
        shown = ', '.join(changed[:3]) + (f' and {len(changed) - 3} more' if len(changed) > 3 else '')
        if args.refresh:
            console.info(f'{len(changed)} input(s) changed ({shown}); updating the graph')
            project.build(update=True)
        else:
            console.warn(
                f'the graph is older than {len(changed)} changed input(s) ({shown}); '
                'results may be out of date. Add --refresh or run `konstruct build --update`'
            )
    return project


def _emit(console: Console, result: Any, as_json: bool) -> None:
    if as_json:
        console.print(json.dumps(result.to_dict(), indent=2))
    else:
        console.print(str(result))


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


# build


def cmd_build(args: argparse.Namespace, console: Console) -> int:
    from konstruct.build import build
    from konstruct.config import load_config
    from konstruct.report import analyse

    root = Path(args.path or args.project)
    if not root.is_dir():
        console.error(f'{root} is not a directory')
        return 1
    config = load_config(root, Path(args.out) if args.out else None)
    config.override(
        run_dbt=False if args.no_dbt else None, warehouses=not args.no_warehouse, dialect=args.dialect
    )
    if not args.json:
        console.info(f'building {config.root}')
    result = build(config, update=args.update)
    graph = result.graph
    findings = analyse(graph)
    stats = graph.stats()
    outputs = [_relative(p) for p in result.outputs]
    if args.json:
        summary = {
            'root': str(config.root),
            'outputs': outputs,
            'seconds': round(result.seconds, 2),
            'inputs': graph.meta.get('sources', []),
            'nodes': {k: v for k, v in stats.items() if not k.startswith('edge:')},
            'edges': {k[5:]: v for k, v in stats.items() if k.startswith('edge:')},
            'lineage': {'covered': findings.lineage_covered, 'columns': findings.lineage_total},
            'cache': {'hits': result.cache_hits, 'misses': result.cache_misses},
            'warnings': graph.warnings,
        }
        console.print(json.dumps(summary, indent=2))
    else:
        _print_build(console, config.root, result, findings, outputs, args.update)
    return 1 if args.strict and graph.warnings else 0


def _print_build(
    console: Console, root: Path, result: Any, findings: Any, outputs: list[str], update: bool
) -> None:
    graph = result.graph
    sources: list[dict[str, str]] = graph.meta.get('sources', [])
    console.print(f'{console.style("konstruct", "bold")}  {root}')
    console.print()
    if not sources:
        console.row('inputs', 'none found: no dbt project, DAG, SQL file or warehouse', style=('yellow',))
    for source in sources:
        name = source.get('name') or source.get('path', '')
        if source.get('input') == 'dbt':
            name = f'{source["name"]} ({source["path"]})'
        text = '  '.join(part for part in (name, source.get('detail', '')) if part)
        note = source.get('note')
        if note:
            text += '  ' + console.style(f'({note})', 'dim')
        failed = source.get('status') == 'failed'
        console.row(source.get('input', ''), text, style=('red',) if failed else ())
    console.print()
    stats = graph.stats()
    counts = ', '.join(
        f'{count} {kind}{"s" if count != 1 else ""}'
        for kind, count in sorted(stats.items())
        if not kind.startswith('edge:') and kind != 'column'
    )
    console.row('graph', f'{counts or "empty"}; {stats.get("column", 0)} columns')
    if findings.lineage_total:
        share = 100 * findings.lineage_covered / findings.lineage_total
        console.row(
            'lineage',
            f'{findings.lineage_covered} of {findings.lineage_total} columns of models and built tables ({share:.0f}%)',
        )
    if update:
        total = result.cache_hits + result.cache_misses
        console.row('reused', f'{result.cache_hits} of {total} cached analyses')
    if graph.warnings:
        console.row('warnings', str(len(graph.warnings)), style=('yellow',))
        for warning in graph.warnings[:5]:
            console.print(f'    {console.style("-", "yellow")} {warning}')
        if len(graph.warnings) > 5:
            console.print(f'    and {len(graph.warnings) - 5} more in {outputs[1]}')
    else:
        console.row('warnings', 'none', style=('green',))
    folder = _relative(result.outputs[0].parent)
    names = ', '.join(path.name for path in result.outputs)
    console.row('wrote', f'{names} to {folder} in {result.seconds:.1f}s')


# status and init


def cmd_status(args: argparse.Namespace, console: Console) -> int:
    from konstruct.status import inspect

    status = inspect(Path(args.project))
    if args.json:
        console.print(json.dumps(status.to_dict(), indent=2))
        return 0
    console.print(f'{console.style("konstruct", "bold")}  {status.root}')
    console.print()
    for key, value in status.lines():
        attention = (
            key == 'graph' and ('changed' in value or 'not built' in value)
        ) or 'not installed' in value
        console.row(key, value, width=13, style=('yellow',) if attention else ())
    return 0


def cmd_init(args: argparse.Namespace, console: Console) -> int:
    from konstruct import dbt
    from konstruct.config import discover_warehouses, load_config
    from konstruct.status import inspect, render_config

    root = Path(args.project).resolve()
    target = root / 'konstruct.toml'
    if target.exists() and not args.force and not args.stdout:
        console.error(f'{_relative(target)} already exists; add --force to replace it')
        return 1
    config = load_config(root)
    text = render_config(config, inspect(root), discover_warehouses(config, dbt.find_projects(root)))
    if args.stdout:
        console.print(text.rstrip())
        return 0
    target.write_text(text, encoding='utf-8')
    console.print(f'konstruct: wrote {_relative(target)}; edit it if needed, then run `konstruct build`')
    return 0


# Questions


def cmd_impact(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    _emit(console, project.impact(args.ref, args.depth), args.json)
    return 0


def cmd_trace(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    _emit(console, project.trace(args.ref, args.depth), args.json)
    return 0


def cmd_path(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    result = project.path_between(args.start, args.end)
    _emit(console, result, args.json)
    return 0 if result.found else 3


def cmd_search(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    _emit(console, project.search(' '.join(args.text), args.limit, args.kind), args.json)
    return 0


def cmd_describe(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    _emit(console, project.describe(args.ref), args.json)
    return 0


def cmd_conflicts(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    result = project.conflicts(args.limit, write=not args.no_write)
    if args.format == 'markdown':
        console.print(result.markdown().rstrip())
    else:
        _emit(console, result, args.json)
    if not args.no_write:
        console.info(f'wrote CONFLICTS.md and conflicts.json to {project.out_dir}')
    return 1 if args.fail_on_conflict and result.conflicts else 0


def cmd_define(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    result = project.define(' '.join(args.ref))
    _emit(console, result, args.json)
    return 0 if result.terms else 3


def cmd_ontology(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    result = project.ontology()
    if args.format == 'yaml':
        console.print(result.to_yaml().rstrip())
    else:
        _emit(console, result, args.json)
    return 1 if args.strict and result.violations else 0


def cmd_explain(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    _emit(console, project.explain(args.ref), args.json)
    return 0


def _singular(kind: str) -> str:
    kind = kind.lower()
    return kind[:-1] if kind.endswith('s') and kind[:-1] in NODE_KINDS else kind


def cmd_list(args: argparse.Namespace, console: Console) -> int:
    kinds = [_singular(k) for k in args.kinds]
    unknown = [k for k in kinds if k not in NODE_KINDS]
    if unknown:
        console.error(f'unknown kind {unknown[0]!r}; choose from {", ".join(NODE_KINDS)}')
        return 2
    project = _ready(_project(args), args, console)
    _emit(console, project.list(kinds or None), args.json)
    return 0


def cmd_query(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    _emit(console, project.query(' '.join(args.question), depth=args.depth), args.json)
    return 0


def cmd_verify(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    if not args.json:
        console.info(
            'perturbing inputs and rerunning models; this reads the warehouse but never writes to it'
        )
    result = project.verify(warehouse=args.warehouse, seed=args.seed, max_rows=args.rows, models=args.model)
    _emit(console, result, args.json)
    if not args.json:
        console.print(
            f'\nWrote VERIFY.md and verify.json to {_relative(project.out_dir)}; graph.json now marks confirmed edges.'
        )
    if not result.checked:
        return 1
    return 1 if args.strict and result.totals()['unexplained'] else 0


def cmd_diff(args: argparse.Namespace, console: Console) -> int:
    from konstruct.diff import DiffError

    # Changed inputs are what diff is about: it reads the current SQL itself, so no stale warning.
    project = _ready(_project(args), args, console, warn_stale=False)
    try:
        result = project.diff(base=args.base, head=args.head)
    except DiffError as error:
        console.error(str(error))
        return 1
    if args.json_file:
        Path(args.json_file).write_text(json.dumps(result.to_dict(), indent=2) + '\n', encoding='utf-8')
    if args.format == 'json':
        console.print(json.dumps(result.to_dict(), indent=2))
    elif args.format == 'markdown':
        console.print(result.markdown().rstrip())
    elif args.format == 'selector':
        console.print(result.selection.selector)
    else:
        console.print(str(result))
    return 1 if args.fail_on_breaking and result.breaking else 0


def cmd_export(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    text = project.export(args.format, select=args.select, columns=args.columns, namespace=args.namespace)
    if args.output:
        Path(args.output).write_text(text, encoding='utf-8')
        console.info(f'wrote {args.output}')
    else:
        sys.stdout.write(text)
    return 0


def cmd_open(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    page = project.out_dir / 'graph.html'
    if not page.is_file():
        console.error(f'no viewer at {page}; run `konstruct build`')
        return 1
    console.print(page.as_uri())
    if not args.print_only:
        webbrowser.open(page.as_uri())
    return 0


# Agents


def cmd_install(args: argparse.Namespace, console: Console) -> int:
    from konstruct.skill import install

    target = Path(args.target).resolve() if args.target else None
    for line in install(agents=args.agent, project=target, mcp=args.mcp, force=args.force):
        console.print(f'konstruct: {line}')
    return 0


def cmd_mcp(args: argparse.Namespace, console: Console) -> int:
    from konstruct.mcp_server import serve

    serve(Path(args.project))
    return 0


# Parser


def _common() -> argparse.ArgumentParser:
    """Options every command accepts, before or after its name."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        '-C',
        '--project',
        '--path',
        dest='project',
        default=argparse.SUPPRESS,
        metavar='DIR',
        help='project directory (default: the current directory)',
    )
    common.add_argument(
        '--no-color',
        '--no-colour',
        dest='no_color',
        action='store_true',
        default=argparse.SUPPRESS,
        help='never colour the output (NO_COLOR is also respected)',
    )
    common.add_argument(
        '-v', '--verbose', action='store_true', default=argparse.SUPPRESS, help='log progress to stderr'
    )
    common.add_argument(
        '-q', '--quiet', action='store_true', default=argparse.SUPPRESS, help='no progress messages on stderr'
    )
    return common


def _query_options(command: argparse.ArgumentParser) -> None:
    command.add_argument('--json', action='store_true', help='print JSON instead of text')
    command.add_argument(
        '--graph', metavar='FILE', help='use this graph.json instead of finding konstruct-out'
    )
    command.add_argument(
        '--refresh', action='store_true', help='update the graph first if its inputs changed'
    )
    command.add_argument('--no-build', action='store_true', help='fail rather than build a missing graph')


EPILOG = """\
examples:
  konstruct build                          build the graph for the current directory
  konstruct status                         what Konstruct finds here; is the graph current?
  konstruct impact orders.amount           what breaks if orders.amount changes
  konstruct trace customers.lifetime_value where a value comes from
  konstruct path raw_payments customer_dashboard
  konstruct search lifetime value
  konstruct list models
  konstruct diff --base origin/main        what this branch changes and breaks, column by column
  konstruct verify                         check the lineage against the data (DuckDB)
  konstruct install                        add /konstruct to Claude Code and Cursor

Names can be a table (orders, main.orders), a column (orders.amount), a dbt unique id or a
DAG task (dag_id.task_id). Query commands build the graph first if it does not exist yet.
Run `konstruct <command> --help` for a command's options.
"""


def parser() -> argparse.ArgumentParser:
    common = _common()
    root = argparse.ArgumentParser(
        prog='konstruct',
        description='Turn a data stack (dbt, SQL, Airflow, warehouse and Iceberg metadata) into a '
        'queryable graph with column-level lineage, for people and coding agents.',
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    root.add_argument('--version', action='version', version=f'konstruct {__version__}')
    commands = root.add_subparsers(dest='command', metavar='<command>', title='commands')

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        description = help_text[:1].upper() + help_text[1:] + '.'
        return commands.add_parser(name, help=help_text, description=description, parents=[common])

    build = add('build', 'build konstruct-out/graph.json, REPORT.md, graph.html and ontology.yml')
    build.add_argument('path', nargs='?', help='project directory (default: -C or the current directory)')
    build.add_argument(
        '-u', '--update', action='store_true', help='reuse cached analyses of unchanged inputs'
    )
    build.add_argument('--out', metavar='DIR', help='output directory (default: <project>/konstruct-out)')
    build.add_argument('--no-dbt', action='store_true', help='never run dbt; use the artefacts in target/')
    build.add_argument('--no-warehouse', action='store_true', help='do not connect to any warehouse')
    build.add_argument(
        '--dialect', help='SQL dialect for sqlglot (default: from the dbt adapter or warehouse)'
    )
    build.add_argument('--strict', action='store_true', help='exit with status 1 if there are warnings')
    build.add_argument('--json', action='store_true', help='print a JSON summary instead of text')
    build.set_defaults(func=cmd_build)

    status = add('status', 'show what Konstruct finds here and whether the graph is up to date')
    status.add_argument('--json', action='store_true', help='print JSON instead of text')
    status.set_defaults(func=cmd_status)

    init = add('init', 'write a konstruct.toml that records what discovery found')
    init.add_argument('--force', action='store_true', help='replace an existing konstruct.toml')
    init.add_argument('--stdout', action='store_true', help='print the configuration instead of writing it')
    init.set_defaults(func=cmd_init)

    for name, func, text in (
        ('impact', cmd_impact, 'everything downstream of a table, model or column'),
        ('trace', cmd_trace, 'everything upstream of a table, model or column'),
    ):
        command = add(name, text)
        command.add_argument('ref', metavar='NAME', help='e.g. orders, main.orders or orders.amount')
        command.add_argument('--depth', type=int, metavar='N', help='stop after N hops')
        _query_options(command)
        command.set_defaults(func=func)

    path = add('path', 'the shortest connection between two tables or columns')
    path.add_argument('start', metavar='FROM')
    path.add_argument('end', metavar='TO')
    _query_options(path)
    path.set_defaults(func=cmd_path)

    search = add('search', 'find tables, columns, tasks and tests by name or description')
    search.add_argument('text', nargs='+', metavar='WORDS')
    search.add_argument(
        '-k', '--kind', action='append', choices=NODE_KINDS, help='only this kind (repeatable)'
    )
    search.add_argument(
        '-n', '--limit', type=int, default=20, metavar='N', help='at most N results (default 20)'
    )
    _query_options(search)
    search.set_defaults(func=cmd_search)

    describe = add('describe', 'columns, types, descriptions, tests and neighbours of one node')
    describe.add_argument('ref', metavar='NAME')
    _query_options(describe)
    describe.set_defaults(func=cmd_describe)

    conflicting = add('conflicts', 'columns that compute the same business idea in different ways')
    conflicting.add_argument('-n', '--limit', type=int, metavar='N', help='at most N conflicts')
    conflicting.add_argument('--format', choices=['text', 'markdown'], default='text')
    conflicting.add_argument(
        '--no-write', action='store_true', help='do not write CONFLICTS.md and conflicts.json'
    )
    conflicting.add_argument(
        '--fail-on-conflict', action='store_true', help='exit with status 1 if any conflict is found'
    )
    _query_options(conflicting)
    conflicting.set_defaults(func=cmd_conflicts)

    defining = add('define', 'what a business term means, who owns it and which columns hold it')
    defining.add_argument(
        'ref', nargs='+', metavar='TERM', help='a term, a synonym, or a column such as orders.amount'
    )
    _query_options(defining)
    defining.set_defaults(func=cmd_define)

    ontological = add('ontology', 'the entities, measures and relationships derived from the data stack')
    ontological.add_argument('--format', choices=['text', 'yaml'], default='text')
    ontological.add_argument(
        '--strict', action='store_true', help='exit with status 1 if any ontology rule is broken'
    )
    _query_options(ontological)
    ontological.set_defaults(func=cmd_ontology)

    explaining = add('explain', "a column's formula, composed down to its source columns")
    explaining.add_argument('ref', metavar='COLUMN', help='e.g. orders.amount')
    _query_options(explaining)
    explaining.set_defaults(func=cmd_explain)

    listing = add('list', 'list nodes by kind (default: tables, models, sources, seeds and snapshots)')
    listing.add_argument(
        'kinds', nargs='*', metavar='KIND', help=f'any of {", ".join(NODE_KINDS)} (plurals work)'
    )
    _query_options(listing)
    listing.set_defaults(func=cmd_list)

    query = add('query', 'answer a free-text question from the graph')
    query.add_argument('question', nargs='+', metavar='QUESTION')
    query.add_argument('--depth', type=int, default=1, metavar='N', help='neighbourhood depth around matches')
    _query_options(query)
    query.set_defaults(func=cmd_query)

    exporting = add(
        'export',
        'the graph as OpenLineage, Mermaid, Graphviz DOT or CSV, or the ontology as an Apache Ossie model',
    )
    exporting.add_argument('format', choices=['openlineage', 'mermaid', 'dot', 'csv', 'ossie'])
    exporting.add_argument('-o', '--output', metavar='FILE', help='write to FILE instead of stdout')
    exporting.add_argument(
        '-s',
        '--select',
        metavar='NAME',
        help='only the lineage of this table or column, both directions (not for ossie)',
    )
    exporting.add_argument(
        '--columns', action='store_true', help='mermaid and dot: draw columns and column lineage'
    )
    exporting.add_argument(
        '--namespace', metavar='NS', help='openlineage: dataset namespace (default: <dialect>://konstruct)'
    )
    _query_options(exporting)
    exporting.set_defaults(func=cmd_export)

    diff = add('diff', 'what a change does, column by column, compared with a base commit (git)')
    diff.add_argument(
        '--base',
        metavar='REF',
        help='commit or date (2026-10-01) to compare with (default: the merge base with main)',
    )
    diff.add_argument('--head', metavar='REF', help='commit or date to compare (default: the working tree)')
    diff.add_argument(
        '--format',
        choices=['text', 'markdown', 'json', 'selector'],
        default='text',
        help='markdown suits a pull request comment; selector prints only the dbt --select',
    )
    diff.add_argument('--json-file', metavar='FILE', help='also write the result as JSON to FILE (for CI)')
    diff.add_argument(
        '--fail-on-breaking', action='store_true', help='exit with status 1 on a breaking change'
    )
    diff.add_argument('--graph', metavar='FILE', help='use this graph.json instead of finding konstruct-out')
    diff.add_argument('--refresh', action='store_true', help='update the graph first if its inputs changed')
    diff.add_argument('--no-build', action='store_true', help='fail rather than build a missing graph')
    diff.set_defaults(func=cmd_diff, json=False)

    verify = add(
        'verify', 'test the column lineage against the data by perturbing inputs (DuckDB or Postgres)'
    )
    verify.add_argument('-m', '--model', action='append', metavar='NAME', help='only this model (repeatable)')
    verify.add_argument(
        '--seed', type=int, default=1729, help='random seed, for reproducible runs (default 1729)'
    )
    verify.add_argument(
        '--rows', type=int, default=10000, metavar='N', help='rows copied per input (default 10000)'
    )
    verify.add_argument(
        '--warehouse',
        metavar='FILE',
        help='DuckDB file or postgresql:// URL with the built models (default: discovered)',
    )
    verify.add_argument(
        '--strict', action='store_true', help='exit with status 1 if any influence is unexplained'
    )
    _query_options(verify)
    verify.set_defaults(func=cmd_verify)

    open_ = add('open', 'open graph.html in a browser')
    open_.add_argument('--print-only', action='store_true', help='print the file URL without opening it')
    _query_options(open_)
    open_.set_defaults(func=cmd_open)

    install = add('install', 'add the /konstruct skill to Claude Code and Cursor')
    install.add_argument(
        '--agent', choices=['claude', 'cursor', 'both'], default='both', help='which agent (default: both)'
    )
    install.add_argument(
        '--target', metavar='DIR', help='install into this repository, not your home directory'
    )
    install.add_argument('--mcp', action='store_true', help='also register the MCP server (with --target)')
    install.add_argument('--force', action='store_true', help='replace a skill that was edited by hand')
    install.set_defaults(func=cmd_install)

    mcp = add('mcp', 'run the MCP server on stdio (for agent configuration, not interactive use)')
    mcp.set_defaults(func=cmd_mcp)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    from konstruct.config import ConfigError
    from konstruct.query import QueryError
    from konstruct.skill import InstallError

    arguments = parser()
    args = arguments.parse_args(argv)
    for name, default in (('project', '.'), ('no_color', False), ('verbose', False), ('quiet', False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    console = Console(colour=False if args.no_color else None, quiet=args.quiet)
    if not getattr(args, 'func', None):
        arguments.print_help()
        return 2
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format='konstruct: %(message)s',
        stream=sys.stderr,
    )
    try:
        code: int = args.func(args, console)
    except (QueryError, ConfigError, InstallError) as error:
        console.error(str(error))
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # The reader went away (`konstruct list | head`); stop quietly, as other tools do.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0
    return code


if __name__ == '__main__':
    sys.exit(main())
