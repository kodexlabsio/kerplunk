"""PySpark jobs, read for the SQL they run, without importing them.

A PySpark job often builds its tables with SQL: ``spark.sql("INSERT INTO ... SELECT ...")``. This
module parses a job file with :mod:`ast` and returns each SQL string passed to a ``.sql(...)``
call on a Spark session (an object named ``spark``, or a name ending in ``spark`` or ``session``),
with its line, so the build can give the tables it writes column lineage in the Spark dialect.
A string is read when it is a literal, an f-string with no placeholders, a concatenation of
literals, or a module-level name bound to one exactly once in the file. SQL built at run time,
a name bound more than once or inside a function, and a call that formats its SQL with keyword
arguments (``spark.sql("... {t}", t=df)``) are reported and skipped. In a file that uses
Snowpark, only a name ending in ``spark`` is a Spark session.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from konstruct.sql import PARSE_ERRORS

_SESSION_HINTS = ('spark', 'session')


@dataclass
class SparkStatement:
    line: int
    """The line of the ``.sql(...)`` call."""
    sql: str
    """The SQL text."""


@dataclass
class SparkFile:
    path: str
    statements: list[SparkStatement] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    """``path:line: message`` for calls whose SQL could not be read."""


def looks_like_spark_file(text: str) -> bool:
    """Whether a Python file is worth parsing as a PySpark job: one that imports PySpark, a
    Databricks notebook exported as source, or one that calls ``spark.sql``."""
    if '.sql(' not in text:
        return False
    return (
        'pyspark' in text
        or 'SparkSession' in text
        or '# Databricks notebook source' in text
        or 'spark.sql(' in text
    )


def _is_session(node: ast.expr, hints: tuple[str, ...]) -> bool:
    name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
    return name is not None and name.lower().endswith(hints)


def _bound_names(tree: ast.Module) -> dict[str, int]:
    """How many times each name is bound anywhere in the file."""
    counts: dict[str, int] = {}

    def bind(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bind(node.id)
        elif isinstance(node, ast.arg):
            bind(node.arg)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                bind(name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name)
        elif isinstance(node, ast.alias):
            bind((node.asname or node.name).split('.')[0])
        elif (isinstance(node, ast.ExceptHandler) and node.name) or (
            isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name
        ):
            bind(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bind(node.rest)
    return counts


def _text(node: ast.expr, constants: dict[str, str]) -> str | None:
    """The string ``node`` is, when it can be known without running anything."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and all(isinstance(v, ast.Constant) for v in node.values):
        return ''.join(str(v.value) for v in node.values if isinstance(v, ast.Constant))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left, constants), _text(node.right, constants)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def parse(text: str, path: str) -> SparkFile:
    """The SQL statements a PySpark job passes to ``spark.sql``."""
    result = SparkFile(path=path)
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as error:
        result.problems.append(f'{path}:{error.lineno or 0}: not valid Python: {error.msg}')
        return result
    # Only a name bound once in the whole file, at module level, is known without running it.
    counts = _bound_names(tree)
    constants: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            value = _text(statement.value, constants)
            if isinstance(target, ast.Name) and value is not None and counts.get(target.id) == 1:
                constants[target.id] = value
    hints = ('spark',) if 'snowpark' in text.lower() else _SESSION_HINTS
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'sql'
            and _is_session(node.func.value, hints)
        ):
            continue
        extra = [k.arg for k in node.keywords if k.arg not in ('sqlQuery', 'args')]
        if extra:
            result.problems.append(
                f'{path}:{node.lineno}: spark.sql formats its SQL with keyword arguments '
                f'({", ".join(str(k) for k in extra)}), so it is skipped'
            )
            continue
        argument = (
            node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == 'sqlQuery'), None)
        )
        sql = _text(argument, constants) if argument is not None else None
        if sql is None:
            result.problems.append(f'{path}:{node.lineno}: spark.sql with SQL built at run time is skipped')
            continue
        result.statements.append(SparkStatement(node.lineno, sql))
    result.statements.sort(key=lambda s: s.line)
    result.problems.sort(key=lambda p: int(p.split(':')[1]) if p.split(':')[1].isdigit() else 0)
    return result


def without_session_catalog(sql: str, dialect: str) -> str:
    """``sql`` with ``spark_catalog.``, the session's default catalogue, left out of table names,
    so ``spark_catalog.raw.orders`` and ``raw.orders`` are the same table."""
    if 'spark_catalog' not in sql.lower():
        return sql
    try:
        trees = sqlglot.parse(sql, read=dialect)
    except PARSE_ERRORS:
        return sql
    for tree in trees:
        for table in tree.find_all(exp.Table) if tree is not None else []:
            if table.catalog.lower() == 'spark_catalog':
                table.set('catalog', None)
    return ';\n'.join(t.sql(dialect=dialect) for t in trees if t is not None)
