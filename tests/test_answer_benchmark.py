"""The answer benchmark: its question set, its rubric, its reference answers and traps on DuckDB,
and the harness end to end with stub agents in place of a model."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import shutil
import subprocess
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


def _root(corpora: Path) -> Path:
    return corpora / 'corpora' / 'jaffle_shop'


# The question set


def test_the_question_set_is_versioned() -> None:
    """The fingerprint recorded with the set is the set's own: changing a question, a reference, an
    answer, the rubric, a definitions file or a corpus pin without recording a new version fails
    here."""
    questions = ab.load_questions()
    assert isinstance(questions.version, int) and questions.version >= 1
    assert questions.recorded == questions.fingerprint, (
        f'the question set changed: raise `version` and record fingerprint {questions.fingerprint}'
    )


def test_the_fingerprint_covers_answers_definitions_and_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    shutil.copyfile(ab.DEFINITIONS / 'jaffle_shop.yml', definitions / 'jaffle_shop.yml')

    # The example's own definitions, which the definitions arm reads, and the corpus pins.
    own = ab.load_questions(ab.QUESTIONS, definitions).definitions
    assert own['jaffle_shop/definitions.yml'].startswith('# Business definitions for the jaffle_shop')
    pinned = dataclasses.replace(ab.CORPORA['jaffle-shop'], commit='0' * 40)
    monkeypatch.setitem(ab.CORPORA, 'jaffle-shop', pinned)
    assert ab.load_questions(ab.QUESTIONS, definitions).fingerprint != original
    monkeypatch.setitem(
        ab.CORPORA, 'jaffle-shop', dataclasses.replace(pinned, commit=ab.CORPORA['jaffle-shop'].commit)
    )
    after = ab.load_questions(ab.QUESTIONS, definitions).corpora['jaffle_shop']['after_sql']
    assert 'create or replace table reporting.customer_ltv' in after


def test_the_harness_digest_and_the_commit_are_recorded() -> None:
    digest = ab.harness_digest()
    assert digest == ab.harness_digest() and len(digest) == 64
    assert ab.git_commit()['commit']


def test_every_corpus_has_definition_and_join_traps() -> None:
    questions = ab.load_questions().questions
    for corpus in ab.CORPORA:
        traps = {q.trap for q in questions if q.corpus == corpus}
        assert {'control', 'fan-out', 'definition'} <= traps, corpus
        assert traps & {'required-filter', 'conflicting-column'}, corpus
    assert all(q.text and q.reference.lower().startswith(('select', 'with')) for q in questions)
    assert all('{' not in q.id + q.text + q.reference + (q.naive or '') for q in questions)
    assert all((q.trap == 'control') == (q.naive is None) for q in questions)


def test_the_jaffle_shop_corpus_is_the_commit_the_real_world_test_pins() -> None:
    from tests.test_realworld import COMMIT, PROFILE, REPOSITORY

    corpus = ab.CORPORA['jaffle-shop']
    assert (corpus.repository, corpus.commit, corpus.profile) == (REPOSITORY, COMMIT, PROFILE)


def _write_set(path: Path, question: dict[str, Any]) -> Path:
    path.write_text(
        yaml.safe_dump({'version': 1, 'fingerprint': '', 'rubric': {'count': 0}, 'questions': [question]})
    )
    return path


@pytest.mark.parametrize(
    ('change', 'message'),
    [
        ({'corpus': 'elsewhere'}, 'unknown corpus'),
        ({'trap': 'surprise', 'naive': 'select 2'}, 'unknown trap'),
        ({'unit': 'furlongs'}, 'no tolerance'),
        ({'answer': 'many'}, 'must be a number'),
        ({'answer': True}, 'must be a number'),
        ({'trap': 'fan-out'}, 'every trap has one'),
        ({'naive': 'select 2'}, 'a control has no naive query'),
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
    with pytest.raises(ValueError, match=message):
        ab.load_questions(_write_set(tmp_path / 'questions.yml', question))


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
    with pytest.raises(ValueError, match='more than one question has this id'):
        ab.load_questions(_write_set(tmp_path / 'questions.yml', question))


def test_cases_expand_into_questions() -> None:
    questions = {q.id: q for q in ab.load_questions().questions}
    april = questions['jaffle_shop.net-revenue-2018-04-01']
    assert april.text == (
        'What was net revenue from orders placed in April 2018, in Australian dollars (zero if there was none)?'
    )
    assert april.template == 'jaffle_shop.net-revenue-{month}'
    assert "date '2018-04-01'" in april.reference and "date '2018-04-01'" in str(april.naive)
    assert april.answer == 0 and april.tolerance == 0.01
    assert questions['jaffle_shop.completed-orders'].tolerance == 0


# The rubric


def _question(answer: float, unit: str = 'currency') -> Any:
    tolerance = {'currency': 0.01, 'percent': 0.05, 'count': 0.0}[unit]
    return ab.Question(
        'q', 'q', 'jaffle_shop', 'control', unit, 'How much?', 'select 1', None, answer, tolerance
    )


@pytest.mark.parametrize(
    ('expected', 'unit', 'answer', 'outcome', 'confidently_wrong'),
    [
        (1103.0, 'currency', ab.Answer(1103.0, True), 'correct', False),
        (1103.0, 'currency', ab.Answer(1103.004, True), 'correct', False),
        (1103.0, 'currency', ab.Answer(1102.99, True), 'correct', False),
        (1103.0, 'currency', ab.Answer(1102.98, True), 'wrong', True),
        (1103.0, 'currency', ab.Answer(1672.0, True), 'wrong', True),
        # Cents for dollars.
        (1672.0, 'currency', ab.Answer(167200.0, True), 'wrong', True),
        (1672.0, 'currency', ab.Answer(167200.0, False, 'the amounts may be in cents'), 'wrong', False),
        # A doubt that is no doubt does not hedge an answer the agent said it was sure of.
        (1672.0, 'currency', ab.Answer(167200.0, True, 'None'), 'wrong', True),
        (1672.0, 'currency', ab.Answer(167200.0, True, 'N/A'), 'wrong', True),
        (1672.0, 'currency', ab.Answer(167200.0, True, 'No doubt'), 'wrong', True),
        (1672.0, 'currency', ab.Answer(167200.0, False), 'wrong', False),
        (1672.0, 'currency', ab.Answer(1672.0, False, 'not sure returns count'), 'correct', False),
        (1672.0, 'currency', ab.Answer(None, False, 'no definition of revenue'), 'abstained', False),
        (1672.0, 'currency', ab.Answer(None, ending='turn-limit'), 'unanswered', False),
        (1672.0, 'currency', ab.Answer(None, ending='refusal'), 'unanswered', False),
        (1672.0, 'currency', ab.Answer(float('nan'), True), 'wrong', True),
        (0.0, 'currency', ab.Answer(0.0, True), 'correct', False),
        (100.0, 'count', ab.Answer(100.0, True), 'correct', False),
        (100.0, 'count', ab.Answer(99.0, True), 'wrong', True),
        (100.0, 'count', ab.Answer(62.0, False, 'customers who have ordered'), 'wrong', False),
        (6.1, 'percent', ab.Answer(6.06, True), 'correct', False),
        (6.1, 'percent', ab.Answer(6.15, True), 'correct', False),
        (6.1, 'percent', ab.Answer(0.061, True), 'wrong', True),
        (6.1, 'percent', ab.Answer(4.0, True), 'wrong', True),
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
        ({'value': 12, 'sure': True}, ab.Answer(12.0, True)),
        ({'value': 12.5, 'sure': False, 'doubt': 'grain'}, ab.Answer(12.5, False, 'grain')),
        ({'value': '1,672.00', 'sure': False, 'doubt': 'cents?'}, ab.Answer(1672.0, False, 'cents?')),
        ({'value': None, 'sure': False, 'doubt': 'cannot tell'}, ab.Answer(None, False, 'cannot tell')),
        ({'sure': True, 'doubt': None}, ab.Answer(None, True)),
    ],
)
def test_a_submission_is_read(arguments: dict[str, Any], answer: Any) -> None:
    assert ab.parse_submission(arguments) == answer


@pytest.mark.parametrize(
    'arguments',
    [
        {'value': 'about 1600', 'sure': True},
        {'value': True, 'sure': True},
        {'value': [1], 'sure': True},
        {'value': float('inf'), 'sure': True},
        {'value': 1, 'sure': True, 'doubt': 3},
        {'value': 1},
        {'value': 1, 'sure': 'yes'},
    ],
)
def test_a_malformed_submission_is_refused(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match='must be'):
        ab.parse_submission(arguments)


def _result(outcome: str, confident: bool, ending: str = 'submitted', template: str = 't') -> Any:
    return ab.Result(
        'q', template, 'jaffle_shop', 'schema', 1, 'control', 1.0, outcome, confident, ending, 1.0, confident,
        '', 1, 0, 0, 0, 'g',
    )  # fmt: skip


def test_metrics_count_confidently_wrong_over_every_conversation() -> None:
    sample = [
        _result('correct', True),
        _result('wrong', True),
        _result('wrong', False),
        _result('abstained', False),
        _result('unanswered', False, 'turn-limit'),
    ]
    data = ab.metrics(sample)
    assert {k: data[k] for k in ('conversations', *ab.OUTCOMES, 'confidently_wrong')} == {
        'conversations': 5,
        'correct': 1,
        'wrong': 2,
        'abstained': 1,
        'unanswered': 1,
        'confidently_wrong': 1,
    }
    assert data['confidently_wrong_rate'] == 0.2 and data['endings'] == {'turn-limit': 1}
    low, high = data['confidently_wrong_interval']
    assert 0 < low < 0.2 < high < 1
    assert ab.metrics([])['confidently_wrong_rate'] is None and ab.wilson(0, 0) is None
    assert ab.wilson(0, 10)[0] == 0 and ab.wilson(10, 10)[1] == 1


def test_results_are_reported_by_template() -> None:
    summary = ab.summarise(
        [_result('correct', True, template='a'), _result('wrong', True, template='b')], ['schema']
    )
    assert list(summary['by_template']) == ['a', 'b']
    assert summary['by_template']['b']['schema']['confidently_wrong'] == 1


# The reference answers, the traps and the warehouse


def test_every_reference_answer_is_right_and_every_trap_bites_on_duckdb(corpora: Path) -> None:
    questions = ab.load_questions().for_corpora(['jaffle_shop'])
    checks = ab.check_references(questions, {'jaffle_shop': _root(corpora) / 'jaffle_shop.duckdb'})
    assert len(checks) == len(questions) > 0
    assert ab._failures(checks) == []
    assert sum(c.trap for c in checks) == sum(q.trap != 'control' for q in questions) > 0


def test_a_wrong_reference_or_a_trap_that_does_not_bite_is_reported(corpora: Path) -> None:
    def question(reference: str, answer: float, naive: str | None = None) -> Any:
        return ab.Question(
            'q', 'q', 'jaffle_shop', 'fan-out', 'count', 'How many?', reference, naive, answer, 0
        )

    warehouse = _root(corpora) / 'jaffle_shop.duckdb'
    checks = ab.check_references(
        [
            question('select count(*) from raw_orders', 1.0),
            question('select * from raw_orders', 1.0),
            question('select count(*) from raw_orders', 99.0, 'select count(*) from orders'),
            question('select count(*) from raw_orders', 99.0, 'select count(*) * 2 from orders'),
        ],
        {'jaffle_shop': warehouse},
    )
    assert (checks[0].ok, checks[0].actual) == (False, 99.0)
    assert not checks[1].ok and 'one value' in str(checks[1].error)
    assert checks[2].ok and not checks[2].bites
    assert checks[3].ok and checks[3].bites
    failures = ab._failures(checks)
    assert [line.split(': ', 1)[1][:22] for line in failures] == [
        'expected 1, got 99.0',
        'expected 1, got ValueE',
        'the trap does not bite',
    ]


def test_the_warehouse_only_reads_and_stops_slow_queries(
    corpora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warehouse = ab.Warehouse(_root(corpora) / 'jaffle_shop.duckdb')
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
        assert rows.count('\n') == ab.MAX_ROWS and 'more than 50 rows' in rows
        assert warehouse.run_sql('select * from orders where false') == '(no rows)'
        schema = warehouse.schema()
        assert 'main.orders(order_id INTEGER' in schema and 'reporting.customer_ltv(' in schema
        monkeypatch.setattr(ab, 'QUERY_SECONDS', 0.2)
        with pytest.raises(TimeoutError, match='was stopped'):
            warehouse.run_sql('select count(*) from range(10000000000) a, range(1000) b')
        assert warehouse.run_sql('select 1 as one') == '{"one": 1}'
        limit = warehouse.run_sql("select current_setting('memory_limit') as limit")
        assert limit in ('{"limit": "1.8 GiB"}', '{"limit": "1.9 GiB"}'), limit
        with pytest.raises(Exception, match='locked'):
            warehouse.engine.con.execute("SET memory_limit = '100GB'")
    finally:
        warehouse.close()


def test_the_benchmark_definitions_join_the_project(corpora: Path) -> None:
    root = _root(corpora)
    text = ab.definitions_text(root)
    assert '# definitions.yml\n' in text and '# answer-benchmark/definitions.yml\n' in text
    assert 'Net revenue' in text and 'Customer lifetime value' in text
    graph = json.loads((root / 'konstruct-out' / 'graph.json').read_text())
    assert graph['warnings'] == []
    from konstruct import Project

    [term] = Project(root).define('net revenue').terms
    assert term.filters == ["orders.status = 'completed'"]


def test_a_prepared_corpus_is_rebuilt_when_the_question_set_or_harness_changed(
    corpora: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corpus prepared from other definitions or another harness would give the arms the old
    definitions and graph under the new fingerprint, so it is built again."""
    out = _with_corpora(corpora, tmp_path)
    root = _root(out)
    ready = json.loads((root / ab.READY).read_text())
    assert (
        ready['question_set'] == ab.load_questions().fingerprint and ready['harness'] == ab.harness_digest()
    )
    marker = root / 'answer-benchmark' / 'marker'
    marker.write_text('from the old preparation')
    assert ab.prepare(ab.CORPORA['jaffle_shop'], out / 'corpora') == root and marker.exists()
    monkeypatch.setattr(ab, 'stamp', lambda: {'question_set': 'new', 'harness': ready['harness']})
    ab.prepare(ab.CORPORA['jaffle_shop'], out / 'corpora')
    assert not marker.exists()
    assert json.loads((root / ab.READY).read_text())['question_set'] == 'new'


def test_a_prepared_corpus_is_rebuilt_when_its_graph_changed(corpora: Path, tmp_path: Path) -> None:
    out = _with_corpora(corpora, tmp_path)
    root = _root(out)
    graph = root / 'konstruct-out' / 'graph.json'
    graph.write_text(graph.read_text().replace('"jaffle_shop"', '"changed"', 1))
    assert ab.prepare(ab.CORPORA['jaffle_shop'], out / 'corpora') == root
    assert ab.graph_digest(root) == ab.prepared_graph(root)
    assert '"changed"' not in graph.read_text()


# Stub agents in place of a model


class Oracle:
    """Replays each question's reference SQL through `run_sql`, then submits the row it read."""

    name = 'oracle'

    def __init__(self, questions: list[Any]) -> None:
        self.references = {q.text: q.reference for q in questions}

    def settings(self) -> dict[str, Any]:
        return {'effort': 'none'}

    def respond(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        assert {t['name'] for t in tools} >= {'run_sql', 'submit_answer'}
        if len(messages) == 1:
            sql = self.references[messages[0]['content']]
            return ab.Turn(
                'querying', [ab.Call('1', 'run_sql', {'sql': sql})], [{'text': sql}], 10, 5, 'tool_use'
            )
        [result] = messages[-1]['content']
        value = next(iter(json.loads(result['content']).values()))
        return ab.Turn('done', [ab.Call('2', 'submit_answer', {'value': value, 'sure': True})], [], 10, 5)


class Scripted:
    """Gives each reply from a list of tool calls, whatever the question."""

    name = 'scripted'

    def __init__(self, replies: list[list[Any]], stop_reason: str = 'end_turn') -> None:
        self.replies = replies
        self.stop_reason = stop_reason
        self.seen: list[list[dict[str, Any]]] = []

    def settings(self) -> dict[str, Any]:
        return {}

    def respond(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        self.seen.append(list(messages))
        index = sum(1 for m in messages if m['role'] == 'assistant')
        calls = self.replies[index] if index < len(self.replies) else []
        stop = 'tool_use' if calls else self.stop_reason
        return ab.Turn(f'reply {index}', calls, [{'calls': [c.name for c in calls]}], stop_reason=stop)


def _sql_setting(corpora: Path) -> tuple[Any, Any]:
    warehouse = ab.Warehouse(_root(corpora) / 'jaffle_shop.duckdb')
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
            [ab.Call('c', 'submit_answer', {'value': 'lots', 'sure': True})],
            [ab.Call('d', 'submit_answer', {'value': 1103, 'sure': True})],
        ]
    )
    try:
        transcript = anyio.run(ab.converse, model, setting, question)
    finally:
        warehouse.close()
    assert transcript.answer == ab.Answer(1103.0, True) and transcript.turns == 4
    results = [step['tool_results'][0] for step in transcript.steps if 'tool_results' in step]
    assert [r['is_error'] for r in results] == [True, False, True, False]
    assert 'nowhere' in results[0]['content']
    assert results[1]['content'] == '{"sum(amount)": 1103.0}'
    assert ab.score(question, transcript.answer).outcome == 'correct'


@pytest.mark.parametrize(
    ('stop_reason', 'ending'),
    [('end_turn', 'no-tool-call'), ('refusal', 'refusal'), ('max_tokens', 'max-tokens')],
)
def test_a_conversation_that_ends_without_a_submission_is_unanswered(
    corpora: Path, stop_reason: str, ending: str
) -> None:
    warehouse, setting = _sql_setting(corpora)
    try:
        silent = anyio.run(ab.converse, Scripted([], stop_reason), setting, _question(1.0))
    finally:
        warehouse.close()
    assert silent.answer == ab.Answer(None, ending=ending) and silent.turns == 1
    assert ab.score(_question(1.0), silent.answer).outcome == 'unanswered'


def test_a_conversation_that_uses_up_its_turns_is_unanswered(corpora: Path) -> None:
    warehouse, setting = _sql_setting(corpora)
    try:
        endless = Scripted([[ab.Call(str(i), 'run_sql', {'sql': 'select 1'})] for i in range(5)])
        cut = anyio.run(ab.converse, endless, setting, _question(1.0), 3)
    finally:
        warehouse.close()
    assert cut.turns == 3 and cut.answer.ending == 'turn-limit'
    assert ab.score(_question(1.0), cut.answer).outcome == 'unanswered'


def test_an_unknown_tool_is_an_error_the_agent_reads(corpora: Path) -> None:
    warehouse, setting = _sql_setting(corpora)
    try:
        output = anyio.run(setting.call, 'define', {'ref': 'revenue'})
    finally:
        warehouse.close()
    assert output == ('there is no tool named define', True)


def test_a_run_scores_every_arm_repeats_and_resumes(corpora: Path, tmp_path: Path) -> None:
    out = _with_corpora(corpora, tmp_path)
    questions = ab.load_questions()
    selected = questions.for_corpora(['jaffle_shop'])[:3]
    root = _root(out)
    graph = ab.prepared_graph(root)

    def go(model: Any, chosen: list[Any], arms: list[str], repeats: int = 1) -> Any:
        return anyio.run(
            lambda: ab.run(
                model, questions, chosen, {'jaffle_shop': root}, arms, out, repeats=repeats, command='x'
            )
        )

    document = go(Oracle(selected), selected, list(ab.ARMS), repeats=2)
    assert document['model'] == 'oracle' and document['command'] == 'x' and document['repeats'] == 2
    assert document['model_settings'] == {'effort': 'none'}
    assert document['question_set'] == {'version': questions.version, 'fingerprint': questions.fingerprint}
    assert document['graphs'] == {'jaffle_shop': graph} and document['harness'] == ab.harness_digest()
    assert document['git']['commit']
    for arm in ab.ARMS:
        assert document['arms'][arm]['correct'] == 6 and document['arms'][arm]['confidently_wrong_rate'] == 0
    assert set(document['by_template']) == {q.template for q in selected}
    lines = [json.loads(line) for line in (out / 'results.jsonl').read_text().splitlines()]
    assert len(lines) == 24 and lines[0]['input_tokens'] == 20 and lines[0]['graph'] == graph
    assert {line['repeat'] for line in lines} == {1, 2}
    assert (out / 'transcripts' / 'konstruct' / f'{selected[0].id}.2.json').is_file()
    summary = (out / 'summary.md').read_text()
    assert '| konstruct | 6 | 6 | 0 | 0 | 0 | 0 | 0.0% (0% to 39%) |' in summary
    assert json.loads((out / 'results.json').read_text()) == document

    # A model that answers every question wrongly is not asked again what is already answered.
    wrong = Scripted([[ab.Call('1', 'submit_answer', {'value': -1, 'sure': True})]])
    more = questions.for_corpora(['jaffle_shop'])[:4]
    again = go(wrong, more, ['schema'])
    assert len(wrong.seen) == 1
    assert again['arms']['schema']['conversations'] == 4 and again['arms']['schema']['confidently_wrong'] == 1
    assert again['by_trap'] and set(again['by_corpus']) == {'jaffle_shop'}


def test_a_run_stops_when_the_graph_changes(corpora: Path, tmp_path: Path) -> None:
    out = _with_corpora(corpora, tmp_path)
    root = _root(out)
    questions = ab.load_questions()
    selected = questions.for_corpora(['jaffle_shop'])[:2]

    class Rebuilding(Scripted):
        def respond(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
            graph = root / 'konstruct-out' / 'graph.json'
            graph.write_text(graph.read_text() + ' ')
            return super().respond(system, messages, tools)

    model = Rebuilding([[ab.Call('1', 'submit_answer', {'value': 1, 'sure': True})]])
    with pytest.raises(SystemExit, match='the graph of jaffle_shop changed'):
        anyio.run(lambda: ab.run(model, questions, selected, {'jaffle_shop': root}, ['schema'], out))
    assert len((out / 'results.jsonl').read_text().splitlines()) == 1


def test_the_konstruct_arm_reaches_only_konstruct_s_read_tools(corpora: Path, tmp_path: Path) -> None:
    out = _with_corpora(corpora, tmp_path)
    root = _root(out)
    question = ab.load_questions().for_corpora(['jaffle_shop'])[0]
    model = Scripted(
        [
            [
                ab.Call('1', 'define', {'ref': 'net revenue'}),
                ab.Call('2', 'describe', {'ref': 'no_such_table'}),
                ab.Call('3', 'gate', {}),
            ],
            [ab.Call('4', 'submit_answer', {'value': question.answer, 'sure': True})],
        ]
    )
    before = ab.graph_digest(root)

    async def main() -> Any:
        async with ab.arm_settings('jaffle_shop', root, ['konstruct']) as made:
            setting = made['konstruct']
            names = [t['name'] for t in setting.tools]
            assert names == ['run_sql', 'submit_answer', *ab.KONSTRUCT_TOOLS]
            for word in ('resource', 'mutation_test', '`why`', 'gate'):
                assert word not in setting.system
            return await ab.converse(model, setting, question)

    transcript = anyio.run(main)
    [define, describe, gate] = transcript.steps[1]['tool_results']
    assert not define['is_error'] and "orders.status = 'completed'" in define['content']
    assert describe['is_error']
    assert gate == {
        'type': 'tool_result',
        'tool_use_id': '3',
        'content': 'there is no tool named gate',
        'is_error': True,
    }
    assert ab.score(question, transcript.answer).outcome == 'correct'
    assert ab.graph_digest(root) == before


def test_the_project_arm_reads_the_dbt_project_s_files(tmp_path: Path) -> None:
    for path in (
        'dbt_project.yml',
        'packages.yml',
        'models/orders.sql',
        'macros/cents.sql',
        'data-tests/positive.sql',
        'analyses/look.sql',
        'seeds/raw.csv',
        'target/manifest.json',
        'README.md',
    ):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(f'-- {path}')
    text = ab.project_text(tmp_path)
    shown = [line[2:] for line in text.splitlines() if line.startswith('# ')]
    assert shown == [
        'dbt_project.yml',
        'packages.yml',
        'models/orders.sql',
        'macros/cents.sql',
        'data-tests/positive.sql',
        'analyses/look.sql',
    ]


def test_the_mcp_server_does_not_get_the_model_s_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'secret')
    monkeypatch.setenv(ab.MODEL_VARIABLE, 'model')
    env = ab.McpTools(tmp_path).params.env
    assert env is not None and 'ANTHROPIC_API_KEY' not in env and ab.MODEL_VARIABLE not in env
    assert env['KONSTRUCT_TRUST_CONFIG'] == '1'


def test_each_arm_sees_what_it_should(corpora: Path) -> None:
    root = _root(corpora)

    async def main() -> dict[str, Any]:
        async with ab.arm_settings('jaffle_shop', root, ['schema', 'definitions', 'project']) as made:
            return dict(made)

    made = anyio.run(main)
    schema, definitions, project = made['schema'], made['definitions'], made['project']
    assert 'main.orders(' in schema.system and 'Net revenue' not in schema.system
    assert definitions.system.startswith(schema.system) and 'Net revenue' in definitions.system
    assert project.system.startswith(definitions.system)
    assert '# models/orders.sql\n' in project.system and '# dags/sql/customer_ltv.sql\n' in project.system
    assert '# dbt_project.yml\n' in project.system and 'raw_payments.csv' not in project.system
    names = [[t['name'] for t in s.tools] for s in (schema, definitions, project)]
    assert names == [['run_sql', 'submit_answer']] * 3
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
    assert f'{len(questions.questions)} questions in 4 arms' in out
    assert all(q.id in out for q in questions.questions)
    ab.main(['--plan', '--corpus', 'jaffle-shop', '--arms', 'schema', '--repeats', '3'])
    count = len(questions.for_corpora(['jaffle-shop']))
    assert f'in 1 arms (schema), 3 run(s) of each: {3 * count} conversations' in capsys.readouterr().out


def test_a_run_needs_a_model_and_an_out_outside_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ab.MODEL_VARIABLE, raising=False)
    with pytest.raises(SystemExit, match=ab.MODEL_VARIABLE):
        ab.main(['--corpus', 'jaffle_shop'])
    with pytest.raises(SystemExit, match='no such questions'):
        ab.main(['--plan', '--only', 'jaffle_shop.nothing'])
    with pytest.raises(SystemExit, match='at least 1'):
        ab.main(['--plan', '--repeats', '0'])
    subprocess.run(['git', 'init', '-q', str(tmp_path / 'repo')], check=True)
    with pytest.raises(SystemExit, match='inside the git repository'):
        ab.main(['--dry-run', '--out', str(tmp_path / 'repo' / 'out')])


def test_a_changed_question_set_is_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    questions = ab.load_questions()
    stale = dataclasses.replace(questions, recorded='stale')
    monkeypatch.setattr(ab, 'load_questions', lambda: stale)
    with pytest.raises(SystemExit, match='raise its version'):
        ab.main(['--dry-run'])


def test_the_dry_run_checks_answers_and_traps_and_writes_prompts(
    corpora: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _with_corpora(corpora, tmp_path)
    assert ab.main(['--dry-run', '--corpus', 'jaffle_shop', '--out', str(out)]) == 0
    questions = ab.load_questions().for_corpora(['jaffle_shop'])
    count, traps = len(questions), sum(q.trap != 'control' for q in questions)
    printed = capsys.readouterr().out
    assert (
        f'{count} of {count} reference answers match the warehouse; {traps} of {traps} traps bite' in printed
    )
    for arm in ab.ARMS:
        prompt = json.loads((out / 'prompts' / arm / 'jaffle_shop.json').read_text())
        assert len(prompt['questions']) == count and prompt['tools'][0]['name'] == 'run_sql'


def test_a_full_run_from_the_command_line(
    corpora: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command line with the Anthropic SDK replaced by a stub that answers every question with
    null: the model and key come from the environment, the effort is sent and recorded, and the
    options are kept for a resume."""
    out = _with_corpora(corpora, tmp_path)
    created: list[dict[str, Any]] = []

    def create(**request: Any) -> Any:
        created.append(request)
        block = SimpleNamespace(
            type='tool_use',
            id='t',
            name='submit_answer',
            input={'value': None, 'sure': False, 'doubt': 'no definition'},
            model_dump=lambda mode: {'type': 'tool_use'},
        )
        usage = SimpleNamespace(input_tokens=7, output_tokens=3)
        return SimpleNamespace(content=[block], usage=usage, stop_reason='tool_use')

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
    assert ab.main([*argv, '--effort', 'low']) == 0
    assert '| schema | 1 | 0 | 0 | 0 | 1 | 0 | 0.0%' in capsys.readouterr().out
    assert created[0]['model'] == 'model-under-test' and created[0]['output_config'] == {'effort': 'low'}
    assert created[0]['messages'][0]['content'].startswith('How many')
    document = json.loads((out / 'results.json').read_text())
    assert document['command'].startswith('answer_benchmark.py --corpus jaffle_shop')
    assert document['model_settings']['effort'] == 'low'
    with pytest.raises(SystemExit, match='holds a run with other options'):
        ab.main(argv)
    created.clear()
    other = tmp_path / 'other'
    _with_corpora(corpora, other)
    assert ab.main([*argv[:-1], str(other), '--effort', 'none']) == 0
    assert 'output_config' not in created[0]
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
    questions = ab.load_questions().for_corpora(['jaffle-shop'])
    count, traps = len(questions), sum(q.trap != 'control' for q in questions)
    printed = capsys.readouterr().out
    assert (
        f'{count} of {count} reference answers match the warehouse; {traps} of {traps} traps bite' in printed
    )
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
        arm = {'correct': 3, 'wrong': 1, 'abstained': 0, 'unanswered': 0, 'confidently_wrong': 1}
        rates = {f'{k}_rate': v / 4 for k, v in arm.items()}
        intervals = {f'{k}_interval': ab.wilson(v, 4) for k, v in arm.items()}
        measured = {**arm, **rates, **intervals, 'conversations': 4}
        results = {
            'model': 'model_x',
            'model_settings': {'effort': 'high'},
            'repeats': 1,
            'arms': dict.fromkeys(ab.ARMS, measured),
        }
        (Path(command[command.index('--out') + 1]) / 'results.json').write_text(json.dumps(results))

    monkeypatch.setattr(generate.subprocess, 'run', fake_run)
    monkeypatch.delenv(generate.ANSWER_BENCHMARK_FLAG, raising=False)
    numbers = generate.Numbers()
    generate.answers(numbers)
    assert calls == []
    assert numbers.values['answerQuestions']['value'] == str(len(ab.load_questions().questions))
    assert numbers.values['answerArms']['value'] == '4' and 'answerKonstructCorrect' not in numbers.values

    monkeypatch.setenv(generate.ANSWER_BENCHMARK_FLAG, '1')
    generate.answers(numbers)
    assert len(calls) == 1 and calls[0][1].endswith('answer_benchmark.py')
    assert numbers.values['answerModel']['value'] == r'model\_x'
    assert numbers.values['answerKonstructConfidentlyWrongPct']['value'] == '25.0'
    assert numbers.values['answerProjectUnanswered']['value'] == '0'
    low, high = ab.wilson(1, 4)
    assert numbers.values['answerKonstructConfidentlyWrongLow']['value'] == f'{100 * low:.1f}'
    assert numbers.values['answerKonstructConfidentlyWrongHigh']['value'] == f'{100 * high:.1f}'
    assert numbers.values['answerConversations']['value'] == '4'
    assert numbers.values['answerEffort']['value'] == 'high'
    numbers.write(tmp_path)
    assert r'\newcommand{\answerDefinitionsAbstainedPct}{0.0}' in (tmp_path / 'numbers.tex').read_text()
