"""Static parsing of Dagster code: assets, their dependencies, dbt assets, jobs and schedules.

Like the Airflow parser, this reads files with `ast` and never imports them, so Dagster does not
need to be installed and no user code runs. It understands the common ways of declaring assets:

* ``@asset`` and ``@multi_asset`` functions, their keys (``name=``, ``key=``, ``key_prefix=``,
  ``AssetKey([...])``), ``group_name=``, docstrings and descriptions;
* dependencies from function parameters, ``ins={"x": AssetIn("key")}`` and ``deps=[...]``
  (asset functions, strings and ``AssetKey``s);
* ``@dbt_assets(manifest=..., select=..., exclude=...)``, which runs a selection of dbt models;
* ``define_asset_job(name, selection=...)`` with string selections and ``AssetSelection.all()``,
  ``.groups(...)`` and ``.keys(...)``; and ``ScheduleDefinition`` / ``build_schedule_from_partitioned_job``;
* SQL passed as a literal to ``.execute(...)``, ``.sql(...)`` or ``.query(...)`` inside an asset
  or op, which is analysed for lineage like an Airflow SQL task;
* op-based jobs: ``@op`` functions composed in ``@graph`` and ``@job`` bodies, including nested
  graphs, ``.alias(...)``, fan-in lists, dynamic ``.map(...)`` / ``.collect()``, ``graph.to_job()``
  and graphs listed in ``Definitions(jobs=[...])``. Each op invocation becomes a task, named by
  its handle as Dagster names it (``op_2`` for a second unnamed call, ``graph.op`` inside a
  graph); ``dbt.cli([...])`` and ``subprocess`` calls to dbt inside an op are read as dbt runs;
* ``@schedule`` and sensors: ``@sensor``, ``@asset_sensor``, ``@multi_asset_sensor``,
  ``@run_status_sensor`` / ``@run_failure_sensor`` and ``SensorDefinition``, with the jobs they
  start and the assets or jobs they watch.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ASSET_DECORATORS = frozenset({'asset', 'multi_asset', 'graph_asset', 'observable_source_asset'})
SQL_METHODS = frozenset({'execute', 'sql', 'query', 'run_query', 'executescript'})
SENSOR_DECORATORS = frozenset(
    {'sensor', 'asset_sensor', 'multi_asset_sensor', 'run_status_sensor', 'run_failure_sensor'}
)
# Methods on an op definition that return the same op under the same or a new name.
OP_WRAPPERS = frozenset({'alias', 'tag', 'with_hooks', 'with_retry_policy', 'configured'})


@dataclass
class AssetInfo:
    key: str
    function: str
    line: int
    group: str | None = None
    description: str | None = None
    deps: list[str] = field(default_factory=list)
    sql: str | None = None
    kinds: list[str] = field(default_factory=list)
    dbt_select: list[str] | None = None
    dbt_exclude: list[str] = field(default_factory=list)


@dataclass
class OpInfo:
    """An ``@op`` definition. It runs once per invocation in a job; see :class:`OpCall`."""

    name: str
    function: str
    line: int
    description: str | None = None
    sql: str | None = None
    kinds: list[str] = field(default_factory=list)
    dbt_commands: list[str] = field(default_factory=list)


@dataclass
class OpCall:
    """One invocation of an op in a job, by its handle, and the handles it takes inputs from."""

    handle: str
    op: str
    upstream: list[str] = field(default_factory=list)


@dataclass
class JobInfo:
    name: str
    line: int
    selection: list[str] = field(default_factory=list)
    schedule: str | None = None
    description: str | None = None
    ops: list[OpCall] = field(default_factory=list)
    """For op-based jobs, the op invocations; asset jobs have a selection instead."""


@dataclass
class SensorInfo:
    name: str
    kind: str
    line: int
    jobs: list[str] = field(default_factory=list)
    """The jobs the sensor starts."""
    assets: list[str] = field(default_factory=list)
    """The assets whose materialisations it watches."""
    monitored_jobs: list[str] = field(default_factory=list)
    """For run status sensors, the jobs whose runs it watches."""
    status: str | None = None
    description: str | None = None


@dataclass
class DagsterFile:
    path: str
    assets: list[AssetInfo] = field(default_factory=list)
    jobs: list[JobInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ops: list[OpInfo] = field(default_factory=list)
    sensors: list[SensorInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DagsterFile:
        jobs = []
        for job in data.get('jobs', []):
            fields = dict(job)
            fields['ops'] = [OpCall(**call) for call in fields.get('ops', [])]
            jobs.append(JobInfo(**fields))
        return cls(
            path=data['path'],
            assets=[AssetInfo(**a) for a in data.get('assets', [])],
            jobs=jobs,
            warnings=list(data.get('warnings', [])),
            ops=[OpInfo(**o) for o in data.get('ops', [])],
            sensors=[SensorInfo(**s) for s in data.get('sensors', [])],
        )


def looks_like_dagster_file(text: str) -> bool:
    return 'dagster' in text and any(
        marker in text
        for marker in (
            '@asset',
            '@multi_asset',
            '@dbt_assets',
            'define_asset_job',
            'Definitions(',
            '@op',
            '@job',
            '@graph',
            'sensor',
            '@schedule',
        )
    )


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class _Parser:
    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.constants: dict[str, Any] = {}
        self.functions: dict[str, AssetInfo] = {}
        self.multi: dict[str, AssetInfo] = {}
        self.result = DagsterFile(path=str(path))
        self.schedules: list[tuple[str, str]] = []
        self.ops: dict[str, OpInfo] = {}
        """Op definitions by the Python name they are bound to."""
        self.graphs: dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, str | None]] = {}
        """``@graph`` and ``@job`` functions: the function, its Dagster name and description."""
        self.job_functions: dict[str, int] = {}
        self.expanding: set[str] = set()

    def value(self, node: ast.expr | None) -> Any:
        if node is None:
            return None
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.functions:
                return {'__asset__': self.functions[node.id].key}
            if node.id in self.job_functions:
                return {'__job__': self.graphs[node.id][1]}
            return self.constants.get(node.id)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                if isinstance(part, ast.Constant):
                    parts.append(str(part.value))
                elif isinstance(part, ast.FormattedValue):
                    inner = self.value(part.value)
                    if inner is None:
                        return None
                    parts.append(str(inner))
            return ''.join(parts)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return [self.value(e) for e in node.elts]
        if isinstance(node, ast.Dict):
            return {
                self.value(k): self.value(v)
                for k, v in zip(node.keys, node.values, strict=True)
                if k is not None
            }
        if isinstance(node, ast.Call):
            name = _name(node)
            if name == 'AssetKey' and node.args:
                return {'__asset__': _key(self.value(node.args[0]))}
            if name == 'AssetIn':
                key = self.value(node.args[0]) if node.args else self.kwarg(node, 'key')
                prefix = self.kwarg(node, 'key_prefix')
                return {'__asset_in__': _key(key, prefix) if key is not None else None}
            if name == 'AssetDep' and node.args:
                inner = self.value(node.args[0])
                return inner if isinstance(inner, dict) else {'__asset__': _key(inner)}
            if name == 'AssetOut':
                return {
                    '__asset_out__': self.kwarg(node, 'key'),
                    'prefix': self.kwarg(node, 'key_prefix'),
                    'group': self.kwarg(node, 'group_name'),
                    'description': self.kwarg(node, 'description'),
                }
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'AssetSelection'
            ):
                return self._selection(node)
            if isinstance(node.func, ast.Attribute) and node.func.attr in (
                'downstream',
                'upstream',
                'required_multi_asset_neighbors',
            ):
                return self.value(node.func.value)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in ('DagsterRunStatus', 'RunStatus')
        ):
            return node.attr
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitOr, ast.Add)):
            left, right = self.value(node.left), self.value(node.right)
            if isinstance(left, list) and isinstance(right, list):
                return left + right
            if isinstance(left, str) and isinstance(right, str) and isinstance(node.op, ast.Add):
                return left + right
        return None

    def _selection(self, call: ast.Call) -> list[str]:
        assert isinstance(call.func, ast.Attribute)
        method = call.func.attr
        values = [self.value(a) for a in call.args]
        if method == 'all':
            return ['*']
        if method == 'groups':
            return [f'group:{v}' for v in values if isinstance(v, str)]
        if method in ('keys', 'assets'):
            found = []
            for item in values:
                if isinstance(item, dict) and item.get('__asset__'):
                    found.append(str(item['__asset__']))
                elif isinstance(item, (str, list)):
                    found.append(_key(item))
            return found
        if method == 'key_prefixes':
            return [f'prefix:{_key(v)}' for v in values if v is not None]
        return []

    def kinds(self, call: ast.Call | None) -> list[str]:
        """``kinds={...}`` and the older ``compute_kind=...`` of an asset or op decorator."""
        found: list[str] = []
        for kind_arg in ('kinds', 'compute_kind'):
            value = self.kwarg(call, kind_arg) if call is not None else None
            found += [value] if isinstance(value, str) else [k for k in value or [] if isinstance(k, str)]
        return found

    def kwarg(self, call: ast.Call, name: str) -> Any:
        for keyword in call.keywords:
            if keyword.arg == name:
                return self.value(keyword.value)
        return None

    def parse(self) -> DagsterFile:
        try:
            return self._parse()
        except RecursionError:
            # Python's parser, or the walk over its tree, gives up on expressions nested thousands
            # deep (a sum of thousands of terms); such a file is reported as one that cannot be read.
            raise SyntaxError(
                'expressions are nested too deeply to read', (str(self.path), 1, 1, None)
            ) from None

    def _parse(self) -> DagsterFile:
        tree = _syntax_tree(self.source, self.path)
        for statement in tree.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                folded = self.value(statement.value)
                if folded is not None:
                    self.constants[statement.targets[0].id] = folded
        nodes = list(ast.walk(tree))
        functions = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for func in functions:
            self.visit_function(func)
        for func in functions:
            if func.name in self.functions or func.name in self.multi:
                self.resolve_dependencies(func)
        for name, line in self.job_functions.items():
            self.op_job(name, self.graphs[name][1], line, self.graphs[name][2])
        assigned: set[int] = set()
        for item in nodes:
            if isinstance(item, ast.Assign) and isinstance(item.value, ast.Call):
                self.visit_call(item.value, item.targets[0])
                assigned.add(id(item.value))
        for item in nodes:
            if isinstance(item, ast.Call) and id(item) not in assigned:
                self.visit_call(item, None)
        for func in functions:
            self.visit_scheduler(func)
        jobs = {job.name: job for job in self.result.jobs}
        for job_name, cron in self.schedules:
            if job_name in jobs:
                jobs[job_name].schedule = cron
        return self.result

    def visit_function(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in func.decorator_list:
            name = _name(decorator) or ''
            call = decorator if isinstance(decorator, ast.Call) else None
            if name == 'dbt_assets':
                select = self.kwarg(call, 'select') if call else None
                exclude = self.kwarg(call, 'exclude') if call else None
                asset = AssetInfo(
                    key=func.name,
                    function=func.name,
                    line=func.lineno,
                    description=ast.get_docstring(func),
                    kinds=['dbt'],
                    dbt_select=str(select).split() if isinstance(select, str) else [],
                    dbt_exclude=str(exclude).split() if isinstance(exclude, str) else [],
                )
                self.functions[func.name] = asset
                self.result.assets.append(asset)
                return
            if name == 'op':
                op_name = self.kwarg(call, 'name') if call else None
                description = (self.kwarg(call, 'description') if call else None) or ast.get_docstring(func)
                op = OpInfo(
                    name=op_name if isinstance(op_name, str) else func.name,
                    function=func.name,
                    line=func.lineno,
                    description=description.strip() if isinstance(description, str) else None,
                    sql=self._sql_in(func),
                    kinds=self.kinds(call),
                    dbt_commands=self._dbt_in(func),
                )
                self.ops[func.name] = op
                self.result.ops.append(op)
                return
            if name in ('graph', 'job'):
                graph_name = self.kwarg(call, 'name') if call else None
                description = (self.kwarg(call, 'description') if call else None) or ast.get_docstring(func)
                self.graphs[func.name] = (
                    func,
                    graph_name if isinstance(graph_name, str) else func.name,
                    description.strip() if isinstance(description, str) else None,
                )
                if name == 'job':
                    self.job_functions[func.name] = func.lineno
                return
            if name not in ASSET_DECORATORS:
                continue
            if name == 'multi_asset' and call is not None:
                outs = self.kwarg(call, 'outs') or {}
                if isinstance(outs, dict):
                    prefix = self.kwarg(call, 'key_prefix')
                    group = self.kwarg(call, 'group_name')
                    first: AssetInfo | None = None
                    for out_name, out in outs.items():
                        spec = out if isinstance(out, dict) else {}
                        out_key = spec.get('__asset_out__') or _key(out_name, spec.get('prefix') or prefix)
                        asset = AssetInfo(
                            key=_key(out_key)
                            if not isinstance(out_key, str) or '/' not in out_key
                            else out_key,
                            function=func.name,
                            line=func.lineno,
                            group=spec.get('group') or group,
                            description=spec.get('description') or ast.get_docstring(func),
                        )
                        self.result.assets.append(asset)
                        first = first or asset
                    if first is not None:
                        # Registered for dependency resolution only; downstream assets name outputs.
                        self.multi[func.name] = first
                    return
            key: Any = func.name
            group = None
            description = ast.get_docstring(func)
            kinds: list[str] = []
            if call is not None:
                explicit = self.kwarg(call, 'key')
                named = self.kwarg(call, 'name')
                prefix = self.kwarg(call, 'key_prefix')
                if isinstance(explicit, dict) and explicit.get('__asset__'):
                    key = explicit['__asset__']
                elif explicit is not None:
                    key = _key(explicit)
                else:
                    key = _key(named or func.name, prefix)
                group = self.kwarg(call, 'group_name')
                description = self.kwarg(call, 'description') or description
                kinds = self.kinds(call)
            asset = AssetInfo(
                key=_key(key),
                function=func.name,
                line=func.lineno,
                group=group,
                description=description.strip() if isinstance(description, str) else None,
                kinds=kinds,
            )
            asset.sql = self._sql_in(func)
            self.functions[func.name] = asset
            self.result.assets.append(asset)
            return

    def resolve_dependencies(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        asset = self.functions.get(func.name) or self.multi[func.name]
        assets = [a for a in self.result.assets if a.function == func.name]
        deps: list[str] = []
        ins: dict[str, Any] = {}
        for decorator in func.decorator_list:
            if isinstance(decorator, ast.Call):
                ins = self.kwarg(decorator, 'ins') or {}
                for item in self.kwarg(decorator, 'deps') or []:
                    if isinstance(item, dict) and item.get('__asset__'):
                        deps.append(str(item['__asset__']))
                    elif isinstance(item, (str, list)):
                        deps.append(_key(item))
        params = [
            a.arg
            for a in func.args.args + func.args.kwonlyargs
            if a.arg not in ('context', 'self', 'config') and not _is_resource(a.annotation)
        ]
        for param in params:
            spec = ins.get(param) if isinstance(ins, dict) else None
            if isinstance(spec, dict) and spec.get('__asset_in__'):
                deps.append(str(spec['__asset_in__']))
            elif param not in ('dbt', 'duckdb', 'database', 'warehouse') and not param.endswith(
                ('_resource', '_client')
            ):
                # As Dagster does: a parameter names the upstream asset under this asset's own key prefix.
                prefix = asset.key.rsplit('/', 1)[0] if '/' in asset.key else None
                deps.append(_key(param, prefix))
        for item in assets or [asset]:
            item.deps = sorted(set(deps) - {item.key})

    def _sql_in(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
        statements = []
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in SQL_METHODS
                and node.args
            ):
                text = self.value(node.args[0])
                if isinstance(text, str) and text.strip():
                    statements.append(text.strip().rstrip(';'))
        return ';\n'.join(statements) or None

    def _dbt_in(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        """dbt commands an op runs: ``dbt.cli(["run", ...])`` or ``subprocess.run(["dbt", ...])``."""
        commands = []
        for node in ast.walk(func):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            words = self.value(node.args[0])
            if isinstance(words, str):
                words = words.split()
            if not isinstance(words, list) or not words or not all(isinstance(w, str) for w in words):
                continue
            method = node.func.attr if isinstance(node.func, ast.Attribute) else _name(node.func)
            if method == 'cli':
                commands.append(' '.join(['dbt', *words]))
            elif words[0] == 'dbt' and method in ('run', 'call', 'check_call', 'check_output', 'Popen'):
                commands.append(' '.join(words))
        return commands

    def op_job(self, function: str, name: str, line: int, description: str | None) -> None:
        calls: list[OpCall] = []
        self.expand(function, '', {}, calls)
        self.result.jobs.append(JobInfo(name=name, line=line, description=description, ops=calls))

    def expand(
        self, function: str, prefix: str, inputs: dict[str, list[str]], calls: list[OpCall]
    ) -> list[str]:
        """Expand a graph or job body into op invocations; returns the handles of its outputs."""
        func, name, _ = self.graphs[function]
        if function in self.expanding:
            self.result.warnings.append(f'line {func.lineno}: graph {name} invokes itself; skipped')
            return []
        self.expanding.add(function)
        env: dict[str, list[str]] = {}
        for arg in func.args.args + func.args.kwonlyargs:
            env[arg.arg] = inputs.get(arg.arg, [])
        used: dict[str, int] = {}
        outputs: list[str] = []

        def unique(handle: str) -> str:
            # Dagster names a repeated invocation without an alias op_2, op_3, ...
            count = used.get(handle, 0) + 1
            used[handle] = count
            return handle if count == 1 else f'{handle}_{count}'

        def callee(node: ast.expr) -> tuple[str, str | None] | None:
            """The op or graph a call target names, and the alias given to it."""
            alias = None
            while True:
                if isinstance(node, ast.Name) and (node.id in self.ops or node.id in self.graphs):
                    return node.id, alias
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in OP_WRAPPERS
                ):
                    if node.func.attr == 'alias' and node.args and alias is None:
                        found = self.value(node.args[0])
                        alias = found if isinstance(found, str) else None
                    elif node.func.attr == 'configured' and alias is None:
                        found = self.kwarg(node, 'name')
                        alias = found if isinstance(found, str) else None
                    node = node.func.value
                    continue
                return None

        def invoke(target: str, alias: str | None, call: ast.Call, extra: list[str]) -> list[str]:
            arguments = [evaluate(a) for a in call.args]
            keywords = {k.arg: evaluate(k.value) for k in call.keywords if k.arg}
            upstream = sorted({h for hs in [*arguments, *keywords.values(), extra] for h in hs})
            if target in self.ops:
                handle = prefix + unique(alias or self.ops[target].name)
                calls.append(OpCall(handle=handle, op=self.ops[target].name, upstream=upstream))
                return [handle]
            inner, inner_name, _ = self.graphs[target]
            params = [a.arg for a in inner.args.args]
            mapped = {params[i]: hs for i, hs in enumerate(arguments) if i < len(params)}
            mapped.update({k: v for k, v in keywords.items() if k})
            handle = unique(alias or inner_name)
            return self.expand(target, f'{prefix}{handle}.', mapped, calls)

        def evaluate(node: ast.expr) -> list[str]:
            if isinstance(node, ast.Name):
                return list(env.get(node.id, []))
            if isinstance(node, ast.Call):
                found = callee(node.func)
                if found:
                    return invoke(found[0], found[1], node, [])
                if isinstance(node.func, ast.Attribute) and node.func.attr in ('map', 'collect'):
                    source = evaluate(node.func.value)
                    if node.func.attr == 'collect' or not node.args:
                        return source
                    mapper = node.args[0]
                    if isinstance(mapper, ast.Lambda):
                        for arg in mapper.args.args:
                            env[arg.arg] = source
                        return evaluate(mapper.body)
                    target = callee(mapper)
                    if target:
                        return invoke(
                            target[0], target[1], ast.Call(func=mapper, args=[], keywords=[]), source
                        )
                    return source
                return sorted(
                    {h for a in [*node.args, *(k.value for k in node.keywords)] for h in evaluate(a)}
                )
            if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                return sorted({h for e in node.elts for h in evaluate(e)})
            if isinstance(node, ast.Dict):
                return sorted({h for v in node.values for h in evaluate(v)})
            if isinstance(node, (ast.Subscript, ast.Attribute, ast.Starred)):
                return evaluate(node.value)
            return []

        def run(body: list[ast.stmt]) -> None:
            for statement in body:
                if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
                    value = evaluate(statement.value)
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    for target in targets:
                        names = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
                        for item in names:
                            if isinstance(item, ast.Name):
                                env[item.id] = value
                elif isinstance(statement, ast.Expr):
                    evaluate(statement.value)
                elif isinstance(statement, ast.Return) and statement.value is not None:
                    outputs.extend(evaluate(statement.value))
                elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                    self.result.warnings.append(
                        f'line {statement.lineno}: loop in graph {name}; its ops are counted once'
                    )
                    run(statement.body)
                elif isinstance(statement, (ast.If, ast.With, ast.AsyncWith, ast.Try)):
                    run(statement.body)
                    run(getattr(statement, 'orelse', []))

        run(func.body)
        self.expanding.discard(function)
        return sorted(set(outputs))

    def job_names(self, value: Any) -> list[str]:
        if isinstance(value, dict) and value.get('__job__'):
            return [str(value['__job__'])]
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [name for item in value for name in self.job_names(item)]
        return []

    def asset_keys(self, value: Any) -> list[str]:
        if isinstance(value, dict) and value.get('__asset__'):
            return [str(value['__asset__'])]
        if isinstance(value, list):
            if value and all(isinstance(v, str) for v in value) and not any('/' in v for v in value):
                return [_key(value)]
            return [key for item in value for key in self.asset_keys(item)]
        if isinstance(value, str):
            return [_key(value)]
        return []

    def visit_scheduler(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """``@schedule`` and sensor decorators, read once every job is known."""
        for decorator in func.decorator_list:
            name = _name(decorator) or ''
            call = decorator if isinstance(decorator, ast.Call) else None
            if name == 'schedule' and call is not None:
                cron = self.kwarg(call, 'cron_schedule')
                if cron is None and call.args:
                    cron = self.value(call.args[0])
                targets = [self.kwarg(call, k) for k in ('job', 'job_name', 'target')]
                for job_name in self.job_names([t for t in targets if t is not None]):
                    self.schedules.append((job_name, cron if isinstance(cron, str) else 'custom'))
            elif name in SENSOR_DECORATORS:
                self.sensor(func.name, func.lineno, name, call, ast.get_docstring(func))

    def sensor(
        self, default: str, line: int, kind: str, call: ast.Call | None, description: str | None
    ) -> None:
        def get(*names: str) -> list[Any]:
            return [v for v in (self.kwarg(call, n) for n in names) if v is not None] if call else []

        sensor_name = get('name')
        status = get('run_status')
        info = SensorInfo(
            name=sensor_name[0] if sensor_name and isinstance(sensor_name[0], str) else default,
            kind=kind,
            line=line,
            jobs=self.job_names(get('job', 'jobs', 'job_name', 'request_job', 'request_jobs', 'target')),
            assets=self.asset_keys(get('asset_key', 'monitored_assets')),
            monitored_jobs=self.job_names(get('monitored_jobs')),
            status='FAILURE'
            if kind == 'run_failure_sensor'
            else (status[0] if status and isinstance(status[0], str) else None),
            description=(get('description') or [description])[0],
        )
        if info.jobs or info.assets or info.monitored_jobs:
            self.result.sensors.append(info)

    def visit_call(self, call: ast.Call, target: ast.expr | None) -> None:
        name = _name(call) or ''
        if (
            name == 'to_job'
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in self.graphs
            and call.func.value.id not in self.job_functions
        ):
            graph = call.func.value.id
            job_name = self.kwarg(call, 'name')
            job_name = job_name if isinstance(job_name, str) else self.graphs[graph][1]
            self.op_job(
                graph, job_name, call.lineno, self.kwarg(call, 'description') or self.graphs[graph][2]
            )
            if isinstance(target, ast.Name):
                self.constants[target.id] = {'__job__': job_name}
            return
        if name == 'Definitions':
            # Graphs listed as jobs become jobs of the same name.
            known = {job.name for job in self.result.jobs}
            for keyword in call.keywords:
                if keyword.arg == 'jobs' and isinstance(keyword.value, (ast.List, ast.Tuple)):
                    for item in keyword.value.elts:
                        if (
                            isinstance(item, ast.Name)
                            and item.id in self.graphs
                            and self.graphs[item.id][1] not in known
                        ):
                            func, graph_name, description = self.graphs[item.id]
                            self.op_job(item.id, graph_name, func.lineno, description)
            return
        if name == 'SensorDefinition':
            sensor_name = self.kwarg(call, 'name')
            self.sensor(
                sensor_name if isinstance(sensor_name, str) else f'sensor_line_{call.lineno}',
                call.lineno,
                'sensor',
                call,
                None,
            )
            return
        if name == 'define_asset_job':
            job_name = self.value(call.args[0]) if call.args else self.kwarg(call, 'name')
            if not isinstance(job_name, str):
                self.result.warnings.append(f'line {call.lineno}: asset job name is not a constant; skipped')
                return
            selection = self.kwarg(call, 'selection')
            if selection is None and len(call.args) > 1:
                selection = self.value(call.args[1])
            self.result.jobs.append(
                JobInfo(
                    name=job_name,
                    line=call.lineno,
                    selection=_selection_list(selection),
                    description=self.kwarg(call, 'description'),
                )
            )
            if isinstance(target, ast.Name):
                self.constants[target.id] = {'__job__': job_name}
        elif name in ('ScheduleDefinition', 'build_schedule_from_partitioned_job'):
            job = (
                self.kwarg(call, 'job')
                or self.kwarg(call, 'target')
                or (self.value(call.args[0]) if call.args else None)
            )
            cron = self.kwarg(call, 'cron_schedule')
            job_name = job.get('__job__') if isinstance(job, dict) else self.kwarg(call, 'job_name')
            if isinstance(job_name, str):
                self.schedules.append((job_name, cron if isinstance(cron, str) else 'partitioned'))


def _is_resource(annotation: ast.expr | None) -> bool:
    """Parameters typed as resources (DuckDBResource, DbtCliResource, ...) or contexts are not assets."""
    name = _name(annotation) if annotation is not None else None
    if isinstance(annotation, ast.Subscript):
        name = _name(annotation.value)
    if not name:
        return False
    return name.endswith(('Resource', 'Context', 'Config')) or name == 'ResourceParam'


def _selection_list(selection: Any) -> list[str]:
    if selection is None:
        return ['*']
    if isinstance(selection, str):
        return [_key(part) if part not in ('*',) and ':' not in part else part for part in selection.split()]
    if isinstance(selection, list):
        found = []
        for item in selection:
            if isinstance(item, dict) and item.get('__asset__'):
                found.append(str(item['__asset__']))
            elif isinstance(item, str):
                found.append(item if item.startswith(('group:', 'prefix:')) or item == '*' else _key(item))
            elif isinstance(item, list):
                found.extend(_selection_list(item))
        return found
    if isinstance(selection, dict) and selection.get('__asset__'):
        return [str(selection['__asset__'])]
    return []


def _key(value: Any, prefix: Any = None) -> str:
    """An asset key as slash-separated text: ``["analytics", "orders"]`` is ``analytics/orders``."""
    if isinstance(value, dict) and value.get('__asset__'):
        base = str(value['__asset__'])
    elif isinstance(value, (list, tuple)):
        base = '/'.join(str(v) for v in value if v is not None)
    else:
        base = str(value)
    if prefix:
        parts = [prefix] if isinstance(prefix, str) else [str(p) for p in prefix]
        base = '/'.join([*parts, base])
    return base


def parse_file(path: Path) -> DagsterFile:
    """Parse one Dagster file. Raises SyntaxError if it is not valid Python, or is nested too deeply
    to read."""
    return _Parser(path, path.read_text(encoding='utf-8')).parse()


def parse_source(path: Path, source: str) -> DagsterFile:
    return _Parser(path, source).parse()


def _syntax_tree(source: str, path: Path) -> ast.Module:
    try:
        return ast.parse(source, filename=str(path))
    except ValueError as error:
        # Python 3.11 rejects a NUL byte with a ValueError; 3.12 and later with a SyntaxError.
        raise SyntaxError(str(error), (str(path), 1, 1, None)) from None
    except MemoryError:
        # The parser's own stack overflows on thousands of nested operators (``x = ----1``).
        raise SyntaxError('the file is too complex for Python to parse', (str(path), 1, 1, None)) from None
