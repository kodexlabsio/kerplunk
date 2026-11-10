"""An example Konstruct plugin: pipelines written as YAML, with a gate check, a pack and an MCP tool.

A pipeline file (``*.pipeline.yml``) names its owner and its steps; each step runs SQL and may run
after other steps::

    pipeline: daily_revenue
    owner: finance
    steps:
      - name: load_orders
        sql: create table analytics.orders as select id, amount from raw.orders
      - name: revenue
        after: [load_orders]
        sql: create table analytics.revenue as select sum(amount) as revenue from analytics.orders

The files are read with ``yaml.safe_load``, so nothing in them is run. Konstruct analyses each
step's SQL for column lineage.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import yaml

from konstruct import Project
from konstruct.plugins import (
    API_VERSION,
    CheckContext,
    Finding,
    Fragment,
    InputContext,
    InputFile,
    PluginEdge,
    PluginNode,
    Source,
    Sql,
)

INPUT_NAME = 'pipelines'
"""The entry point name of the input, which prefixes the ids of its nodes."""


class PipelineInput:
    """``konstruct.inputs``: each pipeline a DAG, each step a task that runs its SQL."""

    api_version = API_VERSION
    patterns = ('*.pipeline.yml', '*.pipeline.yaml')

    def read(self, files: Sequence[InputFile], context: InputContext) -> Fragment:
        """The pipelines in ``files``."""
        fragment = Fragment()
        for file in files:
            try:
                data = yaml.safe_load(file.text)
            except yaml.YAMLError as error:
                fragment.warnings.append(f'{file.path}: not valid YAML: {error}')
                continue
            if not isinstance(data, dict) or not isinstance(data.get('pipeline'), str):
                fragment.warnings.append(f'{file.path}: a pipeline file starts with `pipeline: <name>`')
                continue
            pipeline = data['pipeline']
            dag = f'pipeline.{pipeline}'
            fragment.nodes.append(
                PluginNode(
                    id=dag,
                    kind='dag',
                    name=pipeline,
                    path=file.path,
                    description=data.get('description'),
                    attrs={'owner': data['owner']} if data.get('owner') else {},
                )
            )
            steps = [s for s in data.get('steps') or [] if isinstance(s, dict) and s.get('name')]
            for step in steps:
                task = f'task.{pipeline}.{step["name"]}'
                fragment.nodes.append(
                    PluginNode(id=task, kind='task', name=str(step['name']), path=file.path)
                )
                fragment.edges.append(PluginEdge(dag, task, 'runs'))
                if step.get('sql'):
                    fragment.sql.append(Sql(str(step['sql']), file.path, task=task))
            names = {str(s['name']) for s in steps}
            for step in steps:
                for before in step.get('after') or []:
                    if str(before) in names:
                        fragment.edges.append(
                            PluginEdge(
                                f'task.{pipeline}.{before}', f'task.{pipeline}.{step["name"]}', 'precedes'
                            )
                        )
                    else:
                        fragment.warnings.append(
                            f'{file.path}: step {step["name"]!r} runs after unknown {before!r}'
                        )
            fragment.sources.append(
                Source(file.path, f'{pipeline}: {len(steps)} step{"s" if len(steps) != 1 else ""}')
            )
        fragment.counts['pipelines'] = sum(1 for n in fragment.nodes if n.kind == 'dag')
        return fragment


class OwnerCheck:
    """``konstruct.checks``: a pipeline the change touches must name an owner."""

    api_version = API_VERSION
    description = 'a changed pipeline with no owner'

    def check(self, context: CheckContext) -> list[Finding]:
        """A finding for each changed pipeline file whose pipeline names no owner."""
        changed = set(context.diff.source_files)
        return [
            Finding(
                'owner-required',
                f'pipeline {node.name!r} names no owner',
                node.path,
                fix='Add an `owner:` to the pipeline, naming the team that answers for it.',
            )
            for node in context.graph.nodes.values()
            if node.kind == 'dag'
            and node.id.startswith(f'{INPUT_NAME}.')
            and node.path in changed
            and not node.attrs.get('owner')
        ]


class PipelineTools:
    """``konstruct.mcp_tools``: ``pipelines_owners``, who owns each pipeline."""

    api_version = API_VERSION

    def tools(self, project: Project) -> Mapping[str, Callable[..., Any]]:
        """The tools, bound to ``project``."""

        def owners(pipeline: str | None = None) -> dict[str, Any]:
            """Each YAML pipeline with its owner and file; with `pipeline`, only that one."""
            found = [
                {'pipeline': n.name, 'owner': n.attrs.get('owner'), 'path': n.path}
                for n in project.graph.nodes.values()
                if n.kind == 'dag' and n.id.startswith(f'{INPUT_NAME}.') and pipeline in (None, n.name)
            ]
            return {'pipelines': sorted(found, key=lambda p: str(p['pipeline']))}

        return {'owners': owners}


PIPELINES = PipelineInput()
OWNERS = OwnerCheck()
TOOLS = PipelineTools()
