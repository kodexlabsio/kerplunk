"""The agent toolkit's texts: house style, valid frontmatter, real commands, a current plugin, and
the claims each skill makes about Konstruct's output, checked on a real build of jaffle_shop."""

from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from konstruct.analysis.gate import RULES
from konstruct.interfaces import toolkit
from konstruct.interfaces.cli import main, parser
from konstruct.interfaces.skill import render_rule, render_skill

REPO = Path(__file__).resolve().parents[1]
TOOLKIT = REPO / 'konstruct' / 'interfaces' / 'toolkit'
TEXTS = sorted(p for p in TOOLKIT.rglob('*.md'))
# Words in the American spelling that the house style writes the British way.
EMOJI = re.compile(f'[{chr(0x1F000)}-{chr(0x1FAFF)}{chr(0x2600)}-{chr(0x27BF)}]')
AMERICAN = re.compile(r'\b(behavior|summariz\w*|organiz\w*|analyz\w*|recogniz\w*|color(?!:)|license\b)', re.I)


def all_texts() -> dict[str, str]:
    texts = {
        f'skill {n}': toolkit.skill(n, t) for n in toolkit.SKILLS for t in ('install', 'plugin', 'prompt')
    }
    texts |= {f'command {n}': toolkit.command(n) for n in toolkit.COMMANDS}
    texts |= {'reviewer': toolkit.reviewer(), 'instructions': toolkit.instructions()}
    texts |= {f'rule {a}': render_rule(a) for a in ('cursor', 'windsurf')}
    texts |= {f'general {t}': toolkit.skill('konstruct', t, workflows=False) for t in ('install', 'plugin')}
    texts |= {'general instructions': toolkit.instructions(workflows=False)}
    return texts


def test_every_toolkit_file_is_rendered() -> None:
    names = {p.relative_to(TOOLKIT).as_posix() for p in TEXTS}
    expected = {f'skills/{n}/SKILL.md' for n in toolkit.SKILLS}
    expected |= {f'commands/{n}.md' for n in toolkit.COMMANDS}
    expected |= {f'agents/{toolkit.REVIEWER}.md', 'instructions.md'}
    assert names == expected


@pytest.mark.parametrize('name', sorted(all_texts()))
def test_texts_follow_the_house_style(name: str) -> None:
    text = all_texts()[name]
    assert chr(0x2014) not in text and chr(0x2013) not in text, 'no em or en dashes'
    assert not EMOJI.search(text), 'no emojis'
    assert not AMERICAN.search(text), AMERICAN.search(text)
    assert '[[' not in text, 'every skill reference is rendered'
    assert not re.search(r'\b(claude|anthropic)\b.*\b(generated|written) (by|with)\b', text, re.I)


@pytest.mark.parametrize('name', toolkit.SKILLS)
def test_skill_frontmatter_suits_every_agent(name: str) -> None:
    for target in ('install', 'plugin'):
        document = toolkit.split(toolkit.skill(name, target))
        meta = document.meta
        expected = toolkit.installed_name(name) if target == 'install' else name
        # Cursor and Copilot want lowercase names with hyphens that match the folder.
        assert meta['name'] == expected and re.fullmatch(r'[a-z][a-z0-9-]*', expected)
        assert set(meta) <= {'name', 'description', 'argument-hint', 'allowed-tools'}
        assert isinstance(meta['description'], str) and 100 < len(meta['description']) <= 1024
        assert str(meta['allowed-tools']).startswith('Bash(konstruct *)')
        assert document.body.rstrip().endswith('$ARGUMENTS')


def test_skill_references_are_named_for_each_destination() -> None:
    assert '`konstruct-safe-change`' in toolkit.skill('konstruct')
    assert '`/konstruct:safe-change`' in toolkit.skill('konstruct', 'plugin')
    assert 'the `safe-change` skill' in toolkit.skill('write-query', 'install').replace('konstruct-', '')
    assert 'the `safe-change` prompt' in toolkit.skill('write-query', 'prompt')
    with pytest.raises(KeyError):
        toolkit.skill('nothing')


def commands(text: str) -> list[str]:
    """Every `konstruct ...` command a text tells an agent to run."""
    found = re.findall(r'`(konstruct [^`]+)`', text)
    found += [line.strip() for line in text.splitlines() if line.strip().startswith('konstruct ')]
    return found


def argv(command: str) -> list[str]:
    command = command.replace('$ARGUMENTS', 'orders.amount')
    command = re.sub(r'"<[^>]+>"', 'x', command)
    command = re.sub(r'<[^>]+>', 'x', command)
    return shlex.split(command)[1:]


@pytest.mark.parametrize('name', sorted(all_texts()))
def test_every_command_in_the_texts_is_real(name: str) -> None:
    found = commands(all_texts()[name])
    if name.startswith(('skill', 'command', 'instructions', 'reviewer')):
        assert found
    action = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))
    subcommands = set(action.choices)
    for command in found:
        words = argv(command)
        if len(words) == 1 and words[0] in subcommands:
            continue  # a command named in prose, such as `konstruct impact` on each column
        try:
            parser().parse_args(words)
        except SystemExit as error:  # argparse exits on an unknown command or option
            raise AssertionError(f'{name}: `{command}` is not a valid command') from error


def test_gate_rules_in_the_texts_are_the_gates_own() -> None:
    for name in ('safe-change', 'review-change'):
        text = toolkit.skill(name)
        assert all(f'`{rule}`' in text for rule in RULES)
    assert all(rule in toolkit.reviewer() for rule in RULES)


def test_installed_skills_end_with_a_marker() -> None:
    for name in toolkit.SKILLS:
        text = render_skill(name)
        assert text.startswith(toolkit.skill(name)) and '<!-- generated by konstruct' in text


def test_toolkit_texts_are_package_data() -> None:
    # Packaged under konstruct/, so they ship in the wheel whatever the build backend.
    assert all(p.is_relative_to(REPO / 'konstruct') for p in TEXTS)
    assert toolkit.text('instructions.md') == (TOOLKIT / 'instructions.md').read_text()


# The claims: each skill quotes Konstruct's output, and the output must say what the skill quotes.


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


@pytest.fixture
def jaffle_repo(jaffle_built_graph: Path, tmp_path: Path) -> Path:
    project = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, project)
    (project / '.gitignore').write_text('konstruct-out/\ntarget/\nlogs/\n*.duckdb*\n.user.yml\n')
    git(project, 'init', '-q', '-b', 'main')
    git(project, 'add', '-A')
    git(project, 'commit', '-q', '-m', 'Example')
    return project


class Claims:
    def __init__(self, capsys: pytest.CaptureFixture[str], root: Path) -> None:
        self.capsys = capsys
        self.root = root

    def run(self, *args: str) -> tuple[int, str]:
        code = main(['--no-color', '-C', str(self.root), *args])
        return code, self.capsys.readouterr().out

    def hold(self, skill: str, output: str, *phrases: str) -> None:
        """Each phrase is in the skill's text and in the output it describes."""
        text = toolkit.skill(skill) if skill in toolkit.SKILLS else toolkit.reviewer()
        text = ' '.join(text.split())  # a phrase may be wrapped across lines
        for phrase in phrases:
            assert ' '.join(phrase.split()) in text, f'{skill} does not say {phrase!r}'
            assert phrase in output, f'the output does not show {phrase!r}, as {skill} says'


@pytest.mark.dbt
def test_the_skills_describe_real_output(jaffle_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    claims = Claims(capsys, jaffle_repo)

    code, out = claims.run('status')
    claims.hold('onboard', out, 'up to date')
    # One line each, as the skill lists them.
    rows = {line.split()[0] for line in out.splitlines()[1:] if line.startswith('  ')}
    assert {'config', 'dbt', 'warehouse', 'airflow', 'sql', 'definitions', 'graph', 'skills'} <= rows
    for named in ('configuration', 'dbt projects', 'warehouses', 'DAG and SQL files', 'definitions files'):
        assert named in ' '.join(toolkit.skill('onboard').split())
    report = (jaffle_repo / 'konstruct-out' / 'REPORT.md').read_text()
    for section in re.findall(
        r'\*\*([A-Z][a-z ]+)\*\*', toolkit.skill('onboard').split('## 2. The Report')[1].split('## 3')[0]
    ):
        assert f'## {section}' in report, section

    _, out = claims.run('impact', 'stg_payments.amount')
    claims.hold('konstruct', out, 'Lineage', 'transformed', 'direct', 'reference', 'via', 'Business terms')
    claims.hold('safe-change', out, 'transformed', 'direct', 'reference')
    # The models that join, filter or group on the column are reference lines in the tree.
    assert re.search(r'customer_ltv +reference', out)
    assert '**Lineage**' in toolkit.skill('safe-change') and '\nLineage\n' in out
    for heading in ('Tests', 'Exposures', 'Orchestration', 'Business terms'):
        assert f'**{heading}**' in toolkit.skill('safe-change') and f'\n{heading} (' in out

    _, out = claims.run('trace', 'customers.customer_lifetime_value')
    claims.hold('reconcile', out, 'Columns that filter or join its relation')
    _, out = claims.run('trace', 'customer_dashboard')
    claims.hold('konstruct', out, 'reference')
    _, out = claims.run('describe', 'customer_dashboard')
    assert 'Upstream (' in out and re.search(r'\n  customers +model', out)

    _, out = claims.run('explain', 'customer_ltv.lifetime_value')
    claims.hold('reconcile', out, 'In terms of sources', 'Sources', 'Step by step')
    for clause in ('WHERE', 'JOIN ON', 'GROUP BY'):
        assert f'`{clause}`' in toolkit.skill('reconcile') and re.search(rf'\n +(LEFT )?{clause}  ', out)

    _, out = claims.run('define', 'lifetime', 'value')
    # Each field of a definition is a line that starts with its name.
    for field in ('owner', 'approved', 'grain', 'unit', 'exceptions', 'columns'):
        assert f'`{field}`' in toolkit.skill('write-query') and re.search(rf'\n  {field} ', out), field
    assert 'Customer lifetime value  approved' in out
    _, out = claims.run('define', 'completed', 'order')
    claims.hold('write-query', out, 'required filters')
    assert main(['--no-color', '-C', str(jaffle_repo), 'define', 'zebra']) == 1
    claims.hold('define-terms', capsys.readouterr().err.replace("'zebra'", 'no term'), 'no term')

    _, out = claims.run('ontology')
    claims.hold('write-query', out, 'Entities', 'Measures', 'Relationships')
    claims.hold('write-query', out, 'customer  key customer_id  customers, stg_customers')
    claims.hold(
        'write-query',
        out,
        'order -> customer  many-to-one  orders.customer_id = customers.customer_id   tested',
    )
    assert re.search(r'verified \d+/\d+', out)
    # Each measure with its status, owner and grain.
    assert 'each with its status, owner and grain' in ' '.join(toolkit.skill('write-query').split())
    assert re.search(
        r'Customer lifetime value +approved +Priya Shah \(Head of Finance\), one row per customer', out
    )

    _, out = claims.run('conflicts', '--no-write')
    claims.hold('reconcile', out, 'No conflicting definitions')
    assert not (jaffle_repo / 'konstruct-out' / 'CONFLICTS.md').exists()

    _, out = claims.run('list', 'tasks')
    assert 'jaffle_shop_daily.dbt_run_marts' in out

    code, out = claims.run('gate', '--no-dbt')
    assert code == 0
    claims.hold('safe-change', out, 'Gate passed')

    # A breaking rename: diff and gate say what the skills say they say.
    staging = jaffle_repo / 'models' / 'staging' / 'stg_payments.sql'
    staging.write_text(staging.read_text().replace('amount / 100 as amount', 'amount / 100 as amount_aud'))
    _, out = claims.run('diff')
    claims.hold(
        'review-change',
        out.replace('BREAKS customers', 'BREAKS <model>'),
        'BREAKS <model>, which still reads it',
    )
    claims.hold('review-change', out, 'dbt build --select')
    # A header, one line of counts, and a dbt build --select as the last line.
    lines = out.rstrip().splitlines()
    assert 'a header, one line of counts' in ' '.join(toolkit.skill('review-change').split())
    assert re.fullmatch(r'\d+ breaking change, .*\.', lines[1]) and lines[-1].strip().startswith(
        'dbt build --select '
    )
    code, out = claims.run('gate', '--no-dbt')
    assert code == 1
    assert 'Gate failed' in out and 'Gate failed' in toolkit.skill('review-change')
    for field in ('file', 'fix'):
        assert f'`{field}`' in toolkit.skill('safe-change') and re.search(rf'\n  {field} ', out), field
    assert 'breaking: stg_payments.amount is removed but still read by customers, orders' in out
    git(jaffle_repo, 'checkout', '--', '.')

    # Changing an approved term's column asks for its owner.
    orders = jaffle_repo / 'models' / 'orders.sql'
    orders.write_text(orders.read_text().replace('total_amount as amount', 'total_amount * 2 as amount'))
    code, out = claims.run('gate', '--no-dbt', '--format', 'markdown')
    assert code == 1 and 'approved-definition-changed' in out
    git(jaffle_repo, 'checkout', '--', '.')


@pytest.mark.dbt
def test_the_export_skill_describes_the_ossie_export(
    jaffle_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    claims = Claims(capsys, jaffle_repo)
    code, _ = claims.run('export', 'ossie', '-o', str(jaffle_repo / 'semantic-model.yaml'))
    assert code == 0
    model = (jaffle_repo / 'semantic-model.yaml').read_text()
    claims.hold('export-semantic-layer', model, 'primary_key', 'ai_context', 'KONSTRUCT')
    # The message the skill quotes for a measure that is left out is the export's own.
    source = (REPO / 'konstruct' / 'semantics' / 'ossie.py').read_text()
    assert "'ossie export: measure %r is left out: %s'" in source
    assert "'its formula does not reach source columns'" in source
    # An export named as the skill says is not read back as an input.
    capsys.readouterr()
    code, out = claims.run('build', '--update', '--no-dbt')
    assert code == 0 and 'repeats' not in out and 'same name as a term' not in out


@pytest.mark.dbt
def test_the_verify_step_reads_as_the_skill_says(
    jaffle_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    claims = Claims(capsys, jaffle_repo)
    code, out = claims.run('verify', '-m', 'customers')
    assert code == 0
    claims.hold('reconcile', out, 'confirmed', 'unexplained')
    # The edges verify confirmed are marked in impact and trace, as the general skill says.
    _, out = claims.run('impact', 'stg_payments.amount')
    assert re.search(r'customers\.customer_lifetime_value +transformed +confirmed', out), out
    _, out = claims.run('trace', 'customers.customer_lifetime_value')
    claims.hold('konstruct', out, 'confirmed')
