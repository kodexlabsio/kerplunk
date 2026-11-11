"""`konstruct gate`: what stops a change, and what lets it through."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Collection
from pathlib import Path

import duckdb
import pytest

from konstruct import Project
from konstruct.core.graph import Graph
from konstruct.interfaces.cli import main


def git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            'git',
            '-C',
            str(root),
            '-c',
            'user.name=t',
            '-c',
            'user.email=t@example.com',
            '-c',
            'commit.gpgsign=false',
            *args,
        ],
        check=True,
        capture_output=True,
    )


LTV = (
    'create table main.ltv as select customer_id, sum(amount) as revenue from raw.orders '
    "where status <> 'void' group by customer_id"
)
REPORT = 'create table main.report as select customer_id, revenue from main.ltv where revenue > 100'
DEFINITIONS = """\
version: 1
terms:
  - name: Customer value
    definition: Every order a customer has placed, voids left out.
    status: approved
    owner: Sam Lee
    approved: 2026-10-01
    columns: [main.ltv.revenue]
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    (root / 'sql').mkdir(parents=True)
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute('create table raw.orders (id int, customer_id int, amount int, status varchar)')
    con.close()
    (root / 'sql' / 'ltv.sql').write_text(LTV)
    (root / 'sql' / 'report.sql').write_text(REPORT)
    (root / 'definitions.yml').write_text(DEFINITIONS)
    (root / '.gitignore').write_text('konstruct-out/\n*.duckdb\n')
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'base')
    Project(root).build()
    return root


def test_a_change_that_breaks_nothing_passes(repo: Path) -> None:
    (repo / 'sql' / 'ltv.sql').write_text(
        LTV.replace('sum(amount) as revenue', 'sum(amount) as revenue, count(*) as orders')
    )
    result = Project(repo).gate(base='HEAD')
    assert result.passed
    assert result.summary() == 'Gate passed: 1 changed file, nothing breaks.'


def test_a_removed_column_still_read_downstream_fails(repo: Path) -> None:
    (repo / 'sql' / 'ltv.sql').write_text(
        "create table main.ltv as select customer_id from raw.orders where status <> 'void' group by customer_id"
    )
    result = Project(repo).gate(base='HEAD')
    breaking, approved = result.findings
    assert (breaking.rule, breaking.path) == ('breaking', 'sql/ltv.sql')
    assert breaking.message == 'ltv.revenue is removed but still read by report'
    assert breaking.fix.startswith('Keep the column')
    # The approved term names the removed column, so its meaning changes too.
    assert approved.rule == 'approved-definition-changed' and 'ltv.revenue' in approved.message


def test_an_approved_definition_with_a_new_meaning_fails(repo: Path) -> None:
    (repo / 'definitions.yml').write_text(DEFINITIONS.replace('voids left out', 'voids included'))
    result = Project(repo).gate(base='HEAD')
    [finding] = result.findings
    assert (finding.rule, finding.path) == ('approved-definition-changed', 'definitions.yml')
    assert finding.message == "'Customer value' is approved, but the change gives it a new meaning"


def test_a_new_column_that_conflicts_with_an_existing_one_fails(repo: Path) -> None:
    (repo / 'sql' / 'gross.sql').write_text(
        'create table main.gross as select customer_id, sum(amount) as revenue from raw.orders group by customer_id'
    )
    result = Project(repo).gate(base='HEAD')
    rules = {f.rule for f in result.findings}
    assert rules == {'conflicting-definition'}
    [finding] = result.findings
    assert finding.path == 'sql/gross.sql'
    assert finding.message.startswith('gross.revenue computes ') and 'ltv.revenue' in finding.message
    assert 'filter' in finding.message
    # An existing conflict the change does not touch does not stop it.
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'gross')
    (repo / 'sql' / 'report.sql').write_text(REPORT.replace('> 100', '> 200'))
    assert Project(repo).gate(base='HEAD').passed


def test_a_plugin_check_sees_every_conflict(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Konstruct's own checks need only the conflicts of the columns a change alters, but a
    plugin's check is given every conflict at the head, as `konstruct conflicts` finds them."""
    from konstruct.analysis import gate as gating
    from konstruct.analysis.conflicts import Conflicts, find_conflicts
    from konstruct.analysis.gate import gate
    from konstruct.plugins.api import API_VERSION, CheckContext, Finding
    from konstruct.plugins.registry import Plugin, Plugins

    (repo / 'sql' / 'gross.sql').write_text(
        'create table main.gross as select customer_id, sum(amount) as revenue from raw.orders group by customer_id'
    )
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'gross')
    (repo / 'sql' / 'report.sql').write_text(REPORT.replace('> 100', '> 200'))
    project = Project(repo)
    project.build()
    diff = project.diff(base='HEAD')
    asked: list[Collection[str] | None] = []

    def recorded(
        graph: Graph, limit: int | None = None, *, touching: Collection[str] | None = None
    ) -> Conflicts:
        asked.append(touching)
        return find_conflicts(graph, limit, touching=touching)

    monkeypatch.setattr(gating, 'find_conflicts', recorded)

    class Seen:
        api_version = API_VERSION
        description = 'records the conflicts it is given'

        def __init__(self) -> None:
            self.conflicts: list[Conflicts] = []

        def check(self, context: CheckContext) -> list[Finding]:
            self.conflicts.append(context.conflicts)
            return []

    seen = Seen()
    plugin = Plugin('seen', 'checks', 'seen:SEEN', 'seen', '1.0', False, True, 'enabled', loaded=seen)
    registry = Plugins([*Plugins.builtin().plugins, plugin])
    assert gate(project.graph, diff, plugins=registry).passed
    [given] = seen.conflicts
    assert given.conflicts and given.to_dict() == find_conflicts(project.graph).to_dict()
    assert asked == [None]
    # Without the plugin, only the pairs of the columns the change alters are compared, and the
    # conflict the change does not touch is not among them.
    assert gate(project.graph, diff).passed
    assert asked[1] is not None and 'table.shop.main.report#revenue' in asked[1]
    assert not {'table.shop.main.ltv#revenue', 'table.shop.main.gross#revenue'} & set(asked[1])


def test_the_cli_exits_1_on_failure_and_writes_json(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / 'definitions.yml').write_text(DEFINITIONS.replace('voids left out', 'voids included'))
    out = repo / 'gate.json'
    assert main(['gate', '-C', str(repo), '--base', 'HEAD', '--json-file', str(out), '-q']) == 1
    text = capsys.readouterr().out
    assert text.startswith('Gate failed: 1 finding to fix before proposing the change.')
    assert "\n  fix   Ask the term's owner to re-approve it" in text
    data = json.loads(out.read_text())
    assert data['passed'] is False and data['findings'][0]['rule'] == 'approved-definition-changed'
    assert main(['gate', '-C', str(repo), '--base', 'HEAD', '--format', 'markdown', '-q']) == 1
    assert '| `approved-definition-changed` |' in capsys.readouterr().out
    (repo / 'definitions.yml').write_text(DEFINITIONS)
    assert main(['gate', '-C', str(repo), '--base', 'HEAD', '-q']) == 0


def test_a_changed_formula_under_an_approved_term_fails(repo: Path) -> None:
    (repo / 'sql' / 'ltv.sql').write_text(LTV.replace('sum(amount)', 'sum(amount * 2)'))
    [finding] = Project(repo).gate(base='HEAD').findings
    assert finding.rule == 'approved-definition-changed' and finding.path == 'sql/ltv.sql'
    assert (
        finding.message == "'Customer value' is approved, and the change alters ltv.revenue, which it names"
    )


def test_a_new_row_condition_reaches_the_conflict_rule(repo: Path) -> None:
    (repo / 'definitions.yml').write_text(DEFINITIONS.replace('status: approved', 'status: draft'))
    (repo / 'sql' / 'ltv.sql').write_text(LTV.replace(" where status <> 'void'", ''))
    (repo / 'sql' / 'gross.sql').write_text(
        'create table main.gross as select customer_id, sum(amount) as revenue from raw.orders group by customer_id'
    )
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'two agreeing definitions')
    Project(repo).build()
    (repo / 'sql' / 'ltv.sql').write_text(LTV)
    rules = [f.rule for f in Project(repo).gate(base='HEAD').findings]
    assert rules == ['conflicting-definition']


def test_relations_that_share_a_name_are_told_apart(repo: Path) -> None:
    (repo / 'definitions.yml').write_text(DEFINITIONS.replace('status: approved', 'status: draft'))
    (repo / 'sql' / 'b.sql').write_text(
        'create table b.ltv as select customer_id, sum(amount) as revenue from raw.orders group by customer_id'
    )
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'a second ltv')
    Project(repo).build()
    (repo / 'sql' / 'report.sql').write_text(REPORT.replace('> 100', '> 200'))
    # b.ltv already conflicts with main.ltv; a change to neither of them passes.
    assert Project(repo).gate(base='HEAD').passed
    (repo / 'sql' / 'b.sql').write_text(
        'create table b.ltv as select customer_id, sum(amount) + 0 as revenue from raw.orders group by customer_id'
    )
    findings = Project(repo).gate(base='HEAD').findings
    assert [(f.rule, f.path) for f in findings] == [('conflicting-definition', 'sql/b.sql')]
    assert (
        findings[0].message.startswith('shop.b.ltv.revenue computes ')
        and 'differently from shop.main.ltv.revenue' in findings[0].message
    )


def test_the_build_options_reach_the_update(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    real = Project.build

    def build(self: Project, **kwargs: object) -> object:
        calls.append(kwargs)
        return real(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Project, 'build', build)
    assert main(['gate', '-C', str(repo), '--base', 'HEAD', '--no-dbt', '--no-warehouse', '-q']) == 0
    # The update builds with the project's configuration, widened by the plugins the base enables.
    assert [{k: v for k, v in c.items() if k != 'config'} for c in calls] == [
        {'update': True, 'run_dbt': False, 'warehouses': False}
    ]
    calls.clear()
    assert main(['gate', '-C', str(repo), '--base', 'HEAD', '--no-update', '-q']) == 0
    assert calls == []


def test_an_untracked_binary_file_is_not_compared(repo: Path) -> None:
    (repo / 'model.duckdb.wal').write_bytes(bytes(range(256)))
    assert Project(repo).gate(base='HEAD').passed


def test_removing_a_column_an_approved_term_names_fails(repo: Path) -> None:
    (repo / 'sql' / 'report.sql').write_text('create table main.report as select customer_id from main.ltv')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'nothing reads revenue')
    Project(repo).build()
    (repo / 'sql' / 'ltv.sql').write_text(
        "create table main.ltv as select customer_id from raw.orders where status <> 'void' group by customer_id"
    )
    [finding] = Project(repo).gate(base='HEAD').findings
    assert (finding.rule, finding.path) == ('approved-definition-changed', 'sql/ltv.sql')
    assert 'main.ltv.revenue' in finding.message


def test_two_columns_bound_to_different_terms_are_not_a_conflict(repo: Path) -> None:
    (repo / 'definitions.yml').write_text(
        DEFINITIONS.replace('status: approved', 'status: draft')
        + '  - {name: Gross revenue, definition: Voids included., columns: [main.gross.revenue]}\n'
    )
    (repo / 'sql' / 'gross.sql').write_text(
        'create table main.gross as select customer_id, sum(amount) as revenue from raw.orders group by customer_id'
    )
    assert Project(repo).gate(base='HEAD').passed


def test_a_removed_column_is_not_taken_for_one_of_the_same_name(repo: Path) -> None:
    (repo / 'sql' / 'b.sql').write_text(
        'create table b.ltv as select customer_id, sum(amount) as revenue from raw.orders group by customer_id'
    )
    (repo / 'sql' / 'report.sql').write_text('create table main.report as select customer_id from main.ltv')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'a second ltv')
    Project(repo).build()
    (repo / 'sql' / 'b.sql').write_text(
        'create table b.ltv as select customer_id from raw.orders group by customer_id'
    )
    # The approved term names main.ltv.revenue, which is still there.
    assert Project(repo).gate(base='HEAD').passed
