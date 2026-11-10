"""The plugin registry: Konstruct's own inputs, exports, checks and packs.

Each component is listed here as an entry point would name it, ``module:attribute`` (or a package
directory for packs), and loaded and run through the registry, so that the build, ``export`` and
``gate`` run whatever the registry holds rather than a fixed list. Nothing is imported until a
group is first used.
"""

from __future__ import annotations

import importlib
import importlib.util
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from konstruct.plugins.api import shown

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
    ),
    'packs': (('konstruct', 'konstruct.semantics.ontologies'),),
    'mcp_tools': (),
}
"""Konstruct's own components, by group: each name with the object (or, for packs, the package
directory) that provides it."""

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
    loaded: Any = field(default=None, repr=False, compare=False)
    """The loaded object, or the directory for a pack."""

    @property
    def label(self) -> str:
        """How messages name it: ``input 'pipelines' (konstruct-yaml-pipelines 0.1.0)``."""
        return f'{SINGULAR[self.group]} {self.name!r} ({self.distribution} {self.version})'


class Plugins:
    """The registry: every component, and which of them run."""

    def __init__(self, plugins: list[Plugin]) -> None:
        self.plugins = plugins
        """Every component."""

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

    # Lookup

    def get(self, group: str, name: str) -> Plugin | None:
        """The component of ``group`` called ``name``, loaded if it runs."""
        for plugin in self.plugins:
            if plugin.group == group and plugin.name == name:
                if plugin.enabled:
                    self._load(plugin)
                return plugin
        return None

    def group(self, group: str) -> list[Plugin]:
        """The components of ``group`` that run, loaded."""
        running = [p for p in self.plugins if p.group == group and p.enabled]
        for plugin in running:
            self._load(plugin)
        return running

    # Loading

    def _load(self, plugin: Plugin) -> None:
        if plugin.loaded is not None:
            return
        from konstruct.plugins.api import API_VERSION, api_version, supported

        if plugin.group == 'packs':
            plugin.loaded, version = _pack_directory(plugin.target)
        else:
            plugin.loaded = _load_object(plugin.target)
            version = getattr(plugin.loaded, 'api_version', None)
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
            raise PluginError(f'{plugin.label}: {refusal}')


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
