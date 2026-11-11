"""Golden-file tests: the exact text konstruct prints for the jaffle_shop example.

Paths, timings and dates are normalised. After an intended change to the output, regenerate the
files with `KONSTRUCT_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` and review the diff.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from konstruct import __version__
from konstruct.interfaces.cli import main

GOLDEN = Path(__file__).parent / 'golden'
UPDATE = bool(os.environ.get('KONSTRUCT_UPDATE_GOLDEN'))

pytestmark = pytest.mark.dbt


def normalise(text: str, root: Path) -> str:
    text = text.replace(str(root), '<root>').replace(str(root.resolve()), '<root>')
    text = re.sub(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+\-]\d\d:\d\d', '<time>', text)
    text = re.sub(r'in \d+(\.\d+)?s', 'in <seconds>s', text)
    text = re.sub(rf'([Kk]onstruct) {re.escape(__version__)}', r'\1 <version>', text)
    return text


def check(name: str, text: str) -> None:
    path = GOLDEN / name
    if UPDATE:
        path.write_text(text, encoding='utf-8')
    assert path.is_file(), f'no golden file {path}; run with KONSTRUCT_UPDATE_GOLDEN=1'
    assert text == path.read_text(encoding='utf-8')


@pytest.mark.parametrize(
    ('name', 'args'),
    [
        ('impact_stg_payments_amount.txt', ['impact', 'stg_payments.amount']),
        ('impact_stg_orders.txt', ['impact', 'stg_orders']),
        ('trace_customer_ltv_lifetime_value.txt', ['trace', 'customer_ltv.lifetime_value']),
        ('describe_orders.txt', ['describe', 'orders']),
        ('describe_task.txt', ['describe', 'jaffle_shop_daily.build_customer_ltv']),
        ('path_raw_payments_dashboard.txt', ['path', 'raw_payments', 'customer_dashboard']),
        ('list.txt', ['list']),
        ('search_lifetime_value.txt', ['search', 'lifetime', 'value', '-n', '5']),
        ('query_payment_method.txt', ['query', 'what breaks if I change payment_method']),
        ('sensitivity.txt', ['sensitivity']),
        ('erasure_customer.txt', ['erasure', 'customer']),
    ],
)
def test_command_output(
    jaffle_built_graph: Path, capsys: pytest.CaptureFixture[str], name: str, args: list[str]
) -> None:
    assert main(['--no-color', '-C', str(jaffle_built_graph), *args]) == 0
    check(name, normalise(capsys.readouterr().out, jaffle_built_graph))


def test_report(jaffle_built_graph: Path) -> None:
    text = (jaffle_built_graph / 'konstruct-out' / 'REPORT.md').read_text()
    check('REPORT.md', normalise(text, jaffle_built_graph))


def test_build_summary(jaffle_built_graph: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(['--no-color', 'build', str(jaffle_built_graph), '--update']) == 0
    check('build.txt', normalise(capsys.readouterr().out, jaffle_built_graph))


def test_verify(jaffle_built_graph: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import shutil

    copy = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, copy)
    assert main(['--no-color', '-q', 'verify', '-C', str(copy), '--strict']) == 0
    check('verify.txt', normalise(capsys.readouterr().out, copy))
    assert (copy / 'konstruct-out' / 'VERIFY.md').is_file()
    assert (copy / 'konstruct-out' / 'verify.json').is_file()
