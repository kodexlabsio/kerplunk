"""Run a coding agent on Spider 2.0-DBT with and without Konstruct, and score both.

Spider 2.0-DBT (https://github.com/xlang-ai/Spider2, spider2-dbt) is a set of dbt projects on
DuckDB, each with an instruction and a gold database. This script copies each project twice and
runs the same agent command on both copies with the same prompt:

* ``baseline``: the project as the benchmark ships it;
* ``konstruct``: the same project after ``konstruct build`` and ``konstruct install --target``, so
  the graph and the ``/konstruct`` skill (and, with ``--mcp``, the MCP server) are there for the
  agent to find. The prompt does not mention Konstruct.

As in the benchmark's own protocol, the agent runs ``dbt run`` itself and the project's DuckDB
file is scored as the agent leaves it (``--dbt-run`` runs ``dbt run`` once more afterwards). The
database is the one the project's dbt profile writes, and it is scored against the gold one with
the benchmark's own ``duckdb_match``, read from its evaluation suite.

The agent is any command line. ``{prompt}`` in it is replaced by the prompt, shell-quoted, and it
runs in the project directory, for example::

    --agent 'claude -p {prompt} --permission-mode acceptEdits'
    --agent 'claude -p {prompt} --permission-mode acceptEdits --mcp-config .mcp.json'   # with --mcp

Each copy is its own git repository, outside any other, so an agent reads no instructions from a
directory above it; ``--out`` must not be inside a git repository or under a CLAUDE.md,
CLAUDE.local.md, .claude or AGENTS.md. With ``--mcp`` the baseline copy gets an empty
``.mcp.json``, so an agent command that names it works in both arms. Prepare the benchmark first as its README says (download the databases, run
``setup.py``), then:

    uv run .kodex/scripts/agent_benchmark.py ~/Spider2/spider2-dbt --agent '...'
    uv run .kodex/scripts/agent_benchmark.py ~/Spider2/spider2-dbt --agent '...' --only asana001 shopify001

Each run writes ``<out>/<arm>/<instance>/`` (the project) with its logs beside it,
``<out>/results.jsonl`` with one line per instance and arm, and ``<out>/summary.md``. A run
that is interrupted resumes with the same options: instances with a result are skipped.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

ARMS = ('baseline', 'konstruct')
# Files and directories an agent reads instructions or settings from, in a directory or above it.
INSTRUCTIONS = ('.git', 'CLAUDE.md', 'CLAUDE.local.md', '.claude', 'AGENTS.md')
# The benchmark's reference agent gives its hints in a system prompt; these say the same.
PROMPT = """\
{instruction}

The current directory is an unfinished dbt project on DuckDB ({database}).
- Read the project files first. All the data is in the DuckDB database.
- Find which models the YAML files define whose SQL is missing or unfinished, and write that SQL.
  Mostly this means new SQL files; change an existing one only if it is clearly unfinished.
- Do not change the YAML files. When something fails, fix the SQL to match them.
- When the SQL is written, run `dbt run` to update the database, and check the models it built
  against the YAML definitions. The task is done when every model the YAML files need is built.
"""


@dataclass
class Task:
    instance_id: str
    instruction: str
    evaluation: dict[str, Any]

    @property
    def gold(self) -> str:
        return str(self.evaluation['parameters']['gold'])


@dataclass
class Result:
    instance_id: str
    arm: str
    score: float
    agent_seconds: float
    agent_exit: int | None
    dbt_exit: int | None
    error: str | None = None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def load_tasks(spider: Path) -> list[Task]:
    """The benchmark's instances that are scored on a DuckDB file."""
    instructions = {
        t['instance_id']: t['instruction'] for t in read_jsonl(spider / 'examples' / 'spider2-dbt.jsonl')
    }
    tasks = []
    for gold in read_jsonl(spider / 'evaluation_suite' / 'gold' / 'spider2_eval.jsonl'):
        evaluation = gold['evaluation']
        if isinstance(evaluation, dict) and evaluation.get('func') == 'duckdb_match':
            instance = gold['instance_id']
            if instance in instructions:
                tasks.append(Task(instance, instructions[instance], evaluation))
    return sorted(tasks, key=lambda t: t.instance_id)


def load_matcher(spider: Path) -> Callable[..., float]:
    """``duckdb_match`` from the benchmark's evaluation suite, so the score is the benchmark's own."""
    path = spider / 'evaluation_suite' / 'eval_utils.py'
    spec = importlib.util.spec_from_file_location('spider2_eval_utils', path)
    if spec is None or spec.loader is None:
        raise SystemExit(f'cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    matcher: Callable[..., float] = module.duckdb_match
    return matcher


def result_database(project: Path, fallback: str) -> str:
    """The DuckDB file the project's dbt profile writes, relative to the project; the gold file's
    name when the profile does not say."""
    try:
        name = (yaml.safe_load((project / 'dbt_project.yml').read_text()) or {}).get('profile')
        profile = (yaml.safe_load((project / 'profiles.yml').read_text()) or {})[name]
        output = profile['outputs'][profile.get('target', 'dev')]
        path = Path(str(output['path']))
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return fallback
    return fallback if path.is_absolute() else os.path.normpath(path)


def instructions_above(path: Path) -> Path | None:
    """The first directory at or above ``path`` that an agent would read instructions from."""
    for directory in (path, *path.parents):
        if any((directory / name).exists() for name in INSTRUCTIONS):
            return directory
    return None


def run(command: list[str] | str, cwd: Path, log: Path, timeout: float, *, shell: bool = False) -> int | None:
    """Run a command in its own process group, appending its output to ``log``; the exit code, or
    None on timeout, when the whole group (the agent and anything it started) is stopped."""
    with log.open('a', encoding='utf-8') as out:
        out.write(f'$ {command if isinstance(command, str) else shlex.join(command)}\n')
        out.flush()
        process = subprocess.Popen(
            command, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, shell=shell, start_new_session=True
        )
        try:
            code: int | None = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _signal_group(process.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
            out.write(f'timed out after {timeout:.0f} s\n')
            code = None
        # Whatever the command left running (a background job, a child that ignores SIGTERM)
        # must not keep changing the project or holding its database while it is scored.
        _signal_group(process.pid, signal.SIGKILL)
        if process.poll() is None:
            process.wait()
        return code


def _signal_group(group: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, sig)


def konstruct_command() -> str:
    """The ``konstruct`` command of the environment running this script."""
    beside = Path(sys.executable).parent / 'konstruct'
    found = str(beside) if beside.is_file() else shutil.which('konstruct')
    if found is None:
        raise SystemExit('konstruct is not installed in this environment')
    return found


def prepare(task: Task, arm: str, spider: Path, out: Path, *, mcp: bool, timeout: float) -> Path:
    """A fresh copy of the task's project as its own git repository, with Konstruct set up in it
    for the konstruct arm. A Konstruct setup that fails stops the run: scoring it as a failure
    would count a harness problem against one arm."""
    project = out / arm / task.instance_id
    if project.exists():
        shutil.rmtree(project)
    shutil.copytree(
        spider / 'examples' / task.instance_id, project, ignore=shutil.ignore_patterns('target', 'logs')
    )
    subprocess.run(['git', 'init', '-q', str(project)], check=True)
    if mcp and arm == 'baseline':
        # The same agent command, which names the MCP configuration, must be valid in both arms.
        (project / '.mcp.json').write_text('{"mcpServers": {}}\n')
    if arm == 'konstruct':
        log = out / arm / f'{task.instance_id}.konstruct.log'
        konstruct = konstruct_command()
        for step in (
            [konstruct, 'build', '.', '-q'],
            [konstruct, 'install', '--target', '.', '--agent', 'both', *(['--mcp'] if mcp else [])],
        ):
            if run(step, project, log, timeout) != 0:
                raise SystemExit(f'{task.instance_id}: `konstruct {shlex.join(step[1:3])}` failed; see {log}')
    return project


def attempt(
    task: Task,
    arm: str,
    spider: Path,
    out: Path,
    agent: str,
    matcher: Callable[..., float],
    *,
    mcp: bool,
    dbt_run: bool,
    timeout: float,
) -> Result:
    project = prepare(task, arm, spider, out, mcp=mcp, timeout=timeout)
    database = result_database(project, task.gold)
    prompt = PROMPT.format(instruction=task.instruction.strip(), database=database)
    logs = out / arm
    started = time.perf_counter()
    command = agent.replace('{prompt}', shlex.quote(prompt))
    agent_exit = run(command, project, logs / f'{task.instance_id}.agent.log', timeout, shell=True)
    seconds = time.perf_counter() - started
    dbt_exit = None
    if dbt_run:
        dbt = ['dbt', 'run', '--profiles-dir', '.']
        dbt_exit = run(dbt, project, logs / f'{task.instance_id}.dbt.log', timeout)
    result = Result(task.instance_id, arm, 0.0, round(seconds, 1), agent_exit, dbt_exit)
    if not (project / database).is_file():
        result.error = f'no {database} after the run'
        return result
    parameters = {k: v for k, v in task.evaluation['parameters'].items() if k != 'gold'}
    gold = spider / 'evaluation_suite' / 'gold' / task.instance_id / task.gold
    try:
        result.score = float(matcher(str(project / database), gold=str(gold), **parameters))
    except Exception as error:  # the benchmark's own evaluator scores any failure as 0
        result.error = f'{type(error).__name__}: {error}'
    return result


def summary(results: list[Result], tasks: list[Task]) -> str:
    wanted = {t.instance_id for t in tasks}
    by_arm = {
        arm: {r.instance_id: r for r in results if r.arm == arm and r.instance_id in wanted} for arm in ARMS
    }
    lines = [
        '# Spider 2.0-DBT with and without Konstruct',
        '',
        '| Arm | Solved | Of | Rate |',
        '|---|---|---|---|',
    ]
    for arm in ARMS:
        done = by_arm[arm]
        solved = sum(1 for r in done.values() if r.score == 1)
        rate = f'{solved / len(done):.1%}' if done else '-'
        lines.append(f'| {arm} | {solved} | {len(done)} | {rate} |')
    lines += ['', '| Instance | ' + ' | '.join(ARMS) + ' |', '|---|' + '---|' * len(ARMS)]
    for task in tasks:
        cells = []
        for arm in ARMS:
            result = by_arm[arm].get(task.instance_id)
            cells.append('-' if result is None else ('solved' if result.score == 1 else 'failed'))
        lines.append(f'| {task.instance_id} | ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n', 1)[0])
    parser.add_argument('spider', type=Path, help='the spider2-dbt directory, after its setup.py')
    parser.add_argument(
        '--agent', required=True, help='the agent command; {prompt} is replaced by the prompt'
    )
    parser.add_argument(
        '--out', type=Path, help='where to write the runs (default: a new temporary directory)'
    )
    parser.add_argument('--only', nargs='+', metavar='ID', help='only these instances')
    parser.add_argument('--arms', nargs='+', choices=ARMS, default=list(ARMS), help='which arms to run')
    parser.add_argument(
        '--mcp', action='store_true', help='also register the MCP server in the konstruct arm'
    )
    parser.add_argument(
        '--dbt-run', action='store_true', help='run `dbt run` after the agent, before scoring'
    )
    parser.add_argument('--timeout', type=float, default=1800, help='seconds for each command (default 1800)')
    args = parser.parse_args(argv)
    if '{prompt}' not in args.agent:
        raise SystemExit('--agent must contain {prompt}')

    spider: Path = args.spider.resolve()
    tasks = load_tasks(spider)
    if args.only:
        unknown = sorted(set(args.only) - {t.instance_id for t in tasks})
        if unknown:
            raise SystemExit(f'not instances scored on a DuckDB file: {", ".join(unknown)}')
        tasks = [t for t in tasks if t.instance_id in set(args.only)]
    if not tasks:
        raise SystemExit('no instances to run')
    out: Path = (args.out or Path(tempfile.mkdtemp(prefix='konstruct-agent-benchmark-'))).resolve()
    above = instructions_above(out)
    if above is not None:
        raise SystemExit(f'--out is under {above}, whose instructions an agent would read; choose another')
    matcher = load_matcher(spider)
    out.mkdir(parents=True, exist_ok=True)
    config = {'agent': args.agent, 'mcp': args.mcp, 'dbt_run': args.dbt_run, 'timeout': args.timeout}
    config_path = out / 'config.json'
    if config_path.is_file() and json.loads(config_path.read_text()) != config:
        raise SystemExit(f'{out} holds a run with other options ({config_path}); choose another --out')
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    log = out / 'results.jsonl'
    results = [Result(**r) for r in read_jsonl(log)] if log.is_file() else []
    done = {(r.instance_id, r.arm) for r in results}
    for task in tasks:
        for arm in args.arms:
            if (task.instance_id, arm) in done:
                continue
            result = attempt(
                task,
                arm,
                spider,
                out,
                args.agent,
                matcher,
                mcp=args.mcp,
                dbt_run=args.dbt_run,
                timeout=args.timeout,
            )
            results.append(result)
            with log.open('a', encoding='utf-8') as f:
                f.write(json.dumps(asdict(result)) + '\n')
            print(f'{task.instance_id} {arm}: {"solved" if result.score == 1 else "failed"}', file=sys.stderr)
    (out / 'summary.md').write_text(summary(results, tasks), encoding='utf-8')
    print(f'{out}\n\n' + (out / 'summary.md').read_text(encoding='utf-8'))
    return 0


if __name__ == '__main__':
    os.environ.setdefault('DBT_SEND_ANONYMOUS_USAGE_STATS', 'false')
    raise SystemExit(main())
