"""Konstruct's agent toolkit: the skills, subagent, slash commands and instructions it ships.

The texts are package data beside this module, written once and rendered for each place they go:
an installed skill is named ``konstruct-<name>`` so it cannot clash with a project's own skills,
while the Claude Code plugin namespaces its skills itself (``/konstruct:<name>``). A reference to
another skill is written ``[[name]]`` and rendered for its destination.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal

from konstruct import __version__
from konstruct.core import yamlio

Target = Literal['install', 'plugin', 'prompt']

_ROOT = resources.files(__name__)
# The general skill first, then the workflows in the order the general skill lists them.
SKILLS = (
    'konstruct',
    'safe-change',
    'review-change',
    'reconcile',
    'write-query',
    'define-terms',
    'onboard',
    'export-semantic-layer',
)
WORKFLOWS = SKILLS[1:]
COMMANDS = ('impact', 'review', 'gate', 'explain', 'define')
REVIEWER = 'data-reviewer'
PLUGIN = 'konstruct'

_NAMED = re.compile(r'the \[\[([a-z-]+)\]\] skill')
_BARE = re.compile(r'\[\[([a-z-]+)\]\]')
_WORKFLOW = re.compile(r'\[\[(?:{0})\]\]|`konstruct-(?:{0})`'.format('|'.join(SKILLS[1:])))
_REQUEST = '\n## Request\n\n$ARGUMENTS\n'


def text(path: str) -> str:
    """A toolkit file's text, by its path inside the toolkit (``skills/onboard/SKILL.md``)."""
    return _ROOT.joinpath(*path.split('/')).read_text(encoding='utf-8')


def installed_name(name: str) -> str:
    """The name a skill or subagent is installed under: ``konstruct`` stays, others are prefixed."""
    return name if name == PLUGIN else f'{PLUGIN}-{name}'


def _references(body: str, target: Target) -> str:
    def named(match: re.Match[str]) -> str:
        name = match.group(1)
        if target == 'prompt':
            return f'the `{name}` prompt'
        return f'the `{_display(name, target)}` skill'

    body = _NAMED.sub(named, body)
    return _BARE.sub(lambda m: f'`{_display(m.group(1), target)}`', body)


def _display(name: str, target: Target) -> str:
    return f'/{PLUGIN}:{name}' if target == 'plugin' else installed_name(name)


def _rename(body: str, name: str, target: Target) -> str:
    if target != 'install':
        return body
    return body.replace(f'\nname: {name}\n', f'\nname: {installed_name(name)}\n', 1)


def without_workflows(body: str) -> str:
    """``body`` without the paragraphs that point to the workflow skills, for an install that
    leaves them out (the agent benchmark's general-skill arm), and without a paragraph ending in
    a colon that introduced one."""
    blocks = body.split('\n\n')
    kept: list[str] = []
    for block in blocks:
        if _WORKFLOW.search(block):
            if kept and kept[-1].rstrip().endswith(':'):
                kept.pop()
            continue
        kept.append(block)
    result = '\n\n'.join(kept)
    return result + '\n' if body.endswith('\n') and not result.endswith('\n') else result


def without_request(body: str) -> str:
    """``body`` without its ``$ARGUMENTS`` section, for agents that do not expand it."""
    return body.replace(_REQUEST, '') if body.endswith(_REQUEST) else body


def skill(name: str, target: Target = 'install', *, workflows: bool = True, request: bool = True) -> str:
    """A skill's ``SKILL.md`` for ``target``: installed, in the plugin, or as an MCP prompt.

    Args:
        workflows: Keep the references to the workflow skills.
        request: Keep the ``$ARGUMENTS`` section, which only Claude Code expands.
    """
    if name not in SKILLS:
        raise KeyError(name)
    body = text(f'skills/{name}/SKILL.md')
    body = body if workflows else without_workflows(body)
    body = body if request else without_request(body)
    return _references(_rename(body, name, target), target)


def reviewer(target: Target = 'install') -> str:
    """The data-reviewer subagent's definition."""
    return _references(_rename(text(f'agents/{REVIEWER}.md'), REVIEWER, target), target)


def command(name: str) -> str:
    """A plugin slash command (``/konstruct:<name>``)."""
    return _references(text(f'commands/{name}.md'), 'plugin')


def instructions(*, workflows: bool = True) -> str:
    """The section ``konstruct install`` keeps in AGENTS.md, GEMINI.md and copilot-instructions.md."""
    body = text('instructions.md')
    return body if workflows else without_workflows(body)


@dataclass(frozen=True)
class Document:
    """A Markdown file with YAML frontmatter, split into the two."""

    meta: dict[str, Any]
    body: str


def split(document: str) -> Document:
    """Separate a skill's or command's frontmatter from its body."""
    _, block, body = document.split('---\n', 2)
    meta = yamlio.load(block)
    if not isinstance(meta, dict):
        raise ValueError('frontmatter is not a mapping')
    return Document(meta, body.lstrip('\n'))


# Claude Code evaluates each hook's `if` rule before starting it, so an edit to any other file
# starts no process at all; `Edit(...)` rules cover every tool that edits files.
HOOKS: dict[str, Any] = {
    'hooks': {
        'PostToolUse': [
            {
                'matcher': 'Write|Edit|MultiEdit',
                'hooks': [
                    {
                        'type': 'command',
                        'if': f'Edit(**/*.{suffix})',
                        'command': 'konstruct',
                        'args': ['hook', 'post-edit'],
                        'timeout': 120,
                    }
                    for suffix in ('sql', 'yml', 'yaml')
                ],
            }
        ]
    }
}


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2) + '\n'


def plugin_files() -> dict[str, str]:
    """Every file of the Claude Code plugin in ``claude-code-plugin/``, by path inside it.

    The plugin is built from the same texts as ``konstruct install``, so the two cannot drift;
    ``.kodex/scripts/build_plugin.py`` writes it and a test checks it is current. Its version is
    the package's, which ``.kodex/scripts/prepare_release.py`` sets in plugin.json too.
    """
    files = {
        '.claude-plugin/plugin.json': _json(
            {
                'name': PLUGIN,
                'displayName': 'Konstruct',
                'version': __version__,
                'description': (
                    'An ontology of your data stack (entities, measures, definitions and relationships) '
                    'with column-level lineage checked against the warehouse, for dbt, SQL, Airflow and '
                    'Dagster: skills for safe changes, data reviews, reconciling numbers, writing correct '
                    'queries and semantic layer exports, slash commands, a data-reviewer subagent, the '
                    'Konstruct MCP server and a hook that runs konstruct gate after SQL and dbt YAML edits.'
                ),
                'author': {'name': 'Kodex Labs', 'url': 'https://kodexlabs.io'},
                'homepage': 'https://kodexlabsio.github.io/konstruct/guide/agents.html',
                'repository': 'https://github.com/kodexlabsio/konstruct',
                'license': 'MIT',
                'keywords': [
                    'ontology',
                    'dbt',
                    'sql',
                    'lineage',
                    'column-level lineage',
                    'semantic layer',
                    'mcp',
                ],
            }
        ),
        '.mcp.json': _json(
            {
                'mcpServers': {
                    PLUGIN: {'command': 'konstruct', 'args': ['mcp', '--path', '${CLAUDE_PROJECT_DIR}']}
                }
            }
        ),
        'hooks/hooks.json': _json(HOOKS),
        f'agents/{REVIEWER}.md': reviewer('plugin'),
    }
    for name in SKILLS:
        files[f'skills/{name}/SKILL.md'] = skill(name, 'plugin')
    for name in COMMANDS:
        files[f'commands/{name}.md'] = command(name)
    return files
