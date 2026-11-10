"""Every graph an earlier release built still works with this code, and answers as it did then.

tests/compat/<tag>/<project>/ holds what a release wrote for each corpus project:
graph.json, build.json and the --json output of the commands in commands.json
(.kodex/scripts/compat_snapshot.py writes them). Each test puts an old graph.json in a
konstruct-out of its own and asks this code the same questions, through the CLI, the Python API
and the MCP server, and checks the diff, the report and the viewer on it.

An answer may gain fields. Any other difference must be deliberate and listed in
tests/compat/allowed.yml with its reason; an entry that no longer matches a difference fails too,
so the list only holds what is still true.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import anyio
import pytest
import yaml
from mcp import Client

from konstruct import Project
from konstruct.core.graph import Graph
from konstruct.interfaces.cli import main
from konstruct.interfaces.mcp_server import build_server
from konstruct.outputs import html, report

CORPUS = Path(__file__).parent / 'compat'
ALLOWED = CORPUS / 'allowed.yml'
# Each release, and the folder holding its outputs: a release that answered exactly as the one
# before it shares that one's folder.
RELEASES: dict[str, str] = json.loads((CORPUS / 'releases.json').read_text(encoding='utf-8'))
SNAPSHOTS = sorted(
    path.parent for folder in set(RELEASES.values()) for path in (CORPUS / folder).glob('*/graph.json')
)
IDS = [f'{path.parent.name}/{path.name}' for path in SNAPSHOTS]
# The tests that compare answers with allowed.yml; its entries are checked for staleness once all
# of them have run.
COMPARISONS = {'cli', 'mcp', 'diff'}


def install(snapshot: Path, root: Path) -> Path:
    """A project directory whose konstruct-out holds the snapshot's graph, at ``root``."""
    out = root / 'konstruct-out'
    out.mkdir(parents=True, exist_ok=True)
    text = (snapshot / 'graph.json').read_text(encoding='utf-8')
    (out / 'graph.json').write_text(text.replace('<root>', str(root)), encoding='utf-8')
    return root


def differences(old: Any, new: Any, path: str = '') -> list[str]:
    """How ``new`` differs from ``old``, except for fields ``new`` adds."""
    if isinstance(old, dict) and isinstance(new, dict):
        found = []
        for key, value in old.items():
            if key not in new:
                found.append(f'{path}.{key}: removed')
            else:
                found += differences(value, new[key], f'{path}.{key}')
        return found
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [
            d
            for i, (a, b) in enumerate(zip(old, new, strict=True))
            for d in differences(a, b, f'{path}[{i}]')
        ]
    if isinstance(old, list) and isinstance(new, list):
        return [f'{path}: {len(old)} items, now {len(new)}']
    return [] if old == new else [f'{path}: {old!r}, now {new!r}']


class Allowed:
    """The deliberate differences in allowed.yml, and which of them were seen."""

    def __init__(self, path: Path = ALLOWED) -> None:
        entries = yaml.safe_load(path.read_text(encoding='utf-8'))
        self.entries: list[dict[str, str]] = entries or []
        for entry in self.entries:
            assert set(entry) == {'where', 'path', 'reason'} and entry['reason'].strip(), entry
        self.used: set[int] = set()
        self.checked: set[str] = set()

    def unexplained(self, where: str, found: list[str]) -> list[str]:
        left = []
        for difference in found:
            path = difference.split(':', 1)[0]
            matches = [
                i
                for i, entry in enumerate(self.entries)
                if fnmatch(where, entry['where'])
                and (path == entry['path'] or path.startswith((entry['path'] + '.', entry['path'] + '[')))
            ]
            self.used.update(matches)
            if not matches:
                left.append(f'{where} {difference}')
        return left

    def stale(self) -> list[str]:
        return [f'{e["where"]} {e["path"]}' for i, e in enumerate(self.entries) if i not in self.used]


@pytest.fixture(scope='module')
def allowed() -> Iterator[Allowed]:
    """allowed.yml, shared by the tests that compare answers, so that an entry only one of them
    uses counts as used; once they have all run, every entry must have been used."""
    shared = Allowed()
    yield shared
    if shared.checked == COMPARISONS:
        assert shared.stale() == [], 'allowed.yml lists differences that no longer occur'


def recorded(snapshot: Path) -> dict[str, tuple[list[str], int, Any]]:
    """Each command the release ran on the snapshot: its arguments, exit status and JSON output."""
    commands = json.loads((snapshot / 'commands.json').read_text(encoding='utf-8'))
    found = {}
    for name, run in commands.items():
        output = snapshot / f'{name}.json'
        data = json.loads(output.read_text(encoding='utf-8')) if output.is_file() else None
        found[name] = (run['args'], run['exit'], data)
    return found


def ask(capsys: pytest.CaptureFixture[str], root: Path, args: list[str]) -> tuple[int, Any]:
    capsys.readouterr()
    code = main(['--no-color', '-q', *args, '-C', str(root), '--json', '--no-build'])
    out = capsys.readouterr().out.replace(str(root), '<root>')
    return code, json.loads(out) if out.strip() else None


def _version(tag: str) -> tuple[int, ...] | None:
    parts = tag.removeprefix('v').split('.')
    return tuple(int(p) for p in parts) if tag.startswith('v') and all(p.isdigit() for p in parts) else None


def missing_releases(tags: list[str], recorded: dict[str, str]) -> list[str]:
    """The release tags from v0.11.13 up to the newest one ``recorded`` that it lacks. A tag
    newer than every recorded one waits for its batch, which prepare_release.py holds to adding
    the newest; so releases tagged before a batch fail no pull request."""
    newest = max((_version(tag) or () for tag in recorded), default=())
    found = [tag for tag in tags if len(_version(tag) or ()) == 3]
    return sorted(
        (tag for tag in found if (0, 11, 13) <= (_version(tag) or ()) <= newest and tag not in recorded),
        key=lambda tag: _version(tag) or (),
    )


def test_the_corpus_has_each_release_and_project() -> None:
    """Every release tagged since the corpus began, up to the newest recorded, is in it, so a
    release cannot be left out."""
    listed = subprocess.run(
        ['git', 'tag', '--list', 'v*'], cwd=CORPUS, capture_output=True, text=True, check=False
    ).stdout.split()
    if not listed:
        if os.environ.get('CI'):
            pytest.fail('the checkout has no release tags; fetch them (actions/checkout fetch-tags)')
        pytest.skip('this checkout has no release tags')
    missing = missing_releases(listed, RELEASES)
    assert missing == [], f'add these releases with .kodex/scripts/compat_snapshot.py --ref: {missing}'
    for folder in set(RELEASES.values()):
        assert {path.name for path in (CORPUS / folder).iterdir()} == {'jaffle_shop', 'orchestration'}
        assert RELEASES[folder] == folder, f'{folder} holds outputs but is not recorded as its own'


def test_a_release_newer_than_the_corpus_waits_for_its_batch() -> None:
    recorded = {'v0.11.13': 'v0.11.13', 'v0.12.0': 'v0.11.13', 'v0.12.2': 'v0.11.13'}
    tags = ['v0.11.12', 'v0.11.13', 'v0.12.0', 'v0.12.1', 'v0.12.2', 'v0.12.3', 'v0.12.4', 'v0', 'vnext']
    assert missing_releases(tags, recorded) == ['v0.12.1']  # v0.12.3 and v0.12.4 are newer


def test_old_graphs_answer_as_their_release_did(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], allowed: Allowed
) -> None:
    """The CLI, and through it the Python API, on every old graph: the same exit status and the
    same answer, give or take new fields and the differences allowed.yml explains."""
    unexplained = []
    for snapshot in SNAPSHOTS:
        release, project = snapshot.parent.name, snapshot.name
        root = install(snapshot, tmp_path / release / project)
        for name, (args, status, before) in recorded(snapshot).items():
            code, after = ask(capsys, root, args)
            where = f'{release}/{project}/{name}'
            if code != status:
                unexplained.append(f'{where}: exit status {status}, now {code}')
            elif before is not None:
                unexplained += allowed.unexplained(where, differences(before, after))
    assert unexplained == [], 'answers changed:\n' + '\n'.join(unexplained)
    allowed.checked.add('cli')


@pytest.mark.parametrize('snapshot', SNAPSHOTS, ids=IDS)
def test_old_graphs_load_and_render(snapshot: Path, tmp_path: Path) -> None:
    graph = Graph.read(install(snapshot, tmp_path).joinpath('konstruct-out', 'graph.json'))
    project = Project(tmp_path)
    assert project.graph.stats() == graph.stats()
    assert project.trace('customer_ltv.lifetime_value').columns
    assert project.impact('stg_payments.amount').exposures
    assert project.explain('customers.customer_lifetime_value').composed
    assert project.ontology().entities
    text = report.render(graph)
    assert text.startswith('# ') and 'customers' in text
    page = tmp_path / 'graph.html'
    html.write_html(graph, page)
    assert 'customer_lifetime_value' in page.read_text(encoding='utf-8')


# Each MCP tool, with the arguments of the CLI command it answers like.
TOOLS: dict[str, Any] = {
    'impact': lambda a: {'ref': a[0], **({'max_depth': int(a[2])} if '--depth' in a else {})},
    'trace': lambda a: {'ref': a[0], **({'max_depth': int(a[2])} if '--depth' in a else {})},
    'path': lambda a: {'start': a[0], 'end': a[1]},
    'search': lambda a: {'text': ' '.join(a[: a.index('-n')]), 'limit': int(a[a.index('-n') + 1])},
    'describe': lambda a: {'ref': a[0]},
    'define': lambda a: {'ref': ' '.join(a)},
    'ontology': lambda a: {},
    'explain': lambda a: {'ref': a[0]},
    'query': lambda a: {'question': ' '.join(a)},
}


async def _call(root: Path, calls: list[tuple[str, str, dict[str, Any], Any]]) -> list[Any]:
    async with Client(build_server(root)) as client:
        return [await client.call_tool(tool, arguments) for _, tool, arguments, _ in calls]


def test_mcp_tools_answer_old_graphs_as_the_cli_did(tmp_path: Path, allowed: Allowed) -> None:
    unexplained = []
    for snapshot in SNAPSHOTS:
        root = install(snapshot, tmp_path / snapshot.parent.name / snapshot.name)
        calls = [
            (name, args[0], TOOLS[args[0]](args[1:]), before)
            for name, (args, status, before) in recorded(snapshot).items()
            if args[0] in TOOLS and status == 0
        ]

        assert calls
        for (name, _, _, before), result in zip(calls, anyio.run(_call, root, calls), strict=True):
            assert not result.is_error, (name, result.content)
            after = json.loads(json.dumps(result.structured_content).replace(str(root), '<root>'))
            where = f'{snapshot.parent.name}/{snapshot.name}/{name}'
            unexplained += allowed.unexplained(where, differences(before, after))
    assert unexplained == [], 'answers changed:\n' + '\n'.join(unexplained)
    allowed.checked.add('mcp')


def _git(root: Path, *args: str) -> None:
    env = {'GIT_AUTHOR_NAME': 'Ada', 'GIT_AUTHOR_EMAIL': 'ada@example.com'}
    env |= {'GIT_COMMITTER_NAME': 'Ada', 'GIT_COMMITTER_EMAIL': 'ada@example.com'}
    subprocess.run(
        ['git', '-C', str(root), '-c', 'commit.gpgsign=false', *args],
        env={**env, 'PATH': os.environ['PATH']},
        check=True,
    )


@pytest.mark.dbt
def test_diff_with_an_old_graph_matches_the_diff_with_a_new_one(
    jaffle_built_graph: Path, tmp_path: Path, allowed: Allowed
) -> None:
    """`konstruct diff` reads the graph for what a change breaks downstream: a release's graph of
    the example gives the same review of a change as this code's graph of it."""
    root = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, root, ignore=shutil.ignore_patterns('konstruct-out'))
    (root / '.gitignore').write_text('target/\nlogs/\nkonstruct-out/\n*.duckdb\n')
    _git(root, 'init', '-q', '-b', 'main')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'Add the project')
    payments = root / 'models' / 'staging' / 'stg_payments.sql'
    payments.write_text(payments.read_text().replace('payment_method,', 'payment_method as method,'))
    current = (jaffle_built_graph / 'konstruct-out' / 'graph.json').read_text(encoding='utf-8')
    (root / 'konstruct-out').mkdir()
    (root / 'konstruct-out' / 'graph.json').write_text(current.replace(str(jaffle_built_graph), str(root)))
    expected = Project(root).diff(base='HEAD').to_dict()
    assert expected['breaking'] == [
        {'relation': 'stg_payments', 'column': 'payment_method', 'breaks': ['orders']}
    ]
    unexplained: list[str] = []
    for snapshot in [s for s in SNAPSHOTS if s.name == 'jaffle_shop']:
        install(snapshot, root)
        where = f'{snapshot.parent.name}/{snapshot.name}/diff'
        unexplained += allowed.unexplained(
            where, differences(expected, Project(root).diff(base='HEAD').to_dict())
        )
    assert unexplained == [], 'answers changed:\n' + '\n'.join(unexplained)
    allowed.checked.add('diff')


def test_differences_ignore_added_fields_and_report_the_rest() -> None:
    old = {'total': 2, 'columns': [{'id': 'a'}, {'id': 'b'}], 'summary': 'Two.'}
    assert differences(old, {**old, 'format_version': 1, 'terms': []}) == []
    assert differences(old, {'total': 2, 'columns': [{'id': 'a', 'depth': 1}, {'id': 'c'}]}) == [
        ".columns[1].id: 'b', now 'c'",
        '.summary: removed',
    ]
    assert differences(old, {**old, 'columns': [{'id': 'a'}]}) == ['.columns: 2 items, now 1']


def test_allowed_differences_must_match_and_stay_needed(tmp_path: Path) -> None:
    listed = tmp_path / 'allowed.yml'
    listed.write_text(
        '- {where: "v0.1.*/jaffle_shop/impact_*", path: .columns, reason: Each column says its depth.}\n'
        '- {where: v0.1.0/jaffle_shop/list, path: .items, reason: Nothing differs here any more.}\n'
    )
    allowed = Allowed(listed)
    found = ['.columns[0].depth: 1, now 2', '.columnsx: removed', '.total: 1, now 2']
    assert allowed.unexplained('v0.1.2/jaffle_shop/impact_orders', found) == [
        'v0.1.2/jaffle_shop/impact_orders .columnsx: removed',
        'v0.1.2/jaffle_shop/impact_orders .total: 1, now 2',
    ]
    assert allowed.stale() == ['v0.1.0/jaffle_shop/list .items']
