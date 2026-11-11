"""`konstruct hook`: what a coding agent's hook runs after the agent edits a file.

``post-edit`` reads Claude Code's PostToolUse event on standard input. When the edited file is
SQL, dbt YAML, a definitions file or an Ossie model in a repository Konstruct has built, it
brings a copy of the graph up to date and runs the gate, and prints what the agent should know
as hook JSON on standard output: nothing for other files or other repositories, a one-line note
when the gate passes, and the findings, as a ``block`` decision, when it fails.

The update never runs dbt and connects to no warehouse: a hook runs after every edit, unasked, so
it reads files only, and Konstruct never imports or runs the project's own code. It builds into
``.hook`` inside the output directory, starting from the build cache and without graph.html, so
the graph the user built (with its warehouse metadata) is left as it is. Hooks for edits made in
parallel take turns on ``.hook``, and one that finds the files as the last run left them gives
that run's answer again rather than building once more.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import IO, Any

# Directories of build output, packages and environments: an edit there is not a change to the
# data stack.
_IGNORED = {'target', 'dbt_packages', 'dbt_modules', 'konstruct-out', 'node_modules', 'logs', '.git', '.venv'}
_SUFFIXES = {'.sql', '.yml', '.yaml'}
# A repository with one of these at its root is a data repository Konstruct can build.
_DATA_ROOTS = ('dbt_project.yml', 'konstruct.toml')
# The plugin's timeout for the hook. A hook waits for another one, run for a parallel edit, only
# as long as leaves it time to build and gate before then.
TIMEOUT = 120
_MARGIN = 10.0
# dbt's resource paths and their defaults: YAML under them describes the project; elsewhere in a
# dbt project (CI workflows, tool configuration) it does not.
_RESOURCE_PATHS = {
    'model-paths': ['models'],
    'seed-paths': ['seeds'],
    'snapshot-paths': ['snapshots'],
    'analysis-paths': ['analyses'],
    'macro-paths': ['macros'],
    'test-paths': ['tests'],
}


def edited_path(event: dict[str, Any]) -> Path | None:
    """The file a Write, Edit or MultiEdit tool call changed, if the event names one."""
    tool_input = event.get('tool_input')
    if not isinstance(tool_input, dict):
        return None
    value = tool_input.get('file_path') or tool_input.get('path')
    return Path(value) if isinstance(value, str) and value else None


def data_paths(root: Path) -> list[Path]:
    """The definitions and Ossie files or directories ``konstruct.toml`` names, whose YAML the build
    reads wherever they are; none when there is no configuration or it cannot be read."""
    from konstruct.engine.config import ConfigError, load_config

    if not _configured(root):
        return []
    try:
        config = load_config(root)
    except (ConfigError, OSError):
        return []
    return [*(config.definitions_paths or []), *(config.ossie_paths or [])]


def is_data_file(path: Path, root: Path, extra: Sequence[Path] = ()) -> bool:
    """Whether ``path`` is SQL, dbt YAML or a definitions file of the project at ``root``: SQL
    anywhere, but YAML only when it is ``dbt_project.yml``, a definitions file, an Ossie model, or
    under one of the resource paths of the dbt project that holds it, or under ``extra``, the
    configured definitions and Ossie paths. Nothing under a directory whose name starts with a dot
    (``.github``, ``.venv``) or under build output counts."""
    if path.suffix not in _SUFFIXES:
        return False
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    if _IGNORED & set(relative.parts[:-1]) or any(part.startswith('.') for part in relative.parts):
        return False
    if path.suffix == '.sql':
        return True
    if path.stem in ('definitions', 'dbt_project') or path.name.endswith(('.ossie.yaml', '.ossie.yml')):
        return True
    absolute = root.resolve() / relative
    if any(absolute == p.resolve() or absolute.is_relative_to(p.resolve()) for p in extra):
        return True
    return _in_resource_paths(absolute, root.resolve())


def _in_resource_paths(path: Path, root: Path) -> bool:
    """Whether ``path`` is under a resource path of the nearest dbt project that holds it."""
    import yaml

    for project in path.parents:
        if (project / 'dbt_project.yml').is_file():
            try:
                data = yaml.safe_load((project / 'dbt_project.yml').read_text(encoding='utf-8')) or {}
            except (yaml.YAMLError, OSError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            for key, default in _RESOURCE_PATHS.items():
                paths = data.get(key) or default
                for name in paths if isinstance(paths, list) else default:
                    if path.is_relative_to((project / str(name)).resolve()):
                        return True
            return False
        if project == root:
            break
    return False


def _configured(directory: Path) -> bool:
    pyproject = directory / 'pyproject.toml'
    return (directory / 'konstruct.toml').is_file() or (
        pyproject.is_file() and '[tool.konstruct' in pyproject.read_text(encoding='utf-8', errors='replace')
    )


def find_graph(start: Path) -> tuple[Path, Path] | None:
    """The project root and output directory of the graph built for ``start``, without reading
    the graph: the output directory the nearest Konstruct configuration at or above ``start``
    names, or else the nearest ``konstruct-out`` at or above it, whose parent is the root."""
    for directory in (start, *start.parents):
        if _configured(directory):
            from konstruct.engine.config import ConfigError, load_config

            with contextlib.suppress(ConfigError, OSError):
                out_dir = load_config(directory).out_dir
                if (out_dir / 'graph.json').is_file():
                    return directory, out_dir
            break
    for directory in (start, *start.parents):
        if (directory / 'konstruct-out' / 'graph.json').is_file():
            return directory, directory / 'konstruct-out'
    return None


@contextlib.contextmanager
def locked(directory: Path, seconds: float) -> Iterator[bool]:
    """Hold an exclusive lock on ``directory`` while the body runs; yield False when another
    process holds it for longer than ``seconds``. The lock goes with the process, so a hook
    that is killed leaves none behind."""
    with (directory / 'lock').open('a+') as file:
        deadline = time.monotonic() + seconds
        while not _try_lock(file):
            if time.monotonic() > deadline:
                yield False
                return
            time.sleep(0.2)
        try:
            yield True
        finally:
            _unlock(file)


if sys.platform == 'win32':
    import msvcrt

    def _try_lock(file: IO[str]) -> bool:
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(file: IO[str]) -> None:
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(file: IO[str]) -> bool:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(file: IO[str]) -> None:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _context(text: str) -> dict[str, Any]:
    return {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': text}}


def _marked(answer: dict[str, Any]) -> dict[str, Any]:
    """``answer`` ending with the notice the MCP server gives: it quotes the repository's own
    text (names, SQL, paths, messages), which anyone who can commit to it writes."""
    from konstruct.interfaces.mcp_server import UNTRUSTED_CONTENT

    if 'reason' in answer:
        answer['reason'] += f'\n\n{UNTRUSTED_CONTENT}'
    else:
        answer['hookSpecificOutput']['additionalContext'] += f' ({UNTRUSTED_CONTENT})'
    return answer


def fingerprint(root: Path) -> str | None:
    """What the files the hook builds from hold: the commit and the diff's base, the content of
    every changed or untracked file in the repository, and every dbt project's manifest at its
    target-path. None outside git. Content rather than size and time, so two edits of the same
    size in the same second still differ."""
    from konstruct.analysis.diff import default_base
    from konstruct.inputs.dbt import find_projects

    def run(*args: str) -> bytes:
        return subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=True).stdout

    try:
        # Status paths are relative to the top of the repository, which the project may be under.
        top = Path(os.fsdecode(run('rev-parse', '--show-toplevel').strip()))
        head = run('rev-parse', 'HEAD')
        status = run('status', '--porcelain=v1', '-z', '--untracked-files=all')
    except (OSError, subprocess.CalledProcessError):
        return None
    digest = hashlib.sha256(head + default_base(root).encode() + b'\0' + status)
    entries = status.split(b'\0')
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        if entry[:1] in (b'R', b'C'):
            index += 1  # the path it was renamed or copied from, which -z gives next
        digest.update(entry[3:] + b'\0' + _content(top / os.fsdecode(entry[3:])))
    for project in find_projects(root):
        digest.update(str(project.manifest_path).encode() + b'\0' + _content(project.manifest_path))
    return digest.hexdigest()


def _content(path: Path) -> bytes:
    """A file's content digest, or a marker for a file that is not there."""
    try:
        return hashlib.sha256(path.read_bytes()).digest()
    except OSError:
        return b'missing'


def _last(scratch: Path) -> dict[str, Any]:
    try:
        data = json.loads((scratch / 'last.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _unreadable(path: Path, notes: list[str]) -> list[str]:
    """Why the gate may not see all of a change to ``path``: YAML that does not parse, and SQL
    the diff could not compare (its notes for the file)."""
    import yaml

    problems = list(notes)
    if path.suffix in ('.yml', '.yaml') and path.is_file():
        try:
            yaml.safe_load(path.read_text(encoding='utf-8'))
        except yaml.YAMLError as error:
            problems.append(f'{path.name} is not valid YAML: {" ".join(str(error).split())}')
    return problems


def post_edit(event: dict[str, Any], *, remind: bool = False) -> dict[str, Any] | None:
    """The hook's answer to a PostToolUse event, or None when there is nothing to say.

    Args:
        event: The hook input: ``tool_input.file_path``, and ``cwd``, the session's directory.
            ``CLAUDE_PROJECT_DIR``, when set, is the project directory and is preferred.
        remind: Only remind the agent to run the gate, rather than running it.
    """
    path = edited_path(event)
    # Most edits are to other files: decide before reading any configuration or graph.
    if path is None or path.suffix not in _SUFFIXES:
        return None
    start = Path(str(os.environ.get('CLAUDE_PROJECT_DIR') or event.get('cwd') or '.')).resolve()
    if not path.is_absolute():
        path = Path(str(event.get('cwd') or start)) / path
    found = find_graph(start)
    if found is None:
        # Only a data repository is worth a reminder; any other repository with SQL in it, such
        # as an application's migrations, hears nothing from a hook installed for every project.
        if any((start / name).is_file() for name in _DATA_ROOTS) and is_data_file(path, start):
            return _marked(
                _context(
                    f'Konstruct: {path.resolve().relative_to(start).as_posix()} changed, and this repository has no '
                    'Konstruct graph yet. Run `konstruct build`, then `konstruct gate` before proposing the change.'
                )
            )
        return None
    root, out_dir = found
    if not is_data_file(path, root, data_paths(root) if path.suffix != '.sql' else ()):
        return None
    shown = path.resolve().relative_to(root.resolve()).as_posix()
    if remind:
        return _marked(
            _context(
                f'Konstruct: {shown} changed. Run `konstruct gate` before proposing the change, and fix what it reports.'
            )
        )
    return _gate(root, out_dir, path, shown)


def _gate(root: Path, out_dir: Path, path: Path, shown: str) -> dict[str, Any]:
    from konstruct.analysis.diff import DiffError
    from konstruct.analysis.gate import gate, plugins_at
    from konstruct.analysis.query import QueryError
    from konstruct.core.jsonio import write_text
    from konstruct.engine.config import ConfigError, load_config
    from konstruct.interfaces.project import Project
    from konstruct.plugins.registry import Plugins

    scratch = out_dir / '.hook'
    try:
        scratch.mkdir(exist_ok=True)
        # The copy is Konstruct's working space: keep it out of commits.
        (scratch / '.gitignore').write_text('*\n', encoding='utf-8')
        expected = float(_last(scratch).get('seconds') or 30.0)
        with locked(scratch, max(0.0, TIMEOUT - _MARGIN - expected)) as held:
            if not held:
                return _marked(
                    _context(
                        f'Konstruct: another hook is still running the gate, so the edit to {shown} was '
                        'not checked. Run `konstruct gate` before proposing the change.'
                    )
                )
            started = time.monotonic()
            # Taken before the build, so an edit made while it runs is never mistaken for checked.
            stamp = fingerprint(root)
            last = _last(scratch)
            if stamp is not None and last.get('fingerprint') == stamp and (scratch / 'graph.json').is_file():
                return _answer(path, shown, bool(last['passed']), str(last['result']), last.get('notes', {}))
            if (out_dir / 'cache.json').is_file() and not (scratch / 'cache.json').is_file():
                shutil.copy2(out_dir / 'cache.json', scratch / 'cache.json')
            config = load_config(root, scratch)
            config.override(run_dbt=False, warehouses=False)
            config.html = False
            # As in `konstruct gate`, the plugins main enables run too, so an edit that takes one
            # out of konstruct.toml is still judged by it.
            config.plugins = list(dict.fromkeys([*config.plugins, *plugins_at(root, None)]))
            checker = Project(root, out_dir=scratch)
            # Progress the build prints must not reach standard output, which carries the JSON.
            with contextlib.redirect_stdout(sys.stderr):
                checker.build(update=True, config=config)
                diff = checker.diff()
                result = gate(checker.graph, diff, checker.conflicts(), plugins=Plugins.for_config(config))
            notes: dict[str, list[str]] = {}
            for relation in diff.relations:
                if relation.note:
                    notes.setdefault(relation.path, []).append(f'{relation.label}: {relation.note}')
            record = {
                'fingerprint': stamp,
                'passed': result.passed,
                'result': str(result),
                'notes': notes,
                'seconds': round(time.monotonic() - started, 1),
            }
            write_text(scratch / 'last.json', json.dumps(record))
    except (DiffError, QueryError, ConfigError, OSError) as error:
        return _marked(
            _context(
                f'Konstruct could not run the gate after the edit to {shown} ({error}). Run `konstruct gate` '
                'before proposing the change.'
            )
        )
    return _answer(path, shown, result.passed, str(result), notes)


def _answer(path: Path, shown: str, passed: bool, result: str, notes: dict[str, list[str]]) -> dict[str, Any]:
    """The hook's answer for the edited file, from a gate result and the diff's notes."""
    unread = _unreadable(path, list(notes.get(shown, [])))
    if passed and not unread:
        return _marked(_context(f'Konstruct gate passed after the edit to {shown}.'))
    lines = [f'Konstruct gate {"passed" if passed else "failed"} after the edit to {shown}.']
    if not passed:
        lines += ['', result]
    if unread:
        # A file Konstruct cannot read hides the change from the gate, so it is never a pass.
        lines += ['', 'Konstruct could not read all of the edited file, so the gate cannot see the change:']
        lines += [f'- {problem}' for problem in unread]
    lines += [
        '',
        'If the change is not finished, carry on, and fix these before proposing it. Run `konstruct gate` '
        'to check again.',
    ]
    return _marked({'decision': 'block', 'reason': '\n'.join(lines)})


def main(event_name: str, *, remind: bool = False, stdin: Any = None, stdout: Any = None) -> int:
    """Run the hook for ``event_name`` on the event JSON from ``stdin``; always exit 0, so a
    problem in the hook never stops the agent."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        event = json.loads(stdin.read() or '{}')
    except ValueError:
        return 0
    if not isinstance(event, dict) or event_name != 'post-edit':
        return 0
    try:
        answer = post_edit(event, remind=remind)
    except Exception as error:  # a hook must not fail the agent's edit, whatever went wrong
        answer = _marked(
            _context(
                f'Konstruct could not run the gate after this edit ({type(error).__name__}: {error}). Run '
                '`konstruct gate` before proposing the change.'
            )
        )
    if answer is not None:
        stdout.write(json.dumps(answer) + '\n')
    return 0
