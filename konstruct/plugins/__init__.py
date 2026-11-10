"""Plugins: extend Konstruct with inputs, export formats, gate checks, ontology packs and MCP tools.

A plugin is a Python distribution installed alongside Konstruct that declares entry points in
these groups:

* ``konstruct.inputs``: an :class:`InputPlugin`, which discovers files and reads them into a
  :class:`Fragment` of the graph;
* ``konstruct.exports``: an :class:`ExportPlugin`, a format for ``konstruct export``;
* ``konstruct.checks``: a :class:`CheckPlugin`, a rule of ``konstruct gate``;
* ``konstruct.packs``: a directory of ontology packs, with no code;
* ``konstruct.mcp_tools``: a :class:`ToolsPlugin`, tools for ``konstruct mcp``.

Each code plugin declares the :data:`API_VERSION` it targets. A project enables plugins by
distribution name in ``konstruct.toml`` (``[plugins] enabled``); :class:`Plugins` is the registry
that finds, enables and loads them, and Konstruct's own components are in it too.
"""

from konstruct.plugins.api import (
    API_VERSION,
    MAX_DEPTH,
    PLUGIN_EDGE_KINDS,
    PLUGIN_NODE_ATTRS,
    PLUGIN_NODE_KINDS,
    RESERVED_EDGE_ATTRS,
    RESERVED_NODE_ATTRS,
    CheckContext,
    CheckPlugin,
    Column,
    Endpoint,
    ExportPlugin,
    ExportRequest,
    Finding,
    Fragment,
    InputContext,
    InputFile,
    InputPlugin,
    PluginEdge,
    PluginNode,
    Relation,
    Source,
    Sql,
    ToolsPlugin,
)
from konstruct.plugins.registry import GROUPS, Plugin, PluginError, Plugins

__all__ = [
    'API_VERSION',
    'GROUPS',
    'MAX_DEPTH',
    'PLUGIN_EDGE_KINDS',
    'PLUGIN_NODE_ATTRS',
    'PLUGIN_NODE_KINDS',
    'RESERVED_EDGE_ATTRS',
    'RESERVED_NODE_ATTRS',
    'CheckContext',
    'CheckPlugin',
    'Column',
    'Endpoint',
    'ExportPlugin',
    'ExportRequest',
    'Finding',
    'Fragment',
    'InputContext',
    'InputFile',
    'InputPlugin',
    'Plugin',
    'PluginEdge',
    'PluginError',
    'PluginNode',
    'Plugins',
    'Relation',
    'Source',
    'Sql',
    'ToolsPlugin',
]
