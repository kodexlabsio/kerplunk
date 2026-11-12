//! Dagster code read as syntax trees and never run: assets, their dependencies, dbt assets, ops,
//! jobs, schedules and sensors.
//!
//! Like the Airflow reader, this one needs no Dagster and runs no user code. It understands the
//! common ways of declaring assets:
//!
//! - `@asset` and `@multi_asset` functions, their keys (`name=`, `key=`, `key_prefix=`,
//!   `AssetKey([...])`), `group_name=`, docstrings and descriptions;
//! - dependencies from function parameters, `ins={"x": AssetIn("key")}` and `deps=[...]` (asset
//!   functions, strings and `AssetKey`s);
//! - `@dbt_assets(manifest=..., select=..., exclude=...)`, which runs a selection of dbt models;
//! - `define_asset_job(name, selection=...)` with string selections and `AssetSelection.all()`,
//!   `.groups(...)` and `.keys(...)`; and `ScheduleDefinition` and
//!   `build_schedule_from_partitioned_job`;
//! - SQL passed as a literal to `.execute(...)`, `.sql(...)` or `.query(...)` inside an asset or
//!   op, which is analysed for lineage like an Airflow SQL task;
//! - op-based jobs: `@op` functions composed in `@graph` and `@job` bodies, including nested
//!   graphs, `.alias(...)`, fan-in lists, dynamic `.map(...)` and `.collect()`, `graph.to_job()`
//!   and graphs listed in `Definitions(jobs=[...])`. Each op invocation becomes a task, named by
//!   its handle as Dagster names it (`op_2` for a second unnamed call, `graph.op` inside a
//!   graph); `dbt.cli([...])` and `subprocess` calls to dbt inside an op are read as dbt runs;
//! - `@schedule` and sensors: `@sensor`, `@asset_sensor`, `@multi_asset_sensor`,
//!   `@run_status_sensor`, `@run_failure_sensor` and `SensorDefinition`, with the jobs they start
//!   and the assets or jobs they watch.

#[cfg(feature = "python")]
pub mod python;

use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::Path;

use ruff_python_ast::{Expr, ExprCall, Keyword, Operator, Parameters, Stmt, StmtFunctionDef};

use crate::airflow::{Error, read_text};
use crate::pysyntax::nodes::{call_name, definition_line, docstring, line, literal};
use crate::pysyntax::text;
use crate::pysyntax::value::Value;
use crate::pysyntax::walk::{Node, Orelse, walk};
use crate::pysyntax::{Depth, Failure, Source, parse, with_deep_stack};

pub const ASSET_DECORATORS: [&str; 4] = [
    "asset",
    "multi_asset",
    "graph_asset",
    "observable_source_asset",
];
pub const SQL_METHODS: [&str; 5] = ["execute", "sql", "query", "run_query", "executescript"];
pub const SENSOR_DECORATORS: [&str; 5] = [
    "sensor",
    "asset_sensor",
    "multi_asset_sensor",
    "run_status_sensor",
    "run_failure_sensor",
];
/// Methods on an op definition that return the same op under the same or a new name.
pub const OP_WRAPPERS: [&str; 5] = [
    "alias",
    "tag",
    "with_hooks",
    "with_retry_policy",
    "configured",
];

/// An asset, or a dbt selection that builds many.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct AssetInfo {
    pub key: String,
    pub function: String,
    pub line: usize,
    pub group: Option<String>,
    pub description: Option<String>,
    pub deps: Vec<String>,
    pub sql: Option<String>,
    pub kinds: Vec<String>,
    pub dbt_select: Option<Vec<String>>,
    pub dbt_exclude: Vec<String>,
}

/// An `@op` definition. It runs once per invocation in a job; see [`OpCall`].
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct OpInfo {
    pub name: String,
    pub function: String,
    pub line: usize,
    pub description: Option<String>,
    pub sql: Option<String>,
    pub kinds: Vec<String>,
    pub dbt_commands: Vec<String>,
}

/// One invocation of an op in a job, by its handle, and the handles it takes inputs from.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct OpCall {
    pub handle: String,
    pub op: String,
    pub upstream: Vec<String>,
}

/// A job: the assets it selects, or the ops it runs.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct JobInfo {
    pub name: String,
    pub line: usize,
    pub selection: Vec<String>,
    pub schedule: Option<String>,
    pub description: Option<String>,
    pub ops: Vec<OpCall>,
}

/// A sensor, the jobs it starts and what it watches.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SensorInfo {
    pub name: String,
    pub kind: String,
    pub line: usize,
    pub jobs: Vec<String>,
    pub assets: Vec<String>,
    pub monitored_jobs: Vec<String>,
    pub status: Option<String>,
    pub description: Option<String>,
}

/// What one file declares.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DagsterFile {
    pub path: String,
    pub assets: Vec<AssetInfo>,
    pub jobs: Vec<JobInfo>,
    pub warnings: Vec<String>,
    pub ops: Vec<OpInfo>,
    pub sensors: Vec<SensorInfo>,
}

/// An asset key as slash-separated text: `["analytics", "orders"]` is `analytics/orders`.
fn key(value: &Value, prefix: &Value) -> String {
    let base = match value {
        Value::Dict(_) if value.marked("__asset__").is_some() => value
            .get("__asset__")
            .map_or_else(String::new, Value::to_python_str),
        Value::List(items) => items
            .iter()
            .filter(|v| !v.is_none())
            .map(Value::to_python_str)
            .collect::<Vec<_>>()
            .join("/"),
        other => other.to_python_str(),
    };
    if !prefix.truthy() {
        return base;
    }
    let mut parts: Vec<String> = match prefix {
        Value::Str(p) => vec![p.clone()],
        Value::List(items) => items.iter().map(Value::to_python_str).collect(),
        Value::Dict(items) => items.iter().map(|(k, _)| k.to_python_str()).collect(),
        _ => Vec::new(),
    };
    parts.push(base);
    parts.join("/")
}

fn plain_key(value: &Value) -> String {
    key(value, &Value::None)
}

/// The items `for item in value or []` goes through: the elements of a list, the keys of a dict,
/// the characters of a string.
fn iterate(value: &Value) -> Vec<Value> {
    match value {
        Value::List(items) => items.clone(),
        Value::Dict(items) => items.iter().map(|(k, _)| k.clone()).collect(),
        Value::Str(s) => s.chars().map(|c| Value::Str(c.to_string())).collect(),
        _ => Vec::new(),
    }
}

/// A job selection as the strings that name its assets.
fn selection_list(selection: &Value) -> Vec<String> {
    match selection {
        Value::None => vec!["*".into()],
        Value::Str(s) => text::split_whitespace(s).map(str::to_owned).collect(),
        Value::List(items) => {
            let mut found = Vec::new();
            for item in items {
                match item {
                    Value::Dict(_) => {
                        if let Some(asset) = item.marked("__asset__") {
                            found.push(asset.to_python_str());
                        }
                    }
                    Value::Str(s) => found.push(s.clone()),
                    Value::List(_) => found.extend(selection_list(item)),
                    _ => {}
                }
            }
            found
        }
        Value::Dict(_) => selection
            .marked("__asset__")
            .map(Value::to_python_str)
            .into_iter()
            .collect(),
        _ => Vec::new(),
    }
}

fn strings(values: impl IntoIterator<Item = String>) -> Value {
    Value::List(values.into_iter().map(Value::Str).collect())
}

fn keyword<'e>(call: &'e ExprCall, name: &str) -> Option<&'e Keyword> {
    call.arguments
        .keywords
        .iter()
        .find(|k| k.arg.as_ref().is_some_and(|a| a.as_str() == name))
}

/// `Parameters.args + kwonlyargs`, the names a function takes (not its positional-only ones).
fn parameter_names(parameters: &Parameters) -> Vec<&str> {
    parameters
        .args
        .iter()
        .chain(&parameters.kwonlyargs)
        .map(|p| p.parameter.name.as_str())
        .collect()
}

/// Whether a parameter's annotation says it is a resource or a context rather than an asset.
fn is_resource(annotation: Option<&Expr>) -> bool {
    let Some(annotation) = annotation else {
        return false;
    };
    let name = match annotation {
        Expr::Subscript(subscript) => call_name(&subscript.value),
        other => call_name(other),
    };
    name.is_some_and(|n| {
        !n.is_empty()
            && (n.ends_with("Resource")
                || n.ends_with("Context")
                || n.ends_with("Config")
                || n == "ResourceParam")
    })
}

/// A graph or job function: the function, its Dagster name and its description.
struct Graph<'a> {
    function: &'a StmtFunctionDef,
    name: String,
    description: Option<String>,
}

/// The state of expanding one graph body into op invocations.
struct Frame {
    env: HashMap<String, Vec<String>>,
    used: HashMap<String, usize>,
    outputs: Vec<String>,
    prefix: String,
    name: String,
}

impl Frame {
    /// Dagster names a repeated invocation without an alias `op_2`, `op_3`, ...
    fn unique(&mut self, handle: &str) -> String {
        let count = self.used.entry(handle.to_owned()).or_insert(0);
        *count += 1;
        if *count == 1 {
            handle.to_owned()
        } else {
            format!("{handle}_{count}")
        }
    }
}

struct Parser<'a> {
    source: &'a Source,
    depth: Depth,
    constants: HashMap<String, Value>,
    /// Assets by the Python name of the function that declares them, as indexes of `result.assets`.
    functions: HashMap<String, usize>,
    multi: HashMap<String, usize>,
    result: DagsterFile,
    schedules: Vec<(String, String)>,
    /// Op definitions by the Python name they are bound to, as indexes of `result.ops`.
    ops: HashMap<String, usize>,
    graphs: HashMap<String, Graph<'a>>,
    /// The `@job` functions in the order they were found, with their lines.
    job_functions: Vec<(String, usize)>,
    expanding: HashSet<String>,
}

impl<'a> Parser<'a> {
    fn name_of(node: &Expr) -> String {
        call_name(node).unwrap_or_default().to_owned()
    }

    fn is_job_function(&self, name: &str) -> bool {
        self.job_functions.iter().any(|(n, _)| n == name)
    }

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
        if let Some(found) = literal(node) {
            return found;
        }
        match node {
            Expr::Name(name) => {
                let id = name.id.as_str();
                if let Some(&asset) = self.functions.get(id) {
                    return Value::marker(
                        "__asset__",
                        Value::str(self.result.assets[asset].key.clone()),
                    );
                }
                if self.is_job_function(id) {
                    return Value::marker("__job__", Value::str(self.graphs[id].name.clone()));
                }
                self.constants.get(id).cloned().unwrap_or(Value::None)
            }
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
                                            Value::None => return Value::None,
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
            Expr::List(list) => Value::List(list.elts.iter().map(|e| self.value(e)).collect()),
            Expr::Tuple(tuple) => Value::List(tuple.elts.iter().map(|e| self.value(e)).collect()),
            Expr::Set(set) => Value::List(set.elts.iter().map(|e| self.value(e)).collect()),
            Expr::Dict(dict) => Value::dict(dict.items.iter().filter_map(|item| {
                item.key
                    .as_ref()
                    .map(|k| (self.value(k), self.value(&item.value)))
            })),
            Expr::Call(call) => self.call_value(node, call),
            Expr::Attribute(attribute) => {
                if let Expr::Name(base) = &*attribute.value
                    && matches!(base.id.as_str(), "DagsterRunStatus" | "RunStatus")
                {
                    return Value::Str(attribute.attr.to_string());
                }
                Value::None
            }
            Expr::BinOp(op) if matches!(op.op, Operator::BitOr | Operator::Add) => {
                match (self.value(&op.left), self.value(&op.right)) {
                    (Value::List(mut left), Value::List(right)) => {
                        left.extend(right);
                        Value::List(left)
                    }
                    (Value::Str(left), Value::Str(right)) if op.op == Operator::Add => {
                        Value::Str(left + &right)
                    }
                    _ => Value::None,
                }
            }
            _ => Value::None,
        }
    }

    fn call_value(&self, node: &Expr, call: &ExprCall) -> Value {
        let args = &call.arguments.args;
        let first = || args.first().map(|a| self.value(a));
        match Self::name_of(node).as_str() {
            "AssetKey" if !args.is_empty() => {
                Value::marker("__asset__", Value::str(plain_key(&self.value(&args[0]))))
            }
            "AssetIn" => {
                let given = first().unwrap_or_else(|| self.kwarg(call, "key"));
                let prefix = self.kwarg(call, "key_prefix");
                let found = if given.is_none() {
                    Value::None
                } else {
                    Value::str(key(&given, &prefix))
                };
                Value::marker("__asset_in__", found)
            }
            "AssetDep" if !args.is_empty() => {
                let inner = self.value(&args[0]);
                if inner.as_dict().is_some() {
                    inner
                } else {
                    Value::marker("__asset__", Value::str(plain_key(&inner)))
                }
            }
            "AssetOut" => Value::dict([
                (Value::str("__asset_out__"), self.kwarg(call, "key")),
                (Value::str("prefix"), self.kwarg(call, "key_prefix")),
                (Value::str("group"), self.kwarg(call, "group_name")),
                (Value::str("description"), self.kwarg(call, "description")),
            ]),
            _ => {
                if let Expr::Attribute(attribute) = &*call.func {
                    if let Expr::Name(base) = &*attribute.value
                        && base.id.as_str() == "AssetSelection"
                    {
                        return self.selection(attribute.attr.as_str(), call);
                    }
                    if matches!(
                        attribute.attr.as_str(),
                        "downstream" | "upstream" | "required_multi_asset_neighbors"
                    ) {
                        return self.value(&attribute.value);
                    }
                }
                Value::None
            }
        }
    }

    fn selection(&self, method: &str, call: &ExprCall) -> Value {
        let values: Vec<Value> = call.arguments.args.iter().map(|a| self.value(a)).collect();
        match method {
            "all" => strings(["*".to_owned()]),
            "groups" => strings(
                values
                    .iter()
                    .filter_map(Value::as_str)
                    .map(|v| format!("group:{v}")),
            ),
            "keys" | "assets" => {
                let mut found = Vec::new();
                for item in &values {
                    if let Some(asset) = item
                        .marked("__asset__")
                        .filter(|_| item.as_dict().is_some())
                    {
                        found.push(asset.to_python_str());
                    } else if matches!(item, Value::Str(_) | Value::List(_)) {
                        found.push(plain_key(item));
                    }
                }
                strings(found)
            }
            "key_prefixes" => strings(
                values
                    .iter()
                    .filter(|v| !v.is_none())
                    .map(|v| format!("prefix:{}", plain_key(v))),
            ),
            _ => Value::List(Vec::new()),
        }
    }

    /// `kinds={...}` and the older `compute_kind=...` of an asset or op decorator.
    fn kinds(&self, call: Option<&ExprCall>) -> Vec<String> {
        let mut found = Vec::new();
        for argument in ["kinds", "compute_kind"] {
            let value = call.map_or(Value::None, |call| self.kwarg(call, argument));
            match &value {
                Value::Str(one) => found.push(one.clone()),
                other if other.truthy() => {
                    found.extend(
                        iterate(other)
                            .iter()
                            .filter_map(Value::as_str)
                            .map(str::to_owned),
                    );
                }
                _ => {}
            }
        }
        found
    }

    fn kwarg(&self, call: &ExprCall, name: &str) -> Value {
        keyword(call, name).map_or(Value::None, |k| self.value(&k.value))
    }

    fn run(&mut self, body: &'a [Stmt]) -> Result<DagsterFile, Error> {
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
        let mut nodes: Vec<Node<'a>> = Vec::new();
        walk(body.iter().map(Node::Stmt), |node| nodes.push(node));
        let functions: Vec<&'a StmtFunctionDef> = nodes
            .iter()
            .filter_map(|n| match n {
                Node::Stmt(Stmt::FunctionDef(f)) => Some(f),
                _ => None,
            })
            .collect();
        for function in &functions {
            self.visit_function(function);
        }
        for function in &functions {
            let name = function.name.as_str();
            if self.functions.contains_key(name) || self.multi.contains_key(name) {
                self.resolve_dependencies(function);
            }
        }
        for (name, line) in self.job_functions.clone() {
            let graph = &self.graphs[&name];
            let (job_name, description) = (graph.name.clone(), graph.description.clone());
            self.op_job(&name, &job_name, line, description);
        }
        let mut assigned: HashSet<*const Expr> = HashSet::new();
        for node in &nodes {
            if let Node::Stmt(Stmt::Assign(assign)) = node
                && let Expr::Call(call) = &*assign.value
            {
                self.visit_call(call, assign.targets.first());
                assigned.insert(&*assign.value as *const Expr);
            }
        }
        for node in &nodes {
            if let Node::Expr(expr @ Expr::Call(call)) = node
                && !assigned.contains(&(*expr as *const Expr))
            {
                self.visit_call(call, None);
            }
        }
        for function in &functions {
            self.visit_scheduler(function);
        }
        let mut last: HashMap<&str, usize> = HashMap::new();
        for (index, job) in self.result.jobs.iter().enumerate() {
            last.insert(&job.name, index);
        }
        let assignments: Vec<(usize, String)> = self
            .schedules
            .iter()
            .filter_map(|(name, cron)| last.get(name.as_str()).map(|&index| (index, cron.clone())))
            .collect();
        for (index, cron) in assignments {
            self.result.jobs[index].schedule = Some(cron);
        }
        if self.depth.exceeded() {
            return Err(Failure::too_deep().into());
        }
        Ok(std::mem::take(&mut self.result))
    }

    fn visit_function(&mut self, function: &'a StmtFunctionDef) {
        let function_line = definition_line(self.source, function);
        for decorator in &function.decorator_list {
            let name = Self::name_of(&decorator.expression);
            let call = match &decorator.expression {
                Expr::Call(call) => Some(call),
                _ => None,
            };
            if name == "dbt_assets" {
                let words = |label: &str| match call.map_or(Value::None, |c| self.kwarg(c, label)) {
                    Value::Str(s) => text::split_whitespace(&s).map(str::to_owned).collect(),
                    _ => Vec::new(),
                };
                let asset = AssetInfo {
                    key: function.name.to_string(),
                    function: function.name.to_string(),
                    line: function_line,
                    description: docstring(&function.body),
                    kinds: vec!["dbt".into()],
                    dbt_select: Some(words("select")),
                    dbt_exclude: words("exclude"),
                    ..AssetInfo::default()
                };
                self.register_asset(function, asset);
                return;
            }
            if name == "op" {
                let op_name = call.map_or(Value::None, |c| self.kwarg(c, "name"));
                let given = call.map_or(Value::None, |c| self.kwarg(c, "description"));
                let description = either(given, || {
                    docstring(&function.body).map_or(Value::None, Value::Str)
                });
                let op = OpInfo {
                    name: op_name
                        .as_str()
                        .map_or_else(|| function.name.to_string(), str::to_owned),
                    function: function.name.to_string(),
                    line: function_line,
                    description: description.as_str().map(|d| text::strip(d).to_owned()),
                    sql: self.sql_in(function),
                    kinds: self.kinds(call),
                    dbt_commands: self.dbt_in(function),
                };
                self.ops
                    .insert(function.name.to_string(), self.result.ops.len());
                self.result.ops.push(op);
                return;
            }
            if name == "graph" || name == "job" {
                let given_name = call.map_or(Value::None, |c| self.kwarg(c, "name"));
                let given = call.map_or(Value::None, |c| self.kwarg(c, "description"));
                let description = either(given, || {
                    docstring(&function.body).map_or(Value::None, Value::Str)
                });
                self.graphs.insert(
                    function.name.to_string(),
                    Graph {
                        function,
                        name: given_name
                            .as_str()
                            .map_or_else(|| function.name.to_string(), str::to_owned),
                        description: description.as_str().map(|d| text::strip(d).to_owned()),
                    },
                );
                if name == "job" {
                    match self
                        .job_functions
                        .iter_mut()
                        .find(|(n, _)| *n == *function.name.as_str())
                    {
                        Some(existing) => existing.1 = function_line,
                        None => self
                            .job_functions
                            .push((function.name.to_string(), function_line)),
                    }
                }
                return;
            }
            if !ASSET_DECORATORS.contains(&name.as_str()) {
                continue;
            }
            if name == "multi_asset"
                && let Some(call) = call
            {
                let outs = either(self.kwarg(call, "outs"), || Value::Dict(Vec::new()));
                if let Value::Dict(outs) = outs {
                    let prefix = self.kwarg(call, "key_prefix");
                    let group = self.kwarg(call, "group_name");
                    let mut first: Option<usize> = None;
                    for (out_name, out) in &outs {
                        let none = Value::None;
                        let spec = |field: &str| {
                            if out.as_dict().is_some() {
                                out.get(field).unwrap_or(&none).clone()
                            } else {
                                Value::None
                            }
                        };
                        let out_key = either(spec("__asset_out__"), || {
                            Value::str(key(out_name, &either(spec("prefix"), || prefix.clone())))
                        });
                        let key = match &out_key {
                            Value::Str(s) if s.contains('/') => s.clone(),
                            other => plain_key(other),
                        };
                        let group = either(spec("group"), || group.clone());
                        let description = either(spec("description"), || {
                            docstring(&function.body).map_or(Value::None, Value::Str)
                        });
                        let asset = AssetInfo {
                            key,
                            function: function.name.to_string(),
                            line: function_line,
                            group: group.as_str().map(str::to_owned),
                            description: description.as_str().map(str::to_owned),
                            ..AssetInfo::default()
                        };
                        self.result.assets.push(asset);
                        first.get_or_insert(self.result.assets.len() - 1);
                    }
                    if let Some(first) = first {
                        // Registered for dependency resolution only; downstream assets name outputs.
                        self.multi.insert(function.name.to_string(), first);
                    }
                    return;
                }
            }
            let mut asset_key = Value::str(function.name.to_string());
            let mut group = Value::None;
            let mut description = docstring(&function.body).map_or(Value::None, Value::Str);
            let mut kinds = Vec::new();
            if let Some(call) = call {
                let explicit = self.kwarg(call, "key");
                let named = self.kwarg(call, "name");
                let prefix = self.kwarg(call, "key_prefix");
                if explicit.as_dict().is_some() && explicit.marked("__asset__").is_some() {
                    asset_key = explicit.get("__asset__").cloned().unwrap_or(Value::None);
                } else if !explicit.is_none() {
                    asset_key = Value::str(plain_key(&explicit));
                } else {
                    let base = either(named, || Value::str(function.name.to_string()));
                    asset_key = Value::str(key(&base, &prefix));
                }
                group = self.kwarg(call, "group_name");
                description = either(self.kwarg(call, "description"), || description.clone());
                kinds = self.kinds(Some(call));
            }
            let asset = AssetInfo {
                key: plain_key(&asset_key),
                function: function.name.to_string(),
                line: function_line,
                group: group.as_str().map(str::to_owned),
                description: description.as_str().map(|d| text::strip(d).to_owned()),
                kinds,
                sql: self.sql_in(function),
                ..AssetInfo::default()
            };
            self.register_asset(function, asset);
            return;
        }
    }

    fn register_asset(&mut self, function: &StmtFunctionDef, asset: AssetInfo) {
        self.functions
            .insert(function.name.to_string(), self.result.assets.len());
        self.result.assets.push(asset);
    }

    fn resolve_dependencies(&mut self, function: &'a StmtFunctionDef) {
        let name = function.name.as_str();
        let own = self
            .functions
            .get(name)
            .or_else(|| self.multi.get(name))
            .copied()
            .unwrap_or(0);
        let asset_key = self.result.assets[own].key.clone();
        let mut deps: Vec<String> = Vec::new();
        let mut ins = Value::Dict(Vec::new());
        for decorator in &function.decorator_list {
            if let Expr::Call(call) = &decorator.expression {
                ins = either(self.kwarg(call, "ins"), || Value::Dict(Vec::new()));
                for item in iterate(&either(
                    self.kwarg(call, "deps"),
                    || Value::List(Vec::new()),
                )) {
                    if item.as_dict().is_some() && item.marked("__asset__").is_some() {
                        deps.extend(item.marked("__asset__").map(Value::to_python_str));
                    } else if matches!(item, Value::Str(_) | Value::List(_)) {
                        deps.push(plain_key(&item));
                    }
                }
            }
        }
        let params = function
            .parameters
            .args
            .iter()
            .chain(&function.parameters.kwonlyargs)
            .map(|p| &p.parameter)
            .filter(|p| {
                !matches!(p.name.as_str(), "context" | "self" | "config")
                    && !is_resource(p.annotation.as_deref())
            });
        for param in params {
            let param = param.name.as_str();
            let spec = ins.get(param);
            if let Some(spec) = spec.filter(|s| s.as_dict().is_some())
                && let Some(found) = spec.marked("__asset_in__")
            {
                deps.push(found.to_python_str());
            } else if !matches!(param, "dbt" | "duckdb" | "database" | "warehouse")
                && !param.ends_with("_resource")
                && !param.ends_with("_client")
            {
                // As Dagster does: a parameter names the upstream asset under this asset's own key prefix.
                let prefix = asset_key
                    .rsplit_once('/')
                    .map_or(Value::None, |(prefix, _)| Value::str(prefix));
                deps.push(key(&Value::str(param), &prefix));
            }
        }
        let mut targets: Vec<usize> = (0..self.result.assets.len())
            .filter(|&i| self.result.assets[i].function == name)
            .collect();
        if targets.is_empty() {
            targets.push(own);
        }
        for index in targets {
            let own_key = self.result.assets[index].key.clone();
            let sorted: BTreeSet<&String> = deps.iter().filter(|d| **d != own_key).collect();
            self.result.assets[index].deps = sorted.into_iter().cloned().collect();
        }
    }

    fn sql_in(&self, function: &StmtFunctionDef) -> Option<String> {
        let mut statements: Vec<String> = Vec::new();
        walk([Node::Function(function)], |node| {
            if let Node::Expr(Expr::Call(call)) = node
                && let Expr::Attribute(attribute) = &*call.func
                && SQL_METHODS.contains(&attribute.attr.as_str())
                && let Some(first) = call.arguments.args.first()
                && let Value::Str(found) = self.value(first)
                && !text::strip(&found).is_empty()
            {
                statements.push(text::strip(&found).trim_end_matches(';').to_owned());
            }
        });
        let joined = statements.join(";\n");
        (!joined.is_empty()).then_some(joined)
    }

    /// The dbt commands an op runs: `dbt.cli(["run", ...])` or `subprocess.run(["dbt", ...])`.
    fn dbt_in(&self, function: &StmtFunctionDef) -> Vec<String> {
        let mut commands = Vec::new();
        walk([Node::Function(function)], |node| {
            let Node::Expr(Expr::Call(call)) = node else {
                return;
            };
            let Some(first) = call.arguments.args.first() else {
                return;
            };
            let words: Vec<String> = match self.value(first) {
                Value::Str(s) => text::split_whitespace(&s).map(str::to_owned).collect(),
                Value::List(items) => {
                    let strs: Vec<String> = items
                        .iter()
                        .filter_map(|v| v.as_str().map(str::to_owned))
                        .collect();
                    if strs.len() != items.len() {
                        return;
                    }
                    strs
                }
                _ => return,
            };
            if words.is_empty() {
                return;
            }
            let method = match &*call.func {
                Expr::Attribute(attribute) => Some(attribute.attr.as_str()),
                other => call_name(other),
            };
            if method == Some("cli") {
                commands.push(format!("dbt {}", words.join(" ")));
            } else if words[0] == "dbt"
                && matches!(
                    method,
                    Some("run" | "call" | "check_call" | "check_output" | "Popen")
                )
            {
                commands.push(words.join(" "));
            }
        });
        commands
    }

    fn op_job(&mut self, function: &str, name: &str, line: usize, description: Option<String>) {
        let mut calls = Vec::new();
        self.expand(function, "", HashMap::new(), &mut calls);
        self.result.jobs.push(JobInfo {
            name: name.to_owned(),
            line,
            description,
            ops: calls,
            ..JobInfo::default()
        });
    }

    /// Expands a graph or job body into op invocations; returns the handles of its outputs.
    fn expand(
        &mut self,
        function: &str,
        prefix: &str,
        inputs: HashMap<String, Vec<String>>,
        calls: &mut Vec<OpCall>,
    ) -> Vec<String> {
        let (def, name) = {
            let graph = &self.graphs[function];
            (graph.function, graph.name.clone())
        };
        if self.expanding.contains(function) {
            let at = definition_line(self.source, def);
            self.result
                .warnings
                .push(format!("line {at}: graph {name} invokes itself; skipped"));
            return Vec::new();
        }
        self.expanding.insert(function.to_owned());
        let mut frame = Frame {
            env: parameter_names(&def.parameters)
                .into_iter()
                .map(|p| (p.to_owned(), inputs.get(p).cloned().unwrap_or_default()))
                .collect(),
            used: HashMap::new(),
            outputs: Vec::new(),
            prefix: prefix.to_owned(),
            name,
        };
        self.run_body(&def.body, &mut frame, calls);
        self.expanding.remove(function);
        let outputs: BTreeSet<String> = frame.outputs.into_iter().collect();
        outputs.into_iter().collect()
    }

    /// The op or graph a call target names, and the alias given to it.
    fn callee(&self, node: &Expr) -> Option<(String, Option<String>)> {
        let mut alias: Option<String> = None;
        let mut node = node;
        loop {
            if let Expr::Name(name) = node
                && (self.ops.contains_key(name.id.as_str())
                    || self.graphs.contains_key(name.id.as_str()))
            {
                return Some((name.id.to_string(), alias));
            }
            if let Expr::Call(call) = node
                && let Expr::Attribute(attribute) = &*call.func
                && OP_WRAPPERS.contains(&attribute.attr.as_str())
            {
                if attribute.attr.as_str() == "alias"
                    && !call.arguments.args.is_empty()
                    && alias.is_none()
                {
                    alias = self
                        .value(&call.arguments.args[0])
                        .as_str()
                        .map(str::to_owned);
                } else if attribute.attr.as_str() == "configured" && alias.is_none() {
                    alias = self.kwarg(call, "name").as_str().map(str::to_owned);
                }
                node = &attribute.value;
                continue;
            }
            return None;
        }
    }

    fn invoke(
        &mut self,
        target: &str,
        alias: Option<String>,
        call: Option<&'a ExprCall>,
        extra: &[String],
        frame: &mut Frame,
        calls: &mut Vec<OpCall>,
    ) -> Vec<String> {
        let (args, keywords): (&'a [Expr], &'a [Keyword]) = call.map_or((&[], &[]), |call| {
            (&call.arguments.args, &call.arguments.keywords)
        });
        let arguments: Vec<Vec<String>> = args
            .iter()
            .map(|a| self.evaluate(a, frame, calls))
            .collect();
        let mut named: Vec<(&str, Vec<String>)> = Vec::new();
        for k in keywords {
            if let Some(arg) = &k.arg {
                let found = self.evaluate(&k.value, frame, calls);
                match named.iter_mut().find(|(n, _)| *n == arg.as_str()) {
                    Some(slot) => slot.1 = found,
                    None => named.push((arg.as_str(), found)),
                }
            }
        }
        let upstream: BTreeSet<&String> = arguments
            .iter()
            .flatten()
            .chain(named.iter().flat_map(|(_, hs)| hs))
            .chain(extra)
            .collect();
        let upstream: Vec<String> = upstream.into_iter().cloned().collect();
        let alias = alias.filter(|a| !a.is_empty());
        if let Some(&op) = self.ops.get(target) {
            let op_name = self.result.ops[op].name.clone();
            let unique = frame.unique(alias.as_deref().unwrap_or(&op_name));
            let handle = format!("{}{unique}", frame.prefix);
            calls.push(OpCall {
                handle: handle.clone(),
                op: op_name,
                upstream,
            });
            return vec![handle];
        }
        let (inner, inner_name) = {
            let graph = &self.graphs[target];
            (graph.function, graph.name.clone())
        };
        let params: Vec<&str> = inner
            .parameters
            .args
            .iter()
            .map(|p| p.parameter.name.as_str())
            .collect();
        let mut mapped: HashMap<String, Vec<String>> = HashMap::new();
        for (index, handles) in arguments.into_iter().enumerate() {
            if index < params.len() {
                mapped.insert(params[index].to_owned(), handles);
            }
        }
        for (name, handles) in named {
            mapped.insert(name.to_owned(), handles);
        }
        let handle = frame.unique(alias.as_deref().unwrap_or(&inner_name));
        let prefix = format!("{}{handle}.", frame.prefix);
        self.expand(target, &prefix, mapped, calls)
    }

    fn evaluate(
        &mut self,
        node: &'a Expr,
        frame: &mut Frame,
        calls: &mut Vec<OpCall>,
    ) -> Vec<String> {
        let Some(_level) = self.depth.enter() else {
            return Vec::new();
        };
        let sorted = |handles: Vec<String>| -> Vec<String> {
            handles
                .into_iter()
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect()
        };
        match node {
            Expr::Name(name) => frame.env.get(name.id.as_str()).cloned().unwrap_or_default(),
            Expr::Call(call) => {
                if let Some((target, alias)) = self.callee(&call.func) {
                    return self.invoke(&target, alias, Some(call), &[], frame, calls);
                }
                if let Expr::Attribute(attribute) = &*call.func
                    && matches!(attribute.attr.as_str(), "map" | "collect")
                {
                    let source = self.evaluate(&attribute.value, frame, calls);
                    if attribute.attr.as_str() == "collect" || call.arguments.args.is_empty() {
                        return source;
                    }
                    let mapper = &call.arguments.args[0];
                    if let Expr::Lambda(lambda) = mapper {
                        if let Some(parameters) = &lambda.parameters {
                            for arg in &parameters.args {
                                frame
                                    .env
                                    .insert(arg.parameter.name.to_string(), source.clone());
                            }
                        }
                        return self.evaluate(&lambda.body, frame, calls);
                    }
                    if let Some((target, alias)) = self.callee(mapper) {
                        return self.invoke(&target, alias, None, &source, frame, calls);
                    }
                    return source;
                }
                let mut found = Vec::new();
                for argument in &call.arguments.args {
                    found.extend(self.evaluate(argument, frame, calls));
                }
                for k in &call.arguments.keywords {
                    found.extend(self.evaluate(&k.value, frame, calls));
                }
                sorted(found)
            }
            Expr::Tuple(tuple) => sorted(
                tuple
                    .elts
                    .iter()
                    .flat_map(|e| self.evaluate(e, frame, calls))
                    .collect(),
            ),
            Expr::List(list) => sorted(
                list.elts
                    .iter()
                    .flat_map(|e| self.evaluate(e, frame, calls))
                    .collect(),
            ),
            Expr::Set(set) => sorted(
                set.elts
                    .iter()
                    .flat_map(|e| self.evaluate(e, frame, calls))
                    .collect(),
            ),
            Expr::Dict(dict) => sorted(
                dict.items
                    .iter()
                    .flat_map(|i| self.evaluate(&i.value, frame, calls))
                    .collect(),
            ),
            Expr::Subscript(e) => self.evaluate(&e.value, frame, calls),
            Expr::Attribute(e) => self.evaluate(&e.value, frame, calls),
            Expr::Starred(e) => self.evaluate(&e.value, frame, calls),
            _ => Vec::new(),
        }
    }

    fn run_body(&mut self, body: &'a [Stmt], frame: &mut Frame, calls: &mut Vec<OpCall>) {
        for statement in body {
            match statement {
                Stmt::Assign(assign) => {
                    let value = self.evaluate(&assign.value, frame, calls);
                    for target in &assign.targets {
                        bind(target, &value, frame);
                    }
                }
                Stmt::AnnAssign(assign) => {
                    if let Some(found) = &assign.value {
                        let value = self.evaluate(found, frame, calls);
                        bind(&assign.target, &value, frame);
                    }
                }
                Stmt::Expr(expression) => {
                    self.evaluate(&expression.value, frame, calls);
                }
                Stmt::Return(statement) => {
                    if let Some(found) = &statement.value {
                        let value = self.evaluate(found, frame, calls);
                        frame.outputs.extend(value);
                    }
                }
                Stmt::For(statement) => {
                    self.loop_warning(line(self.source, statement), frame);
                    self.run_body(&statement.body, frame, calls);
                }
                Stmt::While(statement) => {
                    self.loop_warning(line(self.source, statement), frame);
                    self.run_body(&statement.body, frame, calls);
                }
                Stmt::If(statement) => {
                    self.run_body(&statement.body, frame, calls);
                    self.run_orelse(Orelse::of(&statement.elif_else_clauses), frame, calls);
                }
                Stmt::With(statement) => self.run_body(&statement.body, frame, calls),
                Stmt::Try(statement) if !statement.is_star => {
                    self.run_body(&statement.body, frame, calls);
                    self.run_body(&statement.orelse, frame, calls);
                }
                _ => {}
            }
        }
    }

    fn run_orelse(&mut self, orelse: Orelse<'a>, frame: &mut Frame, calls: &mut Vec<OpCall>) {
        match orelse {
            Orelse::Stmts(body) => self.run_body(body, frame, calls),
            Orelse::Elif(clauses) => {
                if let Some(_level) = self.depth.enter() {
                    self.run_body(&clauses[0].body, frame, calls);
                    self.run_orelse(Orelse::of(&clauses[1..]), frame, calls);
                }
            }
        }
    }

    fn loop_warning(&mut self, line: usize, frame: &Frame) {
        self.result.warnings.push(format!(
            "line {line}: loop in graph {}; its ops are counted once",
            frame.name
        ));
    }

    fn job_names(&self, value: &Value) -> Vec<String> {
        match value {
            Value::Dict(_) => value
                .marked("__job__")
                .map(Value::to_python_str)
                .into_iter()
                .collect(),
            Value::Str(s) => vec![s.clone()],
            Value::List(items) => items.iter().flat_map(|item| self.job_names(item)).collect(),
            _ => Vec::new(),
        }
    }

    fn asset_keys(&self, value: &Value) -> Vec<String> {
        match value {
            Value::Dict(_) => value
                .marked("__asset__")
                .map(Value::to_python_str)
                .into_iter()
                .collect(),
            Value::List(items) => {
                let plain = items
                    .iter()
                    .all(|v| matches!(v, Value::Str(s) if !s.contains('/')));
                if !items.is_empty() && plain {
                    vec![plain_key(value)]
                } else {
                    items
                        .iter()
                        .flat_map(|item| self.asset_keys(item))
                        .collect()
                }
            }
            Value::Str(_) => vec![plain_key(value)],
            _ => Vec::new(),
        }
    }

    /// `@schedule` and sensor decorators, read once every job is known.
    fn visit_scheduler(&mut self, function: &StmtFunctionDef) {
        let function_line = definition_line(self.source, function);
        for decorator in &function.decorator_list {
            let name = Self::name_of(&decorator.expression);
            let call = match &decorator.expression {
                Expr::Call(call) => Some(call),
                _ => None,
            };
            if name == "schedule"
                && let Some(call) = call
            {
                let mut cron = self.kwarg(call, "cron_schedule");
                if cron.is_none()
                    && let Some(first) = call.arguments.args.first()
                {
                    cron = self.value(first);
                }
                let targets: Vec<Value> = ["job", "job_name", "target"]
                    .iter()
                    .map(|k| self.kwarg(call, k))
                    .filter(|v| !v.is_none())
                    .collect();
                for job_name in self.job_names(&Value::List(targets)) {
                    self.schedules
                        .push((job_name, cron.as_str().unwrap_or("custom").to_owned()));
                }
            } else if SENSOR_DECORATORS.contains(&name.as_str()) {
                self.sensor(
                    function.name.as_str(),
                    function_line,
                    &name,
                    call,
                    docstring(&function.body),
                );
            }
        }
    }

    fn sensor(
        &mut self,
        default: &str,
        line: usize,
        kind: &str,
        call: Option<&ExprCall>,
        description: Option<String>,
    ) {
        let get = |names: &[&str]| -> Vec<Value> {
            call.map_or_else(Vec::new, |call| {
                names
                    .iter()
                    .map(|n| self.kwarg(call, n))
                    .filter(|v| !v.is_none())
                    .collect()
            })
        };
        let sensor_name = get(&["name"]);
        let status = get(&["run_status"]);
        let wanted = get(&["description"]);
        let info = SensorInfo {
            name: sensor_name
                .first()
                .and_then(Value::as_str)
                .map_or_else(|| default.to_owned(), str::to_owned),
            kind: kind.to_owned(),
            line,
            jobs: self.job_names(&Value::List(get(&[
                "job",
                "jobs",
                "job_name",
                "request_job",
                "request_jobs",
                "target",
            ]))),
            assets: self.asset_keys(&Value::List(get(&["asset_key", "monitored_assets"]))),
            monitored_jobs: self.job_names(&Value::List(get(&["monitored_jobs"]))),
            status: if kind == "run_failure_sensor" {
                Some("FAILURE".into())
            } else {
                status.first().and_then(Value::as_str).map(str::to_owned)
            },
            description: match wanted.first() {
                Some(first) => first.as_str().map(str::to_owned),
                None => description,
            },
        };
        if !info.jobs.is_empty() || !info.assets.is_empty() || !info.monitored_jobs.is_empty() {
            self.result.sensors.push(info);
        }
    }

    fn visit_call(&mut self, call: &'a ExprCall, target: Option<&Expr>) {
        let call_line = line_of(self.source, call);
        // What `_name` of the call is: the name its function ends in, not looking through a call.
        let expr_name = match &*call.func {
            Expr::Name(name) => name.id.to_string(),
            Expr::Attribute(attribute) => attribute.attr.to_string(),
            _ => String::new(),
        };
        let args = &call.arguments.args;
        if expr_name == "to_job"
            && let Expr::Attribute(attribute) = &*call.func
            && let Expr::Name(graph) = &*attribute.value
            && self.graphs.contains_key(graph.id.as_str())
            && !self.is_job_function(graph.id.as_str())
        {
            let graph = graph.id.to_string();
            let named = self.kwarg(call, "name");
            let job_name = named
                .as_str()
                .map_or_else(|| self.graphs[&graph].name.clone(), str::to_owned);
            let description = either(self.kwarg(call, "description"), || {
                self.graphs[&graph]
                    .description
                    .clone()
                    .map_or(Value::None, Value::Str)
            });
            self.op_job(
                &graph,
                &job_name,
                call_line,
                description.as_str().map(str::to_owned),
            );
            if let Some(Expr::Name(name)) = target {
                self.constants.insert(
                    name.id.to_string(),
                    Value::marker("__job__", Value::Str(job_name)),
                );
            }
            return;
        }
        if expr_name == "Definitions" {
            // Graphs listed as jobs become jobs of the same name.
            let known: HashSet<String> = self.result.jobs.iter().map(|j| j.name.clone()).collect();
            for k in &call.arguments.keywords {
                if k.arg.as_ref().is_some_and(|a| a.as_str() == "jobs") {
                    let items = match &k.value {
                        Expr::List(list) => &list.elts,
                        Expr::Tuple(tuple) => &tuple.elts,
                        _ => continue,
                    };
                    for item in items {
                        if let Expr::Name(name) = item
                            && let Some(graph) = self.graphs.get(name.id.as_str())
                            && !known.contains(&graph.name)
                        {
                            let (function, graph_name, description) = (
                                graph.function,
                                graph.name.clone(),
                                graph.description.clone(),
                            );
                            self.op_job(
                                name.id.as_str(),
                                &graph_name,
                                definition_line(self.source, function),
                                description,
                            );
                        }
                    }
                }
            }
            return;
        }
        if expr_name == "SensorDefinition" {
            let named = self.kwarg(call, "name");
            let sensor_name = named
                .as_str()
                .map_or_else(|| format!("sensor_line_{call_line}"), str::to_owned);
            self.sensor(&sensor_name, call_line, "sensor", Some(call), None);
            return;
        }
        if expr_name == "define_asset_job" {
            let job_name = match args.first() {
                Some(first) => self.value(first),
                None => self.kwarg(call, "name"),
            };
            let Value::Str(job_name) = job_name else {
                self.result.warnings.push(format!(
                    "line {call_line}: asset job name is not a constant; skipped"
                ));
                return;
            };
            let mut selection = self.kwarg(call, "selection");
            if selection.is_none() && args.len() > 1 {
                selection = self.value(&args[1]);
            }
            self.result.jobs.push(JobInfo {
                name: job_name.clone(),
                line: call_line,
                selection: selection_list(&selection),
                description: self.kwarg(call, "description").as_str().map(str::to_owned),
                ..JobInfo::default()
            });
            if let Some(Expr::Name(name)) = target {
                self.constants.insert(
                    name.id.to_string(),
                    Value::marker("__job__", Value::Str(job_name)),
                );
            }
        } else if expr_name == "ScheduleDefinition"
            || expr_name == "build_schedule_from_partitioned_job"
        {
            let job = either(self.kwarg(call, "job"), || {
                either(self.kwarg(call, "target"), || {
                    args.first().map_or(Value::None, |a| self.value(a))
                })
            });
            let cron = self.kwarg(call, "cron_schedule");
            let job_name = if job.as_dict().is_some() {
                job.get("__job__").cloned().unwrap_or(Value::None)
            } else {
                self.kwarg(call, "job_name")
            };
            if let Value::Str(job_name) = job_name {
                self.schedules
                    .push((job_name, cron.as_str().unwrap_or("partitioned").to_owned()));
            }
        }
    }
}

/// `a or b`: `a` when it is true, else `b`.
fn either(first: Value, second: impl FnOnce() -> Value) -> Value {
    if first.truthy() { first } else { second() }
}

fn line_of(source: &Source, call: &ExprCall) -> usize {
    source.line(call.range_start)
}

/// Binds a name, or each name of a tuple or list, to `value`.
fn bind(target: &Expr, value: &[String], frame: &mut Frame) {
    let names: Vec<&Expr> = match target {
        Expr::Tuple(tuple) => tuple.elts.iter().collect(),
        Expr::List(list) => list.elts.iter().collect(),
        other => vec![other],
    };
    for item in names {
        if let Expr::Name(name) = item {
            frame.env.insert(name.id.to_string(), value.to_vec());
        }
    }
}

/// Reads Dagster definitions from Python `text`, as if it were the file at `path`.
pub fn parse_source(path: &str, text: &str) -> Result<DagsterFile, Error> {
    let source = Source::new(text);
    let path = path.to_owned();
    with_deep_stack(move |stack| {
        let module = parse(&source, stack)?;
        let mut parser = Parser {
            source: &source,
            depth: Depth::default(),
            constants: HashMap::new(),
            functions: HashMap::new(),
            multi: HashMap::new(),
            result: DagsterFile {
                path,
                ..DagsterFile::default()
            },
            schedules: Vec::new(),
            ops: HashMap::new(),
            graphs: HashMap::new(),
            job_functions: Vec::new(),
            expanding: HashSet::new(),
        };
        parser.run(&module.body)
    })
}

/// Reads the Dagster definitions of the file at `path`.
pub fn parse_file(path: &str) -> Result<DagsterFile, Error> {
    let text = read_text(Path::new(path))?;
    parse_source(path, &text)
}
