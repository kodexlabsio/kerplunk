//! Airflow DAG files read as syntax trees and never run: the DAGs they declare, their tasks and
//! the dependencies between them.
//!
//! Airflow does not need to be installed and no DAG code runs. The reader understands the common
//! ways of writing DAGs and tasks (`with DAG(...)`, `DAG(...)` assigned to a name, `@dag`,
//! operators, TaskFlow `@task` functions with `.expand` and `.override`, task groups, `>>`,
//! `<<`, `set_upstream`, `chain` and `cross_downstream`, assets and datasets, Cosmos and dbt
//! operators, and `bash_command` strings that run dbt); anything it cannot resolve statically
//! (computed task ids, SQL built at runtime) is skipped and reported as a warning of the DAG.

pub mod dbt;
#[cfg(feature = "python")]
pub mod python;

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use ruff_python_ast::{Expr, ExprCall, Operator, Stmt, StmtFunctionDef};

pub use dbt::{DbtInvocation, parse_dbt_commands};

use crate::pysyntax::nodes::{call_name, definition_line, docstring, dotted, line};
use crate::pysyntax::text;
use crate::pysyntax::value::Value;
use crate::pysyntax::walk::{Node, Orelse, walk};
use crate::pysyntax::{Depth, Failure, Source, parse, universal_newlines, with_deep_stack};

pub const DAG_CLASSES: [&str; 1] = ["DAG"];
pub const GROUP_CLASSES: [&str; 1] = ["TaskGroup"];
pub const COSMOS_CLASSES: [&str; 2] = ["DbtTaskGroup", "DbtDag"];
pub const SQL_KWARGS: [&str; 3] = ["sql", "query", "sql_query"];
pub const WRITE_KWARGS: [&str; 3] = [
    "target_table",
    "destination_table",
    "destination_project_dataset_table",
];
pub const READ_KWARGS: [&str; 3] = ["table", "source_table", "source_project_dataset_table"];
pub const UNRESOLVED: &str = "{{ unresolved }}";
/// Marks a whole TaskGroup in a dependency until its first and last tasks are known.
pub const GROUP: &str = "\0group:";

/// Why a file could not be read.
#[derive(Debug)]
pub enum Error {
    /// The text is not Python.
    Syntax(Failure),
    /// A file could not be read.
    Io(std::io::Error),
    /// A file is not UTF-8.
    NotUtf8 {
        bytes: Vec<u8>,
        error: std::str::Utf8Error,
    },
}

impl From<Failure> for Error {
    fn from(failure: Failure) -> Self {
        Self::Syntax(failure)
    }
}

impl From<std::io::Error> for Error {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

/// A file's text, or why it cannot be read as UTF-8 text with universal newlines.
pub fn read_text(path: &Path) -> Result<String, Error> {
    let bytes = std::fs::read(path)?;
    match std::str::from_utf8(&bytes) {
        Ok(text) => Ok(universal_newlines(text).into_owned()),
        Err(error) => Err(Error::NotUtf8 { error, bytes }),
    }
}

/// One task of a DAG.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct TaskInfo {
    pub task_id: String,
    pub operator: String,
    pub line: usize,
    pub doc: Option<String>,
    pub bash_command: Option<String>,
    pub sql: Option<String>,
    pub sql_path: Option<String>,
    pub dbt: Vec<DbtInvocation>,
    pub reads: Vec<String>,
    pub writes: Vec<String>,
    pub inlets: Vec<String>,
    pub outlets: Vec<String>,
}

/// The tasks of a DAG by id, in the order they were found.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Tasks {
    order: Vec<TaskInfo>,
    index: HashMap<String, usize>,
}

impl Tasks {
    /// `dict.setdefault`: a task whose id is taken is dropped.
    fn add(&mut self, task: TaskInfo) {
        if !self.index.contains_key(&task.task_id) {
            self.index.insert(task.task_id.clone(), self.order.len());
            self.order.push(task);
        }
    }

    pub fn contains(&self, id: &str) -> bool {
        self.index.contains_key(id)
    }

    pub fn iter(&self) -> impl Iterator<Item = &TaskInfo> {
        self.order.iter()
    }

    pub fn into_vec(self) -> Vec<TaskInfo> {
        self.order
    }

    pub fn len(&self) -> usize {
        self.order.len()
    }

    pub fn is_empty(&self) -> bool {
        self.order.is_empty()
    }
}

/// One DAG of a file.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DagInfo {
    pub dag_id: String,
    pub path: String,
    pub line: usize,
    pub schedule: Option<String>,
    pub description: Option<String>,
    pub tags: Vec<String>,
    pub tasks: Tasks,
    pub dependencies: Vec<(String, String)>,
    pub warnings: Vec<String>,
}

/// `Path.stem`: the name without its last suffix.
fn stem(name: &str) -> &str {
    match name.rfind('.') {
        Some(at) if at > 0 && at + 1 < name.len() => &name[..at],
        _ => name,
    }
}

struct Pending {
    dag: Option<usize>,
    upstream: String,
    downstream: String,
}

struct Parser<'a> {
    source: &'a Source,
    path: &'a str,
    parent: PathBuf,
    stem: String,
    depth: Depth,
    constants: HashMap<String, Value>,
    dags: Vec<DagInfo>,
    dag_vars: HashMap<String, usize>,
    task_vars: HashMap<String, Vec<String>>,
    taskflow: HashMap<String, &'a StmtFunctionDef>,
    group_members: HashMap<String, Vec<String>>,
    group_functions: HashMap<String, String>,
    unowned: Vec<TaskInfo>,
    template_paths: Vec<PathBuf>,
    warnings: Vec<String>,
    pending: Vec<Pending>,
    aliases: HashMap<String, String>,
    /// The first file that could not be read, which stops the reading.
    failure: Option<Error>,
}

/// `a or b` for the values the readers compare: `a` when it is true, else `b`.
fn either(first: Value, second: impl FnOnce() -> Value) -> Value {
    if first.truthy() { first } else { second() }
}

/// The strings among `value`'s words: `str.split()` of a string, of each string of a list.
fn as_list(value: &Value) -> Vec<String> {
    match value {
        Value::Str(s) => text::split_whitespace(s).map(str::to_owned).collect(),
        Value::List(items) => items
            .iter()
            .filter_map(Value::as_str)
            .flat_map(text::split_whitespace)
            .map(str::to_owned)
            .collect(),
        _ => Vec::new(),
    }
}

/// The assets a value names: the keys of asset markers and the strings.
fn assets(value: &Value) -> Vec<String> {
    let one = |item: &Value| match item {
        Value::Dict(_) => item
            .get("__asset__")
            .and_then(Value::as_str)
            .map(str::to_owned),
        Value::Str(s) => Some(s.clone()),
        _ => None,
    };
    match value {
        Value::List(items) => items.iter().filter_map(one).collect(),
        other => one(other).into_iter().collect(),
    }
}

/// The dbt command a Cosmos or dbt operator class is named for: `DbtRunOperator` runs `run`.
fn dbt_command_from_class(name: &str) -> Option<&'static str> {
    let bytes = name.as_bytes();
    let mut words: Vec<&str> = Vec::new();
    let mut at = 0;
    while at < bytes.len() {
        if bytes[at].is_ascii_uppercase() {
            let mut end = at + 1;
            while end < bytes.len() && bytes[end].is_ascii_lowercase() {
                end += 1;
            }
            if end > at + 1 {
                words.push(&name[at..end]);
                at = end;
                continue;
            }
        }
        at += 1;
    }
    words.iter().skip(1).find_map(|word| {
        dbt::DBT_COMMANDS
            .iter()
            .find(|c| word.eq_ignore_ascii_case(c))
            .copied()
    })
}

fn keyword<'e>(call: &'e ExprCall, name: &str) -> Option<&'e Expr> {
    call.arguments
        .keywords
        .iter()
        .find(|k| k.arg.as_ref().is_some_and(|a| a.as_str() == name))
        .map(|k| &k.value)
}

impl<'a> Parser<'a> {
    /// The class or function a call names, seen through `import ... as` aliases.
    fn name_of(&self, node: &Expr) -> Option<String> {
        let name = call_name(node)?;
        Some(
            self.aliases
                .get(name)
                .map_or(name, String::as_str)
                .to_owned(),
        )
    }

    // Constant folding

    /// The value an expression folds to, or `None`.
    fn value(&self, node: &Expr) -> Value {
        let folded = self.fold(node);
        if folded.too_big() {
            Value::None
        } else {
            folded
        }
    }

    fn fold(&self, node: &Expr) -> Value {
        let Some(_level) = self.depth.enter() else {
            return Value::None;
        };
        if let Some(found) = crate::pysyntax::nodes::literal(node) {
            return found;
        }
        match node {
            Expr::Name(name) => self
                .constants
                .get(name.id.as_str())
                .cloned()
                .unwrap_or(Value::None),
            Expr::FString(fstring) => {
                let mut out = String::new();
                for part in fstring.value.as_slice() {
                    match part {
                        ruff_python_ast::FStringPart::Literal(literal) => {
                            out.push_str(&literal.value)
                        }
                        ruff_python_ast::FStringPart::FString(f) => {
                            for element in f.elements.iter() {
                                match element {
                                    ruff_python_ast::InterpolatedStringElement::Literal(l) => {
                                        out.push_str(&l.value);
                                    }
                                    ruff_python_ast::InterpolatedStringElement::Interpolation(
                                        i,
                                    ) => {
                                        if let Some(debug) = &i.debug_text {
                                            out.push_str(debug.as_str());
                                        }
                                        match self.value(&i.expression) {
                                            Value::None => out.push_str(UNRESOLVED),
                                            inner => out.push_str(&inner.to_python_str()),
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                Value::Str(out)
            }
            Expr::BinOp(op) if op.op == Operator::Add => {
                match (self.value(&op.left), self.value(&op.right)) {
                    (Value::Str(left), Value::Str(right)) => Value::Str(left + &right),
                    _ => Value::None,
                }
            }
            Expr::List(list) => Value::List(list.elts.iter().map(|e| self.value(e)).collect()),
            Expr::Tuple(tuple) => Value::List(tuple.elts.iter().map(|e| self.value(e)).collect()),
            Expr::Dict(dict) => Value::dict(dict.items.iter().filter_map(|item| {
                item.key
                    .as_ref()
                    .map(|key| (self.value(key), self.value(&item.value)))
            })),
            Expr::Call(call) => match self.name_of(node).as_deref() {
                Some("Asset" | "Dataset") => {
                    let uri = match call.arguments.args.first() {
                        Some(first) => self.value(first),
                        None => self.kwarg(call, "uri"),
                    };
                    Value::marker("__asset__", either(uri, || self.kwarg(call, "name")))
                }
                Some("dedent") if !call.arguments.args.is_empty() => {
                    self.value(&call.arguments.args[0])
                }
                _ => Value::None,
            },
            _ => Value::None,
        }
    }

    fn kwarg(&self, call: &ExprCall, name: &str) -> Value {
        keyword(call, name).map_or(Value::None, |node| self.value(node))
    }

    /// A DAG or task id: only a fully resolved constant counts.
    fn name_kwarg(&self, call: &ExprCall, names: &[&str]) -> Option<String> {
        names.iter().find_map(|name| match self.kwarg(call, name) {
            Value::Str(text) if !text.contains(UNRESOLVED) => Some(text),
            _ => None,
        })
    }

    // Entry point

    fn run(&mut self, body: &'a [Stmt]) -> Result<Vec<DagInfo>, Error> {
        walk(body.iter().map(Node::Stmt), |node| {
            let Node::Stmt(statement) = node else { return };
            let names = match statement {
                Stmt::Import(import) => &import.names,
                Stmt::ImportFrom(import) => &import.names,
                _ => return,
            };
            for alias in names {
                if let Some(asname) = &alias.asname {
                    let last = alias.name.as_str().rsplit('.').next().unwrap_or_default();
                    self.aliases.insert(asname.to_string(), last.to_owned());
                }
            }
        });
        if let Some(doc) = docstring(body).filter(|doc| !doc.is_empty()) {
            self.constants.insert("__doc__".into(), Value::Str(doc));
        }
        for statement in body {
            if let Stmt::Assign(assign) = statement
                && let [Expr::Name(target)] = assign.targets.as_slice()
            {
                let folded = self.fold(&assign.value);
                if folded.too_big() {
                    // Left unresolved: the name is unbound, not kept at its last smaller value.
                    self.constants.remove(target.id.as_str());
                } else if !folded.is_none() {
                    self.constants.insert(target.id.to_string(), folded);
                }
            }
        }
        self.visit_body(body, None, None);
        if self.depth.exceeded() {
            return Err(Failure::too_deep().into());
        }
        if let Some(failure) = self.failure.take() {
            return Err(failure);
        }
        if !self.unowned.is_empty() {
            if self.dags.len() == 1 {
                for task in std::mem::take(&mut self.unowned) {
                    self.dags[0].tasks.add(task);
                }
            } else {
                let ids: Vec<&str> = self.unowned.iter().map(|t| t.task_id.as_str()).collect();
                self.warnings.push(format!(
                    "{} task(s) could not be matched to a DAG: {}",
                    self.unowned.len(),
                    ids.join(", ")
                ));
            }
        }
        for (hint, upstream, downstream) in self.expand_groups() {
            let owner = hint.or_else(|| {
                self.dags
                    .iter()
                    .position(|d| d.tasks.contains(&upstream) && d.tasks.contains(&downstream))
            });
            if let Some(owner) = owner {
                let pair = (upstream, downstream);
                if !self.dags[owner].dependencies.contains(&pair) {
                    self.dags[owner].dependencies.push(pair);
                }
            }
        }
        for dag in &mut self.dags {
            dag.warnings.extend(self.warnings.iter().cloned());
        }
        Ok(std::mem::take(&mut self.dags))
    }

    // Walking statements

    fn visit_body(&mut self, body: &'a [Stmt], dag: Option<usize>, group: Option<&str>) {
        for statement in body {
            self.visit_statement(statement, dag, group);
        }
    }

    fn visit_statement(&mut self, statement: &'a Stmt, dag: Option<usize>, group: Option<&str>) {
        match statement {
            Stmt::With(with) if !with.is_async => {
                let mut inner_dag = dag;
                let mut inner_group = group.map(str::to_owned);
                for item in &with.items {
                    let expr = &item.context_expr;
                    let name = self.name_of(expr);
                    let Expr::Call(call) = expr else { continue };
                    if name.as_deref().is_some_and(|n| DAG_CLASSES.contains(&n)) {
                        let created = self.make_dag(Some(call), line(self.source, with), None);
                        inner_dag = Some(created);
                        if let Some(Expr::Name(var)) = item.optional_vars.as_deref() {
                            self.dag_vars.insert(var.id.to_string(), created);
                        }
                    } else if name.as_deref().is_some_and(|n| GROUP_CLASSES.contains(&n)) {
                        let group_id = match call.arguments.args.first() {
                            Some(first) => self.value(first),
                            None => self.kwarg(call, "group_id"),
                        };
                        if let Value::Str(group_id) = group_id {
                            let name = match group.filter(|g| !g.is_empty()) {
                                Some(outer) => format!("{outer}.{group_id}"),
                                None => group_id,
                            };
                            self.group_members.entry(name.clone()).or_default();
                            if let Some(Expr::Name(var)) = item.optional_vars.as_deref() {
                                self.task_vars
                                    .insert(var.id.to_string(), vec![format!("{GROUP}{name}")]);
                            }
                            inner_group = Some(name);
                        }
                    }
                }
                self.visit_body(&with.body, inner_dag, inner_group.as_deref());
            }
            Stmt::FunctionDef(function) => self.visit_function(function, dag, group),
            Stmt::Assign(assign) => {
                let value = &*assign.value;
                if let Expr::Call(call) = value
                    && self
                        .name_of(value)
                        .as_deref()
                        .is_some_and(|n| DAG_CLASSES.contains(&n))
                {
                    let created = self.make_dag(Some(call), line(self.source, assign), None);
                    for target in &assign.targets {
                        if let Expr::Name(name) = target {
                            self.dag_vars.insert(name.id.to_string(), created);
                        }
                    }
                    return;
                }
                for target in &assign.targets {
                    if let (Expr::Tuple(targets), Expr::Tuple(values)) = (target, value)
                        && targets.elts.len() == values.elts.len()
                    {
                        for (var, element) in targets.elts.iter().zip(&values.elts) {
                            let found = self.visit_expression(element, dag, group);
                            if let (Expr::Name(var), Some(found)) = (var, found) {
                                self.task_vars.insert(var.id.to_string(), found);
                            }
                        }
                        return;
                    }
                }
                let tasks = self.visit_expression(value, dag, group);
                if let Some(tasks) = tasks {
                    for target in &assign.targets {
                        if let Expr::Name(name) = target {
                            self.task_vars.insert(name.id.to_string(), tasks.clone());
                        }
                    }
                }
            }
            Stmt::Expr(expression) => {
                self.visit_expression(&expression.value, dag, group);
            }
            Stmt::If(statement) => {
                self.visit_branch(
                    &statement.body,
                    Orelse::of(&statement.elif_else_clauses),
                    dag,
                    group,
                );
            }
            Stmt::For(statement) if !statement.is_async => {
                self.visit_branch(
                    &statement.body,
                    Orelse::Stmts(&statement.orelse),
                    dag,
                    group,
                );
            }
            Stmt::While(statement) => {
                self.visit_branch(
                    &statement.body,
                    Orelse::Stmts(&statement.orelse),
                    dag,
                    group,
                );
            }
            Stmt::Try(statement) if !statement.is_star => {
                self.visit_body(&statement.body, dag, group);
                self.visit_body(&statement.orelse, dag, group);
                self.visit_body(&statement.finalbody, dag, group);
                for handler in &statement.handlers {
                    let ruff_python_ast::ExceptHandler::ExceptHandler(handler) = handler;
                    self.visit_body(&handler.body, dag, group);
                }
            }
            _ => {}
        }
    }

    /// The body of an `if`, `for` or `while` and what follows it, each read once. (The reader
    /// this replaced read the statements of an `else` twice, and a chain of `n` `elif` took 2^n,
    /// with a warning for every read.)
    fn visit_branch(
        &mut self,
        body: &'a [Stmt],
        orelse: Orelse<'a>,
        dag: Option<usize>,
        group: Option<&str>,
    ) {
        self.visit_body(body, dag, group);
        self.visit_orelse(orelse, dag, group);
    }

    fn visit_orelse(&mut self, orelse: Orelse<'a>, dag: Option<usize>, group: Option<&str>) {
        match orelse {
            Orelse::Stmts(body) => self.visit_body(body, dag, group),
            // An `elif` is an `If` statement that is the whole of the `orelse`.
            Orelse::Elif(clauses) => {
                if let Some(_level) = self.depth.enter() {
                    self.visit_branch(&clauses[0].body, Orelse::of(&clauses[1..]), dag, group);
                }
            }
        }
    }

    fn visit_function(
        &mut self,
        function: &'a StmtFunctionDef,
        dag: Option<usize>,
        group: Option<&str>,
    ) {
        for decorator in &function.decorator_list {
            let (target, call) = match &decorator.expression {
                Expr::Call(call) => (&*call.func, Some(call)),
                other => (other, None),
            };
            let name = dotted(target).unwrap_or_default();
            let function_line = definition_line(self.source, function);
            if matches!(
                name.as_str(),
                "dag" | "airflow.decorators.dag" | "airflow.sdk.dag"
            ) {
                let created = self.make_dag(call, function_line, Some(function.name.as_str()));
                self.dag_vars.insert(function.name.to_string(), created);
                self.visit_body(&function.body, Some(created), None);
                return;
            }
            if name == "task" || name.starts_with("task.") {
                self.taskflow.insert(function.name.to_string(), function);
                return;
            }
            if matches!(name.as_str(), "task_group" | "taskgroup") || name.ends_with(".task_group")
            {
                let group_id = call.map_or(Value::None, |call| self.kwarg(call, "group_id"));
                let own = if group_id.truthy() {
                    group_id.to_python_str()
                } else {
                    function.name.to_string()
                };
                let func_group = match group.filter(|g| !g.is_empty()) {
                    Some(outer) => format!("{outer}.{own}"),
                    None => own,
                };
                self.taskflow.insert(function.name.to_string(), function);
                self.group_functions
                    .insert(function.name.to_string(), func_group.clone());
                self.group_members.entry(func_group.clone()).or_default();
                self.visit_body(&function.body, dag, Some(&func_group));
                return;
            }
        }
    }

    fn make_dag(
        &mut self,
        call: Option<&ExprCall>,
        line: usize,
        default_id: Option<&str>,
    ) -> usize {
        let kwarg = |name: &str| call.map_or(Value::None, |call| self.kwarg(call, name));
        let schedule = either(kwarg("schedule"), || kwarg("schedule_interval"));
        let description = either(kwarg("description"), || kwarg("doc_md"));
        let tags = kwarg("tags");
        let paths = kwarg("template_searchpath");
        let mut dag_id = call
            .and_then(|call| self.name_kwarg(call, &["dag_id"]))
            .map(Value::Str);
        if dag_id.is_none()
            && let Some(first) = call.and_then(|call| call.arguments.args.first())
        {
            dag_id = Some(self.value(first));
        }
        let dag_id = match dag_id {
            Some(Value::Str(id)) if !id.contains(UNRESOLVED) => id,
            _ => {
                let id =
                    default_id.map_or_else(|| format!("{}_line_{line}", self.stem), str::to_owned);
                if default_id.is_none() {
                    self.warnings
                        .push(format!("line {line}: DAG id is not a constant; using {id}"));
                }
                id
            }
        };
        let items: Vec<&str> = match &paths {
            Value::Str(one) => vec![one],
            Value::List(items) => items.iter().filter_map(Value::as_str).collect(),
            Value::Dict(items) => items.iter().filter_map(|(key, _)| key.as_str()).collect(),
            _ => Vec::new(),
        };
        for item in items {
            let base = Path::new(item);
            self.template_paths.push(if base.is_absolute() {
                base.to_path_buf()
            } else {
                self.parent.join(base)
            });
        }
        self.dags.push(DagInfo {
            dag_id,
            path: self.path.to_owned(),
            line,
            schedule: schedule.as_str().map(str::to_owned),
            description: description.as_str().map(|d| text::strip(d).to_owned()),
            tags: tags
                .as_list()
                .map(|tags| {
                    tags.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default(),
            ..DagInfo::default()
        });
        self.dags.len() - 1
    }

    // Expressions: task creation and dependencies

    /// Registers the tasks `node` creates and returns the task ids it evaluates to.
    fn visit_expression(
        &mut self,
        node: &'a Expr,
        dag: Option<usize>,
        group: Option<&str>,
    ) -> Option<Vec<String>> {
        let _level = self.depth.enter()?;
        match node {
            Expr::BinOp(op) if matches!(op.op, Operator::RShift | Operator::LShift) => {
                let left = self
                    .visit_expression(&op.left, dag, group)
                    .unwrap_or_default();
                let right = self
                    .visit_expression(&op.right, dag, group)
                    .unwrap_or_default();
                if op.op == Operator::RShift {
                    self.depend(dag, &left, &right);
                } else {
                    self.depend(dag, &right, &left);
                }
                Some(right)
            }
            Expr::List(list) => Some(self.visit_each(&list.elts, dag, group)),
            Expr::Tuple(tuple) => Some(self.visit_each(&tuple.elts, dag, group)),
            Expr::Name(name) => self.task_vars.get(name.id.as_str()).cloned(),
            Expr::Call(call) => self.visit_call(node, call, dag, group),
            _ => None,
        }
    }

    fn visit_each(
        &mut self,
        elements: &'a [Expr],
        dag: Option<usize>,
        group: Option<&str>,
    ) -> Vec<String> {
        let mut found = Vec::new();
        for element in elements {
            found.extend(
                self.visit_expression(element, dag, group)
                    .unwrap_or_default(),
            );
        }
        found
    }

    fn visit_call(
        &mut self,
        node: &'a Expr,
        call: &'a ExprCall,
        dag: Option<usize>,
        group: Option<&str>,
    ) -> Option<Vec<String>> {
        let func = &*call.func;
        let name = self.name_of(node).unwrap_or_default();
        let args = &call.arguments.args;
        if let Expr::Attribute(attribute) = func
            && matches!(attribute.attr.as_str(), "set_downstream" | "set_upstream")
            && !args.is_empty()
        {
            let this = self
                .visit_expression(&attribute.value, dag, group)
                .unwrap_or_default();
            let other = self
                .visit_expression(&args[0], dag, group)
                .unwrap_or_default();
            if attribute.attr.as_str() == "set_downstream" {
                self.depend(dag, &this, &other);
            } else {
                self.depend(dag, &other, &this);
            }
            return Some(this);
        }
        if name == "chain" {
            let groups: Vec<Vec<String>> = args
                .iter()
                .map(|arg| self.visit_expression(arg, dag, group).unwrap_or_default())
                .collect();
            for pair in groups.windows(2) {
                self.depend(dag, &pair[0], &pair[1]);
            }
            return Some(groups.last().cloned().unwrap_or_default());
        }
        if name == "cross_downstream" && args.len() == 2 {
            let upstream = self
                .visit_expression(&args[0], dag, group)
                .unwrap_or_default();
            let downstream = self
                .visit_expression(&args[1], dag, group)
                .unwrap_or_default();
            self.depend(dag, &upstream, &downstream);
            return Some(downstream);
        }
        // TaskFlow: fn(), fn.expand(...), fn.partial(...).expand(...), fn.override(task_id=...)(...)
        let (target, task_id) = self.taskflow_target(func);
        if let Some(target) = target {
            return Some(self.taskflow_task(target, dag, group, task_id));
        }
        if let Expr::Attribute(attribute) = func
            && matches!(
                attribute.attr.as_str(),
                "expand" | "expand_kwargs" | "partial"
            )
        {
            return match &*attribute.value {
                inner @ Expr::Call(inner_call) => self.visit_call(inner, inner_call, dag, group),
                _ => None,
            };
        }
        let cosmos = COSMOS_CLASSES.contains(&name.as_str());
        if cosmos
            || name.ends_with("Operator")
            || name.ends_with("Sensor")
            || name.starts_with("Dbt")
        {
            return self.operator_task(call, &name, dag, group);
        }
        None
    }

    /// Replaces the group markers in the dependencies as Airflow does: a group upstream of
    /// something contributes its last tasks, and a group downstream of something receives it at
    /// its first tasks.
    fn expand_groups(&self) -> Vec<(Option<usize>, String, String)> {
        let concrete: Vec<(&str, &str)> = self
            .pending
            .iter()
            .filter(|p| !p.upstream.starts_with(GROUP) && !p.downstream.starts_with(GROUP))
            .map(|p| (p.upstream.as_str(), p.downstream.as_str()))
            .collect();
        let members = |group: &str| self.group_members.get(group).map_or(&[][..], Vec::as_slice);
        let ends = |group: &str, first: bool| -> Vec<String> {
            let inside: std::collections::HashSet<&str> =
                members(group).iter().map(String::as_str).collect();
            let linked: std::collections::HashSet<&str> = concrete
                .iter()
                .filter(|(u, d)| inside.contains(u) && inside.contains(d))
                .map(|&(u, d)| if first { d } else { u })
                .collect();
            members(group)
                .iter()
                .filter(|t| !linked.contains(t.as_str()))
                .cloned()
                .collect()
        };
        let mut expanded = Vec::new();
        for pending in &self.pending {
            let ups = match pending.upstream.strip_prefix(GROUP) {
                Some(group) => ends(group, false),
                None => vec![pending.upstream.clone()],
            };
            let downs = match pending.downstream.strip_prefix(GROUP) {
                Some(group) => ends(group, true),
                None => vec![pending.downstream.clone()],
            };
            for up in &ups {
                for down in &downs {
                    if up != down {
                        expanded.push((pending.dag, up.clone(), down.clone()));
                    }
                }
            }
        }
        expanded
    }

    fn depend(&mut self, dag: Option<usize>, upstream: &[String], downstream: &[String]) {
        for up in upstream {
            for down in downstream {
                if up != down {
                    self.pending.push(Pending {
                        dag,
                        upstream: up.clone(),
                        downstream: down.clone(),
                    });
                }
            }
        }
    }

    fn add_task(
        &mut self,
        mut task: TaskInfo,
        dag_kwarg: Option<&Expr>,
        dag: Option<usize>,
        group: Option<&str>,
    ) -> Vec<String> {
        let group = group.filter(|g| !g.is_empty());
        if let Some(group) = group {
            task.task_id = format!("{group}.{}", task.task_id);
        }
        let mut owner = dag;
        if let Some(Expr::Name(name)) = dag_kwarg
            && let Some(&found) = self.dag_vars.get(name.id.as_str())
        {
            owner = Some(found);
        }
        let id = task.task_id.clone();
        match owner {
            None => self.unowned.push(task),
            Some(owner) => self.dags[owner].tasks.add(task),
        }
        if let Some(group) = group {
            let parts: Vec<&str> = group.split('.').collect();
            for depth in 1..=parts.len() {
                self.group_members
                    .entry(parts[..depth].join("."))
                    .or_default()
                    .push(id.clone());
            }
        }
        vec![id]
    }

    fn operator_task(
        &mut self,
        call: &'a ExprCall,
        name: &str,
        dag: Option<usize>,
        group: Option<&str>,
    ) -> Option<Vec<String>> {
        let call_line = source_line(self.source, call);
        let Some(task_id) = self.name_kwarg(call, &["task_id", "group_id", "dag_id"]) else {
            self.warnings.push(format!(
                "line {call_line}: {name} has no constant task_id; skipped"
            ));
            return None;
        };
        let mut task = TaskInfo {
            task_id,
            operator: name.to_owned(),
            line: call_line,
            ..TaskInfo::default()
        };
        let doc = either(self.kwarg(call, "doc_md"), || self.kwarg(call, "doc"));
        task.doc = doc.as_str().map(|d| text::strip(d).to_owned());
        if let Value::Str(command) = self.kwarg(call, "bash_command") {
            task.dbt.extend(parse_dbt_commands(&command));
            task.bash_command = Some(command);
        }
        if COSMOS_CLASSES.contains(&name) {
            let mut invocation = DbtInvocation {
                command: "build".into(),
                ..DbtInvocation::default()
            };
            if let Some(Expr::Call(render)) = keyword(call, "render_config") {
                invocation.selectors = as_list(&self.kwarg(render, "select"));
                invocation.excludes = as_list(&self.kwarg(render, "exclude"));
            }
            task.dbt.push(invocation);
        } else if name.starts_with("Dbt")
            && let Some(command) = dbt_command_from_class(name)
        {
            let mut selectors = as_list(&self.kwarg(call, "select"));
            selectors.extend(as_list(&self.kwarg(call, "models")));
            task.dbt.push(DbtInvocation {
                command: command.to_owned(),
                selectors,
                excludes: as_list(&self.kwarg(call, "exclude")),
                project_dir: self.kwarg(call, "project_dir").as_str().map(str::to_owned),
                unsupported: Vec::new(),
            });
        }
        for key in SQL_KWARGS {
            self.read_sql(&mut task, &self.kwarg(call, key));
        }
        let configuration = self.kwarg(call, "configuration");
        if let Some(query) = configuration.get("query").filter(|q| q.as_dict().is_some()) {
            self.read_sql(&mut task, query.get("query").unwrap_or(&Value::None));
            if let Some(destination) = query
                .get("destinationTable")
                .filter(|d| d.as_dict().is_some())
            {
                let parts: Vec<&str> = ["projectId", "datasetId", "tableId"]
                    .iter()
                    .filter_map(|key| destination.get(key).and_then(Value::as_str))
                    .collect();
                task.writes.push(parts.join("."));
            }
        }
        for key in WRITE_KWARGS {
            if let Value::Str(value) = self.kwarg(call, key) {
                task.writes.push(value);
            }
        }
        for key in READ_KWARGS {
            if let Value::Str(value) = self.kwarg(call, key) {
                task.reads.push(value);
            }
        }
        task.inlets = assets(&self.kwarg(call, "inlets"));
        task.outlets = assets(&self.kwarg(call, "outlets"));
        Some(self.add_task(task, keyword(call, "dag"), dag, group))
    }

    /// The TaskFlow function a call ends up invoking, and any task id given by `override()`.
    fn taskflow_target(&self, func: &Expr) -> (Option<&'a StmtFunctionDef>, Option<String>) {
        let mut task_id: Option<String> = None;
        let mut func = func;
        loop {
            match func {
                Expr::Name(name) => return (self.taskflow.get(name.id.as_str()).copied(), task_id),
                Expr::Call(call) => {
                    let Expr::Attribute(attribute) = &*call.func else {
                        return (None, None);
                    };
                    if attribute.attr.as_str() == "override" {
                        let given = self.kwarg(call, "task_id");
                        if task_id.as_deref().is_none_or(str::is_empty) {
                            task_id = given.as_str().map(str::to_owned);
                        }
                    } else if attribute.attr.as_str() != "partial" {
                        return (None, None);
                    }
                    func = &attribute.value;
                }
                Expr::Attribute(attribute)
                    if matches!(
                        attribute.attr.as_str(),
                        "expand" | "expand_kwargs" | "override" | "partial"
                    ) =>
                {
                    func = &attribute.value;
                }
                _ => return (None, None),
            }
        }
    }

    fn taskflow_task(
        &mut self,
        function: &'a StmtFunctionDef,
        dag: Option<usize>,
        group: Option<&str>,
        override_id: Option<String>,
    ) -> Vec<String> {
        let mut decorator_call: Option<&ExprCall> = None;
        let mut kind = "python".to_owned();
        for decorator in &function.decorator_list {
            let (target, call) = match &decorator.expression {
                Expr::Call(call) => (&*call.func, Some(call)),
                other => (other, None),
            };
            let name = dotted(target).unwrap_or_default();
            if name == "task" || name.starts_with("task.") {
                kind = name
                    .split_once('.')
                    .map_or("python", |(_, rest)| rest)
                    .to_owned();
                decorator_call = call;
            }
        }
        if let Some(group) = self.group_functions.get(function.name.as_str()) {
            // A @task_group function: its tasks were registered when the function was visited.
            return vec![format!("{GROUP}{group}")];
        }
        let mut task_id = function.name.to_string();
        if let Some(call) = decorator_call
            && let Value::Str(declared) = self.kwarg(call, "task_id")
        {
            task_id = declared;
        }
        if let Some(over) = override_id.filter(|o| !o.is_empty()) {
            task_id = over;
        }
        let mut task = TaskInfo {
            task_id,
            operator: if kind == "python" {
                "@task".into()
            } else {
                format!("@task.{kind}")
            },
            line: definition_line(self.source, function),
            ..TaskInfo::default()
        };
        task.doc = docstring(&function.body);
        if kind == "bash" || kind == "sql" {
            let mut returned = Vec::new();
            walk([Node::Function(function)], |node| {
                if let Node::Stmt(Stmt::Return(statement)) = node
                    && let Some(value) = &statement.value
                {
                    returned.push(self.value(value));
                }
            });
            if kind == "bash" {
                let commands: Vec<&str> = returned.iter().filter_map(Value::as_str).collect();
                if let Some(first) = commands.first() {
                    task.bash_command = Some((*first).to_owned());
                    for command in &commands {
                        task.dbt.extend(parse_dbt_commands(command));
                    }
                }
            } else {
                for value in &returned {
                    self.read_sql(&mut task, value);
                }
            }
        }
        if let Some(call) = decorator_call {
            task.inlets = assets(&self.kwarg(call, "inlets"));
            task.outlets = assets(&self.kwarg(call, "outlets"));
        }
        self.add_task(task, None, dag, group)
    }

    fn read_sql(&mut self, task: &mut TaskInfo, value: &Value) {
        if let Value::List(items) = value {
            for item in items {
                self.read_sql(task, item);
            }
            return;
        }
        let Some(raw) = value.as_str() else { return };
        let text = text::strip(raw);
        if text.is_empty() {
            return;
        }
        let join = |task: &TaskInfo, sql: &str| match task.sql.as_deref() {
            Some(before) if !before.is_empty() => format!("{before};\n{sql}"),
            _ => sql.to_owned(),
        };
        if text.ends_with(".sql") && !text.contains('\n') {
            for base in &self.template_paths {
                let Ok(candidate) = std::fs::canonicalize(base.join(text)) else {
                    continue;
                };
                if !candidate.is_file() {
                    continue;
                }
                match read_text(&candidate) {
                    Ok(sql) => {
                        task.sql = Some(join(task, &sql));
                        task.sql_path = Some(display(&candidate));
                    }
                    Err(error) => self.failure = Some(error),
                }
                return;
            }
            self.warnings
                .push(format!("task {}: SQL file {text} not found", task.task_id));
            return;
        }
        task.sql = Some(join(task, text));
    }
}

/// A resolved path as text, without the `\\?\` prefix Windows gives to a canonical one.
fn display(path: &Path) -> String {
    let text = path.to_string_lossy();
    text.strip_prefix(r"\\?\").unwrap_or(&text).to_owned()
}

fn source_line(source: &Source, call: &ExprCall) -> usize {
    source.line(call.range_start)
}

/// Reads DAGs from Python `text`, as if it were the file at `path`.
pub fn parse_source(path: &str, text: &str) -> Result<Vec<DagInfo>, Error> {
    let source = Source::new(text);
    let path = path.to_owned();
    let file = PathBuf::from(&path);
    let parent = match file.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent.to_path_buf(),
        _ => PathBuf::from("."),
    };
    let name = file
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    with_deep_stack(move |stack| {
        let module = parse(&source, stack)?;
        let mut parser = Parser {
            source: &source,
            path: &path,
            template_paths: vec![parent.clone()],
            parent,
            stem: stem(&name).to_owned(),
            depth: Depth::default(),
            constants: HashMap::new(),
            dags: Vec::new(),
            dag_vars: HashMap::new(),
            task_vars: HashMap::new(),
            taskflow: HashMap::new(),
            group_members: HashMap::new(),
            group_functions: HashMap::new(),
            unowned: Vec::new(),
            warnings: Vec::new(),
            pending: Vec::new(),
            aliases: HashMap::new(),
            failure: None,
        };
        parser.run(&module.body)
    })
}

/// Reads the DAGs of the file at `path`.
pub fn parse_file(path: &str) -> Result<Vec<DagInfo>, Error> {
    let text = read_text(Path::new(path))?;
    parse_source(path, &text)
}
