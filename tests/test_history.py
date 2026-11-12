"""History: when a column's formula changed, and who changed the terms bound to it."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from konstruct import Project, QueryError
from konstruct.interfaces.cli import main


def git(root: Path, *args: str, author: str = 'Ada', committed: str | None = None) -> str:
    env = {
        **os.environ,
        'GIT_AUTHOR_NAME': author,
        'GIT_AUTHOR_EMAIL': f'{author.lower()}@example.com',
        'GIT_COMMITTER_NAME': author,
        'GIT_COMMITTER_EMAIL': f'{author.lower()}@example.com',
        **({'GIT_COMMITTER_DATE': committed} if committed else {}),
    }
    return subprocess.run(
        ['git', '-C', str(root), '-c', 'commit.gpgsign=false', *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout


def commit(root: Path, message: str, author: str, date: str, committed: str | None = None) -> None:
    git(root, 'add', '-A', author=author)
    git(root, 'commit', '-q', '-m', message, f'--date={date}', author=author, committed=committed or date)


TERM = """terms:
  - name: lifetime value
    definition: What a customer has paid us.
    status: {status}
    owner: {{name: {owner}}}
    columns: [main.ltv.total]
"""


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, 'init', '-q', '-b', 'main')
    (tmp_path / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) as total from raw.orders group by 1'
    )
    (tmp_path / 'definitions.yml').write_text(TERM.format(status='draft', owner='Ada'))
    commit(tmp_path, 'Add lifetime value', 'Ada', '2026-01-05T10:00:00+00:00')
    (tmp_path / 'definitions.yml').write_text(TERM.format(status='approved', owner='Grace'))
    commit(tmp_path, 'Approve lifetime value', 'Grace', '2026-02-01T10:00:00+00:00')
    # Reformatting is not a change to the formula.
    (tmp_path / 'ltv.sql').write_text(
        'create table main.ltv as\nselect customer_id,\n  SUM(amount) as total\nfrom raw.orders\ngroup by 1\n'
    )
    commit(tmp_path, 'Reformat', 'Linus', '2026-03-01T10:00:00+00:00')
    (tmp_path / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount - refund) as total from raw.orders group by 1'
    )
    commit(tmp_path, 'Net of refunds', 'Linus', '2026-04-01T10:00:00+00:00')
    return tmp_path


def test_formula_changes_and_approvals(repository: Path) -> None:
    project = Project(repository)
    project.build(warehouses=False, run_dbt=False)
    history = project.history('ltv.total')
    assert [(c.commit.author, c.before, c.after) for c in history.formulas] == [
        ('Ada', None, 'SUM(orders.amount)'),
        ('Linus', 'SUM(orders.amount)', 'SUM(orders.amount - orders.refund)'),
    ]
    assert history.formulas[1].commit.subject == 'Net of refunds'
    assert history.formulas[1].commit.date.startswith('2026-04-01')
    assert [(t.commit.author, t.changes) for t in history.terms] == [
        (
            'Ada',
            {
                'definition': (None, 'What a customer has paid us.'),
                'owner': (None, 'Ada'),
                'status': (None, 'draft'),
            },
        ),
        ('Grace', {'owner': ('Ada', 'Grace'), 'status': ('draft', 'approved')}),
    ]
    approval = history.last_approval
    assert approval is not None and approval.commit.author == 'Grace'
    assert history.approved_since_last_change is False
    assert history.summary() == (
        "ltv.total: 2 formula changes; 'lifetime value' last approved by Grace in "
        f'{approval.commit.short}, before the latest formula change'
    )
    data = history.to_dict()
    assert data['approved_since_last_change'] is False and data['terms'][-1]['approved'] is True


def test_uncommitted_edits_are_last(repository: Path) -> None:
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, max(amount) as total from raw.orders group by 1'
    )
    project = Project(repository)
    project.build(warehouses=False, run_dbt=False)
    last = project.history('ltv.total').formulas[-1]
    assert (last.commit.sha, last.after) == ('', 'MAX(orders.amount)')
    assert 'working tree (uncommitted)' in str(project.history('ltv.total'))


def test_renamed_files_are_followed(repository: Path) -> None:
    git(repository, 'mv', 'ltv.sql', 'customer_ltv.sql')
    commit(repository, 'Rename', 'Ada', '2026-05-01T10:00:00+00:00')
    project = Project(repository)
    project.build(warehouses=False, run_dbt=False)
    history = project.history('ltv.total')
    assert [c.path for c in history.formulas] == ['ltv.sql', 'ltv.sql']
    assert len(history.formulas) == 2


def test_the_command(repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(['history', 'ltv.total', '-C', str(repository)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == 'History of ltv.total (ltv.sql)'
    assert out.splitlines()[1].startswith('2 formula changes')
    assert re.search(r'  Grace  Approve lifetime value$', out, re.M)
    assert "    term  'lifetime value'  owner   Ada -> Grace\n" in out
    assert re.search(r'^ +status +draft -> approved$', out, re.M)
    assert '    was  SUM(orders.amount)\n' in out and '    now  SUM(orders.amount - orders.refund)\n' in out


def test_not_a_column_or_not_a_repository(tmp_path: Path) -> None:
    (tmp_path / 'a.sql').write_text('create table main.a as select 1 as x')
    project = Project(tmp_path)
    project.build(warehouses=False, run_dbt=False)
    with pytest.raises(QueryError, match='history takes a column'):
        project.history('main.a')
    history = project.history('a.x')
    assert history.notes == ['not a git repository, so there is no history']


def built(root: Path) -> Project:
    project = Project(root)
    project.build(warehouses=False, run_dbt=False)
    return project


def test_moving_the_approval_date_is_a_new_approval(repository: Path) -> None:
    (repository / 'definitions.yml').write_text(
        TERM.format(status='approved', owner='Grace').replace(
            'status: approved', 'status: approved\n    approved: 2026-05-01'
        )
    )
    commit(repository, 'Re-approve', 'Grace', '2026-05-01T10:00:00+00:00')
    history = built(repository).history('ltv.total')
    assert history.last_approval is not None and history.last_approval.commit.subject == 'Re-approve'
    assert history.approved_since_last_change is True


def test_history_order_is_ancestry_not_dates(repository: Path) -> None:
    # A change written before the approval but landed after it, as a rebase leaves it.
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, avg(amount) as total from raw.orders group by 1'
    )
    commit(repository, 'Average', 'Linus', '2026-01-10T10:00:00+00:00', committed='2026-05-01T10:00:00+00:00')
    history = built(repository).history('ltv.total')
    assert history.formulas[-1].commit.subject == 'Average'
    assert history.approved_since_last_change is False


def test_a_renamed_term_is_not_a_new_approval(repository: Path) -> None:
    (repository / 'definitions.yml').write_text(
        TERM.format(status='approved', owner='Grace').replace('lifetime value', 'customer value')
    )
    commit(repository, 'Rename the term', 'Mallory', '2026-05-01T10:00:00+00:00')
    history = built(repository).history('ltv.total')
    assert history.last_approval is None
    assert history.approved_since_last_change is None
    assert [t.commit.author for t in history.terms] == ['Mallory']


def test_merges_are_not_changes(repository: Path) -> None:
    git(repository, 'checkout', '-q', '-b', 'feature')
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, max(amount) as total, 1 as one from raw.orders group by 1'
    )
    commit(repository, 'Max on a branch', 'Bob', '2026-05-01T10:00:00+00:00')
    git(repository, 'checkout', '-q', 'main')
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount - refund) as total, 2 as two '
        'from raw.orders group by 1'
    )
    commit(repository, 'Another column', 'Mo', '2026-05-02T10:00:00+00:00')
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, max(amount) as total, 2 as two from raw.orders group by 1'
    )
    git(repository, 'merge', '-q', '--no-ff', '-s', 'ours', 'feature', '-m', 'Merge', author='Mo')
    git(repository, 'commit', '-q', '-a', '--amend', '--no-edit', author='Mo')
    history = built(repository).history('ltv.total')
    assert [c.commit.subject for c in history.formulas] == [
        'Add lifetime value',
        'Net of refunds',
        'Max on a branch',
    ]
    assert not [c for c in history.formulas if not c.commit.sha]


def test_a_shallow_clone_and_a_limit(repository: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    clone = tmp_path_factory.mktemp('clone') / 'shallow'
    subprocess.run(['git', 'clone', '-q', '--depth', '1', f'file://{repository}', str(clone)], check=True)
    history = built(clone).history('ltv.total')
    assert history.formulas == [] and history.last_approval is None
    assert any('shallow clone' in n for n in history.notes)
    limited = built(repository).history('ltv.total', limit=1)
    assert [(c.commit.subject, c.before) for c in limited.formulas] == [
        ('Net of refunds', 'SUM(orders.amount)')
    ]
    assert any('only the last commit that changed ltv.sql was read' in n for n in limited.notes)
    # Exactly as many commits as the limit: every one was read.
    changed = len(git(repository, 'log', '--format=%H', '--follow', '--', 'ltv.sql').split())
    whole = built(repository).history('ltv.total', limit=changed)
    assert not any('only the last' in n for n in whole.notes)
    with pytest.raises(QueryError, match='at least 1'):
        built(repository).history('ltv.total', limit=0)


def test_the_last_statement_that_writes_the_table(tmp_path: Path) -> None:
    git(tmp_path, 'init', '-q', '-b', 'main')
    statements = (
        'create table main.ltv (customer_id int, total int);\ninsert into main.ltv (customer_id, total) {};\n'
    )
    (tmp_path / 'ltv.sql').write_text(
        statements.format('select customer_id, sum(amount) from raw.orders group by 1')
    )
    commit(tmp_path, 'Add', 'Ada', '2026-01-01T10:00:00+00:00')
    (tmp_path / 'ltv.sql').write_text(
        statements.format('select customer_id, max(amount) from raw.orders group by 1')
    )
    commit(tmp_path, 'Max', 'Linus', '2026-02-01T10:00:00+00:00')
    history = built(tmp_path).history('ltv.total')
    assert [c.after for c in history.formulas] == ['SUM(orders.amount)', 'MAX(orders.amount)']


def test_files_with_unusual_names_and_dag_sql(tmp_path: Path) -> None:
    git(tmp_path, 'init', '-q', '-b', 'main')
    (tmp_path / 'modèles').mkdir()
    (tmp_path / 'modèles' / 'ltv.sql').write_text(
        'create table main.ltv as select sum(amount) as total from raw.orders'
    )
    commit(tmp_path, 'Add', 'Ada', '2026-01-01T10:00:00+00:00')
    history = built(tmp_path).history('ltv.total')
    assert [c.commit.author for c in history.formulas] == ['Ada']


def test_a_dbt_model(jaffle_with_artefacts: Path, tmp_path: Path) -> None:
    import shutil

    jaffle = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_with_artefacts, jaffle)
    git(jaffle, 'init', '-q', '-b', 'main')
    commit(jaffle, 'Add the project', 'Ada', '2026-01-01T10:00:00+00:00')
    model = jaffle / 'models' / 'staging' / 'stg_payments.sql'
    model.write_text(model.read_text().replace('amount / 100', 'amount / 100.0'))
    commit(jaffle, 'Divide exactly', 'Linus', '2026-02-01T10:00:00+00:00')
    project = Project(jaffle)
    project.build(run_dbt=False)
    history = project.history('stg_payments.amount')
    assert [c.commit.author for c in history.formulas] == ['Ada', 'Linus']
    assert history.formulas[-1].after is not None and '100.0' in history.formulas[-1].after
    assert any('dbt macros' in n for n in history.notes)


def test_a_formula_that_cannot_be_worked_out(repository: Path) -> None:
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount as total from'
    )
    commit(repository, 'Broken', 'Bob', '2026-05-01T10:00:00+00:00')
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount - refund) as total from raw.orders group by 1'
    )
    commit(repository, 'Fixed', 'Bob', '2026-05-02T10:00:00+00:00')
    history = built(repository).history('ltv.total')
    assert [c.commit.subject for c in history.formulas] == ['Add lifetime value', 'Net of refunds']
    assert any(n.startswith('the formula could not be worked out at ') for n in history.notes)


def test_the_sql_file_a_dag_task_runs(tmp_path: Path) -> None:
    git(tmp_path, 'init', '-q', '-b', 'main')
    (tmp_path / 'sql').mkdir()
    (tmp_path / 'sql' / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount) as total from raw.orders group by 1'
    )
    (tmp_path / 'dag.py').write_text(
        'from airflow.sdk import DAG\n'
        'from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator\n\n'
        'with DAG("nightly"):\n'
        '    ltv = SQLExecuteQueryOperator(task_id="ltv", sql="sql/ltv.sql")\n'
        '    inline = SQLExecuteQueryOperator(task_id="inline", sql="create table main.x as select 1 as one")\n'
    )
    commit(tmp_path, 'Add', 'Ada', '2026-01-01T10:00:00+00:00')
    (tmp_path / 'sql' / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, max(amount) as total from raw.orders group by 1'
    )
    commit(tmp_path, 'Max', 'Linus', '2026-02-01T10:00:00+00:00')
    project = built(tmp_path)
    assert [c.commit.author for c in project.history('ltv.total').formulas] == ['Ada', 'Linus']
    inline = project.history('x.one')
    assert inline.read_formulas is False and inline.approved_since_last_change is None
    assert inline.notes == ['x is written by the SQL inside task nightly.inline, whose history is not read']
    assert inline.summary() == 'x.one: the formula history was not read'


def test_an_approval_on_a_branch_that_never_saw_the_change(repository: Path) -> None:
    git(repository, 'checkout', '-q', '-b', 'review', 'HEAD~1')
    (repository / 'definitions.yml').write_text(
        TERM.format(status='approved', owner='Grace').replace(
            'status: approved', 'status: approved\n    approved: 2026-05-01'
        )
    )
    commit(repository, 'Approve again on a branch', 'Grace', '2026-05-01T10:00:00+00:00')
    git(repository, 'checkout', '-q', 'main')
    git(repository, 'merge', '-q', '--no-ff', 'review', '-m', 'Merge review', author='Grace')
    history = built(repository).history('ltv.total')
    assert (
        history.last_approval is not None
        and history.last_approval.commit.subject == 'Approve again on a branch'
    )
    assert history.approved_since_last_change is False


def test_an_approval_date_on_a_draft_is_not_an_approval(tmp_path: Path) -> None:
    git(tmp_path, 'init', '-q', '-b', 'main')
    (tmp_path / 'ltv.sql').write_text('create table main.ltv as select sum(amount) as total from raw.orders')
    (tmp_path / 'definitions.yml').write_text(TERM.format(status='draft', owner='Ada'))
    commit(tmp_path, 'Add', 'Ada', '2026-01-01T10:00:00+00:00')
    (tmp_path / 'definitions.yml').write_text(
        TERM.format(status='draft', owner='Ada').replace(
            'status: draft', 'status: draft\n    approved: 2026-03-01'
        )
    )
    commit(tmp_path, 'Date it', 'Mo', '2026-03-01T10:00:00+00:00')
    history = built(tmp_path).history('ltv.total')
    assert history.last_approval is None and history.approved_since_last_change is None


def test_a_term_approved_from_the_start_flags_a_later_change(tmp_path: Path) -> None:
    git(tmp_path, 'init', '-q', '-b', 'main')
    (tmp_path / 'ltv.sql').write_text('create table main.ltv as select sum(amount) as total from raw.orders')
    (tmp_path / 'definitions.yml').write_text(TERM.format(status='approved', owner='Ada'))
    commit(tmp_path, 'Add', 'Ada', '2026-01-01T10:00:00+00:00')
    (tmp_path / 'ltv.sql').write_text('create table main.ltv as select max(amount) as total from raw.orders')
    commit(tmp_path, 'Max', 'Dee', '2026-02-01T10:00:00+00:00')
    history = built(tmp_path).history('ltv.total')
    assert history.last_approval is None
    assert history.approved_since_last_change is False


def test_a_commit_that_cannot_be_worked_out_after_the_approval(repository: Path) -> None:
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount as total from'
    )
    commit(repository, 'Broken', 'Bob', '2026-05-01T10:00:00+00:00')
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount - refund) as total from raw.orders group by 1'
    )
    commit(repository, 'Restored', 'Bob', '2026-05-02T10:00:00+00:00')
    history = built(repository).history('ltv.total')
    assert [c.subject for c in history.unknown] == ['Broken', 'Restored']
    assert history.approved_since_last_change is None


def test_an_approval_must_follow_every_parallel_change(repository: Path) -> None:
    git(repository, 'checkout', '-q', '-b', 'side', 'HEAD~1')
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as\nselect customer_id,\n  SUM(amount) as total, 1 as one\nfrom raw.orders\ngroup by 1\n'
    )
    commit(repository, 'Another column on a branch', 'Mo', '2026-05-01T10:00:00+00:00')
    (repository / 'definitions.yml').write_text(
        TERM.format(status='approved', owner='Grace').replace(
            'status: approved', 'status: approved\n    approved: 2026-05-02'
        )
    )
    commit(repository, 'Approve on the branch', 'Grace', '2026-05-02T10:00:00+00:00')
    git(repository, 'checkout', '-q', 'main')
    git(repository, 'merge', '-q', '--no-ff', '-X', 'ours', 'side', '-m', 'Merge side', author='Grace')
    history = built(repository).history('ltv.total')
    assert history.approved_since_last_change is False


def test_a_window_without_changes_does_not_vouch_for_what_came_before(repository: Path) -> None:
    for day in (10, 11, 12):
        (repository / 'ltv.sql').write_text((repository / 'ltv.sql').read_text() + f'\n-- {day}\n')
        commit(repository, f'Comment {day}', 'Linus', f'2026-05-{day}T10:00:00+00:00')
    history = built(repository).history('ltv.total', limit=3)
    assert history.formulas == [] and history.window is not None
    assert history.approved_since_last_change is None


def test_a_formula_first_worked_out_is_not_an_addition(repository: Path) -> None:
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount as total from'
    )
    commit(repository, 'Broken', 'Bob', '2026-05-01T10:00:00+00:00')
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, sum(amount - refund) as total from raw.orders group by 1'
    )
    commit(repository, 'Restored', 'Cy', '2026-05-02T10:00:00+00:00')
    limited = built(repository).history('ltv.total', limit=2)
    assert not [c for c in limited.formulas if c.commit.author == 'Cy']


def test_approved_from_the_start_then_a_commit_that_cannot_be_worked_out(tmp_path: Path) -> None:
    git(tmp_path, 'init', '-q', '-b', 'main')
    (tmp_path / 'ltv.sql').write_text('create table main.ltv as select sum(amount) as total from raw.orders')
    (tmp_path / 'definitions.yml').write_text(TERM.format(status='approved', owner='Ada'))
    commit(tmp_path, 'Add', 'Ada', '2026-01-01T10:00:00+00:00')
    (tmp_path / 'ltv.sql').write_text('create table main.ltv as select sum(amount as total from')
    commit(tmp_path, 'Broken', 'Linus', '2026-02-01T10:00:00+00:00')
    (tmp_path / 'ltv.sql').write_text(
        'create table main.ltv as select sum(amount) * 2 as total from raw.orders'
    )
    commit(tmp_path, 'Doubled', 'Linus', '2026-03-01T10:00:00+00:00')
    history = built(tmp_path).history('ltv.total')
    assert history.approved_since_last_change is not True


def test_an_approval_in_the_same_commit_as_the_change(repository: Path) -> None:
    (repository / 'ltv.sql').write_text(
        'create table main.ltv as select customer_id, max(amount) as total from raw.orders group by 1'
    )
    (repository / 'definitions.yml').write_text(
        TERM.format(status='approved', owner='Grace').replace(
            'status: approved', 'status: approved\n    approved: 2026-05-01'
        )
    )
    commit(repository, 'Max, approved', 'Linus', '2026-05-01T10:00:00+00:00')
    history = built(repository).history('ltv.total')
    assert history.approved_since_last_change is True
    git(repository, 'init', '-q', str(repository / 'fresh'))
    fresh = repository / 'fresh'
    (fresh / 'ltv.sql').write_text('create table main.ltv as select sum(amount) as total from raw.orders')
    (fresh / 'definitions.yml').write_text(TERM.format(status='approved', owner='Ada'))
    commit(fresh, 'Add', 'Ada', '2026-01-01T10:00:00+00:00')
    assert built(fresh).history('ltv.total').approved_since_last_change is not False
