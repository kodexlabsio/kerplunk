"""The answer benchmark: its question set, its rubric, its reference answers on DuckDB, and the
harness end to end with stub agents in place of a model."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import anyio
import pytest
import yaml

from .conftest import dbt_executable

SCRIPT = Path(__file__).resolve().parents[1] / '.kodex' / 'scripts' / 'answer_benchmark.py'


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location('answer_benchmark', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules['answer_benchmark'] = module
    spec.loader.exec_module(module)
    return module


ab = load()


@pytest.fixture(scope='module')
def corpora(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The bundled example prepared as a run prepares it: built by dbt, with the benchmark's
    definitions and the Konstruct graph. The directory holds it as ``corpora/jaffle_shop``."""
    if dbt_executable() is None:
        pytest.skip('dbt is not installed')
    out = tmp_path_factory.mktemp('answer-benchmark')
    ab.prepare(ab.CORPORA['jaffle_shop'], out / 'corpora')
    return out


def _with_corpora(corpora: Path, out: Path) -> Path:
    """A fresh output directory that already holds the prepared corpus, as a resumed run would."""
    shutil.copytree(corpora / 'corpora', out / 'corpora')
    return out


# The question set


def test_the_question_set_is_versioned() -> None:
    """The fingerprint recorded with the set is the set's own: changing a question, a reference, an
    answer, the rubric or a definitions file without recording a new version fails here."""
    questions = ab.load_questions()
    assert isinstance(questions.version, int) and questions.version >= 1
    assert questions.recorded == questions.fingerprint, (
        f'the question set changed: raise `version` and record fingerprint {questions.fingerprint}'
    )


def test_the_fingerprint_covers_answers_and_definitions(tmp_path: Path) -> None:
    data = yaml.safe_load(ab.QUESTIONS.read_text())
    definitions = tmp_path / 'definitions'
    shutil.copytree(ab.DEFINITIONS, definitions)
    original = ab.load_questions(ab.QUESTIONS, definitions).fingerprint

    data['questions'][0]['answer'] += 1
    changed = tmp_path / 'questions.yml'
    changed.write_text(yaml.safe_dump(data))
    assert ab.load_questions(changed, definitions).fingerprint != original

    (definitions / 'jaffle_shop.yml').write_text('version: 1\nterms: []\n')
    assert ab.load_questions(ab.QUESTIONS, definitions).fingerprint != original


def test_every_corpus_has_definition_and_join_traps() -> None:
    questions = ab.load_questions().questions
    for corpus in ab.CORPORA:
        traps = {q.trap for q in questions if q.corpus == corpus}
        assert {'control', 'fan-out', 'required-filter'} <= traps, corpus
        assert traps & {'definition', 'conflicting-column'}, corpus
    assert all(q.text and q.reference.lower().startswith(('select', 'with')) for q in questions)
    assert all('{' not in q.id + q.text + q.reference for q in questions)


def test_the_jaffle_shop_corpus_is_the_commit_the_real_world_test_pins() -> None:
    from tests.test_realworld import COMMIT, PROFILE, REPOSITORY

    corpus = ab.CORPORA['jaffle-shop']
    assert (corpus.repository, corpus.commit, corpus.profile) == (REPOSITORY, COMMIT, PROFILE)


@pytest.mark.parametrize(
    ('change', 'message'),
    [
        ({'corpus': 'elsewhere'}, 'unknown corpus'),
        ({'trap': 'surprise'}, 'unknown trap'),
        ({'unit': 'furlongs'}, 'no tolerance'),
        ({'answer': 'many'}, 'must be a number'),
        ({'answer': True}, 'must be a number'),
    ],
)
def test_a_bad_question_is_refused(tmp_path: Path, change: dict[str, Any], message: str) -> None:
    question = {
        'id': 'q',
        'corpus': 'jaffle_shop',
        'trap': 'control',
        'unit': 'count',
        'question': 'How many?',
        'reference': 'select 1',
        'answer': 1,
        **change,
    }
    path = tmp_path / 'questions.yml'
    path.write_text(
        yaml.safe_dump({'version': 1, 'fingerprint': '', 'rubric': {'count': 0}, 'questions': [question]})
    )
    with pytest.raises(ValueError, match=message):
        ab.load_questions(path)


def test_two_questions_with_one_id_are_refused(tmp_path: Path) -> None:
    question = {
        'id': 'q-{n}',
        'corpus': 'jaffle_shop',
        'trap': 'control',
        'unit': 'count',
        'question': 'How many {n}?',
        'reference': 'select {n}',
        'cases': [{'n': 1, 'answer': 1}, {'n': 1, 'answer': 1}],
    }
    path = tmp_path / 'questions.yml'
    path.write_text(
        yaml.safe_dump({'version': 1, 'fingerprint': '', 'rubric': {'count': 0}, 'questions': [question]})
    )
    with pytest.raises(ValueError, match='more than one question has this id'):
        ab.load_questions(path)


def test_cases_expand_into_questions() -> None:
    questions = {q.id: q for q in ab.load_questions().questions}
    april = questions['jaffle_shop.net-revenue-2018-04-01']
    assert april.text == 'What was net revenue from orders placed in April 2018, in Australian dollars?'
    assert "date '2018-04-01'" in april.reference and april.answer == 0 and april.tolerance == 0.01
    assert questions['jaffle_shop.completed-orders'].tolerance == 0


# The rubric


def _question(answer: float, unit: str = 'currency') -> Any:
    tolerance = {'currency': 0.01, 'percent': 0.05, 'count': 0.0}[unit]
    return ab.Question('q', 'jaffle_shop', 'control', unit, 'How much?', 'select 1', answer, tolerance)


@pytest.mark.parametrize(
    ('expected', 'unit', 'answer', 'outcome', 'confidently_wrong'),
    [
        (1103.0, 'currency', ab.Answer(1103.0), 'correct', False),
        (1103.0, 'currency', ab.Answer(1103.004), 'correct', False),
        (1103.0, 'currency', ab.Answer(1102.99), 'correct', False),
        (1103.0, 'currency', ab.Answer(1102.98), 'wrong', True),
        (1103.0, 'currency', ab.Answer(1672.0), 'wrong', True),
        # Cents for dollars.
        (1672.0, 'currency', ab.Answer(167200.0), 'wrong', True),
        (1672.0, 'currency', ab.Answer(167200.0, 'the amounts may be in cents'), 'wrong', False),
        (1672.0, 'currency', ab.Answer(1672.0, 'not sure returns count'), 'correct', False),
        (1672.0, 'currency', ab.Answer(1672.0, '   '), 'correct', False),
        (1672.0, 'currency', ab.Answer(None), 'abstained', False),
        (1672.0, 'currency', ab.Answer(None, 'no definition of revenue'), 'abstained', False),
        (1672.0, 'currency', ab.Answer(None, submitted=False), 'abstained', False),
        (1672.0, 'currency', ab.Answer(float('nan')), 'wrong', True),
        (0.0, 'currency', ab.Answer(0.0), 'correct', False),
        (100.0, 'count', ab.Answer(100.0), 'correct', False),
        (100.0, 'count', ab.Answer(99.0), 'wrong', True),
        (100.0, 'count', ab.Answer(62.0, 'customers who have ordered'), 'wrong', False),
        (6.1, 'percent', ab.Answer(6.06), 'correct', False),
        (6.1, 'percent', ab.Answer(6.15), 'correct', False),
        (6.1, 'percent', ab.Answer(0.061), 'wrong', True),
        (6.1, 'percent', ab.Answer(4.0), 'wrong', True),
    ],
)
def test_the_scorer_classifies_sample_answers(
    expected: float, unit: str, answer: Any, outcome: str, confidently_wrong: bool
) -> None:
    graded = ab.score(_question(expected, unit), answer)
    assert (graded.outcome, graded.confidently_wrong) == (outcome, confidently_wrong)


@pytest.mark.parametrize(
    ('arguments', 'answer'),
    [
        ({'value': 12, 'doubt': ''}, ab.Answer(12.0)),
        ({'value': 12.5}, ab.Answer(12.5)),
        ({'value': '1,672.00', 'doubt': 'cents?'}, ab.Answer(1672.0, 'cents?')),
        ({'value': None, 'doubt': 'cannot tell'}, ab.Answer(None, 'cannot tell')),
        ({'doubt': None}, ab.Answer(None)),
    ],
)
def test_a_submission_is_read(arguments: dict[str, Any], answer: Any) -> None:
    assert ab.parse_submission(arguments) == answer


@pytest.mark.parametrize(
    'arguments',
    [
        {'value': 'about 1600'},
        {'value': True},
        {'value': [1]},
        {'value': float('inf')},
        {'value': 1, 'doubt': 3},
    ],
)
def test_a_malformed_submission_is_refused(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match='must be'):
        ab.parse_submission(arguments)


def test_metrics_count_confidently_wrong_over_every_question() -> None:
    def result(outcome: str, confident: bool) -> Any:
        return ab.Result(
            'q', 'jaffle_shop', 'schema', 'control', 1.0, outcome, confident, 1.0, '', True, 1, 0, 0, 0
        )

    sample = [
        result('correct', True),
        result('wrong', True),
        result('wrong', False),
        result('abstained', False),
    ]
    data = ab.metrics(sample)
    assert data == {
        'questions': 4,
        'correct': 1,
        'wrong': 2,
        'abstained': 1,
        'confidently_wrong': 1,
        'correct_rate': 0.25,
        'wrong_rate': 0.5,
        'abstained_rate': 0.25,
        'confidently_wrong_rate': 0.25,
    }
    assert ab.metrics([])['confidently_wrong_rate'] is None


# The reference answers and the warehouse


def test_every_reference_answer_is_right_on_duckdb(corpora: Path) -> None:
    root = corpora / 'corpora' / 'jaffle_shop'
    questions = ab.load_questions().for_corpora(['jaffle_shop'])
    checks = ab.check_references(questions, {'jaffle_shop': root / 'jaffle_shop.duckdb'})
    assert len(checks) == len(questions) > 0
    assert [(c.question, c.expected, c.actual, c.error) for c in checks if not c.ok] == []


def test_a_wrong_reference_answer_is_reported(corpora: Path) -> None:
    question = ab.Question(
        'q', 'jaffle_shop', 'control', 'count', 'How many?', 'select count(*) from raw_orders', 1.0, 0
    )
    broken = ab.Question(
        'b', 'jaffle_shop', 'control', 'count', 'How many?', 'select * from raw_orders', 1.0, 0
    )
    warehouse = corpora / 'corpora' / 'jaffle_shop' / 'jaffle_shop.duckdb'
    first, second = ab.check_references([question, broken], {'jaffle_shop': warehouse})
    assert (first.ok, first.actual) == (False, 99.0)
    assert not second.ok and 'one value' in str(second.error)


def test_the_warehouse_only_reads(corpora: Path) -> None:
    warehouse = ab.Warehouse(corpora / 'corpora' / 'jaffle_shop' / 'jaffle_shop.duckdb')
    try:
        for sql in (
            'create table copied as select * from orders',
            'delete from orders',
            'select 1; select 2',
            "select * from read_csv('/etc/hostname')",
        ):
            with pytest.raises(Exception):  # noqa: B017 - refused by the parser or by DuckDB
                warehouse.run_sql(sql)
        rows = warehouse.run_sql('select * from raw_payments order by id')
        assert rows.count('\n') == ab.MAX_ROWS and 'the first 50 are shown' in rows
        assert warehouse.run_sql('select * from orders where false') == '(no rows)'
        schema = warehouse.schema()
        assert 'main.orders(order_id INTEGER' in schema and 'reporting.customer_ltv(' in schema
    finally:
        warehouse.close()


def test_the_benchmark_definitions_join_the_project(corpora: Path) -> None:
    root = corpora / 'corpora' / 'jaffle_shop'
    text = ab.definitions_text(root)
    assert '# definitions.yml\n' in text and '# answer-benchmark/definitions.yml\n' in text
    assert 'Net revenue' in text and 'Customer lifetime value' in text
    graph = json.loads((root / 'konstruct-out' / 'graph.json').read_text())
    assert graph['warnings'] == []
    from konstruct import Project

    [term] = Project(root).define('net revenue').terms
    assert term.filters == ["orders.status = 'completed'"]


# Stub agents in place of a model


class Oracle:
    """Replays each question's reference SQL through `run_sql`, then submits the row it read."""

    name = 'oracle'

    def __init__(self, questions: list[Any], tool: str = 'run_sql') -> None:
        self.references = {q.text: q.reference for q in questions}
        self.tool = tool

    def respond(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        assert {t['name'] for t in tools} >= {'run_sql', 'submit_answer'}
        if len(messages) == 1:
            sql = self.references[messages[0]['content']]
            return ab.Turn('querying', [ab.Call('1', 'run_sql', {'sql': sql})], [{'text': sql}], 10, 5)
        [result] = messages[-1]['content']
        value = next(iter(json.loads(result['content']).values()))
        return ab.Turn('done', [ab.Call('2', 'submit_answer', {'value': value, 'doubt': ''})], [], 10, 5)


class Scripted:
    """Gives each reply from a list of tool calls, whatever the question."""

    name = 'scripted'

    def __init__(self, replies: list[list[Any]]) -> None:
        self.replies = replies
        self.seen: list[list[dict[str, Any]]] = []

    def respond(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        self.seen.append(list(messages))
        index = sum(1 for m in messages if m['role'] == 'assistant')
        calls = self.replies[index] if index < len(self.replies) else []
        return ab.Turn(f'reply {index}', calls, [{'calls': [c.name for c in calls]}])


def _sql_setting(corpora: Path) -> tuple[Any, Any]:
    warehouse = ab.Warehouse(corpora / 'corpora' / 'jaffle_shop' / 'jaffle_shop.duckdb')
    setting = ab.Setting(
        'schema', 'jaffle_shop', 'system', [ab.RUN_SQL, ab.SUBMIT_ANSWER], ab.sql_caller(warehouse)
    )
    return warehouse, setting


def test_a_conversation_runs_tools_until_an_answer(corpora: Path) -> None:
    warehouse, setting = _sql_setting(corpora)
    question = _question(1103.0)
    model = Scripted(
        [
            [ab.Call('a', 'run_sql', {'sql': 'select nonsense from nowhere'})],
            [ab.Call('b', 'run_sql', {'sql': "select sum(amount) from orders where status = 'completed'"})],
            [ab.Call('c', 'submit_answer', {'value': 'lots', 'doubt': ''})],
            [ab.Call('d', 'submit_answer', {'value': 1103, 'doubt': ''})],
        ]
    )
    try:
        transcript = anyio.run(ab.converse, model, setting, question)
    finally:
        warehouse.close()
    assert transcript.answer == ab.Answer(1103.0) and transcript.turns == 4
    results = [step['tool_results'][0] for step in transcript.steps if 'tool_results' in step]
    assert [r['is_error'] for r in results] == [True, False, True, False]
    assert 'Catalog Error' in results[0]['content'] or 'nowhere' in results[0]['content']
    assert results[1]['content'] == '{"sum(amount)": 1103.0}'
    assert ab.score(question, transcript.answer).outcome == 'correct'


def test_a_conversation_that_stops_without_answering_abstains(corpora: Path) -> None:
    warehouse, setting = _sql_setting(corpora)
    try:
        silent = anyio.run(ab.converse, Scripted([]), setting, _question(1.0))
        endless = Scripted([[ab.Call(str(i), 'run_sql', {'sql': 'select 1'})] for i in range(5)])
        cut = anyio.run(ab.converse, endless, setting, _question(1.0), 3)
    finally:
        warehouse.close()
    assert silent.answer == ab.Answer(None, submitted=False) and silent.turns == 1
    assert cut.turns == 3 and not cut.answer.submitted
    assert ab.score(_question(1.0), cut.answer).outcome == 'abstained'


def test_an_unknown_tool_is_an_error_the_agent_reads(corpora: Path) -> None:
    warehouse, setting = _sql_setting(corpora)
    try:
        output = anyio.run(setting.call, 'define', {'ref': 'revenue'})
    finally:
        warehouse.close()
    assert output == ('there is no tool named define', True)


def test_a_run_scores_every_arm_and_resumes(corpora: Path, tmp_path: Path) -> None:
    out = _with_corpora(corpora, tmp_path)
    questions = ab.load_questions()
    selected = questions.for_corpora(['jaffle_shop'])[:4]
    root = out / 'corpora' / 'jaffle_shop'
    document = anyio.run(
        lambda: ab.run(
            Oracle(selected), questions, selected, {'jaffle_shop': root}, list(ab.ARMS), out, command='x'
        )
    )
    assert document['model'] == 'oracle' and document['command'] == 'x'
    assert document['question_set'] == {'version': questions.version, 'fingerprint': questions.fingerprint}
    for arm in ab.ARMS:
        assert document['arms'][arm]['correct'] == 4 and document['arms'][arm]['confidently_wrong_rate'] == 0
    lines = (out / 'results.jsonl').read_text().splitlines()
    assert len(lines) == 12 and json.loads(lines[0])['input_tokens'] == 20
    assert (out / 'transcripts' / 'konstruct' / f'{selected[0].id}.json').is_file()
    summary = (out / 'summary.md').read_text()
    assert '| konstruct | 4 | 4 | 0 | 0 | 0 | 0.0% |' in summary
    assert json.loads((out / 'results.json').read_text()) == document

    # A model that answers every question wrongly is not asked again what is already answered.
    wrong = Scripted([[ab.Call('1', 'submit_answer', {'value': -1, 'doubt': ''})]])
    more = questions.for_corpora(['jaffle_shop'])[:5]
    again = anyio.run(lambda: ab.run(wrong, questions, more, {'jaffle_shop': root}, ['schema'], out))
    assert len(wrong.seen) == 1
    assert again['arms']['schema']['questions'] == 5 and again['arms']['schema']['confidently_wrong'] == 1
    assert again['by_trap'] and set(again['by_corpus']) == {'jaffle_shop'}


def test_the_konstruct_arm_reaches_konstruct_over_mcp(corpora: Path, tmp_path: Path) -> None:
    out = _with_corpora(corpora, tmp_path)
    root = out / 'corpora' / 'jaffle_shop'
    question = ab.load_questions().for_corpora(['jaffle_shop'])[0]
    model = Scripted(
        [
            [
                ab.Call('1', 'define', {'ref': 'net revenue'}),
                ab.Call('2', 'describe', {'ref': 'no_such_table'}),
            ],
            [ab.Call('3', 'submit_answer', {'value': question.answer, 'doubt': ''})],
        ]
    )

    async def main() -> Any:
        async with ab.arm_settings('jaffle_shop', root, ['konstruct']) as made:
            setting = made['konstruct']
            assert 'Konstruct answers questions' in setting.system
            assert {'define', 'explain', 'ontology', 'run_sql', 'submit_answer'} <= {
                t['name'] for t in setting.tools
            }
            return await ab.converse(model, setting, question)

    transcript = anyio.run(main)
    [define, describe] = transcript.steps[1]['tool_results']
    assert not define['is_error'] and "orders.status = 'completed'" in define['content']
    assert describe['is_error']
    assert ab.score(question, transcript.answer).outcome == 'correct'


def test_each_arm_sees_what_it_should(corpora: Path) -> None:
    root = corpora / 'corpora' / 'jaffle_shop'

    async def main() -> dict[str, Any]:
        async with ab.arm_settings('jaffle_shop', root, ['schema', 'definitions']) as made:
            return dict(made)

    made = anyio.run(main)
    schema, definitions = made['schema'], made['definitions']
    assert 'main.orders(' in schema.system and 'Net revenue' not in schema.system
    assert definitions.system.startswith(schema.system) and 'Net revenue' in definitions.system
    assert (
        [t['name'] for t in schema.tools]
        == [t['name'] for t in definitions.tools]
        == ['run_sql', 'submit_answer']
    )
    with pytest.raises(ValueError, match='needs the MCP server'):
        anyio.run(
            ab.settings, 'jaffle_shop', root, ab.Warehouse(root / 'jaffle_shop.duckdb'), ['konstruct'], None
        )


# The command line


def test_the_plan_lists_every_question_without_a_warehouse(capsys: pytest.CaptureFixture[str]) -> None:
    assert ab.main(['--plan']) == 0
    out = capsys.readouterr().out
    questions = ab.load_questions()
    assert f'fingerprint {questions.fingerprint}' in out
    assert f'{len(questions.questions)} questions in 3 arms' in out
    assert all(q.id in out for q in questions.questions)
    ab.main(['--plan', '--corpus', 'jaffle-shop', '--arms', 'schema'])
    assert 'questions in 1 arms (schema)' in capsys.readouterr().out


def test_a_run_needs_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ab.MODEL_VARIABLE, raising=False)
    with pytest.raises(SystemExit, match=ab.MODEL_VARIABLE):
        ab.main(['--corpus', 'jaffle_shop'])
    with pytest.raises(SystemExit, match='no such questions'):
        ab.main(['--plan', '--only', 'jaffle_shop.nothing'])


def test_a_changed_question_set_is_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    questions = ab.load_questions()
    monkeypatch.setattr(
        ab, 'load_questions', lambda: ab.QuestionSet(1, 'stale', questions.rubric, questions.questions, {})
    )
    with pytest.raises(SystemExit, match='raise its version'):
        ab.main(['--dry-run'])


def test_the_dry_run_checks_answers_and_writes_prompts(
    corpora: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _with_corpora(corpora, tmp_path)
    assert ab.main(['--dry-run', '--corpus', 'jaffle_shop', '--out', str(out)]) == 0
    count = len(ab.load_questions().for_corpora(['jaffle_shop']))
    assert f'{count} of {count} reference answers match the warehouse' in capsys.readouterr().out
    for arm in ab.ARMS:
        prompt = json.loads((out / 'prompts' / arm / 'jaffle_shop.json').read_text())
        assert len(prompt['questions']) == count and prompt['tools'][0]['name'] == 'run_sql'


def test_a_full_run_from_the_command_line(
    corpora: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command line with the Anthropic SDK replaced by a stub that answers every question with
    null: the model and key come from the environment, and the options are kept for a resume."""
    out = _with_corpora(corpora, tmp_path)
    created: list[dict[str, Any]] = []

    def create(**request: Any) -> Any:
        created.append(request)
        block = SimpleNamespace(
            type='tool_use',
            id='t',
            name='submit_answer',
            input={'value': None, 'doubt': 'no definition'},
            model_dump=lambda mode: {'type': 'tool_use'},
        )
        return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=7, output_tokens=3))

    stub = ModuleType('anthropic')
    stub.Anthropic = lambda: SimpleNamespace(messages=SimpleNamespace(create=create))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'anthropic', stub)
    monkeypatch.setenv(ab.MODEL_VARIABLE, 'model-under-test')
    argv = [
        '--corpus',
        'jaffle_shop',
        '--arms',
        'schema',
        '--only',
        'jaffle_shop.customers',
        '--out',
        str(out),
    ]
    assert ab.main(argv) == 0
    assert '| schema | 1 | 0 | 0 | 0 | 1 | 0.0% |' in capsys.readouterr().out
    assert created[0]['model'] == 'model-under-test' and created[0]['messages'][0]['content'].startswith(
        'How many'
    )
    document = json.loads((out / 'results.json').read_text())
    assert document['command'].startswith('answer_benchmark.py --corpus jaffle_shop')
    monkeypatch.setenv(ab.MODEL_VARIABLE, 'another-model')
    with pytest.raises(SystemExit, match='holds a run with other options'):
        ab.main(argv)
    monkeypatch.setitem(sys.modules, 'anthropic', None)
    with pytest.raises(SystemExit, match='needs the Anthropic SDK'):
        ab.AnthropicModel('m')


@pytest.mark.realworld
def test_every_reference_answer_is_right_on_jaffle_shop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """dbt Labs' jaffle-shop, cloned and built: needs network access, so it runs with the real-world
    tests (KONSTRUCT_REALWORLD=1), as the dry run CI makes."""
    if not os.environ.get('KONSTRUCT_REALWORLD'):
        pytest.skip('set KONSTRUCT_REALWORLD=1 to run (needs network access)')
    assert ab.main(['--dry-run', '--corpus', 'jaffle-shop', '--out', str(tmp_path)]) == 0
    count = len(ab.load_questions().for_corpora(['jaffle-shop']))
    assert f'{count} of {count} reference answers match the warehouse' in capsys.readouterr().out
    graph = json.loads((tmp_path / 'corpora' / 'jaffle-shop' / 'konstruct-out' / 'graph.json').read_text())
    assert [w for w in graph['warnings'] if w.startswith('definitions:')] == []


# The paper


def _paper_generator() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / 'paper' / 'generate.py'
    spec = importlib.util.spec_from_file_location('paper_generate', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules['paper_generate'] = module
    spec.loader.exec_module(module)
    return module


def test_the_paper_counts_the_questions_and_runs_the_benchmark_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate = _paper_generator()
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> None:
        calls.append(command)
        arm = {'correct': 3, 'wrong': 1, 'abstained': 0, 'confidently_wrong': 1}
        rates = {f'{k}_rate': v / 4 for k, v in arm.items()}
        results = {'model': 'model_x', 'arms': {a: {**arm, **rates} for a in ab.ARMS}}
        (Path(command[command.index('--out') + 1]) / 'results.json').write_text(json.dumps(results))

    monkeypatch.setattr(generate.subprocess, 'run', fake_run)
    monkeypatch.delenv(generate.ANSWER_BENCHMARK_FLAG, raising=False)
    numbers = generate.Numbers()
    generate.answers(numbers)
    assert calls == []
    assert numbers.values['answerQuestions']['value'] == str(len(ab.load_questions().questions))
    assert numbers.values['answerArms']['value'] == '3' and 'answerKonstructCorrect' not in numbers.values

    monkeypatch.setenv(generate.ANSWER_BENCHMARK_FLAG, '1')
    generate.answers(numbers)
    assert len(calls) == 1 and calls[0][1].endswith('answer_benchmark.py')
    assert numbers.values['answerModel']['value'] == r'model\_x'
    assert numbers.values['answerKonstructConfidentlyWrongPct']['value'] == '25.0'
    assert numbers.values['answerSchemaCorrect']['value'] == '3'
    numbers.write(tmp_path)
    assert r'\newcommand{\answerDefinitionsAbstainedPct}{0.0}' in (tmp_path / 'numbers.tex').read_text()
