from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from konstruct.interfaces.skill import InstallError, install, render_skill


def frontmatter(text: str) -> dict[str, object]:
    _, block, _ = text.split('---', 2)
    data = yaml.safe_load(block)
    assert isinstance(data, dict)
    return data


def test_skill_frontmatter_suits_both_agents() -> None:
    meta = frontmatter(render_skill())
    assert meta['name'] == 'konstruct'
    assert isinstance(meta['description'], str) and len(meta['description']) < 1024
    assert meta['allowed-tools'] == 'Bash(konstruct *)'
    assert '$ARGUMENTS' in render_skill()


def test_install_for_both_writes_one_claude_skill(tmp_path: Path) -> None:
    messages = install(agents='both', home=tmp_path)
    path = tmp_path / '.claude' / 'skills' / 'konstruct' / 'SKILL.md'
    assert path.read_text() == render_skill()
    assert messages == [f'installed /konstruct skill for Claude Code and Cursor: {path}']
    assert not (tmp_path / '.cursor').exists()
    assert install(agents='both', home=tmp_path)[0].startswith('unchanged')


def test_install_for_cursor_only(tmp_path: Path) -> None:
    install(agents='cursor', home=tmp_path)
    assert (tmp_path / '.cursor' / 'skills' / 'konstruct' / 'SKILL.md').is_file()
    assert not (tmp_path / '.claude').exists()


def test_reinstall_updates_old_versions_but_not_hand_edits(tmp_path: Path) -> None:
    install(agents='claude', home=tmp_path)
    path = tmp_path / '.claude' / 'skills' / 'konstruct' / 'SKILL.md'
    path.write_text(render_skill().replace('konstruct 0', 'konstruct 0.0.0-old 0', 1))
    assert install(agents='claude', home=tmp_path)[0].startswith('updated')
    path.write_text(render_skill().replace('Steps', 'My steps'))
    with pytest.raises(InstallError, match='edited by hand'):
        install(agents='claude', home=tmp_path)
    assert install(agents='claude', home=tmp_path, force=True)[0].startswith('updated')
    assert path.read_text() == render_skill()


def test_project_install_registers_mcp_without_losing_other_servers(tmp_path: Path) -> None:
    (tmp_path / '.mcp.json').write_text(json.dumps({'mcpServers': {'other': {'command': 'x'}}}))
    messages = install(agents='both', project=tmp_path, mcp=True)
    assert len(messages) == 3
    claude = json.loads((tmp_path / '.mcp.json').read_text())
    assert claude['mcpServers'] == {
        'other': {'command': 'x'},
        'konstruct': {'command': 'konstruct', 'args': ['mcp', '--path', '.']},
    }
    cursor = json.loads((tmp_path / '.cursor' / 'mcp.json').read_text())
    assert cursor['mcpServers']['konstruct']['args'] == ['mcp', '--path', '${workspaceFolder}']
    assert all(m.startswith('unchanged') for m in install(agents='both', project=tmp_path, mcp=True))


def test_mcp_needs_a_project_and_valid_json(tmp_path: Path) -> None:
    messages = install(agents='claude', home=tmp_path, mcp=True)
    assert 'claude mcp add konstruct -- konstruct mcp' in messages[-1]
    (tmp_path / '.mcp.json').write_text('{broken')
    with pytest.raises(InstallError, match='not valid JSON'):
        install(agents='claude', project=tmp_path, mcp=True)
