"""Static parsing of Airflow DAG files.

DAG files are read with `ast` and never imported, so Airflow does not need to be installed and
no DAG code runs. The parser understands the common ways of writing DAGs and tasks; anything it
cannot resolve statically (computed task ids, dynamic task mapping, SQL built at runtime) is
skipped and reported.
"""

from __future__ import annotations

import ast
import itertools
import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DAG_CLASSES = frozenset({'DAG'})
GROUP_CLASSES = frozenset({'TaskGroup'})
COSMOS_CLASSES = frozenset({'DbtTaskGroup', 'DbtDag'})
SQL_KWARGS = ('sql', 'query', 'sql_query')
WRITE_KWARGS = ('target_table', 'destination_table', 'destination_project_dataset_table')
READ_KWARGS = ('table', 'source_table', 'source_project_dataset_table')
UNRESOLVED = '{{ unresolved }}'
# Marks a whole TaskGroup in a dependency until its first and last tasks are known.
GROUP = '\x00group:'
DBT_COMMANDS = frozenset({'run', 'build', 'test', 'seed', 'snapshot', 'compile'})


@dataclass
class DbtInvocation:
    command: str
    selectors: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    project_dir: str | None = None
    unsupported: list[str] = field(default_factory=list)


@dataclass
class TaskInfo:
    task_id: str
    operator: str
    line: int
    doc: str | None = None
    bash_command: str | None = None
    sql: str | None = None
    sql_path: str | None = None
    dbt: list[DbtInvocation] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    inlets: list[str] = field(default_factory=list)
    outlets: list[str] = field(default_factory=list)


@dataclass
class DagInfo:
    dag_id: str
    path: str
    line: int
    schedule: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    tasks: dict[str, TaskInfo] = field(default_factory=dict)
    dependencies: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DagInfo:
        tasks = {
            key: TaskInfo(**{**value, 'dbt': [DbtInvocation(**d) for d in value.get('dbt', [])]})
            for key, value in data.get('tasks', {}).items()
        }
        return cls(
            **{
                **data,
                'tasks': tasks,
                'dependencies': [(a, b) for a, b in data.get('dependencies', [])],
            }
        )


def looks_like_dag_file(text: str) -> bool:
    return 'airflow' in text and ('DAG' in text or '@dag' in text)


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f'{base}.{node.attr}' if base else node.attr
    return None


class _Parser:
    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.constants: dict[str, Any] = {}
        self.dags: list[DagInfo] = []
        self.dag_vars: dict[str, DagInfo] = {}
        self.task_vars: dict[str, list[str]] = {}
        self.taskflow: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.group_members: dict[str, list[str]] = {}
        self.group_functions: dict[str, str] = {}
        self.unowned: list[TaskInfo] = []
        self.template_paths: list[Path] = [path.parent]
        self.warnings: list[str] = []
        self.pending: list[tuple[DagInfo | None, str, str]] = []
        self.aliases: dict[str, str] = {}

    def call_name(self, node: ast.expr) -> str | None:
        """Class or function name of a call, seen through ``import ... as`` aliases."""
        name = _call_name(node)
        return self.aliases.get(name, name) if name else None

    # Constant folding

    def value(self, node: ast.expr | None) -> Any:
        """Best-effort static value of an expression, or None."""
        if node is None:
            return None
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id == '__doc__':
                return self.constants.get('__doc__')
            return self.constants.get(node.id)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                if isinstance(part, ast.Constant):
                    parts.append(str(part.value))
                elif isinstance(part, ast.FormattedValue):
                    inner = self.value(part.value)
                    parts.append(str(inner) if inner is not None else UNRESOLVED)
            return ''.join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.value(node.left), self.value(node.right)
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            return None
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.value(e) for e in node.elts]
        if isinstance(node, ast.Dict):
            return {
                self.value(k): self.value(v)
                for k, v in zip(node.keys, node.values, strict=True)
                if k is not None
            }
        if isinstance(node, ast.Call):
            name = self.call_name(node)
            if name in ('Asset', 'Dataset'):
                uri = self.value(node.args[0]) if node.args else self.kwarg(node, 'uri')
                return {'__asset__': uri or self.kwarg(node, 'name')}
            if name == 'dedent' and node.args:
                return self.value(node.args[0])
        return None

    @staticmethod
    def kwarg_node(call: ast.Call, name: str) -> ast.expr | None:
        for keyword in call.keywords:
            if keyword.arg == name:
                return keyword.value
        return None

    def kwarg(self, call: ast.Call, name: str) -> Any:
        return self.value(self.kwarg_node(call, name))

    def name_kwarg(self, call: ast.Call, *names: str) -> str | None:
        """A DAG or task id: only a fully resolved constant counts."""
        for name in names:
            value = self.kwarg(call, name)
            if isinstance(value, str) and UNRESOLVED not in value:
                return value
        return None

    # Entry point

    def parse(self) -> list[DagInfo]:
        try:
            return self._parse()
        except RecursionError:
            # Python's parser, or the walk over its tree, gives up on expressions nested thousands
            # deep (a sum of thousands of terms); such a file is reported as one that cannot be read.
            raise SyntaxError(
                'expressions are nested too deeply to read', (str(self.path), 1, 1, None)
            ) from None

    def _parse(self) -> list[DagInfo]:
        tree = _syntax_tree(self.source, self.path)
        for statement in ast.walk(tree):
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                for alias in statement.names:
                    if alias.asname:
                        self.aliases[alias.asname] = alias.name.rsplit('.', 1)[-1]
        doc = ast.get_docstring(tree)
        if doc:
            self.constants['__doc__'] = doc
        for statement in tree.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
                if isinstance(target, ast.Name):
                    folded = self.value(statement.value)
                    if folded is not None:
                        self.constants[target.id] = folded
        self.visit_body(tree.body, dag=None, group=None)
        if self.unowned:
            if len(self.dags) == 1:
                for task in self.unowned:
                    self.dags[0].tasks.setdefault(task.task_id, task)
            else:
                self.warnings.append(
                    f'{len(self.unowned)} task(s) could not be matched to a DAG: '
                    + ', '.join(t.task_id for t in self.unowned)
                )
        for hint, upstream, downstream in self._expand_groups():
            owner = hint or next(
                (d for d in self.dags if upstream in d.tasks and downstream in d.tasks), None
            )
            if owner is not None and (upstream, downstream) not in owner.dependencies:
                owner.dependencies.append((upstream, downstream))
        for dag in self.dags:
            dag.warnings.extend(self.warnings)
        return self.dags

    # Walking statements

    def visit_body(self, body: list[ast.stmt], dag: DagInfo | None, group: str | None) -> None:
        for statement in body:
            self.visit_statement(statement, dag, group)

    def visit_statement(self, statement: ast.stmt, dag: DagInfo | None, group: str | None) -> None:
        if isinstance(statement, ast.With):
            inner_dag, inner_group = dag, group
            for item in statement.items:
                expr = item.context_expr
                name = self.call_name(expr)
                if isinstance(expr, ast.Call) and name in DAG_CLASSES:
                    inner_dag = self.make_dag(expr, statement.lineno)
                    if isinstance(item.optional_vars, ast.Name):
                        self.dag_vars[item.optional_vars.id] = inner_dag
                elif isinstance(expr, ast.Call) and name in GROUP_CLASSES:
                    group_id = self.value(expr.args[0]) if expr.args else self.kwarg(expr, 'group_id')
                    if isinstance(group_id, str):
                        inner_group = f'{group}.{group_id}' if group else group_id
                        self.group_members.setdefault(inner_group, [])
                        if isinstance(item.optional_vars, ast.Name):
                            self.task_vars[item.optional_vars.id] = [GROUP + inner_group]
            self.visit_body(statement.body, inner_dag, inner_group)
            return
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.visit_function(statement, dag, group)
            return
        if isinstance(statement, ast.Assign):
            value = statement.value
            if isinstance(value, ast.Call) and self.call_name(value) in DAG_CLASSES:
                created = self.make_dag(value, statement.lineno)
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        self.dag_vars[target.id] = created
                return
            for target in statement.targets:
                if (
                    isinstance(target, ast.Tuple)
                    and isinstance(value, ast.Tuple)
                    and len(target.elts) == len(value.elts)
                ):
                    for var, element in zip(target.elts, value.elts, strict=True):
                        found = self.visit_expression(element, dag, group)
                        if isinstance(var, ast.Name) and found is not None:
                            self.task_vars[var.id] = found
                    return
            tasks = self.visit_expression(value, dag, group)
            for target in statement.targets:
                if isinstance(target, ast.Name) and tasks is not None:
                    self.task_vars[target.id] = tasks
            return
        if isinstance(statement, ast.Expr):
            self.visit_expression(statement.value, dag, group)
            return
        if isinstance(statement, (ast.If, ast.For, ast.While, ast.Try)):
            for child in ast.iter_child_nodes(statement):
                if isinstance(child, ast.stmt):
                    self.visit_statement(child, dag, group)
            for handler in getattr(statement, 'handlers', []):
                self.visit_body(handler.body, dag, group)
            for extra in ('orelse', 'finalbody'):
                self.visit_body(getattr(statement, extra, []) or [], dag, group)

    def visit_function(
        self, func: ast.FunctionDef | ast.AsyncFunctionDef, dag: DagInfo | None, group: str | None
    ) -> None:
        for decorator in func.decorator_list:
            name = _dotted(decorator.func if isinstance(decorator, ast.Call) else decorator) or ''
            if name in ('dag', 'airflow.decorators.dag', 'airflow.sdk.dag'):
                call = decorator if isinstance(decorator, ast.Call) else ast.Call(ast.Name('dag'), [], [])
                created = self.make_dag(call, func.lineno, default_id=func.name)
                self.dag_vars[func.name] = created
                self.visit_body(func.body, created, None)
                return
            if name == 'task' or name.startswith('task.'):
                self.taskflow[func.name] = func
                return
            if name in ('task_group', 'taskgroup') or name.endswith('.task_group'):
                group_id = self.kwarg(decorator, 'group_id') if isinstance(decorator, ast.Call) else None
                func_group = str(group_id or func.name)
                func_group = f'{group}.{func_group}' if group else func_group
                self.taskflow[func.name] = func
                self.group_functions[func.name] = func_group
                self.group_members.setdefault(func_group, [])
                self.visit_body(func.body, dag, func_group)
                return

    def make_dag(self, call: ast.Call, line: int, default_id: str | None = None) -> DagInfo:
        dag_id: Any = self.name_kwarg(call, 'dag_id')
        if dag_id is None and call.args:
            dag_id = self.value(call.args[0])
        if not isinstance(dag_id, str) or UNRESOLVED in dag_id:
            dag_id = default_id or f'{self.path.stem}_line_{line}'
            if default_id is None:
                self.warnings.append(f'line {line}: DAG id is not a constant; using {dag_id}')
        schedule = self.kwarg(call, 'schedule') or self.kwarg(call, 'schedule_interval')
        description = self.kwarg(call, 'description') or self.kwarg(call, 'doc_md')
        tags = self.kwarg(call, 'tags')
        paths = self.kwarg(call, 'template_searchpath')
        for item in [paths] if isinstance(paths, str) else paths or []:
            if isinstance(item, str):
                base = Path(item)
                self.template_paths.append(base if base.is_absolute() else self.path.parent / base)
        dag = DagInfo(
            dag_id=dag_id,
            path=str(self.path),
            line=line,
            schedule=schedule if isinstance(schedule, str) else None,
            description=description.strip() if isinstance(description, str) else None,
            tags=[t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
        )
        self.dags.append(dag)
        return dag

    # Expressions: task creation and dependencies

    def visit_expression(self, node: ast.expr, dag: DagInfo | None, group: str | None) -> list[str] | None:
        """Register tasks created by ``node`` and return the task ids it evaluates to."""
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
            left = self.visit_expression(node.left, dag, group) or []
            right = self.visit_expression(node.right, dag, group) or []
            upstream, downstream = (left, right) if isinstance(node.op, ast.RShift) else (right, left)
            self.depend(dag, upstream, downstream)
            return right
        if isinstance(node, (ast.List, ast.Tuple)):
            found: list[str] = []
            for element in node.elts:
                found.extend(self.visit_expression(element, dag, group) or [])
            return found
        if isinstance(node, ast.Name):
            return self.task_vars.get(node.id)
        if isinstance(node, ast.Call):
            return self.visit_call(node, dag, group)
        return None

    def visit_call(self, call: ast.Call, dag: DagInfo | None, group: str | None) -> list[str] | None:
        func = call.func
        name = self.call_name(call) or ''
        if isinstance(func, ast.Attribute) and func.attr in ('set_downstream', 'set_upstream') and call.args:
            this = self.visit_expression(func.value, dag, group) or []
            other = self.visit_expression(call.args[0], dag, group) or []
            if func.attr == 'set_downstream':
                self.depend(dag, this, other)
            else:
                self.depend(dag, other, this)
            return this
        if name == 'chain':
            groups = [self.visit_expression(arg, dag, group) or [] for arg in call.args]
            for upstream, downstream in itertools.pairwise(groups):
                self.depend(dag, upstream, downstream)
            return groups[-1] if groups else []
        if name == 'cross_downstream' and len(call.args) == 2:
            upstream = self.visit_expression(call.args[0], dag, group) or []
            downstream = self.visit_expression(call.args[1], dag, group) or []
            self.depend(dag, upstream, downstream)
            return downstream
        # TaskFlow: fn(), fn.expand(...), fn.partial(...).expand(...), fn.override(task_id=...)(...)
        target, task_id = self._taskflow_target(func)
        if target is not None:
            return self.taskflow_task(self.taskflow[target], dag, group, task_id)
        if isinstance(func, ast.Attribute) and func.attr in ('expand', 'expand_kwargs', 'partial'):
            if isinstance(func.value, ast.Call):
                return self.visit_call(func.value, dag, group)
            return None
        if name in COSMOS_CLASSES or name.endswith(('Operator', 'Sensor')) or name.startswith('Dbt'):
            return self.operator_task(call, name, dag, group)
        return None

    def _expand_groups(self) -> list[tuple[DagInfo | None, str, str]]:
        """Replace TaskGroup markers as Airflow does: a group upstream of something contributes
        its last tasks, and a group downstream of something receives it at its first tasks."""
        concrete = [(u, d) for _, u, d in self.pending if not u.startswith(GROUP) and not d.startswith(GROUP)]

        def members(group: str) -> list[str]:
            return self.group_members.get(group, [])

        def ends(group: str, first: bool) -> list[str]:
            inside = set(members(group))
            linked = {d if first else u for u, d in concrete if u in inside and d in inside}
            return [t for t in members(group) if t not in linked]

        expanded: list[tuple[DagInfo | None, str, str]] = []
        for hint, upstream, downstream in self.pending:
            ups = ends(upstream[len(GROUP) :], first=False) if upstream.startswith(GROUP) else [upstream]
            downs = (
                ends(downstream[len(GROUP) :], first=True) if downstream.startswith(GROUP) else [downstream]
            )
            expanded.extend((hint, u, d) for u in ups for d in downs if u != d)
        return expanded

    def depend(self, dag: DagInfo | None, upstream: list[str], downstream: list[str]) -> None:
        for up in upstream:
            for down in downstream:
                if up != down:
                    self.pending.append((dag, up, down))

    def add_task(
        self, task: TaskInfo, dag_kwarg: ast.expr | None, dag: DagInfo | None, group: str | None
    ) -> list[str]:
        if group:
            task.task_id = f'{group}.{task.task_id}'
        owner = dag
        if isinstance(dag_kwarg, ast.Name) and dag_kwarg.id in self.dag_vars:
            owner = self.dag_vars[dag_kwarg.id]
        if owner is None:
            self.unowned.append(task)
        else:
            owner.tasks.setdefault(task.task_id, task)
        if group:
            parts = group.split('.')
            for depth in range(1, len(parts) + 1):
                self.group_members.setdefault('.'.join(parts[:depth]), []).append(task.task_id)
        return [task.task_id]

    def operator_task(
        self, call: ast.Call, name: str, dag: DagInfo | None, group: str | None
    ) -> list[str] | None:
        task_id = self.name_kwarg(call, 'task_id', 'group_id', 'dag_id')
        if task_id is None:
            self.warnings.append(f'line {call.lineno}: {name} has no constant task_id; skipped')
            return None
        task = TaskInfo(task_id=task_id, operator=name, line=call.lineno)
        doc = self.kwarg(call, 'doc_md') or self.kwarg(call, 'doc')
        task.doc = doc.strip() if isinstance(doc, str) else None
        command = self.kwarg(call, 'bash_command')
        if isinstance(command, str):
            task.bash_command = command
            task.dbt.extend(parse_dbt_commands(command))
        if name in COSMOS_CLASSES:
            task.dbt.append(_cosmos_invocation(self.kwarg_node(call, 'render_config'), self))
        elif name.startswith('Dbt'):
            command_name = _dbt_command_from_class(name)
            if command_name:
                selectors = _as_list(self.kwarg(call, 'select')) + _as_list(self.kwarg(call, 'models'))
                task.dbt.append(
                    DbtInvocation(
                        command=command_name,
                        selectors=selectors,
                        excludes=_as_list(self.kwarg(call, 'exclude')),
                        project_dir=_str_or_none(self.kwarg(call, 'project_dir')),
                    )
                )
        for key in SQL_KWARGS:
            self.read_sql(task, self.kwarg(call, key))
        configuration = self.kwarg(call, 'configuration')
        if isinstance(configuration, dict):
            query = configuration.get('query')
            if isinstance(query, dict):
                self.read_sql(task, query.get('query'))
                destination = query.get('destinationTable')
                if isinstance(destination, dict):
                    parts = [destination.get(k) for k in ('projectId', 'datasetId', 'tableId')]
                    task.writes.append('.'.join(p for p in parts if isinstance(p, str)))
        for key in WRITE_KWARGS:
            value = self.kwarg(call, key)
            if isinstance(value, str):
                task.writes.append(value)
        for key in READ_KWARGS:
            value = self.kwarg(call, key)
            if isinstance(value, str):
                task.reads.append(value)
        task.inlets = _assets(self.kwarg(call, 'inlets'))
        task.outlets = _assets(self.kwarg(call, 'outlets'))
        return self.add_task(task, self.kwarg_node(call, 'dag'), dag, group)

    def _taskflow_target(self, func: ast.expr) -> tuple[str | None, str | None]:
        """The TaskFlow function a call ends up invoking, and any task_id given by override()."""
        task_id: str | None = None
        while True:
            if isinstance(func, ast.Name):
                return (func.id if func.id in self.taskflow else None), task_id
            if isinstance(func, ast.Call) and isinstance(func.func, ast.Attribute):
                if func.func.attr == 'override':
                    override = self.kwarg(func, 'task_id')
                    task_id = task_id or (override if isinstance(override, str) else None)
                elif func.func.attr != 'partial':
                    return None, None
                func = func.func.value
                continue
            if isinstance(func, ast.Attribute) and func.attr in (
                'expand',
                'expand_kwargs',
                'override',
                'partial',
            ):
                func = func.value
                continue
            return None, None

    def taskflow_task(
        self,
        func: ast.FunctionDef | ast.AsyncFunctionDef,
        dag: DagInfo | None,
        group: str | None,
        override_id: str | None = None,
    ) -> list[str]:
        decorator_call: ast.Call | None = None
        kind = 'python'
        for decorator in func.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            dotted = _dotted(target) or ''
            if dotted == 'task' or dotted.startswith('task.'):
                kind = dotted.split('.', 1)[1] if '.' in dotted else 'python'
                decorator_call = decorator if isinstance(decorator, ast.Call) else None
        if func.name in self.group_functions:
            # A @task_group function: its tasks were registered when the function was visited.
            return [GROUP + self.group_functions[func.name]]
        task_id = func.name
        if decorator_call is not None:
            declared = self.kwarg(decorator_call, 'task_id')
            task_id = declared if isinstance(declared, str) else task_id
        task_id = override_id or task_id
        task = TaskInfo(
            task_id=task_id, operator=f'@task.{kind}' if kind != 'python' else '@task', line=func.lineno
        )
        task.doc = ast.get_docstring(func)
        if kind == 'bash':
            returned = [self.value(n.value) for n in ast.walk(func) if isinstance(n, ast.Return) and n.value]
            commands = [r for r in returned if isinstance(r, str)]
            if commands:
                task.bash_command = commands[0]
                for command in commands:
                    task.dbt.extend(parse_dbt_commands(command))
        if kind == 'sql':
            returned = [self.value(n.value) for n in ast.walk(func) if isinstance(n, ast.Return) and n.value]
            for value in returned:
                self.read_sql(task, value)
        if decorator_call is not None:
            task.inlets = _assets(self.kwarg(decorator_call, 'inlets'))
            task.outlets = _assets(self.kwarg(decorator_call, 'outlets'))
        return self.add_task(task, None, dag, group)

    def read_sql(self, task: TaskInfo, value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                self.read_sql(task, item)
            return
        if not isinstance(value, str) or not value.strip():
            return
        text = value.strip()
        if text.endswith('.sql') and '\n' not in text:
            for base in self.template_paths:
                candidate = (base / text).resolve()
                if candidate.is_file():
                    sql = candidate.read_text(encoding='utf-8')
                    task.sql = f'{task.sql};\n{sql}' if task.sql else sql
                    task.sql_path = str(candidate)
                    return
            self.warnings.append(f'task {task.task_id}: SQL file {text} not found')
            return
        task.sql = f'{task.sql};\n{text}' if task.sql else text


def _str_or_none(value: Any) -> str | None:
    return str(value) if isinstance(value, str) else None


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list):
        return [v for item in value if isinstance(item, str) for v in item.split()]
    return []


def _assets(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    found = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get('__asset__'), str):
            found.append(item['__asset__'])
        elif isinstance(item, str):
            found.append(item)
    return found


def _dbt_command_from_class(name: str) -> str | None:
    words: list[str] = re.findall(r'[A-Z][a-z]+', name)
    for word in words[1:]:
        if word.lower() in DBT_COMMANDS:
            return word.lower()
    return None


def _cosmos_invocation(render_config: ast.expr | None, parser: _Parser) -> DbtInvocation:
    invocation = DbtInvocation(command='build')
    if isinstance(render_config, ast.Call):
        invocation.selectors = _as_list(parser.kwarg(render_config, 'select'))
        invocation.excludes = _as_list(parser.kwarg(render_config, 'exclude'))
    return invocation


_SHELL_SPLIT = re.compile(r'&&|\|\||;|\n|\|')
_SELECT_FLAGS = {'--select', '-s', '--models', '-m'}
_EXCLUDE_FLAGS = {'--exclude'}


def parse_dbt_commands(command: str) -> list[DbtInvocation]:
    """Find ``dbt <command>`` invocations in a shell command and parse their selection flags."""
    invocations = []
    for segment in _SHELL_SPLIT.split(command):
        try:
            words = shlex.split(segment, posix=True)
        except ValueError:
            words = segment.split()
        for index, word in enumerate(words):
            if Path(word).name != 'dbt' or index + 1 >= len(words):
                continue
            rest = words[index + 1 :]
            subcommand = next((w for w in rest if not w.startswith('-')), None)
            if subcommand not in DBT_COMMANDS:
                break
            invocation = DbtInvocation(command=subcommand)
            current: list[str] | None = None
            position = 0
            while position < len(rest):
                word = rest[position]
                flag, _, inline = word.partition('=')
                if flag in _SELECT_FLAGS:
                    current = invocation.selectors
                elif flag in _EXCLUDE_FLAGS:
                    current = invocation.excludes
                elif flag == '--project-dir' and position + 1 < len(rest):
                    invocation.project_dir = inline or rest[position + 1]
                    current = None
                    position += 1 if not inline else 0
                elif flag in ('--selector', '--state', '--defer') or flag.startswith('state:'):
                    invocation.unsupported.append(word)
                    current = None
                elif word.startswith('-'):
                    current = None
                elif current is not None:
                    if (
                        word.startswith('state:')
                        or word.startswith('source_status:')
                        or word.startswith('result:')
                    ):
                        invocation.unsupported.append(word)
                    else:
                        current.append(word)
                position += 1
                if inline and flag in _SELECT_FLAGS | _EXCLUDE_FLAGS and current is not None:
                    current.append(inline)
            invocations.append(invocation)
            break
    return invocations


def parse_source(path: Path, source: str) -> list[DagInfo]:
    """Parse DAG source as if it were the file at ``path`` (for other versions of a file)."""
    return _Parser(path, source).parse()


def parse_file(path: Path) -> list[DagInfo]:
    """Parse one DAG file. Raises SyntaxError if the file is not valid Python, or is nested too
    deeply to read."""
    source = path.read_text(encoding='utf-8')
    return _Parser(path, source).parse()


def _syntax_tree(source: str, path: Path) -> ast.Module:
    try:
        return ast.parse(source, filename=str(path))
    except ValueError as error:
        # Python 3.11 rejects a NUL byte with a ValueError; 3.12 and later with a SyntaxError.
        raise SyntaxError(str(error), (str(path), 1, 1, None)) from None
