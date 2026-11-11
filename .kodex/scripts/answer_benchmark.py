"""Ask an AI agent business questions about a data project, and count its confidently wrong answers.

Each question in ``answer_questions/questions.yml`` has one numeric answer, computed from the
warehouse by a reference query committed with it. The agent answers every question in three arms,
each with the same tool to run read-only SQL on the warehouse:

* ``schema``: the warehouse's tables and columns only;
* ``definitions``: the same, with the project's business definitions (every ``definitions.yml``)
  given as text, as a declared semantic layer would serve them;
* ``konstruct``: the schema, with Konstruct's MCP server over the built graph (``konstruct mcp``).

The agent answers through a ``submit_answer`` tool: a number, or null to abstain, and any doubt it
has. The rubric is committed and deterministic: a number within the tolerance the question's unit
sets (``rubric`` in the question set) is correct, any other number is wrong, and a wrong number
given with no doubt is *confidently wrong*. No model grades an answer.

The corpora are the bundled jaffle_shop example and dbt Labs' jaffle-shop at a pinned commit, each
built with dbt into its DuckDB warehouse, with the benchmark's definitions from
``answer_questions/definitions/`` added to the project. Building them needs dbt (a dev
dependency) and, for jaffle-shop, network access.

Usage::

    uv run .kodex/scripts/answer_benchmark.py --plan              # the questions; no warehouse, no API
    uv run .kodex/scripts/answer_benchmark.py --dry-run           # build, check every reference answer,
                                                                  # render each arm's prompt; no API
    KONSTRUCT_BENCH_MODEL=<model id> ANTHROPIC_API_KEY=<key> \\
        uv run --with anthropic .kodex/scripts/answer_benchmark.py --out ~/answer-benchmark

A run calls the Anthropic Messages API with the model ``KONSTRUCT_BENCH_MODEL`` names, and the key
the SDK reads from the environment. It writes ``results.jsonl`` (one line per question and arm),
``transcripts/``, ``summary.md`` and ``results.json`` (the command line, the model, the versions
of Konstruct and of the question set, and the rates of each arm) to ``--out``; a run that stops
resumes with the same options.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
QUESTIONS = HERE / 'answer_questions' / 'questions.yml'
DEFINITIONS = HERE / 'answer_questions' / 'definitions'
ARMS = ('schema', 'definitions', 'konstruct')
OUTCOMES = ('correct', 'wrong', 'abstained')
TRAPS = (
    'control',
    'definition',
    'required-filter',
    'fan-out',
    'grain',
    'minor-units',
    'conflicting-column',
)
MODEL_VARIABLE = 'KONSTRUCT_BENCH_MODEL'
MAX_TURNS = 20
MAX_ROWS = 50
MAX_RESULT_CHARACTERS = 20_000
# Where the harness puts a corpus's benchmark definitions inside the project, so Konstruct finds
# them as it finds any definitions.yml.
OVERLAY = Path('answer-benchmark') / 'definitions.yml'


def _sibling(name: str) -> ModuleType:
    """A script beside this one, as a module: the agent benchmark this harness extends."""
    spec = importlib.util.spec_from_file_location(f'konstruct_scripts_{name}', HERE / f'{name}.py')
    if spec is None or spec.loader is None:
        raise SystemExit(f'cannot load {name}.py beside {__file__}')
    module = importlib.util.module_from_spec(spec)
    # Dataclasses look their module up by name while the module runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agent_benchmark = _sibling('agent_benchmark')
read_jsonl: Callable[[Path], list[dict[str, Any]]] = agent_benchmark.read_jsonl


# The corpora


@dataclass(frozen=True)
class Corpus:
    """A dbt project on DuckDB and how to build its warehouse."""

    name: str
    title: str
    warehouse: str
    dbt: tuple[tuple[str, ...], ...]
    repository: str | None = None
    commit: str | None = None
    profile: str | None = None
    # SQL run on the warehouse after dbt, as the example's Airflow DAG does in production.
    after: str | None = None


_JAFFLE_SHOP_VARIABLES = '{"load_source_data": true}'
CORPORA: dict[str, Corpus] = {
    'jaffle_shop': Corpus(
        name='jaffle_shop',
        title='the bundled jaffle_shop example',
        warehouse='jaffle_shop.duckdb',
        dbt=(('build',),),
        after='dags/sql/customer_ltv.sql',
    ),
    'jaffle-shop': Corpus(
        name='jaffle-shop',
        title="dbt Labs' jaffle-shop",
        warehouse='jaffle.duckdb',
        dbt=(
            ('deps',),
            ('seed', '--vars', _JAFFLE_SHOP_VARIABLES),
            ('build', '--vars', _JAFFLE_SHOP_VARIABLES),
        ),
        repository='https://github.com/dbt-labs/jaffle-shop.git',
        commit='5beb145b00f5465ec759cfcdd9745e858818cf95',
        profile='default:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: jaffle.duckdb\n',
    ),
}


# The question set


@dataclass(frozen=True)
class Question:
    id: str
    corpus: str
    trap: str
    unit: str
    text: str
    reference: str
    answer: float
    tolerance: float


@dataclass(frozen=True)
class QuestionSet:
    version: int
    recorded: str
    rubric: dict[str, float]
    questions: tuple[Question, ...]
    definitions: dict[str, str]

    @property
    def fingerprint(self) -> str:
        """A digest of every question, reference, answer, tolerance and definitions file: a change
        to any of them is a new question set, so results from two sets are never compared."""
        content = {
            'rubric': self.rubric,
            'questions': [asdict(q) for q in self.questions],
            'definitions': self.definitions,
        }
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

    def for_corpora(self, corpora: Iterable[str]) -> list[Question]:
        wanted = set(corpora)
        return [q for q in self.questions if q.corpus in wanted]


def _words(text: str) -> str:
    return ' '.join(str(text).split())


def load_questions(path: Path = QUESTIONS, definitions: Path = DEFINITIONS) -> QuestionSet:
    """The question set, each ``cases`` entry expanded into its own question, with the
    definitions each corpus adds."""
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    rubric = {str(k): float(v) for k, v in data['rubric'].items()}
    questions: list[Question] = []
    for entry in data['questions']:
        cases = entry.get('cases') or [{'answer': entry.get('answer')}]
        for case in cases:
            values = {k: str(v) for k, v in case.items() if k != 'answer'}
            answer = case.get('answer')
            if isinstance(answer, bool) or not isinstance(answer, int | float):
                raise ValueError(f'{entry["id"]}: the answer must be a number, not {answer!r}')
            unit = str(entry['unit'])
            if unit not in rubric:
                raise ValueError(f'{entry["id"]}: unit {unit!r} has no tolerance in the rubric')
            questions.append(
                Question(
                    id=entry['id'].format_map(values),
                    corpus=str(entry['corpus']),
                    trap=str(entry['trap']),
                    unit=unit,
                    text=_words(entry['question'].format_map(values)),
                    reference=entry['reference'].format_map(values).strip(),
                    answer=float(answer),
                    tolerance=rubric[unit],
                )
            )
    problems = [f'{q.id}: unknown corpus {q.corpus!r}' for q in questions if q.corpus not in CORPORA]
    problems += [f'{q.id}: unknown trap {q.trap!r}' for q in questions if q.trap not in TRAPS]
    ids = [q.id for q in questions]
    problems += [
        f'{i}: more than one question has this id' for i in sorted({i for i in ids if ids.count(i) > 1})
    ]
    if problems:
        raise ValueError('; '.join(problems))
    texts = {name: (definitions / f'{name}.yml').read_text(encoding='utf-8') for name in CORPORA}
    return QuestionSet(int(data['version']), str(data['fingerprint']), rubric, tuple(questions), texts)


# The rubric


@dataclass(frozen=True)
class Answer:
    """What the agent submitted: a number, or None to abstain, and any doubt it stated.
    ``submitted`` is False when the conversation ended without a submission."""

    value: float | None
    doubt: str = ''
    submitted: bool = True


@dataclass(frozen=True)
class Score:
    outcome: str
    confident: bool

    @property
    def confidently_wrong(self) -> bool:
        return self.outcome == 'wrong' and self.confident


def score(question: Question, answer: Answer) -> Score:
    """Correct within the question's tolerance, wrong otherwise, abstained when there is no
    number; confident when the agent stated no doubt."""
    confident = answer.submitted and not answer.doubt.strip()
    if answer.value is None:
        return Score('abstained', confident)
    correct = math.isfinite(answer.value) and abs(answer.value - question.answer) <= question.tolerance + 1e-9
    return Score('correct' if correct else 'wrong', confident)


def parse_submission(arguments: dict[str, Any]) -> Answer:
    """The ``submit_answer`` arguments as an answer, or ``ValueError`` saying what is wrong, which
    the agent reads and can correct. A number may come as a string with thousands separators."""
    value = arguments.get('value')
    doubt = arguments.get('doubt') or ''
    if not isinstance(doubt, str):
        raise ValueError('`doubt` must be a string')
    if value is None:
        return Answer(None, doubt)
    if isinstance(value, str):
        try:
            value = float(value.replace(',', '').strip())
        except ValueError:
            raise ValueError(f'`value` must be a number or null, not {value!r}') from None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f'`value` must be a number or null, not {value!r}')
    return Answer(float(value), doubt)


# The warehouse, read through verify's read-only engine


class Warehouse:
    """A corpus's DuckDB warehouse attached read only, with files, URLs and extensions off; only a
    single query that reads is run."""

    def __init__(self, path: Path) -> None:
        from konstruct.analysis.verify import DuckDBEngine, quote

        self.engine = DuckDBEngine(path)
        self.engine.run(f'USE {quote(self.engine.database)}')

    def query(self, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        from konstruct.analysis.verify import read_only_query

        read_only_query(sql)
        return self.engine.read(sql)

    def scalar(self, sql: str) -> float | None:
        names, rows = self.query(sql)
        if len(names) != 1 or len(rows) != 1:
            raise ValueError(f'a reference returns one value, not {len(rows)} rows of {len(names)} columns')
        value = rows[0][0]
        return None if value is None else float(value)

    def schema(self) -> str:
        """Every table and view with its columns and their types."""
        catalog = self.engine.database.replace("'", "''")
        _, rows = self.query(
            'SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns '
            f"WHERE table_catalog = '{catalog}' ORDER BY table_schema, table_name, ordinal_position"
        )
        tables: dict[str, list[str]] = {}
        for schema, table, column, dtype in rows:
            tables.setdefault(f'{schema}.{table}', []).append(f'{column} {dtype}')
        return '\n'.join(f'{name}({", ".join(columns)})' for name, columns in tables.items())

    def run_sql(self, sql: str) -> str:
        """A query's rows as the agent reads them: JSON, at most ``MAX_ROWS`` of them."""
        names, rows = self.query(sql)
        shown = [dict(zip(names, row, strict=True)) for row in rows[:MAX_ROWS]]
        text = '\n'.join(json.dumps(row, default=str) for row in shown)
        more = f'\n({len(rows)} rows; the first {MAX_ROWS} are shown)' if len(rows) > MAX_ROWS else ''
        return f'{text}{more}' if rows else '(no rows)'

    def close(self) -> None:
        self.engine.close()


@dataclass
class Check:
    question: str
    expected: float
    actual: float | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.actual is not None and abs(self.actual - self.expected) <= 1e-9


def check_references(questions: list[Question], warehouses: dict[str, Path]) -> list[Check]:
    """Run each question's reference on its warehouse and compare it with the committed answer."""
    checks: list[Check] = []
    for name, path in warehouses.items():
        warehouse = Warehouse(path)
        try:
            for question in (q for q in questions if q.corpus == name):
                try:
                    actual = warehouse.scalar(question.reference)
                except Exception as error:  # a broken reference is reported with the others
                    checks.append(
                        Check(question.id, question.answer, None, f'{type(error).__name__}: {error}')
                    )
                    continue
                checks.append(Check(question.id, question.answer, actual))
        finally:
            warehouse.close()
    return checks


# Building a corpus


def _dbt() -> str:
    beside = Path(sys.executable).parent / 'dbt'
    found = str(beside) if beside.is_file() else shutil.which('dbt')
    if found is None:
        raise SystemExit('dbt is not installed in this environment; run with `uv run`')
    return found


def prepare(corpus: Corpus, out: Path) -> Path:
    """The corpus as a built project under ``out``: its warehouse built by dbt, the benchmark's
    definitions added and the Konstruct graph built. A project already prepared is reused."""
    root = out / corpus.name
    ready = root / 'answer-benchmark' / 'ready'
    if ready.is_file():
        return root
    if root.exists():
        shutil.rmtree(root)
    if corpus.repository is None:
        shutil.copytree(
            ROOT / 'examples' / corpus.name,
            root,
            ignore=shutil.ignore_patterns('target', 'logs', 'konstruct-out', '*.duckdb', '*.duckdb.wal'),
        )
    else:
        subprocess.run(['git', 'clone', '--quiet', corpus.repository, str(root)], check=True)
        subprocess.run(['git', '-C', str(root), 'checkout', '--quiet', str(corpus.commit)], check=True)
    if corpus.profile is not None:
        (root / 'profiles.yml').write_text(corpus.profile, encoding='utf-8')
    (root / OVERLAY).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFINITIONS / f'{corpus.name}.yml', root / OVERLAY)
    env = {**os.environ, 'DBT_PROFILES_DIR': str(root), 'DBT_SEND_ANONYMOUS_USAGE_STATS': 'false'}
    dbt = _dbt()
    env['PATH'] = f'{Path(dbt).parent}{os.pathsep}{env.get("PATH", "")}'
    for step in corpus.dbt:
        subprocess.run([dbt, *step, '--quiet', '--no-version-check'], cwd=root, env=env, check=True)
    if corpus.after is not None:
        import duckdb

        con = duckdb.connect(str(root / corpus.warehouse))
        try:
            con.execute((root / corpus.after).read_text(encoding='utf-8'))
        finally:
            con.close()
    from konstruct import Project
    from konstruct.engine.config import load_config

    # The harness made this copy itself, so it trusts it: its warehouse connection included.
    Project(root).build(run_dbt=False, config=load_config(root, trusted=True))
    ready.write_text(corpus.commit or 'bundled', encoding='utf-8')
    return root


def definitions_text(root: Path) -> str:
    """Every definitions file Konstruct reads in the project, as the definitions arm is given them."""
    from konstruct.engine.config import discover_definitions_files, load_config

    files = discover_definitions_files(load_config(root, trusted=True))
    return '\n\n'.join(
        f'# {path.relative_to(root).as_posix()}\n{path.read_text(encoding="utf-8").strip()}'
        for path in sorted(files)
    )


# The arms


SYSTEM = """\
You answer business questions about a company from its data warehouse, a DuckDB database. Use the
run_sql tool to query it, one read-only query at a time.

Answer with the submit_answer tool, once, giving the number the question asks for in the unit it
asks for, without rounding unless the question says how to round. Submit null as the value when
the question cannot be answered from what you have. If you have any doubt that your number is what
the business means by the question, say what the doubt is in `doubt`; leave `doubt` empty only
when you are sure.

The warehouse's tables and views, with their columns and types:
{schema}
"""

DEFINITIONS_CONTEXT = """
The business's definitions, as its data team wrote them:
{definitions}
"""

KONSTRUCT_CONTEXT = """
The other tools are Konstruct's, over this project's dbt models, lineage, business definitions and
ontology. Konstruct's instructions:
{instructions}
"""

RUN_SQL: dict[str, Any] = {
    'name': 'run_sql',
    'description': f'Run one read-only DuckDB query on the warehouse; returns up to {MAX_ROWS} rows as JSON.',
    'input_schema': {
        'type': 'object',
        'properties': {'sql': {'type': 'string', 'description': 'A single SELECT query.'}},
        'required': ['sql'],
    },
}

SUBMIT_ANSWER: dict[str, Any] = {
    'name': 'submit_answer',
    'description': 'Submit the answer to the question. Call it once, when you are done.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'value': {
                'type': ['number', 'null'],
                'description': 'The number the question asks for, or null when it cannot be answered.',
            },
            'doubt': {
                'type': 'string',
                'description': 'Any doubt that the number is what the question means; empty when sure.',
            },
        },
        'required': ['value', 'doubt'],
    },
}


@dataclass
class Setting:
    """What one arm gives the agent on one corpus: the system prompt and the tools, and how a call
    to a tool other than ``submit_answer`` is answered."""

    arm: str
    corpus: str
    system: str
    tools: list[dict[str, Any]]
    call: Callable[[str, dict[str, Any]], Awaitable[tuple[str, bool]]]


def _truncate(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARACTERS:
        return text
    return text[:MAX_RESULT_CHARACTERS] + f'\n(cut at {MAX_RESULT_CHARACTERS} characters)'


def sql_caller(warehouse: Warehouse) -> Callable[[str, dict[str, Any]], Awaitable[tuple[str, bool]]]:
    async def call(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if name != 'run_sql':
            return f'there is no tool named {name}', True
        try:
            return _truncate(warehouse.run_sql(str(arguments.get('sql', '')))), False
        except Exception as error:  # the agent reads the error and can correct its query
            return f'{type(error).__name__}: {error}', True

    return call


def konstruct_caller(
    run_sql: Callable[[str, dict[str, Any]], Awaitable[tuple[str, bool]]], mcp: McpTools
) -> Callable[[str, dict[str, Any]], Awaitable[tuple[str, bool]]]:
    async def call(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if name == 'run_sql':
            return await run_sql(name, arguments)
        try:
            return await mcp.call(name, arguments)
        except Exception as error:  # an MCP error is the agent's to read, as any tool error
            return f'{type(error).__name__}: {error}', True

    return call


class McpTools:
    """Konstruct's MCP server over the corpus's graph, as a stdio subprocess."""

    def __init__(self, root: Path) -> None:
        from mcp.client.stdio import StdioServerParameters

        self.params = StdioServerParameters(
            command=sys.executable,
            args=['-m', 'konstruct.interfaces.cli', 'mcp', '--path', str(root), '--no-prompts'],
            env={**os.environ, 'KONSTRUCT_TRUST_CONFIG': '1'},
        )
        self.client: Any = None

    async def __aenter__(self) -> McpTools:
        from mcp import Client

        self.client = await Client(self.params).__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.__aexit__(*exc)

    async def tools(self) -> list[dict[str, Any]]:
        listing = await self.client.list_tools()
        return [
            {'name': t.name, 'description': t.description or '', 'input_schema': t.input_schema}
            for t in listing.tools
        ]

    @property
    def instructions(self) -> str:
        return str(self.client.instructions or '')

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        result = await self.client.call_tool(name, arguments)
        parts = [block.text for block in result.content if getattr(block, 'type', '') == 'text']
        if not parts and result.structured_content is not None:
            parts = [json.dumps(result.structured_content, default=str)]
        return _truncate('\n'.join(parts)), bool(result.is_error)


async def settings(
    corpus: str, root: Path, warehouse: Warehouse, arms: Iterable[str], mcp: McpTools | None
) -> dict[str, Setting]:
    """The setting of each arm on one corpus. ``mcp`` serves the konstruct arm."""
    schema = warehouse.schema()
    base = SYSTEM.format(schema=schema)
    run_sql = sql_caller(warehouse)
    made: dict[str, Setting] = {}
    for arm in arms:
        if arm == 'schema':
            made[arm] = Setting(arm, corpus, base, [RUN_SQL, SUBMIT_ANSWER], run_sql)
        elif arm == 'definitions':
            system = base + DEFINITIONS_CONTEXT.format(definitions=definitions_text(root))
            made[arm] = Setting(arm, corpus, system, [RUN_SQL, SUBMIT_ANSWER], run_sql)
        elif arm == 'konstruct':
            if mcp is None:
                raise ValueError('the konstruct arm needs the MCP server')
            system = base + KONSTRUCT_CONTEXT.format(instructions=mcp.instructions)
            tools = [RUN_SQL, SUBMIT_ANSWER, *await mcp.tools()]
            made[arm] = Setting(arm, corpus, system, tools, konstruct_caller(run_sql, mcp))
        else:
            raise ValueError(f'unknown arm {arm!r}')
    return made


@contextlib.asynccontextmanager
async def arm_settings(corpus: str, root: Path, arms: Iterable[str]) -> AsyncIterator[dict[str, Setting]]:
    """The settings of the arms on one corpus, with its warehouse open and, for the konstruct arm,
    Konstruct's MCP server running, until the block ends."""
    arms = list(arms)
    warehouse = Warehouse(root / CORPORA[corpus].warehouse)
    try:
        async with contextlib.AsyncExitStack() as stack:
            mcp = await stack.enter_async_context(McpTools(root)) if 'konstruct' in arms else None
            yield await settings(corpus, root, warehouse, arms, mcp)
    finally:
        warehouse.close()


# The conversation


@dataclass
class Call:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Turn:
    """One reply of the model: what to send back as the assistant's message, its tool calls, and
    a record of it for the transcript."""

    content: Any
    calls: list[Call]
    record: Any
    input_tokens: int = 0
    output_tokens: int = 0


class Model(Protocol):
    name: str

    def respond(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn: ...


class AnthropicModel:
    """A model on the Anthropic Messages API; the SDK reads the key from the environment."""

    def __init__(self, name: str) -> None:
        try:
            anthropic: Any = importlib.import_module('anthropic')
        except ImportError:
            raise SystemExit('a run needs the Anthropic SDK: uv run --with anthropic ...') from None
        self.name = name
        self.client = anthropic.Anthropic()

    def respond(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        response = self.client.messages.create(
            model=self.name,
            max_tokens=16000,
            system=system,
            messages=messages,
            tools=tools,
            # The system prompt and tools are the same for every question of an arm.
            cache_control={'type': 'ephemeral'},
        )
        calls = [Call(b.id, b.name, dict(b.input)) for b in response.content if b.type == 'tool_use']
        return Turn(
            content=response.content,
            calls=calls,
            record=[b.model_dump(mode='json') for b in response.content],
            input_tokens=int(response.usage.input_tokens),
            output_tokens=int(response.usage.output_tokens),
        )


@dataclass
class Transcript:
    answer: Answer
    steps: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


async def converse(
    model: Model, setting: Setting, question: Question, max_turns: int = MAX_TURNS
) -> Transcript:
    """Ask one question in one arm, answering tool calls until the agent submits an answer, stops
    calling tools or uses up ``max_turns`` replies."""
    messages: list[dict[str, Any]] = [{'role': 'user', 'content': question.text}]
    transcript = Transcript(Answer(None, submitted=False))
    while transcript.turns < max_turns:
        turn = model.respond(setting.system, messages, setting.tools)
        transcript.turns += 1
        transcript.input_tokens += turn.input_tokens
        transcript.output_tokens += turn.output_tokens
        transcript.steps.append({'assistant': turn.record})
        messages.append({'role': 'assistant', 'content': turn.content})
        if not turn.calls:
            break
        results = []
        submitted = False
        for call in turn.calls:
            if call.name == 'submit_answer':
                try:
                    transcript.answer = parse_submission(call.arguments)
                    output, error = 'Recorded.', False
                    submitted = True
                except ValueError as problem:
                    output, error = str(problem), True
            else:
                output, error = await setting.call(call.name, call.arguments)
            results.append(
                {'type': 'tool_result', 'tool_use_id': call.id, 'content': output, 'is_error': error}
            )
        transcript.steps.append({'tool_results': results})
        messages.append({'role': 'user', 'content': results})
        if submitted:
            break
    return transcript


# A run


@dataclass
class Result:
    question: str
    corpus: str
    arm: str
    trap: str
    expected: float
    outcome: str
    confident: bool
    value: float | None
    doubt: str
    submitted: bool
    turns: int
    seconds: float
    input_tokens: int
    output_tokens: int


def metrics(results: list[Result]) -> dict[str, Any]:
    """The counts and rates of a set of results; the confidently wrong rate is over every question."""
    total = len(results)
    counts = {o: sum(1 for r in results if r.outcome == o) for o in OUTCOMES}
    wrong = sum(1 for r in results if r.outcome == 'wrong' and r.confident)
    data: dict[str, Any] = {'questions': total, **counts, 'confidently_wrong': wrong}
    for key in (*OUTCOMES, 'confidently_wrong'):
        data[f'{key}_rate'] = round(data[key] / total, 4) if total else None
    return data


def summarise(results: list[Result], arms: Iterable[str]) -> dict[str, Any]:
    arms = [a for a in ARMS if a in set(arms)]
    by = {arm: [r for r in results if r.arm == arm] for arm in arms}
    return {
        'arms': {arm: metrics(rs) for arm, rs in by.items()},
        'by_trap': {
            trap: {arm: metrics([r for r in rs if r.trap == trap]) for arm, rs in by.items()}
            for trap in TRAPS
            if any(r.trap == trap for r in results)
        },
        'by_corpus': {
            corpus: {arm: metrics([r for r in rs if r.corpus == corpus]) for arm, rs in by.items()}
            for corpus in CORPORA
            if any(r.corpus == corpus for r in results)
        },
    }


def _percent(rate: float | None) -> str:
    return '-' if rate is None else f'{rate:.1%}'


def summary_markdown(summary: dict[str, Any], questions: QuestionSet, model: str) -> str:
    lines = [
        '# Confidently wrong answers to business questions',
        '',
        f'Model `{model}`, question set version {questions.version} (`{questions.fingerprint[:12]}`).',
        '',
        '| Arm | Questions | Correct | Wrong | Confidently wrong | Abstained | Confidently wrong rate |',
        '|---|---|---|---|---|---|---|',
    ]
    for arm, m in summary['arms'].items():
        lines.append(
            f'| {arm} | {m["questions"]} | {m["correct"]} | {m["wrong"]} | {m["confidently_wrong"]} | '
            f'{m["abstained"]} | {_percent(m["confidently_wrong_rate"])} |'
        )
    arms = list(summary['arms'])
    lines += ['', '| Trap | ' + ' | '.join(f'{a} correct, confidently wrong' for a in arms) + ' |']
    lines.append('|---|' + '---|' * len(arms))
    for trap, per_arm in summary['by_trap'].items():
        cells = [
            f'{_percent(m["correct_rate"])}, {_percent(m["confidently_wrong_rate"])}'
            for m in per_arm.values()
        ]
        lines.append(f'| {trap} | ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines) + '\n'


def _versions() -> dict[str, str]:
    from konstruct import __version__

    return {'konstruct': __version__, 'python': sys.version.split()[0]}


async def run(
    model: Model,
    questions: QuestionSet,
    selected: list[Question],
    corpora: dict[str, Path],
    arms: list[str],
    out: Path,
    *,
    max_turns: int = MAX_TURNS,
    command: str = '',
) -> dict[str, Any]:
    """Ask every selected question in every arm, appending each result to ``results.jsonl`` as
    it comes, and write the summary. Questions already answered there are skipped."""
    log = out / 'results.jsonl'
    results = [Result(**r) for r in read_jsonl(log)] if log.is_file() else []
    done = {(r.question, r.arm) for r in results}
    for name, root in corpora.items():
        async with arm_settings(name, root, arms) as made:
            for question in (q for q in selected if q.corpus == name):
                for arm in arms:
                    if (question.id, arm) in done:
                        continue
                    started = time.perf_counter()
                    transcript = await converse(model, made[arm], question, max_turns)
                    result = record(question, arm, transcript, time.perf_counter() - started)
                    results.append(result)
                    path = out / 'transcripts' / arm / f'{question.id}.json'
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(transcript.steps, indent=1, default=str), encoding='utf-8')
                    with log.open('a', encoding='utf-8') as f:
                        f.write(json.dumps(asdict(result)) + '\n')
                    print(f'{question.id} {arm}: {result.outcome}', file=sys.stderr, flush=True)
    wanted = {q.id for q in selected}
    summary = summarise([r for r in results if r.question in wanted], arms)
    document = {
        'command': command,
        'model': model.name,
        'versions': _versions(),
        'question_set': {'version': questions.version, 'fingerprint': questions.fingerprint},
        'max_turns': max_turns,
        **summary,
    }
    (out / 'results.json').write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
    (out / 'summary.md').write_text(summary_markdown(summary, questions, model.name), encoding='utf-8')
    return document


def record(question: Question, arm: str, transcript: Transcript, seconds: float) -> Result:
    graded = score(question, transcript.answer)
    return Result(
        question=question.id,
        corpus=question.corpus,
        arm=arm,
        trap=question.trap,
        expected=question.answer,
        outcome=graded.outcome,
        confident=graded.confident,
        value=transcript.answer.value,
        doubt=transcript.answer.doubt,
        submitted=transcript.answer.submitted,
        turns=transcript.turns,
        seconds=round(seconds, 1),
        input_tokens=transcript.input_tokens,
        output_tokens=transcript.output_tokens,
    )


# Plan and dry run


def plan(questions: QuestionSet, selected: list[Question], arms: list[str]) -> str:
    """What a run would ask, with no warehouse and no API."""
    lines = [
        f'Question set version {questions.version}, fingerprint {questions.fingerprint}',
        f'{len(selected)} questions in {len(arms)} arms ({", ".join(arms)}): '
        f'{len(selected) * len(arms)} conversations of at most {MAX_TURNS} model replies each',
        '',
    ]
    for corpus in CORPORA:
        mine = [q for q in selected if q.corpus == corpus]
        if not mine:
            continue
        traps = ', '.join(
            f'{t} {sum(1 for q in mine if q.trap == t)}' for t in TRAPS if any(q.trap == t for q in mine)
        )
        lines += [f'{corpus} ({CORPORA[corpus].title}): {len(mine)} questions; {traps}']
        lines += [f'  {q.id} [{q.trap}] {q.text} = {q.answer:g}' for q in mine]
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


async def dry_run(
    questions: list[Question], corpora: dict[str, Path], arms: list[str], out: Path
) -> list[Check]:
    """Check every reference answer and write each arm's prompt and tools, with no API."""
    checks = check_references(questions, {n: root / CORPORA[n].warehouse for n, root in corpora.items()})
    for name, root in corpora.items():
        async with arm_settings(name, root, arms) as made:
            for arm, setting in made.items():
                path = out / 'prompts' / arm / f'{name}.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                document = {
                    'system': setting.system,
                    'tools': setting.tools,
                    'questions': [q.text for q in questions if q.corpus == name],
                }
                path.write_text(json.dumps(document, indent=1) + '\n', encoding='utf-8')
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n', 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        '--plan', action='store_true', help='list the questions a run would ask; no warehouse or API'
    )
    mode.add_argument(
        '--dry-run',
        action='store_true',
        help='build the corpora, check every reference answer, write the prompts',
    )
    parser.add_argument(
        '--corpus', nargs='+', choices=list(CORPORA), help='only these corpora (default: all)'
    )
    parser.add_argument(
        '--arms', nargs='+', choices=ARMS, default=list(ARMS), help='the arms to run (default: all)'
    )
    parser.add_argument('--only', nargs='+', metavar='ID', help='only these questions')
    parser.add_argument(
        '--out', type=Path, help='where to write the run (default: a new temporary directory)'
    )
    parser.add_argument(
        '--max-turns', type=int, default=MAX_TURNS, help=f'model replies per question (default {MAX_TURNS})'
    )
    args = parser.parse_args(argv)

    questions = load_questions()
    selected = questions.for_corpora(args.corpus or CORPORA)
    if args.only:
        unknown = sorted(set(args.only) - {q.id for q in selected})
        if unknown:
            raise SystemExit(f'no such questions: {", ".join(unknown)}')
        selected = [q for q in selected if q.id in set(args.only)]
    arms = [a for a in ARMS if a in args.arms]
    if args.plan:
        print(plan(questions, selected, arms), end='')
        return 0
    if questions.recorded != questions.fingerprint:
        raise SystemExit(
            f'the question set changed: raise its version and record the fingerprint {questions.fingerprint}'
        )
    model_name = os.environ.get(MODEL_VARIABLE)
    if not args.dry_run and not model_name:
        raise SystemExit(f'set {MODEL_VARIABLE} to the model to run, or pass --plan or --dry-run')
    out: Path = (args.out or Path(tempfile.mkdtemp(prefix='konstruct-answer-benchmark-'))).resolve()
    out.mkdir(parents=True, exist_ok=True)
    needed = [c for c in CORPORA if any(q.corpus == c for q in selected)]
    corpora = {name: prepare(CORPORA[name], out / 'corpora') for name in needed}

    import anyio

    if args.dry_run:
        checks = anyio.run(dry_run, selected, corpora, arms, out)
        failed = [c for c in checks if not c.ok]
        for check in failed:
            print(
                f'{check.question}: expected {check.expected:g}, got {check.error or check.actual}',
                file=sys.stderr,
            )
        print(
            f'{out}\n{len(checks) - len(failed)} of {len(checks)} reference answers match the warehouse; '
            f'a run would hold {len(selected) * len(arms)} conversations'
        )
        return 1 if failed else 0

    config = {
        'model': model_name,
        'arms': arms,
        'max_turns': args.max_turns,
        'question_set': questions.fingerprint,
    }
    config_path = out / 'config.json'
    if config_path.is_file() and json.loads(config_path.read_text()) != config:
        raise SystemExit(f'{out} holds a run with other options ({config_path}); choose another --out')
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    failed = [
        c
        for c in check_references(selected, {n: r / CORPORA[n].warehouse for n, r in corpora.items()})
        if not c.ok
    ]
    if failed:
        raise SystemExit(
            f'{len(failed)} reference answers do not match the warehouse; run --dry-run to see them'
        )
    model = AnthropicModel(str(model_name))
    command = shlex.join(['answer_benchmark.py', *(argv if argv is not None else sys.argv[1:])])
    anyio.run(
        lambda: run(model, questions, selected, corpora, arms, out, max_turns=args.max_turns, command=command)
    )
    print(f'{out}\n\n' + (out / 'summary.md').read_text(encoding='utf-8'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
