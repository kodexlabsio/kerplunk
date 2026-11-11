"""The plugin registry: Konstruct's own inputs, exports, checks and packs, and those of installed plugins.

Plugins are found only through the entry points of installed distributions (``importlib.metadata``),
never in the repository being analysed. Konstruct's own components are listed here in the same
form, ``module:attribute``, and loaded and run by the same code, so a plugin goes through exactly
the path a built-in does.

A third-party plugin runs only when the project enables its distribution by name in
``konstruct.toml`` (``[plugins] enabled``), and never with ``--no-plugins`` or
``KONSTRUCT_NO_PLUGINS=1``. Nothing is imported until a group is first used, so commands that use
no plugins pay nothing for them.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import re
import tomllib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from konstruct.plugins.api import briefly, shown

if TYPE_CHECKING:
    from konstruct.engine.config import Config
    from konstruct.interfaces.project import Project

log = logging.getLogger(__name__)

GROUPS: tuple[str, ...] = ('inputs', 'exports', 'checks', 'packs', 'mcp_tools')
"""The plugin groups, each the entry-point group ``konstruct.<group>``."""

SINGULAR = {'inputs': 'input', 'exports': 'export', 'checks': 'check', 'packs': 'pack', 'mcp_tools': 'tools'}

BUILTINS: dict[str, tuple[tuple[str, str], ...]] = {
    'inputs': (('looker', 'konstruct.inputs.looker:INPUT'),),
    'exports': (
        ('openlineage', 'konstruct.outputs.export:OPENLINEAGE'),
        ('mermaid', 'konstruct.outputs.export:MERMAID'),
        ('dot', 'konstruct.outputs.export:DOT'),
        ('csv', 'konstruct.outputs.export:CSV'),
        ('ossie', 'konstruct.outputs.export:OSSIE'),
    ),
    'checks': (
        ('breaking', 'konstruct.analysis.gate:BREAKING'),
        ('approved-definition-changed', 'konstruct.analysis.gate:APPROVED_DEFINITION_CHANGED'),
        ('conflicting-definition', 'konstruct.analysis.gate:CONFLICTING_DEFINITION'),
        ('data-law-broken', 'konstruct.analysis.gate:DATA_LAW_BROKEN'),
        ('consumer-pin-changed', 'konstruct.analysis.gate:CONSUMER_PIN_CHANGED'),
    ),
    'packs': (('konstruct', 'konstruct.semantics.ontologies'),),
    'mcp_tools': (),
}
"""Konstruct's own components, by group: each name with the object (or, for packs, the package
directory) that provides it."""

# The input names Konstruct uses for its own inputs, configuration tables and node id prefixes:
# an input's nodes are named ``<input>.<id>`` and its settings ``[<input>]``, so a plugin input may
# not take one of these.
RESERVED = frozenset(
    {
        'airbyte', 'airflow', 'analysis', 'column', 'dag', 'dagster', 'dbt', 'definitions', 'exposure',
        'fivetran', 'looker', 'metric', 'model', 'ontologies', 'operation', 'ossie', 'plugins',
        'powerbi', 'repositories', 'saved_query', 'seed', 'semantic_model', 'snapshot', 'source',
        'spark', 'sql', 'table', 'task', 'term', 'test', 'usage', 'vocabularies', 'vocabulary',
        'warehouse',
    }
)  # fmt: skip

NAME = re.compile(r'^[a-z][a-z0-9_-]*$')
MANIFEST = 'plugin.toml'
"""The file in a pack directory that declares its ``api_version``."""

_REQUIRED: dict[str, tuple[str, ...]] = {
    'inputs': ('patterns', 'read'),
    'exports': ('description', 'export'),
    'checks': ('description', 'check'),
    'mcp_tools': ('tools',),
}


class PluginError(Exception):
    """A plugin that cannot be used as asked, or that failed while it ran."""


def normalise(name: str) -> str:
    """A distribution name in its canonical form (PEP 503), as ``[plugins] enabled`` compares them."""
    return re.sub(r'[-_.]+', '-', name).lower()


def describe(error: BaseException) -> str:
    """An exception as one line, its type first, cut short. A plugin's exception says what the
    plugin wrote, so text JSON cannot hold (a lone surrogate) is escaped: the line goes into
    graph.json's warnings."""
    try:
        text = str(error).strip().splitlines()
    except Exception:
        text = []
    line = f'{type(error).__name__}: {text[0]}' if text else type(error).__name__
    line = line.encode('utf-8', 'backslashreplace').decode('utf-8')
    return line if len(line) <= 300 else f'{line[:297]}...'


@dataclass
class Plugin:
    """One component in the registry: Konstruct's own, or one an installed plugin provides."""

    name: str
    """The entry point's name: an input's name, an export format, a check's rule prefix, a tool
    prefix or a pack directory's name."""
    group: str
    """One of :data:`GROUPS`."""
    target: str
    """What the entry point names: ``module:attribute``, or a package path for packs."""
    distribution: str
    """The distribution that provides it (``konstruct`` for Konstruct's own)."""
    version: str
    """The distribution's version."""
    builtin: bool
    """Whether it is part of Konstruct."""
    enabled: bool
    """Whether it runs for this project."""
    reason: str
    """Why it does or does not run."""
    api_version: tuple[int, int] | None = None
    """The plugin API version it targets, once loaded, as ``(major, minor)``."""
    error: str | None = None
    """Why it could not be loaded or was refused, when it could not."""
    loaded: Any = field(default=None, repr=False, compare=False)
    """The loaded object, or the directory for a pack."""
    requested: bool = field(default=False, repr=False)
    """Whether the project enabled it before it was refused."""

    @property
    def label(self) -> str:
        """How messages name it: ``input 'pipelines' (konstruct-yaml-pipelines 0.1.0)``."""
        return f'{SINGULAR[self.group]} {self.name!r} ({self.distribution} {self.version})'

    def to_dict(self) -> dict[str, Any]:
        """The plugin as JSON-ready data."""
        return {
            'name': self.name,
            'group': self.group,
            'target': self.target,
            'distribution': self.distribution,
            'version': self.version,
            'builtin': self.builtin,
            'enabled': self.enabled,
            'reason': self.reason,
            'api_version': shown(self.api_version) if self.api_version is not None else None,
        }


def _turned_off(off: bool, environment: bool) -> str | None:
    if off:
        return 'plugins are turned off (--no-plugins)'
    value = os.environ.get('KONSTRUCT_NO_PLUGINS', '') if environment else ''
    if value and value != '0':
        return 'plugins are turned off (KONSTRUCT_NO_PLUGINS)'
    return None


class Plugins:
    """The registry for one project: every component, which of them run, and the problems found."""

    def __init__(self, plugins: list[Plugin], missing: Sequence[str] = ()) -> None:
        self.plugins = plugins
        """Every component, Konstruct's own first, then installed plugins by distribution."""
        self.missing = list(missing)
        """Distributions ``[plugins] enabled`` names that are not installed."""
        self.off: str | None = None
        """Why plugins are turned off, when they are."""

    @property
    def problems(self) -> list[str]:
        """What stops a plugin the project enables from running, for build warnings."""
        return [f'{p.label} is refused: {p.error}' for p in self.installed() if p.requested and p.error] + [
            f'konstruct.toml enables {name!r}, which is not installed' for name in self.missing
        ]

    @classmethod
    def builtin(cls) -> Plugins:
        """Konstruct's own components alone."""
        from konstruct import __version__

        return cls(
            [
                Plugin(name, group, target, 'konstruct', __version__, True, True, 'built in')
                for group in GROUPS
                for name, target in BUILTINS[group]
            ]
        )

    @classmethod
    def discover(cls, enabled: Iterable[str] = (), *, off: bool = False, environment: bool = True) -> Plugins:
        """Konstruct's own components and every installed plugin, running those whose
        distributions ``enabled`` names unless ``off`` (``--no-plugins``) or, with
        ``environment``, ``KONSTRUCT_NO_PLUGINS`` turns plugins off."""
        from importlib.metadata import entry_points

        registry = cls.builtin()
        allowed = {normalise(name) for name in enabled}
        turned_off = _turned_off(off, environment)
        found: list[Plugin] = []
        installed: set[str] = set()
        every = entry_points()
        for group in GROUPS:
            for entry in every.select(group=f'konstruct.{group}'):
                dist = entry.dist
                distribution = normalise(dist.name) if dist is not None else 'unknown'
                installed.add(distribution)
                if turned_off:
                    state, reason = False, turned_off
                elif distribution in allowed:
                    state, reason = True, 'enabled in konstruct.toml'
                else:
                    state, reason = False, f'not enabled: add "{distribution}" to [plugins] enabled'
                found.append(
                    Plugin(
                        entry.name,
                        group,
                        entry.value,
                        distribution,
                        dist.version if dist is not None else '?',
                        False,
                        state,
                        reason,
                    )
                )
        found.sort(key=lambda p: (p.distribution, GROUPS.index(p.group), p.name))
        registry._refuse_bad_names(found)
        registry.plugins += found
        registry.missing = sorted(allowed - installed)
        registry.off = turned_off
        return registry

    @classmethod
    def for_config(cls, config: Config) -> Plugins:
        """The registry as ``config`` sets it: its ``[plugins] enabled`` list, unless plugins are
        turned off."""
        return cls.discover(config.plugins, off=not config.use_plugins)

    def _refuse_bad_names(self, found: list[Plugin]) -> None:
        taken = {(p.group, p.name) for p in self.plugins}
        counts: dict[tuple[str, str], list[str]] = {}
        for plugin in found:
            counts.setdefault((plugin.group, plugin.name), []).append(plugin.distribution)
        for plugin in found:
            if not NAME.match(plugin.name):
                refusal = f'the name {plugin.name!r} must be lower case letters, digits, - and _'
            elif (plugin.group, plugin.name) in taken:
                refusal = f'Konstruct has its own {SINGULAR[plugin.group]} named {plugin.name!r}'
            elif plugin.group == 'inputs' and plugin.name in RESERVED:
                refusal = f'Konstruct reserves the input name {plugin.name!r}'
            elif len(counts[(plugin.group, plugin.name)]) > 1:
                refusal = f'{" and ".join(counts[(plugin.group, plugin.name)])} both provide it'
            else:
                continue
            self._refuse(plugin, refusal)

    def _refuse(self, plugin: Plugin, refusal: str) -> None:
        plugin.requested = plugin.requested or plugin.enabled
        plugin.enabled, plugin.error, plugin.reason = False, refusal, f'refused: {refusal}'

    # Lookup

    def names(self, group: str) -> list[str]:
        """Every name in ``group``, whether or not it runs, each once."""
        return list(dict.fromkeys(p.name for p in self.plugins if p.group == group))

    def get(self, group: str, name: str) -> Plugin | None:
        """The component of ``group`` called ``name``, loaded if it runs."""
        for plugin in self.plugins:
            if plugin.group == group and plugin.name == name:
                if plugin.enabled:
                    self._load(plugin)
                return plugin
        return None

    def group(self, group: str) -> list[Plugin]:
        """The components of ``group`` that run, loaded; one that cannot be loaded is left out,
        with a problem recorded."""
        running = [p for p in self.plugins if p.group == group and p.enabled]
        for plugin in running:
            self._load(plugin)
        return [p for p in running if p.enabled]

    def failed(self, group: str) -> list[Plugin]:
        """The components of ``group`` the project enables that could not be loaded or were refused."""
        return [p for p in self.installed() if p.group == group and p.requested and p.error]

    def installed(self) -> list[Plugin]:
        """The components of installed plugins, not Konstruct's own."""
        return [p for p in self.plugins if not p.builtin]

    def tools(self, project: Project) -> list[tuple[str, Callable[..., Any], Plugin]]:
        """The MCP tools of the plugins that run, each named ``<plugin>_<tool>``, bound to
        ``project`` and with the plugin it comes from. A plugin whose ``tools`` raises or returns
        no mapping, a tool named badly, and a tool whose joined name another plugin's tool already
        has, are left out with a warning logged."""
        from collections.abc import Mapping

        found: list[tuple[str, Callable[..., Any], Plugin]] = []
        named: dict[str, tuple[Plugin, str]] = {}
        for plugin in self.group('mcp_tools'):
            try:
                tools = plugin.loaded.tools(project)
                if not isinstance(tools, Mapping):
                    log.warning(
                        '%s returned %s, not a mapping of names to functions; its tools are left out',
                        plugin.label,
                        briefly(tools),
                    )
                    continue
                # A name that is not a string still sorts, by its text, so it can be reported.
                listed = sorted(dict(tools).items(), key=lambda item: str(item[0]))
            except Exception as error:
                log.warning('%s raised %s; its tools are left out', plugin.label, describe(error))
                continue
            for name, function in listed:
                if not isinstance(name, str) or not NAME.match(name) or not callable(function):
                    log.warning(
                        '%s: tool %s is left out: it needs a lower-case name and a function',
                        plugin.label,
                        briefly(name),
                    )
                    continue
                # Konstruct's own tools have no underscore, so only another plugin's can clash:
                # a_b's tool c and a's tool b_c are both a_b_c.
                joined = f'{plugin.name}_{name}'
                if joined in named:
                    other, tool = named[joined]
                    log.warning(
                        '%s: tool %r is left out: its name, %s, is taken by tool %r of tools %r',
                        plugin.label,
                        name,
                        joined,
                        tool,
                        other.name,
                    )
                    continue
                named[joined] = (plugin, name)
                found.append((joined, function, plugin))
        return found

    def load_all(self) -> Plugins:
        """Load every component that runs, so their API versions and load errors are known."""
        for group in GROUPS:
            self.group(group)
        return self

    # Loading

    def _load(self, plugin: Plugin) -> None:
        if plugin.loaded is not None:
            return
        from konstruct.plugins.api import API_VERSION, api_version, supported

        try:
            if plugin.group == 'packs':
                plugin.loaded, version = _pack_directory(plugin.target)
            else:
                plugin.loaded = _load_object(plugin.target)
                version = getattr(plugin.loaded, 'api_version', None)
        except Exception as error:
            if plugin.builtin:
                raise
            self._refuse(plugin, f'cannot be loaded: {describe(error)}')
            return
        plugin.api_version = api_version(version)
        refusal = None
        if plugin.api_version is None:
            refusal = 'it declares no api_version'
        elif not supported(plugin.api_version):
            runs = shown(API_VERSION) if not API_VERSION[1] else f'{API_VERSION[0]}.0 to {shown(API_VERSION)}'
            refusal = f'it targets plugin API {shown(plugin.api_version)}; this Konstruct runs API {runs}'
        elif isinstance(plugin.loaded, type):
            refusal = f'{plugin.target} is a class; the entry point names an instance'
        else:
            missing = [a for a in _REQUIRED.get(plugin.group, ()) if not hasattr(plugin.loaded, a)]
            if missing:
                refusal = f'it has no {", ".join(missing)}'
        if refusal is not None:
            if plugin.builtin:
                raise PluginError(f'{plugin.label}: {refusal}')
            self._refuse(plugin, refusal)

    # The listing

    def to_dict(self) -> dict[str, Any]:
        """The registry as JSON-ready data: the plugin API version, every component and the problems."""
        from konstruct.plugins.api import API_VERSION

        return {
            'api_version': shown(API_VERSION),
            'plugins': [p.to_dict() for p in self.plugins],
            'problems': self.problems,
        }

    def text(self, style: Any = None) -> str:
        """The listing as the CLI prints it, the same text on a terminal as in a pipe."""
        return str(self)

    def __str__(self) -> str:
        from konstruct.plugins.api import API_VERSION

        lines = [f'Plugins (plugin API {shown(API_VERSION)})', '']
        installed = self.installed()
        if not installed:
            lines.append('No plugins are installed.')
        by_distribution: dict[str, list[Plugin]] = {}
        for plugin in installed:
            by_distribution.setdefault(plugin.distribution, []).append(plugin)
        for distribution, plugins in by_distribution.items():
            state = 'enabled' if any(p.enabled for p in plugins) else plugins[0].reason
            lines.append(f'{distribution} {plugins[0].version}: {state}')
            for plugin in plugins:
                api = f'API {shown(plugin.api_version)}' if plugin.api_version is not None else 'API ?'
                note = '' if plugin.enabled or plugin.error is None else f'  {plugin.reason}'
                lines.append(f'  {plugin.group:<10} {plugin.name:<24} {api}{note}')
        for name in self.missing:
            lines.append(f'{name}: enabled in konstruct.toml, but not installed')
        builtin = '; '.join(
            f'{group.replace("_", " ")} {", ".join(p.name for p in self.plugins if p.builtin and p.group == group)}'
            for group in GROUPS
            if BUILTINS[group]
        )
        lines += ['', f'Built in: {builtin}.']
        return '\n'.join(lines)


def _load_object(target: str) -> Any:
    module_name, _, attribute = target.partition(':')
    loaded: Any = importlib.import_module(module_name)
    for part in attribute.split('.') if attribute else ():
        loaded = getattr(loaded, part)
    return loaded


def _pack_directory(target: str) -> tuple[Path, Any]:
    """The directory a pack entry point names, found without importing anything, and the
    ``api_version`` its ``plugin.toml`` declares."""
    if ':' in target:
        raise PluginError(f'{target} names an object; a pack names a package directory')
    top, *rest = target.split('.')
    # find_spec on a top-level name locates it without running it, through any import hooks.
    spec = importlib.util.find_spec(top)
    locations: Sequence[str] = (spec.submodule_search_locations or []) if spec is not None else []
    for location in locations:
        directory = Path(location, *rest)
        if directory.is_dir():
            manifest = directory / MANIFEST
            if not manifest.is_file():
                raise PluginError(f'{directory} has no {MANIFEST}')
            with manifest.open('rb') as handle:
                return directory, tomllib.load(handle).get('api_version')
    raise PluginError(f'no directory {target}')
