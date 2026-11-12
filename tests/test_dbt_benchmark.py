"""The benchmark on a project dbt compiles: the project it makes, and every measurement on it."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.dbt

SCRIPT = Path(__file__).resolve().parents[1] / '.kodex' / 'scripts' / 'dbt_benchmark.py'


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location('dbt_benchmark', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules['dbt_benchmark'] = module
    spec.loader.exec_module(module)
    return module


def test_the_project_is_compiled_by_dbt_with_tags_and_dags(tmp_path: Path) -> None:
    benchmark = load()
    project = benchmark.prepare(tmp_path, 60)
    manifest = json.loads((project / 'target' / 'manifest.json').read_text())
    models = {k: v for k, v in manifest['nodes'].items() if k.startswith('model.')}
    assert len(models) == 60
    assert models['model.big.m055']['tags'] == ['group1']
    assert models['model.big.m055']['depends_on']['nodes'] == ['model.big.m027', 'model.big.m018']
    assert 'model.big.m055' in json.loads((project / 'target' / 'catalog.json').read_text())['nodes']
    assert sorted(p.name for p in (project / 'dags').iterdir()) == ['group0.py']
    assert benchmark.prepare(tmp_path, 60) == project  # made once


def test_every_command_is_measured(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    benchmark = load()
    assert (
        benchmark.main(
            ['--work', str(tmp_path), '--models', '60', '--repeat', '1', '--json', str(tmp_path / 'r.json')]
        )
        == 0
    )
    found = json.loads((tmp_path / 'r.json').read_text())['medians']
    (medians,) = found.values()
    assert list(medians) == [
        'cold build', '--update, no change', 'trace m059.c01', 'impact m010.c01', 'explain m059.c01',
        'check-sql', 'conflicts', 'hook, files as at its last run', 'hook after an edit to the last model',
        'gate after an edit', 'MCP gate over its copy',
    ]  # fmt: skip
    assert all(v['total'] >= v['own'] > 0 for v in medians.values())
    assert '| cold build |' in capsys.readouterr().out
    # The edits the hook and the gate judge are undone.
    assert '* 3 as c01' not in (tmp_path / 'dbt-60' / 'models' / 'm045.sql').read_text()
    assert '* 3 as c01' not in (tmp_path / 'dbt-60' / 'models' / 'm059.sql').read_text()


def test_a_command_that_fails_stops_the_benchmark(tmp_path: Path) -> None:
    """A failing command is no fast time: only the gate may exit 1 (a change it fails), and
    nothing may raise."""
    benchmark = load()
    project = benchmark.prepare(tmp_path, 60)
    root = Path(__file__).resolve().parents[1]
    assert benchmark.run(root, project, benchmark.CLI, 'build', '--no-dbt', '--no-warehouse', '-q')['own'] > 0
    with pytest.raises(SystemExit, match='trace'):
        benchmark.run(root, project, benchmark.CLI, 'trace', 'no_such_model.c00')
    with pytest.raises(SystemExit, match='Traceback'):
        benchmark.run(root, project, 'raise RuntimeError("broken")', allowed=(0, 1))
    assert benchmark.run(root, project, 'import sys; sys.exit(1)', allowed=(0, 1))['own'] > 0
