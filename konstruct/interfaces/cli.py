"""The Konstruct command line.

Exit status: 0 on success, 1 on an error (or, with ``build --strict``, on warnings), 2 on a usage
error, 3 when ``path`` or ``relate`` finds no connection, or ``define``, ``entity``, ``metric`` or
``relate`` finds nothing by that name.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import sys
import textwrap
import webbrowser
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from konstruct import __version__
from konstruct.core.formats import FIELD, FIRST, FormatError, document
from konstruct.interfaces.console import Console, style_for
from konstruct.interfaces.layout import Style, Text, paragraph, wrap
from konstruct.interfaces.layout import command as command_lines
from konstruct.interfaces.project import OUT_DIR, Project, find_out_dir
from konstruct.interfaces.render import build_lines
from konstruct.interfaces.skill import AGENT_CHOICES
from konstruct.plugins.registry import BUILTINS

if TYPE_CHECKING:
    from konstruct.analysis.query import SearchMode
    from konstruct.analysis.release_notes import ReleaseNotes

__all__ = ['OUT_DIR', 'find_out_dir', 'main', 'parser']

BUILTIN_FORMATS = tuple(name for name, _ in BUILTINS['exports'])
NODE_KINDS = ('source', 'table', 'column', 'model', 'seed', 'snapshot', 'dag', 'task', 'test', 'exposure')


def _project(args: argparse.Namespace) -> Project:
    if getattr(args, 'graph', None):
        graph = Path(args.graph).resolve()
        return Project(graph.parent.parent, out_dir=graph.parent, plugins=not args.no_plugins)
    return Project(args.project, plugins=not args.no_plugins)


def _ready(
    project: Project, args: argparse.Namespace, console: Console, *, warn_stale: bool = True
) -> Project:
    """Make sure a query command has a graph: build one if there is none, and refresh or warn
    when inputs changed since the last build."""
    if not project.has_graph():
        if args.no_build:
            from konstruct.analysis.query import QueryError

            raise QueryError(f'no graph at {project.graph_path}; run `konstruct build` first')
        console.info(f'no graph yet; building {project.path}')
        with console.progress(f'Building {project.path}'):
            project.build()
        return project
    changed = project.changed_inputs()
    if changed and (warn_stale or args.refresh):
        shown = ', '.join(changed[:3]) + (f' and {len(changed) - 3} more' if len(changed) > 3 else '')
        if args.refresh:
            console.info(f'{len(changed)} input(s) changed ({shown}); updating the graph')
            with console.progress('Updating the graph'):
                project.build(update=True)
        else:
            console.warn(
                f'the graph is older than {len(changed)} changed input(s) ({shown})',
                'results may be out of date: add --refresh, or run `konstruct build --update`',
            )
    return project


def _emit(console: Console, result: Any, as_json: bool) -> None:
    if as_json:
        console.print(json.dumps(document(result), indent=2))
    else:
        console.print(result.text(console.ui))


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


# build


def cmd_build(args: argparse.Namespace, console: Console) -> int:
    from konstruct.engine.build import build
    from konstruct.engine.config import load_config
    from konstruct.outputs.report import analyse

    root = Path(args.path or args.project)
    if not root.is_dir():
        console.error(f'{root} is not a directory')
        return 1
    config = load_config(root, Path(args.out) if args.out else None)
    config.override(
        run_dbt=False if args.no_dbt else None,
        warehouses=not args.no_warehouse,
        dialect=args.dialect,
        plugins=not args.no_plugins,
    )
    progress = console.progress(f'Building {config.root}')
    if not args.json and not progress.enabled:
        console.info(f'building {config.root}')
    with progress:
        result = build(config, update=args.update)
    graph = result.graph
    findings = analyse(graph)
    stats = graph.stats()
    outputs = [_relative(p) for p in result.outputs]
    if args.json:
        summary = {
            FIELD: FIRST,
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
    folder = _relative(result.outputs[0].parent)
    names = ', '.join(path.name for path in result.outputs)
    lines = build_lines(
        console.ui,
        str(root),
        graph.meta.get('sources', []),
        graph.stats(),
        findings.lineage_covered,
        findings.lineage_total,
        (result.cache_hits, result.cache_hits + result.cache_misses) if update else None,
        graph.warnings,
        f'{names} to {folder} in {result.seconds:.1f}s',
        outputs[1],
    )
    console.print(console.ui.render(lines))


# trust


def cmd_trust(args: argparse.Namespace, console: Console) -> int:
    result = Project(args.path or args.project).trust(revoke=args.revoke)
    _emit(console, result, args.json)
    return 0


# status and init


def cmd_status(args: argparse.Namespace, console: Console) -> int:
    from konstruct.interfaces.status import inspect

    status = inspect(Path(args.project), plugins=not args.no_plugins)
    _emit(console, status, args.json)
    return 0


def cmd_init(args: argparse.Namespace, console: Console) -> int:
    from konstruct.engine.config import discover_warehouses, load_config
    from konstruct.inputs import dbt
    from konstruct.interfaces.status import inspect, render_config

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
    mode: SearchMode = 'semantic' if args.meaning else 'keyword'
    _emit(console, project.search(' '.join(args.text), args.limit, args.kind, mode), args.json)
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


def cmd_check_sql(args: argparse.Namespace, console: Console) -> int:
    as_json = args.json or args.format == 'json'
    for option, value in (('--days', args.days), ('--limit', args.limit)):
        if value is not None and value < 1:
            console.error(f'{option} must be at least 1')
            return 2
    if args.history:
        if args.file is not None:
            console.error('give a file of SQL or --history, not both')
            return 2
        if args.format == 'markdown':
            console.error('--format markdown is for a check of SQL; --history prints text or json')
            return 2
        project = _ready(_project(args), args, console)
        with console.progress('Checking the query history'):
            history = project.check_history(days=args.days, limit=args.limit, pseudonymise=args.pseudonymise)
        _emit(console, history, as_json)
        return 1 if args.strict and history.approved_shadow else 0
    if args.file is None:
        console.error('give a file of SQL, - for standard input, or --history')
        return 2
    if args.file == '-':
        sql = sys.stdin.read()
    else:
        try:
            sql = Path(args.file).read_text(encoding='utf-8')
        except OSError as error:
            console.error(f'cannot read {args.file}: {error.strerror or error}')
            return 1
    project = _ready(_project(args), args, console)
    result = project.check_sql(sql)
    if args.format == 'markdown':
        console.print(result.markdown().rstrip())
    else:
        _emit(console, result, as_json)
    return 1 if result.failed(strict=args.strict) else 0


def cmd_ontology(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    result = project.ontology()
    if args.format == 'yaml':
        console.print(result.to_yaml().rstrip())
    else:
        _emit(console, result, args.json)
    return 1 if args.strict and result.violations else 0


def _found(console: Console, question: Any) -> Any:
    """``question()``'s answer, or None after saying what it did not find: like ``define``, a
    name that matches nothing exits with status 3."""
    from konstruct.analysis.query import QueryError

    try:
        return question()
    except QueryError as error:
        console.error(str(error), hint=error.hint, suggestions=error.suggestions)
        return None


def cmd_entity(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    result = _found(console, lambda: project.entity(' '.join(args.name)))
    if result is None:
        return 3
    _emit(console, result, args.json)
    return 0


def cmd_metric(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    name = ' '.join(args.name) if args.name else None
    result = _found(console, lambda: project.metric(name, limit=args.limit))
    if result is None:
        return 3
    _emit(console, result, args.json)
    return 0


def cmd_relate(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    result = _found(console, lambda: project.relate(args.start, args.end))
    if result is None:
        return 3
    _emit(console, result, args.json)
    return 0 if result.found else 3


def cmd_sensitivity(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    result = project.sensitivity(args.ref)
    _emit(console, result, args.json)
    return 1 if args.strict and (result.unheld or result.unheld_exposures or result.unmasked) else 0


def cmd_erasure(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    _emit(console, project.erasure(args.ref, levels=args.levels), args.json)
    return 0


def cmd_history(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    _emit(console, project.history(args.ref, limit=args.limit), args.json)
    return 0


def cmd_explain(args: argparse.Namespace, console: Console) -> int:
    project = _ready(_project(args), args, console)
    result = project.explain(args.ref)
    if not args.english:
        _emit(console, result, args.json)
        return 0
    english = project.english(args.ref)
    if args.json:
        console.print(json.dumps({**document(result), 'english': english.to_dict()}, indent=2))
    else:
        from konstruct.interfaces.render import english_section, show

        console.print(result.text(console.ui) + '\n\n' + show(english_section, english, console.ui))
    return 0


def cmd_release_notes(args: argparse.Namespace, console: Console) -> int:
    # The notes read each version from git, so changed inputs since the build do not matter.
    project = _ready(_project(args), args, console, warn_stale=False)
    with console.progress(f'Reading the changes since {args.since or "the latest tag"}'):
        result = project.release_notes(args.since, until=args.until, limit=args.limit)
    _notes(console, result, args)
    return 0


def _notes(console: Console, result: ReleaseNotes, args: argparse.Namespace) -> None:
    if args.format == 'markdown':
        console.print(result.markdown().rstrip())
    else:
        _emit(console, result, args.json or args.format == 'json')


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
    # --mutate with --why runs the mutation tests, as it always has; --effect is new, so it refuses both.
    others = [f'--{m}' for m in ('mutate', 'why') if getattr(args, m)]
    if args.effect and others:
        console.error(f'--effect and {" and ".join(others)} are separate modes of verify; give one')
        return 2
    if args.format != 'text' and not args.effect:
        console.error('--format picks how --effect prints the release notes; add --effect, or use --json')
        return 2
    if args.laws and (args.mutate or args.why or args.effect):
        other = '--mutate' if args.mutate else '--why' if args.why else '--effect'
        console.error(f'--laws and {other} are separate modes of verify; give one at a time')
        return 2
    if not args.laws and (args.propose or args.tolerance is not None):
        console.error(f'{"--propose" if args.propose else "--tolerance"} is for the data laws; add --laws')
        return 2
    if args.laws and args.model:
        console.error('--model does not apply to --laws: a law follows its total through every model')
        return 2
    project = _ready(_project(args), args, console)
    if args.mutate:
        return _mutate(project, args, console)
    if args.why:
        return _why(project, args, console)
    if args.effect:
        return _effect(project, args, console)
    if args.since or args.until:
        console.error('--since and --until pick the commits for --effect; add --effect')
        return 2
    if args.laws:
        return _laws(project, args, console)
    if args.where:
        console.error('--where picks the row for --why; give the column with --why COLUMN')
        return 2
    if not args.json:
        console.info(
            'perturbing inputs and rerunning models; this reads the warehouse but never writes to it'
        )
    with console.progress('Verifying the lineage against the data'):
        result = project.verify(
            warehouse=args.warehouse,
            seed=args.seed,
            max_rows=args.rows or 10000,
            models=args.model,
            ontology=not args.no_ontology,
        )
    _emit(console, result, args.json)
    if not args.json:
        wrote = Text(
            (f'Wrote VERIFY.md and verify.json to {_relative(project.out_dir)}; ', 'dim'),
            ('graph.json now marks confirmed edges.', 'dim'),
        )
        console.print('\n' + console.ui.line(wrote))
    if not result.checked:
        return 1
    return 1 if args.strict and result.totals()['unexplained'] else 0


def _mutate(project: Project, args: argparse.Namespace, console: Console) -> int:
    """`verify --mutate`: mutation test the dbt tests of each model."""
    operators = [o.strip() for o in args.operators.split(',') if o.strip()] if args.operators else None
    if not args.json:
        console.info(
            'running mutants of each model and its dbt tests; this reads the warehouse but never writes to it'
        )
    with console.progress('Mutation testing the dbt tests'):
        result = project.mutate(
            warehouse=args.warehouse,
            seed=args.seed,
            max_rows=_rows(args, 1000),
            max_mutants=args.max_mutants,
            operators=operators,
            models=args.model,
            suggest=not args.no_suggest,
        )
    _emit(console, result, args.json)
    if args.markdown:
        Path(args.markdown).write_text(result.markdown(), encoding='utf-8')
    if not args.json:
        wrote = f'Wrote MUTATION.md and mutation.json to {_relative(project.out_dir)}'
        extra = f', and the report to {args.markdown}' if args.markdown else ''
        console.print('\n' + console.ui.line(Text((f'{wrote}{extra}.', 'dim'))))
    return 0 if result.checked else 1


def _why(project: Project, args: argparse.Namespace, console: Console) -> int:
    """`verify --why`: the source rows behind one value."""
    from konstruct.analysis.why import parse_where

    where = parse_where(args.where or [])
    with console.progress(f'Tracing the rows behind {args.why}'):
        result = project.why(
            args.why, where, warehouse=args.warehouse, limit=args.limit, max_rows=_rows(args, 1_000_000)
        )
    _emit(console, result, args.json)
    return 0


def _effect(project: Project, args: argparse.Namespace, console: Console) -> int:
    """`verify --effect`: the release notes, with each change run before and after on today's data."""
    if not args.json:
        console.info(
            'running each changed model as it was before and after; this reads the warehouse but never writes to it'
        )
    with console.progress(f"Running the changes since {args.since or 'the latest tag'} on today's data"):
        result = project.effect(
            args.since,
            until=args.until,
            warehouse=args.warehouse,
            models=args.model,
            max_rows=_rows(args, 1_000_000),
        )
    _notes(console, result, args)
    return 0


def _laws(project: Project, args: argparse.Namespace, console: Console) -> int:
    """`verify --laws`: propose the data laws from the SQL and check them against the data."""
    confirm = not args.propose
    if confirm and not args.json:
        console.info(
            'comparing totals and probing additivity; this reads the warehouse but never writes to it'
        )
    with console.progress('Checking the data laws' if confirm else 'Proposing the data laws'):
        result = project.laws(
            confirm=confirm, warehouse=args.warehouse, tolerance=args.tolerance, max_rows=_rows(args, 10000)
        )
    _emit(console, result, args.json)
    if confirm and not args.json:
        console.print(
            '\n' + console.ui.line(Text((f'Wrote laws.json to {_relative(project.out_dir)}.', 'dim')))
        )
    return 1 if args.strict and result.totals()['broken'] else 0


def cmd_lock(args: argparse.Namespace, console: Console) -> int:
    if args.check and (args.reads or args.update is not None or args.no_write):
        console.error(
            '--check compares the lockfile with the build; it takes no names, --update or --no-write'
        )
        return 2
    if not args.check and (args.format == 'markdown' or args.no_history):
        console.error(
            f'{"--format markdown" if args.format == "markdown" else "--no-history"} is for --check'
        )
        return 2
    project = _ready(_project(args), args, console)
    if args.check:
        with console.progress('Checking the pinned upstream columns'):
            result = project.check_lock(history=not args.no_history)
        if args.format == 'markdown':
            console.print(result.markdown().rstrip())
        else:
            _emit(console, result, args.json or args.format == 'json')
        return 0 if result.passed else 1
    update: bool | list[str] = (args.update or True) if args.update is not None else False
    with console.progress('Pinning the upstream columns'):
        written = project.lock(args.reads or None, update=update, write=not args.no_write)
    _emit(console, written, args.json or args.format == 'json')
    return 0


def cmd_gate(args: argparse.Namespace, console: Console) -> int:
    from konstruct.analysis.diff import DiffError

    project = _project(args)
    options: dict[str, Any] = {'run_dbt': False if args.no_dbt else None, 'warehouses': not args.no_warehouse}
    update = not args.no_update
    try:
        with console.progress(f'Checking the change against {args.base or "the merge base with main"}'):
            if not project.has_graph():
                # One build, with the gate's options, rather than a default build and then an update.
                project.build(**options)
                update = False
            result = project.gate(base=args.base, update=update, **options)
    except DiffError as error:
        console.error(str(error))
        return 1
    if args.json_file:
        Path(args.json_file).write_text(json.dumps(document(result), indent=2) + '\n', encoding='utf-8')
    if args.format == 'json' or args.json:
        console.print(json.dumps(document(result), indent=2))
    elif args.format == 'markdown':
        console.print(result.markdown().rstrip())
    else:
        console.print(result.text(console.ui))
    return 0 if result.passed else 1


def cmd_diff(args: argparse.Namespace, console: Console) -> int:
    from konstruct.analysis.diff import DiffError

    # Changed inputs are what diff is about: it reads the current SQL itself, so no stale warning.
    project = _ready(_project(args), args, console, warn_stale=False)
    try:
        with console.progress(f'Comparing with {args.base or "the merge base with main"}'):
            result = project.diff(base=args.base, head=args.head)
    except DiffError as error:
        console.error(str(error))
        return 1
    if args.json_file:
        Path(args.json_file).write_text(json.dumps(document(result), indent=2) + '\n', encoding='utf-8')
    if args.format == 'json' or args.json:
        console.print(json.dumps(document(result), indent=2))
    elif args.format == 'markdown':
        console.print(result.markdown().rstrip())
    elif args.format == 'selector':
        console.print(result.selection.selector)
    else:
        console.print(result.text(console.ui))
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


def cmd_receipts(args: argparse.Namespace, console: Console) -> int:
    if args.action == 'show':
        # A receipt is read as it was recorded: no graph is needed, and none is built.
        _emit(console, _project(args).receipts([args.id])[0], args.json)
        return 0
    # check refreshes a graph older than its inputs itself, and says so if told not to.
    project = _ready(_project(args), args, console, warn_stale=args.action == 'issue')
    if args.action == 'issue':
        receipt = project.receipt(args.ref, question=args.question, answer=args.answer)
        _emit(console, receipt, args.json)
        if not args.json:
            where = _relative(project.out_dir / 'receipts')
            console.print('\n' + console.ui.line(Text((f'Recorded in {where}.', 'dim'))))
        return 0
    result = project.check_receipts(args.id, history=not args.no_history, refresh=not args.no_refresh)
    _emit(console, result, args.json)
    return 0 if result.passed else 1


class _StoreGiven(argparse._StoreAction):
    """An option whose default depends on the mode it is used in: setting it records its name in
    ``given``, so a mode with another default (see :func:`_rows`) knows it was not given."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        super().__call__(parser, namespace, values, option_string)
        namespace.given = {*getattr(namespace, 'given', ()), self.dest}


def _rows(args: argparse.Namespace, default: int) -> int:
    """``verify --rows`` as given, or ``default``, the mode's own."""
    return int(args.rows) if 'rows' in getattr(args, 'given', ()) else default


class _ExtendDefault(argparse._ExtendAction):
    """An ``extend`` option whose values replace its default, rather than add to it, the first
    time it is given."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, self.dest, None) is self.default:
            setattr(namespace, self.dest, None)
        super().__call__(parser, namespace, values, option_string)


def cmd_install(args: argparse.Namespace, console: Console) -> int:
    from konstruct.interfaces.skill import DEFAULT_AGENTS, install

    target = Path(args.target).resolve() if args.target else None
    agents = args.agent or DEFAULT_AGENTS
    for line in install(agents=agents, project=target, mcp=args.mcp, force=args.force):
        console.print(f'konstruct: {line}')
    return 0


def cmd_hook(args: argparse.Namespace, console: Console) -> int:
    from konstruct.interfaces import hook

    return hook.main(args.event, remind=args.remind)


def cmd_serve(args: argparse.Namespace, console: Console) -> int:
    from konstruct.interfaces.serve import run

    return run(args, console)


def cmd_mcp(args: argparse.Namespace, console: Console) -> int:
    from konstruct.interfaces.mcp_server import serve

    serve(Path(args.project), plugins=not args.no_plugins, prompts=not args.no_prompts)
    return 0


def cmd_plugins(args: argparse.Namespace, console: Console) -> int:
    _emit(console, _project(args).plugins().load_all(), args.json)
    return 0


class _ExportFormats:
    """The export formats, Konstruct's own and those of installed plugins. Installed plugins are
    looked up only for a name that is not Konstruct's, so other commands, and exports in
    Konstruct's own formats, do not read the installed distributions here."""

    def _names(self) -> list[str]:
        from konstruct.plugins.registry import Plugins

        return Plugins.discover().names('exports')

    def __contains__(self, name: object) -> bool:
        return name in BUILTIN_FORMATS or name in self._names()

    def __iter__(self) -> Iterator[str]:
        return iter(self._names())


# Parser

GROUPS = (
    ('Build', ('build', 'status', 'trust', 'init', 'open', 'serve')),
    ('Lineage', ('impact', 'trace', 'path', 'explain', 'history')),
    ('Explore', ('search', 'describe', 'list', 'query', 'export')),
    ('Business meaning', ('define', 'ontology', 'entity', 'metric', 'relate', 'conflicts', 'check-sql')),
    ('Sensitive data', ('sensitivity', 'erasure')),
    ('Changes', ('diff', 'gate', 'release-notes', 'verify', 'lock')),
    ('Agents', ('install', 'mcp', 'receipts', 'hook')),
    ('Plugins', ('plugins',)),
)
"""The commands in ``konstruct --help``, grouped by what they are for."""

EXAMPLES: dict[str, tuple[tuple[str, str], ...]] = {
    '': (
        ('konstruct build', 'build the graph for the current directory'),
        ('konstruct status', 'what Konstruct finds here; is the graph current?'),
        ('konstruct impact orders.amount', 'what breaks if orders.amount changes'),
        ('konstruct trace customers.lifetime_value', 'where a value comes from'),
        ('konstruct explain customers.lifetime_value', 'its formula, down to source columns'),
        ('konstruct diff --base origin/main', 'what this branch changes and breaks, column by column'),
        ('konstruct gate', 'the check to pass before opening a pull request'),
        ('konstruct install', 'add the Konstruct skills to Claude Code and Cursor'),
    ),
    'build': (
        ('konstruct build', 'build the graph for the current directory'),
        ('konstruct build -u', 'reuse the analyses of unchanged inputs'),
        ('konstruct build --no-dbt --strict', 'never run dbt, and fail on any warning'),
    ),
    'status': (
        ('konstruct status', 'what Konstruct finds here, and whether the graph is current'),
        ('konstruct status --json', 'the same as JSON'),
    ),
    'trust': (
        ('konstruct trust', 'after reviewing the project: use its connections, run its dbt'),
        ('konstruct trust --revoke', 'stop trusting it'),
        ('konstruct build --trust-config', 'trust it for one command, in CI on a reviewed branch'),
    ),
    'init': (
        ('konstruct init', 'write konstruct.toml from what discovery finds'),
        ('konstruct init --stdout', 'print it instead'),
    ),
    'impact': (
        ('konstruct impact orders.amount', 'what breaks if orders.amount changes'),
        ('konstruct impact stg_orders --depth 2', 'two hops downstream of a model'),
        ('konstruct impact orders.amount --json', 'the same as JSON, for scripts'),
    ),
    'trace': (
        ('konstruct trace customers.lifetime_value', 'the source columns a value comes from'),
        ('konstruct trace orders --depth 1', 'the direct inputs of a model'),
    ),
    'path': (('konstruct path raw_payments customer_dashboard', 'how a seed reaches a dashboard'),),
    'search': (
        ('konstruct search lifetime value', 'names and descriptions that match'),
        ('konstruct search -k column -n 5 amount', 'the five best matching columns'),
        ('konstruct search --meaning revenue', 'columns that mean revenue, whatever their names'),
    ),
    'describe': (
        ('konstruct describe orders', 'columns, tests and neighbours of a model'),
        ('konstruct describe orders.amount', 'one column, with its formula'),
        ('konstruct describe my_dag.load_orders', 'a DAG task'),
    ),
    'conflicts': (
        ('konstruct conflicts', 'columns that compute one idea in different ways'),
        ('konstruct conflicts --format markdown --no-write', 'as markdown, writing nothing'),
        ('konstruct conflicts --fail-on-conflict', 'exit 1 on a conflict, for CI'),
    ),
    'check-sql': (
        ('konstruct check-sql query.sql', 'check a query against the definitions before it runs'),
        ('konstruct check-sql - --strict < query.sql', 'from standard input; exit 1 on a warning too'),
        ('konstruct check-sql --history --days 30', 'shadow metrics in the warehouse query history'),
    ),
    'define': (
        ('konstruct define lifetime value', 'a term, by name or synonym'),
        ('konstruct define orders.amount', 'the terms bound to a column'),
    ),
    'ontology': (
        ('konstruct ontology', 'entities, measures and relationships'),
        ('konstruct ontology --format yaml', 'the ontology.yml the build writes'),
        ('konstruct ontology --strict', 'exit 1 on a rule violation'),
    ),
    'entity': (
        ('konstruct entity customer', 'its key, properties, relationships and metrics'),
        ('konstruct entity customer --json', 'the same as JSON, for an agent'),
    ),
    'metric': (
        ('konstruct metric', 'every metric, defined and proposed'),
        ('konstruct metric lifetime value', 'one metric: definition, formula, grain and trust'),
        ('konstruct metric orders.amount', 'the metric a column holds'),
    ),
    'relate': (
        ('konstruct relate customer payment', 'the relationships between them and the joins'),
        ('konstruct relate order customer --json', 'the same as JSON'),
    ),
    'sensitivity': (
        ('konstruct sensitivity', 'sensitive columns, where the lineage takes them and what stops it'),
        ('konstruct sensitivity customers.email', 'why one column is sensitive, or what stopped it'),
        (
            'konstruct sensitivity --strict',
            'exit 1 when sensitive data reaches a model not marked to hold it',
        ),
    ),
    'erasure': (
        (
            'konstruct erasure customer',
            "every relation holding one customer's rows, and what to do with each",
        ),
        ('konstruct erasure customers.customer_id --json', 'the same from a key column, as JSON'),
    ),
    'explain': (
        ('konstruct explain customers.lifetime_value', 'its formula, down to source columns'),
        ('konstruct explain orders.credit_card_amount --english', 'the same formula in plain English'),
    ),
    'history': (
        ('konstruct history customers.lifetime_value', 'formula changes and approvals'),
        ('konstruct history orders.amount -n 200', 'read further back'),
    ),
    'list': (
        ('konstruct list', 'tables, models, sources, seeds and snapshots'),
        ('konstruct list tasks dags', 'the orchestration'),
    ),
    'query': (
        ('konstruct query what breaks if I change payment_method', 'impact, from a question'),
        ('konstruct query where does lifetime value come from', 'lineage, from a question'),
    ),
    'export': (
        ('konstruct export mermaid -s orders --columns', 'a Mermaid diagram of one model'),
        ('konstruct export openlineage -o lineage.json', 'OpenLineage events for a catalogue'),
        ('konstruct export ossie -o model.yml', 'the ontology as an Apache Ossie model'),
        ('konstruct export semantic-view -o semantic_view.sql', 'the ontology as a Snowflake semantic view'),
    ),
    'diff': (
        ('konstruct diff', 'this branch against its merge base with main'),
        ('konstruct diff --base origin/main --format markdown', 'for a pull request comment'),
        ('dbt build --select $(konstruct diff --format selector)', 'build only what the change needs'),
    ),
    'gate': (
        ('konstruct gate', 'the check to pass before opening a pull request'),
        ('konstruct gate --base origin/main --json-file gate.json', 'in CI, keeping the result'),
    ),
    'release-notes': (
        ('konstruct release-notes', 'what changed in each formula since the latest tag, in plain English'),
        ('konstruct release-notes --since 2026-10-01 --format markdown', 'to post to a wiki or a channel'),
    ),
    'verify': (
        ('konstruct verify', 'check every model in the discovered warehouse'),
        ('konstruct verify -m orders --strict', 'one model; exit 1 on an unexplained influence'),
        ('konstruct verify --warehouse snowflake --rows 2000', 'a sample of the configured Snowflake'),
        ('konstruct verify --mutate -m orders', "would orders' dbt tests catch a wrong change to it?"),
        (
            'konstruct verify --why customer_ltv.lifetime_value --where customer_id=3',
            'the source rows behind one value, each with its share',
        ),
        (
            'konstruct verify --effect --since v1.4',
            "what each formula change since v1.4 does on today's data",
        ),
        ('konstruct verify --laws', 'the totals the lineage says must agree, checked per day'),
        ('konstruct verify --laws --propose', 'the laws alone, reading no warehouse'),
    ),
    'lock': (
        ('konstruct lock', 'pin what each upstream column this project reads means'),
        ('konstruct lock --check', 'exit 1 when a pinned column means something else, and say what'),
        ('konstruct lock --update main.ltv.total', 'accept a reviewed change to one pinned column'),
        ('konstruct lock --check --format markdown', 'the check for a pull request comment'),
    ),
    'open': (
        ('konstruct open', 'graph.html in the default browser'),
        ('konstruct open --print-only', 'print its file URL'),
    ),
    'install': (
        ('konstruct install', 'the skills for Claude Code and Cursor, in your home directory'),
        ('konstruct install --target . --mcp', 'into this repository, with the MCP server'),
        ('konstruct install --target . --agent all', 'for every agent: Codex, Copilot, Gemini CLI too'),
    ),
    'serve': (
        ('konstruct serve', 'build, then serve the viewer and the HTTP API on 127.0.0.1:8000'),
        ('konstruct serve --watch --open', 'rebuild as inputs change, and open the browser'),
        ('konstruct serve --no-build --port 0', 'the graph as it is, on any free port'),
    ),
    'mcp': (('konstruct mcp', 'run by an agent from its configuration, not by hand'),),
    'receipts': (
        (
            'konstruct receipts issue customer_ltv.lifetime_value --question "LTV of customer 3?"',
            'record what an answer rests on',
        ),
        ('konstruct receipts show r-7f3a', 'one receipt, by its id or the start of it'),
        ('konstruct receipts check', 'which answers rest on something that has changed since'),
    ),
    'receipts issue': (
        ('konstruct receipts issue customers.customer_lifetime_value', 'what an answer about it rests on'),
        ('konstruct receipts issue "Customer lifetime value" --json', 'a term and its columns, as JSON'),
    ),
    'receipts show': (('konstruct receipts show r-7f3a', 'one receipt, by its id or the start of it'),),
    'receipts check': (
        ('konstruct receipts check', 'every receipt; exit 1 when one does not hold'),
        ('konstruct receipts check r-7f3a --json', 'one receipt, as JSON'),
    ),
    'plugins': (
        ('konstruct plugins', 'installed plugins, and whether each runs for this project'),
        ('konstruct plugins --json', 'the same as JSON'),
    ),
    'hook': (('konstruct hook post-edit', "run by a Claude Code hook, with the hook's event on stdin"),),
}

NAMES = """\
Names can be a table (orders, main.orders), a column (orders.amount), a dbt unique id or a
DAG task (dag_id.task_id). Query commands build the graph first if it does not exist yet."""

DOCS = 'https://kodexlabsio.github.io/konstruct/'

_help_style = Style()
"""How help and usage errors are drawn: set from stdout when the command line is read."""


def _examples(name: str) -> str:
    """A command's examples, aligned, for its help and the documentation. The heading ends in
    ``::`` so that the reference renders them as a literal block; help shows ``Examples:``."""
    rows = EXAMPLES.get(name, ())
    width = max((len(command) for command, _ in rows), default=0)
    lines = [f'  {command.ljust(width)}  {comment}' for command, comment in rows]
    return 'Examples::\n\n' + '\n'.join(lines)


class _Help(argparse.RawDescriptionHelpFormatter):
    """argparse's layout with capitalised, bold section headings and a wider option column."""

    def __init__(self, prog: str) -> None:
        # argparse can run one column past the width it is given.
        width = min(_help_style.width or 100, 100) - 1
        super().__init__(prog, max_help_position=min(32, width // 3), width=width)

    def start_section(self, heading: str | None) -> None:
        shown = heading[:1].upper() + heading[1:] if heading else heading
        super().start_section(_help_style.paint(shown, 'title') if shown else shown)

    def add_usage(self, usage: Any, actions: Any, groups: Any, prefix: str | None = None) -> None:
        # The prefix stays plain here: argparse measures it to indent the usage lines, and escape
        # codes would count as columns. format_help colours it afterwards.
        super().add_usage(usage, actions, groups, 'Usage: ' if prefix is None else prefix)

    def format_help(self) -> str:
        text = super().format_help()
        if text.startswith('Usage: '):
            text = _help_style.paint('Usage:', 'title') + text[len('Usage:') :]
        return text

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        # Example blocks: the heading bold, the comments dim.
        out = []
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line == 'Examples::':
                out.append(indent + _help_style.paint('Examples:', 'title'))
            elif not line and index and lines[index - 1] == 'Examples::':
                continue
            elif line.startswith('  konstruct') or line.startswith('  dbt '):
                command, _, comment = line[2:].partition('  ')
                shown = f'{indent}  {_help_style.paint(command, "accent")}'
                if len(indent) + len(line) <= width:
                    out.append(f'{shown}  {_help_style.paint(comment, "dim")}')
                else:
                    # Too narrow for both: the comment goes under its command, and a command too
                    # long for the line breaks between words, as a shell continues it.
                    style = Style(width=width)
                    out += [
                        indent + _help_style.paint(part.plain, 'accent')
                        for part in command_lines(style, command)
                    ]
                    out += [
                        _help_style.paint(part, 'dim')
                        for part in textwrap.wrap(
                            comment.strip(),
                            width,
                            initial_indent=indent + '      ',
                            subsequent_indent=indent + '      ',
                        )
                    ]
            else:
                out += textwrap.wrap(line, width, initial_indent=indent, subsequent_indent=indent) or ['']
        return '\n'.join(out) + '\n'


class _Parser(argparse.ArgumentParser):
    """A parser whose usage errors say what is wrong, suggest a near name, and point to help."""

    def error(self, message: str) -> NoReturn:
        import difflib

        hint = None
        choice = re.search(r"invalid choice: '([^']*)' \(choose from (.*)\)", message)
        if choice:
            given = choice.group(1)
            # Python 3.12.8 and later list the choices without quotes.
            options = [option.strip().strip("'") for option in choice.group(2).split(',')]
            close = difflib.get_close_matches(given, options, n=3, cutoff=0.5)
            what = 'command' if self.prog == 'konstruct' else 'choice'
            message = f'unknown {what} {given!r}'
            hint = f'did you mean {" or ".join(close)}?' if close else f'choose from {", ".join(options)}'
        prog = self.prog
        if message.startswith('unrecognized arguments') and prog == 'konstruct':
            # argparse reports an unknown option from the top-level parser; the help that lists
            # the options is the command's own.
            named = next((a for a in _given if a in _COMMANDS), None)
            prog = f'konstruct {named}' if named else prog
        ui = style_for(sys.stderr, False if _no_colour() else None)
        lines = [f'{ui.paint("konstruct: error:", "error+strong")} {message}']
        if hint:
            lines.append(f'  {hint}')
        lines.append(f'  run {ui.paint(f"{prog} --help", "accent")} for its options and examples')
        self.exit(2, '\n'.join(lines) + '\n')


class _Root(_Parser):
    """The top-level parser, whose help lists the commands by group."""

    def format_help(self) -> str:
        ui = _help_style
        choices: dict[str, str] = {}
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                choices = {a.dest: a.help or '' for a in action._choices_actions}
        lines = [
            Text(('Konstruct', 'title'), ' ', (__version__, 'dim')),
            *paragraph(
                ui,
                'The ontology and column-level lineage of a data stack, checked against the data, for people '
                'and coding agents.',
                indent=0,
            ),
            Text(),
            Text(('Usage:', 'title'), ' konstruct ', ('<command>', 'accent'), ' [options]'),
        ]
        width = max(len(n) for n in choices)
        for title, names in GROUPS:
            lines += [Text(), Text((title, 'title'))]
            for name in names:
                if name in choices:
                    lead = Text('  ', (name.ljust(width), 'accent'), '  ')
                    lines += wrap(ui, Text(choices[name]), first=lead, hanging=width + 4)
        options = [
            ('-C, --project DIR', 'project directory (default: the current directory)'),
            ('--no-color', 'never colour the output (NO_COLOR is also respected)'),
            ('-q, --quiet', 'no progress messages on stderr'),
            ('-v, --verbose', 'log progress to stderr'),
            ('-h, --help', "show this help, or a command's with konstruct <command> --help"),
            ('--version', 'print the version'),
        ]
        lines += [Text(), Text(('Options', 'title'))]
        flags = max(len(flag) for flag, _ in options)
        for flag, about in options:
            lines += wrap(
                ui, Text(about), first=Text('  ', (flag.ljust(flags), 'accent'), '  '), hanging=flags + 4
            )
        lines += [Text(), Text(('Examples', 'title'))]
        commands = max(len(c) for c, _ in EXAMPLES[''])
        for example, about in EXAMPLES['']:
            if ui.width is None or 4 + commands + len(about) <= ui.width:
                lines.append(Text('  ', (example.ljust(commands), 'accent'), '  ', (about, 'dim')))
            else:
                lines.append(Text('  ', (example, 'accent')))
                lines += paragraph(ui, Text((about, 'dim')), indent=6)
        lines += [Text(), *paragraph(ui, ' '.join(NAMES.split()), indent=0)]
        lines += [Text(), Text('Documentation: ', (DOCS, 'accent'), whole=True)]
        return ui.render(lines) + '\n'


_given: list[str] = []
"""The command line being read, for usage errors, which argparse raises before it is parsed."""

_COMMANDS = frozenset(name for _, names in GROUPS for name in names)


def _no_colour() -> bool:
    return any(a in ('--no-color', '--no-colour') for a in _given)


def _common() -> _Parser:
    """Options every command accepts, before or after its name."""
    common = _Parser(add_help=False)
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
        '--trust-config',
        action='store_true',
        default=argparse.SUPPRESS,
        help='trust the project for this command, as KONSTRUCT_TRUST_CONFIG=1 does',
    )
    common.add_argument(
        '-v', '--verbose', action='store_true', default=argparse.SUPPRESS, help='log progress to stderr'
    )
    common.add_argument(
        '-q', '--quiet', action='store_true', default=argparse.SUPPRESS, help='no progress messages on stderr'
    )
    common.add_argument(
        '--no-plugins',
        action='store_true',
        default=argparse.SUPPRESS,
        help='run no plugins, whatever konstruct.toml enables (KONSTRUCT_NO_PLUGINS=1 also does)',
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


EPILOG = f"""\
{_examples('')}

{NAMES}
Run ``konstruct <command> --help`` for a command's options and examples.
"""


def parser() -> argparse.ArgumentParser:
    common = _common()
    root = _Root(
        prog='konstruct',
        description='Derive an ontology and column-level lineage from a data stack (dbt, SQL, Airflow, '
        'Dagster, Looker, Power BI and the warehouse), check them against the data, and check each change, '
        'for people and coding agents.',
        epilog=EPILOG,
        formatter_class=_Help,
        parents=[common],
    )
    root.add_argument('--version', action='version', version=f'konstruct {__version__}')
    commands = root.add_subparsers(
        dest='command', metavar='<command>', title='commands', parser_class=_Parser
    )

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        description = help_text[:1].upper() + help_text[1:] + '.'
        return commands.add_parser(
            name,
            help=help_text,
            description=description,
            epilog=_examples(name) if name in EXAMPLES else None,
            formatter_class=_Help,
            parents=[common],
        )

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

    trust = add('trust', "after review, use the project's own connections and run its dbt")
    trust.add_argument('path', nargs='?', help='project directory (default: -C or the current directory)')
    trust.add_argument('--revoke', action='store_true', help='stop trusting it')
    trust.add_argument('--json', action='store_true', help='print JSON instead of text')
    trust.set_defaults(func=cmd_trust)

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
    search.add_argument(
        '-m', '--meaning', action='store_true', help='also match by meaning: synonyms, formulas, lineage'
    )
    _query_options(search)
    search.set_defaults(func=cmd_search)

    describe = add('describe', 'columns, types, descriptions, tests and neighbours of one node')
    describe.add_argument('ref', metavar='NAME')
    _query_options(describe)
    describe.set_defaults(func=cmd_describe)

    conflicting = add('conflicts', 'columns that compute the same business idea in different ways')
    conflicting.add_argument('-n', '--limit', type=int, metavar='N', help='at most N conflicts')
    conflicting.add_argument(
        '--format', choices=['text', 'markdown'], default='text', metavar='FORMAT', help='text or markdown'
    )
    conflicting.add_argument(
        '--no-write', action='store_true', help='do not write CONFLICTS.md and conflicts.json'
    )
    conflicting.add_argument(
        '--fail-on-conflict', action='store_true', help='exit with status 1 if any conflict is found'
    )
    _query_options(conflicting)
    conflicting.set_defaults(func=cmd_conflicts)

    checking = add(
        'check-sql', "check SQL against the project's definitions, keys and relationships, without running it"
    )
    checking.add_argument(
        'file', nargs='?', metavar='FILE', help='a file of SQL, or - to read it from standard input'
    )
    checking.add_argument(
        '--format',
        choices=['text', 'markdown', 'json'],
        default='text',
        metavar='FORMAT',
        help='text, markdown or json',
    )
    checking.add_argument(
        '--strict',
        action='store_true',
        help='exit with status 1 on a warning as well as an error; with --history, on a shadow metric of an '
        'approved term',
    )
    reading = checking.add_argument_group(
        'query history', "with --history: the queries run in the project's warehouses, checked"
    )
    reading.add_argument(
        '--history',
        action='store_true',
        help='check the query history of the configured Snowflake, BigQuery and Databricks warehouses '
        'for measures computed differently from their definitions (shadow metrics)',
    )
    reading.add_argument(
        '--days', type=int, metavar='N', help='read N days of history (default: [usage] days, 90)'
    )
    reading.add_argument(
        '-n',
        '--limit',
        type=int,
        default=5000,
        metavar='N',
        help='the most distinct query texts read, those run most often first (default 5000)',
    )
    reading.add_argument(
        '--pseudonymise',
        action='store_true',
        help='count people without naming them, and leave literals out of the example queries',
    )
    _query_options(checking)
    checking.set_defaults(func=cmd_check_sql)

    defining = add('define', 'what a business term means, who owns it and which columns hold it')
    defining.add_argument(
        'ref', nargs='+', metavar='TERM', help='a term, a synonym, or a column such as orders.amount'
    )
    _query_options(defining)
    defining.set_defaults(func=cmd_define)

    ontological = add('ontology', 'the entities, measures and relationships derived from the data stack')
    ontological.add_argument(
        '--format', choices=['text', 'yaml'], default='text', metavar='FORMAT', help='text or yaml'
    )
    ontological.add_argument(
        '--strict', action='store_true', help='exit with status 1 if any ontology rule is broken'
    )
    _query_options(ontological)
    ontological.set_defaults(func=cmd_ontology)

    entity = add('entity', 'one business entity: its key, properties, relationships and metrics')
    entity.add_argument('name', nargs='+', metavar='NAME', help='an entity, or the term naming it')
    _query_options(entity)
    entity.set_defaults(func=cmd_entity)

    metric = add('metric', 'business metrics, defined and proposed, with formula, grain and confidence')
    metric.add_argument(
        'name', nargs='*', metavar='NAME', help='a metric or a column holding one; all of them if left out'
    )
    metric.add_argument('-n', '--limit', type=int, metavar='N', help='at most N metrics')
    _query_options(metric)
    metric.set_defaults(func=cmd_metric)

    relating = add('relate', 'how two entities connect: relationships, cardinality and joins')
    relating.add_argument('start', metavar='FROM', help='an entity, such as customer')
    relating.add_argument('end', metavar='TO', help='another entity, such as payment')
    _query_options(relating)
    relating.set_defaults(func=cmd_relate)

    sensitive = add(
        'sensitivity', 'where sensitive data is, where the lineage takes it and which transforms stop it'
    )
    sensitive.add_argument('ref', nargs='?', metavar='NAME', help='only this table, model or column')
    sensitive.add_argument(
        '--strict',
        action='store_true',
        help='exit with status 1 if sensitive data reaches a model or exposure not marked to hold it, '
        'or a table copies a masked column in clear',
    )
    _query_options(sensitive)
    sensitive.set_defaults(func=cmd_sensitivity)

    erasing = add(
        'erasure', "every relation that holds one person's rows, found by a key, for an erasure request"
    )
    erasing.add_argument(
        'ref', metavar='KEY', help='a key column (customers.customer_id), an entity (customer) or a table'
    )
    erasing.add_argument(
        '--levels',
        type=int,
        default=3,
        metavar='N',
        help="how many steps of rows the person owns to follow, such as an order's payments (default 3)",
    )
    _query_options(erasing)
    erasing.set_defaults(func=cmd_erasure)

    explaining = add('explain', "a column's formula, composed down to its source columns")
    explaining.add_argument('ref', metavar='COLUMN', help='e.g. orders.amount')
    explaining.add_argument(
        '--english',
        action='store_true',
        help='also word the formula, its joins, filters and grouping, and its terms in plain English',
    )
    _query_options(explaining)
    explaining.set_defaults(func=cmd_explain)

    historical = add('history', "when a column's formula changed, and who approved the terms bound to it")
    historical.add_argument('ref', metavar='COLUMN', help='e.g. customers.lifetime_value')
    historical.add_argument(
        '-n',
        '--limit',
        type=int,
        default=50,
        metavar='N',
        help='the most commits to read per file (default 50)',
    )
    _query_options(historical)
    historical.set_defaults(func=cmd_history)

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
        'the graph as OpenLineage, Mermaid, Graphviz DOT or CSV, or the ontology as Apache Ossie, OWL, '
        'a Snowflake semantic view or Databricks metric views',
    )
    exporting.add_argument(
        'format',
        choices=_ExportFormats(),
        # A metavar, so that argparse does not list the choices, which would look for installed
        # plugins whenever the parser is built.
        metavar='FORMAT',
        help=f'{", ".join(BUILTIN_FORMATS)}, or a format a plugin adds',
    )
    exporting.add_argument('-o', '--output', metavar='FILE', help='write to FILE instead of stdout')
    exporting.add_argument(
        '-s',
        '--select',
        metavar='NAME',
        help='only the lineage of this table or column, both directions (not for the ontology formats)',
    )
    exporting.add_argument(
        '--columns', action='store_true', help='mermaid and dot: draw columns and column lineage'
    )
    exporting.add_argument(
        '--namespace',
        metavar='NS',
        help='openlineage: dataset namespace (default: <dialect>://konstruct); owl: the ontology IRI',
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
        metavar='FORMAT',
        default='text',
        help='text, markdown, json or selector: markdown suits a pull request comment, and selector '
        'prints only the dbt selection',
    )
    diff.add_argument('--json', action='store_true', help='print JSON instead of text')
    diff.add_argument('--json-file', metavar='FILE', help='also write the result as JSON to FILE (for CI)')
    diff.add_argument(
        '--fail-on-breaking', action='store_true', help='exit with status 1 on a breaking change'
    )
    diff.add_argument('--graph', metavar='FILE', help='use this graph.json instead of finding konstruct-out')
    diff.add_argument('--refresh', action='store_true', help='update the graph first if its inputs changed')
    diff.add_argument('--no-build', action='store_true', help='fail rather than build a missing graph')
    diff.set_defaults(func=cmd_diff)

    noting = add(
        'release-notes', 'the changes to formulas, filters and business terms in a range of commits (git)'
    )
    noting.add_argument(
        '--since',
        metavar='REF',
        help='commit, tag or date (2026-10-01) the notes start after (default: the latest tag)',
    )
    noting.add_argument('--until', metavar='REF', help='commit, tag or date the notes end at (default HEAD)')
    noting.add_argument(
        '-n', '--limit', type=int, default=200, metavar='N', help='the most commits read (default 200)'
    )
    noting.add_argument(
        '--format',
        choices=['text', 'markdown', 'json'],
        default='text',
        metavar='FORMAT',
        help='text, markdown or json: markdown suits a wiki page or a chat message',
    )
    _query_options(noting)
    noting.set_defaults(func=cmd_release_notes)

    gate_ = add('gate', 'the check a change must pass before it is proposed (exits 1 when it fails)')
    gate_.add_argument(
        '--base', metavar='REF', help='commit or date to compare with (default: the merge base with main)'
    )
    gate_.add_argument(
        '--format',
        choices=['text', 'markdown', 'json'],
        default='text',
        metavar='FORMAT',
        help='text, markdown or json',
    )
    gate_.add_argument('--json', action='store_true', help='print JSON instead of text')
    gate_.add_argument('--json-file', metavar='FILE', help='also write the result as JSON to FILE (for CI)')
    gate_.add_argument('--graph', metavar='FILE', help='use this graph.json instead of finding konstruct-out')
    gate_.add_argument('--no-update', action='store_true', help='use the graph as it is (just built, say)')
    gate_.add_argument('--no-dbt', action='store_true', help='never run dbt when updating the graph')
    gate_.add_argument(
        '--no-warehouse', action='store_true', help='connect to no warehouse when updating the graph'
    )
    gate_.set_defaults(func=cmd_gate, refresh=False, no_build=False)

    verify = add(
        'verify',
        'test the column lineage against the data by perturbing inputs (DuckDB, Postgres, or a sample '
        'of Snowflake, BigQuery, Databricks or Trino)',
    )
    verify.add_argument('-m', '--model', action='append', metavar='NAME', help='only this model (repeatable)')
    verify.add_argument(
        '--seed', type=int, default=1729, help='random seed, for reproducible runs (default 1729)'
    )
    verify.add_argument(
        '--rows',
        type=int,
        default=10000,
        action=_StoreGiven,
        metavar='N',
        help='rows copied per input (default 10000, 1000 with --mutate, 1000000 with --why or --effect)',
    )
    verify.add_argument(
        '--warehouse',
        metavar='WAREHOUSE',
        help='DuckDB file, postgresql:// URL, or snowflake, bigquery, databricks or trino for the configured one, '
        'sampled (default: discovered)',
    )
    verify.add_argument(
        '--strict', action='store_true', help='exit with status 1 if any influence is unexplained'
    )
    verify.add_argument(
        '--no-ontology',
        action='store_true',
        help="do not check the ontology's keys and relationships over whole tables",
    )
    mutating = verify.add_argument_group(
        'mutation testing', 'with --mutate: would the dbt tests fail if a model were wrong?'
    )
    mutating.add_argument(
        '--mutate',
        action='store_true',
        help='run small changes to each dbt model against its data and unit tests, and suggest a test '
        'for each change they miss; writes MUTATION.md and mutation.json',
    )
    mutating.add_argument(
        '--max-mutants', type=int, default=30, metavar='N', help='the most mutants run per model (default 30)'
    )
    mutating.add_argument(
        '--operators',
        metavar='LIST',
        help='comma-separated kinds of mutation (default all): aggregate, filter, join_condition, '
        'join_type, boundary, comparison, date, coalesce, case',
    )
    mutating.add_argument(
        '--no-suggest', action='store_true', help='do not look for a test that kills each survivor'
    )
    mutating.add_argument('--markdown', metavar='FILE', help='also write the report as markdown to FILE')
    tracing = verify.add_argument_group(
        'the rows behind a value', 'with --why: which source rows make up one value, and which were left out'
    )
    tracing.add_argument(
        '--why',
        metavar='COLUMN',
        help='list the source rows behind one value of COLUMN (e.g. customer_ltv.lifetime_value), each '
        'with its share, and the related rows a filter or join left out',
    )
    tracing.add_argument(
        '--where',
        action='append',
        metavar='COLUMN=VALUE',
        help='the row of the model whose value to explain, by an output column (repeatable)',
    )
    tracing.add_argument(
        '-n',
        '--limit',
        type=int,
        default=50,
        metavar='N',
        help='the most rows listed in each table (default 50)',
    )
    affecting = verify.add_argument_group(
        'the effect of a change', "with --effect: what each formula change does on today's data"
    )
    affecting.add_argument(
        '--effect',
        action='store_true',
        help='run each changed model as it was before and after each commit on the same data, and '
        'print the release notes with the difference',
    )
    affecting.add_argument(
        '--since', metavar='REF', help='commit, tag or date the changes start after (default: the latest tag)'
    )
    affecting.add_argument(
        '--until', metavar='REF', help='commit, tag or date the changes end at (default HEAD)'
    )
    affecting.add_argument(
        '--format',
        choices=['text', 'markdown', 'json'],
        default='text',
        metavar='FORMAT',
        help='with --effect: text, markdown or json',
    )
    lawful = verify.add_argument_group(
        'data laws', 'with --laws: the totals the lineage says the data must keep, checked per day'
    )
    lawful.add_argument(
        '--laws',
        action='store_true',
        help="propose the conservation, count and additivity laws each model's SQL implies and check "
        'them against the data; writes laws.json, which the gate reads',
    )
    lawful.add_argument(
        '--propose',
        action='store_true',
        help='with --laws, only propose them, reading no warehouse and writing nothing',
    )
    lawful.add_argument(
        '--tolerance',
        type=float,
        metavar='X',
        help="how far a floating-point total may part from its source's, relative to the day's sum of "
        'magnitudes (default 1e-9); integer and decimal totals must agree exactly',
    )
    _query_options(verify)
    verify.set_defaults(func=cmd_verify)

    locking = add('lock', 'pin what each upstream column this project reads means, in konstruct.lock')
    locking.add_argument(
        'reads',
        nargs='*',
        metavar='NAME',
        help='what to pin: columns, relations, exposures or terms (default: what konstruct.lock names, or '
        'every upstream column the project reads)',
    )
    locking.add_argument(
        '--check',
        action='store_true',
        help='compare konstruct.lock with the build instead, and exit 1 when a pinned column changed',
    )
    locking.add_argument(
        '--update',
        nargs='*',
        metavar='COLUMN',
        help='accept every change to the pinned columns, or the changes to these',
    )
    locking.add_argument(
        '--no-write', action='store_true', help='show what would be written, writing nothing'
    )
    locking.add_argument(
        '--no-history', action='store_true', help='with --check, do not read git history for who changed what'
    )
    locking.add_argument(
        '--format',
        choices=['text', 'markdown', 'json'],
        default='text',
        metavar='FORMAT',
        help='text, markdown (with --check, for a pull request comment) or json',
    )
    _query_options(locking)
    locking.set_defaults(func=cmd_lock)

    open_ = add('open', 'open graph.html in a browser')
    open_.add_argument('--print-only', action='store_true', help='print the file URL without opening it')
    _query_options(open_)
    open_.set_defaults(func=cmd_open)

    install = add('install', 'add the Konstruct skills and instructions to coding agents')
    install.add_argument(
        '--agent',
        action=_ExtendDefault,
        default='both',
        nargs='+',
        choices=AGENT_CHOICES,
        metavar='AGENT',
        help='claude, cursor, codex, copilot, gemini or windsurf; both (claude and cursor, the '
        'default) or all',
    )
    install.add_argument(
        '--target', metavar='DIR', help='install into this repository, not your home directory'
    )
    install.add_argument(
        '--mcp', action='store_true', help="also register the MCP server in each agent's configuration"
    )
    install.add_argument(
        '--force', action='store_true', help='replace files and sections that were edited by hand'
    )
    install.set_defaults(func=cmd_install)

    hooking = add(
        'hook',
        'check a data file a coding agent just edited, from its hook event on stdin (for agent hooks)',
    )
    hooking.add_argument('event', choices=['post-edit'], help='the hook event: post-edit, after a file edit')
    hooking.add_argument(
        '--remind', action='store_true', help='only remind the agent to run the gate, without running it'
    )
    hooking.set_defaults(func=cmd_hook)

    serving = add('serve', 'serve the viewer and a JSON API over the graph, reloading after each build')
    serving.add_argument('path', nargs='?', metavar='PROJECT', help='project directory (default: -C or .)')
    serving.add_argument('--port', type=int, default=8000, help='port (default 8000; 0 for any free port)')
    serving.add_argument(
        '--host', default='127.0.0.1', help='address to listen on (default 127.0.0.1: this machine only)'
    )
    serving.add_argument('--watch', action='store_true', help='rebuild the graph when its inputs change')
    serving.add_argument('--open', action='store_true', help='open the viewer in a browser')
    serving.add_argument('--no-build', action='store_true', help='serve the graph as it is, if there is one')
    serving.set_defaults(func=cmd_serve)

    receipting = add('receipts', 'record what an answer rests on, and check later whether it still holds')
    actions = receipting.add_subparsers(
        dest='action', metavar='<action>', title='actions', parser_class=_Parser, required=True
    )

    def action(name: str, help_text: str) -> argparse.ArgumentParser:
        key = f'receipts {name}'
        return actions.add_parser(
            name,
            help=help_text,
            description=help_text[:1].upper() + help_text[1:] + '.',
            epilog=_examples(key) if key in EXAMPLES else None,
            formatter_class=_Help,
            parents=[common],
        )

    issuing = action('issue', 'record what an answer rests on, and print a citation for it')
    issuing.add_argument(
        'ref',
        nargs='+',
        metavar='NAME',
        help='a column, relation, exposure or business term the answer rests on',
    )
    issuing.add_argument('--question', metavar='TEXT', help='the question answered, recorded as given')
    issuing.add_argument('--answer', metavar='TEXT', help='the answer given, recorded as given')
    _query_options(issuing)
    showing = action('show', 'print one receipt as it was issued')
    showing.add_argument('id', metavar='ID', help='a receipt id, or the start of one')
    showing.add_argument('--json', action='store_true', help='print JSON instead of text')
    showing.add_argument(
        '--graph',
        metavar='FILE',
        help='use the receipts beside this graph.json instead of finding konstruct-out',
    )
    checking = action('check', 'whether what each receipt rests on still holds (exits 1 when one does not)')
    checking.add_argument('id', nargs='*', metavar='ID', help='only these receipts (default: every one)')
    checking.add_argument(
        '--no-history', action='store_true', help='do not read git history to say who changed what'
    )
    checking.add_argument(
        '--no-refresh',
        action='store_true',
        help='do not update a graph older than its inputs first; receipts are then unchecked',
    )
    checking.add_argument('--json', action='store_true', help='print JSON instead of text')
    checking.add_argument(
        '--graph', metavar='FILE', help='use this graph.json instead of finding konstruct-out'
    )
    checking.add_argument('--no-build', action='store_true', help='fail rather than build a missing graph')
    checking.set_defaults(refresh=False)
    receipting.set_defaults(func=cmd_receipts)

    mcp = add('mcp', 'run the MCP server on stdio (for agent configuration, not interactive use)')
    mcp.add_argument(
        '--no-prompts', action='store_true', help='offer the tools and resources without the workflow prompts'
    )
    mcp.set_defaults(func=cmd_mcp)

    plugins = add('plugins', 'list installed plugins and whether each runs for this project')
    plugins.add_argument('--json', action='store_true', help='print JSON instead of text')
    plugins.set_defaults(func=cmd_plugins)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    global _help_style, _given
    _given = list(sys.argv[1:] if argv is None else argv)
    _help_style = style_for(sys.stdout, False if _no_colour() else None)
    if _help_style.width is None:
        # Help is prose: piped, it keeps the 80 columns argparse would give it.
        _help_style = dataclasses.replace(_help_style, width=80)
    arguments = parser()
    args = arguments.parse_args(_given)
    from konstruct.engine.trust import trusting

    defaults = (
        ('project', '.'),
        ('no_color', False),
        ('verbose', False),
        ('quiet', False),
        ('no_plugins', False),
        ('trust_config', False),
    )
    for name, default in defaults:
        if not hasattr(args, name):
            setattr(args, name, default)
    console = Console(colour=False if args.no_color else None, quiet=args.quiet, verbose=args.verbose)
    if not getattr(args, 'func', None):
        console.print(arguments.format_help().rstrip())
        return 2
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format='konstruct: %(message)s',
        stream=sys.stderr,
    )
    try:
        with trusting(args.trust_config):
            code: int = args.func(args, console)
    except Exception as error:
        # The error types are imported only when one is raised: importing them up front would
        # load the whole build (and sqlglot) for every command, `--help` included.
        from konstruct.analysis.query import QueryError
        from konstruct.engine.config import ConfigError
        from konstruct.interfaces.skill import InstallError
        from konstruct.outputs.export import ExportError

        if isinstance(error, QueryError):
            console.error(str(error), hint=error.hint, suggestions=error.suggestions)
            return 1
        if isinstance(error, (ConfigError, InstallError, ExportError, FormatError)):
            console.error(str(error))
            return 1
        if isinstance(error, BrokenPipeError):
            # The reader went away (`konstruct list | head`); stop quietly, as other tools do.
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            return 0
        raise
    except KeyboardInterrupt:
        return 130
    return code


if __name__ == '__main__':
    sys.exit(main())
