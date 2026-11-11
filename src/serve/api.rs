//! The HTTP API's contract: every operation under `/api/v1/`, its parameters, where it is
//! answered and what it returns. Routing and the OpenAPI 3.1 document both read [`OPERATIONS`],
//! so the served API and its description cannot differ.
//!
//! Every operation calls the one Python function that answers it for the CLI and the MCP server
//! (`konstruct.interfaces.http_api`), holding the GIL only for that call, so there is one
//! implementation of each answer; answers that depend only on the graph are kept until the graph
//! changes.

use serde_json::{Map, Value, json};

/// Where the API's paths start; a new major version gets a new prefix.
pub const PREFIX: &str = "/api/v1";
/// The version of the contract, in the OpenAPI document's `info.version`.
pub const API_VERSION: &str = "1.0.0";
/// Problem types (RFC 9457) are this page's anchors.
pub const PROBLEMS: &str = "https://kodexlabsio.github.io/konstruct/reference/http-api.html#";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Text,
    Integer,
    Boolean,
    /// A parameter given any number of times.
    TextList,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Place {
    /// The last path segment, `{name}`; it may contain `/`.
    Path,
    Query,
}

#[derive(Clone, Copy, Debug)]
pub struct Parameter {
    pub name: &'static str,
    pub place: Place,
    pub kind: Kind,
    pub required: bool,
    pub description: &'static str,
    /// The values allowed, when they are fixed.
    pub choices: &'static [&'static str],
}

const fn query(name: &'static str, kind: Kind, description: &'static str) -> Parameter {
    Parameter {
        name,
        place: Place::Query,
        kind,
        required: false,
        description,
        choices: &[],
    }
}

const fn required(name: &'static str, description: &'static str) -> Parameter {
    Parameter {
        name,
        place: Place::Query,
        kind: Kind::Text,
        required: true,
        description,
        choices: &[],
    }
}

const fn segment(name: &'static str, description: &'static str) -> Parameter {
    Parameter {
        name,
        place: Place::Path,
        kind: Kind::Text,
        required: true,
        description,
        choices: &[],
    }
}

/// How a list is paged: which array of the answer holds its items, and its default page size.
#[derive(Clone, Copy, Debug)]
pub struct Paging {
    pub items: &'static str,
    pub default_limit: usize,
}

/// The most items one page holds.
pub const MAX_LIMIT: usize = 1000;

#[derive(Clone, Copy, Debug)]
pub struct Operation {
    /// The OpenAPI `operationId`, and the name the Python side answers to.
    pub id: &'static str,
    /// The path after [`PREFIX`], with `{name}` for a path parameter at the end.
    pub path: &'static str,
    pub summary: &'static str,
    pub description: &'static str,
    pub parameters: &'static [Parameter],
    /// Whether the answer depends only on the graph and the arguments, so it is kept until the
    /// graph changes; operations that read git or the project's files are answered each time.
    pub cached: bool,
    /// The component schema of a successful answer.
    pub schema: &'static str,
    /// The media types of a successful answer: one for JSON, one for each format of an export.
    pub media: &'static [&'static str],
    pub paging: Option<Paging>,
    pub tag: &'static str,
}

/// Operations are the same when their ids are: the table holds each id once.
impl PartialEq for Operation {
    fn eq(&self, other: &Self) -> bool {
        self.id == other.id
    }
}

impl Eq for Operation {}

const ID: Parameter = segment(
    "id",
    "A node id, such as `model.shop.orders#amount` (percent-encode `#` as `%23`).",
);
const REF: Parameter = segment(
    "ref",
    "A name as the command line takes it (`orders`, `orders.amount`, `dag_id.task_id`) or a node id.",
);
const DEPTH: Parameter = query("depth", Kind::Integer, "Stop after this many steps.");
const LIMIT: Parameter = query("limit", Kind::Integer, "The most items in one page.");
const CURSOR: Parameter = query(
    "cursor",
    Kind::Text,
    "The `next_cursor` of the previous page.",
);
const KIND: Parameter = query(
    "kind",
    Kind::TextList,
    "Only nodes of this kind; repeat for several.",
);

const LISTED: Paging = Paging {
    items: "items",
    default_limit: 100,
};

const JSON: &[&str] = &["application/json"];

/// What `/export/{format}` answers for each format, in the order of its `format` choices; the
/// Python side sends these (`konstruct.interfaces.http_api.EXPORT_MEDIA`).
pub const EXPORT_MEDIA: &[&str] = &[
    "application/x-ndjson",
    "text/plain; charset=utf-8",
    "text/vnd.graphviz; charset=utf-8",
    "text/csv; charset=utf-8",
    "application/yaml",
];

/// Every operation of the API, in the order the document lists them.
pub const OPERATIONS: &[Operation] = &[
    Operation {
        id: "getNode",
        path: "/nodes/{id}",
        summary: "One node, as graph.json holds it",
        description: "The node's entry in graph.json, its label, and the edges into and out of it in the graph's order.",
        parameters: &[ID],
        cached: true,
        schema: "NodeEntry",
        media: JSON,
        paging: None,
        tag: "graph",
    },
    Operation {
        id: "listNodes",
        path: "/nodes",
        summary: "Nodes by kind",
        description: "Every node of the kinds given (by default tables, models, sources, seeds and snapshots), in label order.",
        parameters: &[KIND, LIMIT, CURSOR],
        cached: true,
        schema: "Listing",
        media: JSON,
        paging: Some(LISTED),
        tag: "graph",
    },
    Operation {
        id: "resolve",
        path: "/resolve",
        summary: "The nodes a name refers to",
        description: "Resolve a name as the command line does: a table, `table.column`, a dbt source as `source.table`, a task as `dag.task`, or a node id. Several nodes come back when the name is ambiguous.",
        parameters: &[required("name", "The name to resolve.")],
        cached: true,
        schema: "Resolution",
        media: JSON,
        paging: None,
        tag: "graph",
    },
    Operation {
        id: "impact",
        path: "/impact/{id}",
        summary: "Everything downstream of a node",
        description: "What a change to the node affects: derived columns, relations, the relations that join, filter or group on it, tests, exposures, tasks, business terms and entities.",
        parameters: &[ID, DEPTH],
        cached: true,
        schema: "Walk",
        media: JSON,
        paging: None,
        tag: "lineage",
    },
    Operation {
        id: "trace",
        path: "/trace/{id}",
        summary: "Everything upstream of a node",
        description: "Where the node's data comes from, each column step marked direct or transformed, with the columns that filter or join its relation and the tasks that build it.",
        parameters: &[ID, DEPTH],
        cached: true,
        schema: "Walk",
        media: JSON,
        paging: None,
        tag: "lineage",
    },
    Operation {
        id: "path",
        path: "/path",
        summary: "The shortest connection between two nodes",
        description: "Along data flow downstream if there is a path that way, else upstream, else through containment and task order.",
        parameters: &[
            required("from", "The node id it starts at."),
            required("to", "The node id it ends at."),
        ],
        cached: true,
        schema: "PathResult",
        media: JSON,
        paging: None,
        tag: "lineage",
    },
    Operation {
        id: "search",
        path: "/search",
        summary: "Nodes ranked by the words of a query",
        description: "Words in a name count for more than words in a description, rarer words for more than common ones, and business terms rank above columns with the same words.",
        parameters: &[required("q", "The words to look for."), KIND, LIMIT, CURSOR],
        cached: true,
        schema: "SearchResult",
        media: JSON,
        paging: Some(Paging {
            items: "results",
            default_limit: 20,
        }),
        tag: "graph",
    },
    Operation {
        id: "describe",
        path: "/describe/{ref}",
        summary: "One node in detail",
        description: "For a relation, its columns, tests, conditions and neighbours; for a column, its expression and terms; for a task, what it runs; for a term, its definition and columns.",
        parameters: &[REF],
        cached: true,
        schema: "Description",
        media: JSON,
        paging: None,
        tag: "graph",
    },
    Operation {
        id: "explain",
        path: "/explain/{ref}",
        summary: "How a column is calculated",
        description: "The column's expression with CTEs and aliases inlined, the formula composed down to source columns, and each column on the way with the joins, filters and grouping of its relation.",
        parameters: &[REF],
        cached: true,
        schema: "Explanation",
        media: JSON,
        paging: None,
        tag: "lineage",
    },
    Operation {
        id: "define",
        path: "/define",
        summary: "A business term, or the terms bound to a column",
        description: "By term name or synonym, or by column or relation for the terms that define it; approved terms first.",
        parameters: &[required("ref", "A term, a synonym or a column.")],
        cached: true,
        schema: "Definitions",
        media: JSON,
        paging: None,
        tag: "meaning",
    },
    Operation {
        id: "listTerms",
        path: "/terms",
        summary: "Every business term",
        description: "The business terms in the graph, from definitions files, vocabularies and ontology packs, in label order.",
        parameters: &[LIMIT, CURSOR],
        cached: true,
        schema: "Listing",
        media: JSON,
        paging: Some(LISTED),
        tag: "meaning",
    },
    Operation {
        id: "ontology",
        path: "/ontology",
        summary: "The ontology derived from the graph",
        description: "Entities with their keys, measures with their composed formulas, relationships and the rules that are broken.",
        parameters: &[],
        cached: true,
        schema: "Ontology",
        media: JSON,
        paging: None,
        tag: "meaning",
    },
    Operation {
        id: "conflicts",
        path: "/conflicts",
        summary: "Columns that compute one business idea in different ways",
        description: "Pairs of columns that mean the same by name, synonym or term but differ in sources, filters, joins or aggregation, ranked by what depends on them.",
        parameters: &[LIMIT, CURSOR],
        cached: true,
        schema: "Conflicts",
        media: JSON,
        paging: Some(Paging {
            items: "conflicts",
            default_limit: 20,
        }),
        tag: "meaning",
    },
    Operation {
        id: "query",
        path: "/query",
        summary: "A question in words",
        description: "The nodes the question's words match, with their connections, and impact or lineage when it asks for them.",
        parameters: &[
            required("q", "The question."),
            query(
                "depth",
                Kind::Integer,
                "Steps of connections around each match (default 1).",
            ),
        ],
        cached: true,
        schema: "QueryResult",
        media: JSON,
        paging: None,
        tag: "graph",
    },
    Operation {
        id: "history",
        path: "/history/{ref}",
        summary: "When a column's formula changed",
        description: "From the git history of the files that define the column and its terms; read each time, as commits do not change the graph.",
        parameters: &[
            REF,
            query(
                "limit",
                Kind::Integer,
                "The most commits to read for each file (default 50).",
            ),
        ],
        cached: false,
        schema: "History",
        media: JSON,
        paging: None,
        tag: "change",
    },
    Operation {
        id: "diff",
        path: "/diff",
        summary: "What a change does, column by column",
        description: "The working tree (or `head`) compared with `base` (default: the merge base with main), read from git each time.",
        parameters: &[
            query("base", Kind::Text, "A commit, or a date, to compare with."),
            query(
                "head",
                Kind::Text,
                "A commit, or a date, to compare (default: the working tree).",
            ),
        ],
        cached: false,
        schema: "Diff",
        media: JSON,
        paging: None,
        tag: "change",
    },
    Operation {
        id: "gate",
        path: "/gate",
        summary: "The gate's findings for the change against a base",
        description: "As `konstruct gate --no-update`: the graph as served, compared with `base`, and what stops the change.",
        parameters: &[query(
            "base",
            Kind::Text,
            "A commit, or a date, to compare with.",
        )],
        cached: false,
        schema: "Gate",
        media: JSON,
        paging: None,
        tag: "change",
    },
    Operation {
        id: "status",
        path: "/status",
        summary: "What Konstruct finds in the project",
        description: "Configuration, dbt projects, warehouses configured, DAG, SQL and definitions files, and whether the graph is older than its inputs; read each time.",
        parameters: &[],
        cached: false,
        schema: "Status",
        media: JSON,
        paging: None,
        tag: "project",
    },
    Operation {
        id: "report",
        path: "/report",
        summary: "The report's summary",
        description: "What REPORT.md summarises: node and edge counts, lineage coverage, the most used columns, orphans, untested models, columns with no lineage and warnings.",
        parameters: &[],
        cached: true,
        schema: "Report",
        media: JSON,
        paging: None,
        tag: "project",
    },
    Operation {
        id: "export",
        path: "/export/{format}",
        summary: "The graph in another tool's format",
        description: "OpenLineage events (JSON lines), Mermaid, Graphviz DOT, CSV, or the ontology as an Apache Ossie model.",
        parameters: &[
            Parameter {
                name: "format",
                place: Place::Path,
                kind: Kind::Text,
                required: true,
                description: "The format.",
                choices: &["openlineage", "mermaid", "dot", "csv", "ossie"],
            },
            query(
                "select",
                Kind::Text,
                "Only the lineage of this table or column, both directions (not for ossie).",
            ),
            query(
                "columns",
                Kind::Boolean,
                "For mermaid and dot, draw columns and column lineage.",
            ),
            query(
                "namespace",
                Kind::Text,
                "For openlineage, the dataset namespace.",
            ),
        ],
        cached: true,
        schema: "Export",
        media: EXPORT_MEDIA,
        paging: None,
        tag: "project",
    },
];

/// A parameter's value, checked against its kind.
#[derive(Clone, Debug, PartialEq)]
pub enum Argument {
    Text(String),
    Integer(i64),
    Boolean(bool),
    TextList(Vec<String>),
}

impl Argument {
    pub fn to_json(&self) -> Value {
        match self {
            Self::Text(text) => Value::from(text.as_str()),
            Self::Integer(n) => Value::from(*n),
            Self::Boolean(b) => Value::from(*b),
            Self::TextList(items) => Value::from(items.clone()),
        }
    }
}

/// The operation a path names, with the value of its path parameter (still percent-encoded).
pub fn find(path: &str) -> Option<(&'static Operation, Option<&str>)> {
    let rest = path.strip_prefix(PREFIX)?;
    for operation in OPERATIONS {
        match operation.path.split_once('{') {
            None if operation.path == rest => return Some((operation, None)),
            Some((prefix, _)) => {
                if let Some(value) = rest.strip_prefix(prefix)
                    && !value.is_empty()
                {
                    return Some((operation, Some(value)));
                }
            }
            None => {}
        }
    }
    None
}

/// The parameters whose values reach git as revisions (`base`, `head`) or name a node (`ref`,
/// which `history` looks up in git). None may start with `-`, which git would read as an option
/// such as `--output=<file>`; no branch, tag, revision or node name starts with one.
pub const NOT_OPTIONS: &[&str] = &["base", "head", "ref"];

/// The operation's arguments from its path value and query pairs, or why they are wrong.
pub fn arguments(
    operation: &Operation,
    path_value: Option<String>,
    pairs: &[(String, String)],
) -> Result<Vec<(&'static str, Argument)>, String> {
    let mut found = Vec::new();
    for parameter in operation.parameters {
        let argument = match (parameter.place, parameter.kind) {
            (Place::Path, _) => path_value.clone().map(Argument::Text),
            (Place::Query, Kind::TextList) => {
                let values: Vec<String> = pairs
                    .iter()
                    .filter(|(key, value)| key == parameter.name && !value.is_empty())
                    .map(|(_, value)| value.clone())
                    .collect();
                (!values.is_empty()).then_some(Argument::TextList(values))
            }
            (Place::Query, kind) => match pairs.iter().find(|(key, _)| key == parameter.name) {
                None => None,
                Some((_, value)) if value.is_empty() && kind != Kind::Text => None,
                Some((_, value)) => Some(match kind {
                    Kind::Integer => Argument::Integer(
                        value
                            .parse()
                            .map_err(|_| format!("{} must be a whole number", parameter.name))?,
                    ),
                    Kind::Boolean => Argument::Boolean(match value.as_str() {
                        "true" | "1" | "yes" => true,
                        "false" | "0" | "no" => false,
                        _ => return Err(format!("{} must be true or false", parameter.name)),
                    }),
                    _ => Argument::Text(value.clone()),
                }),
            },
        };
        match argument {
            Some(Argument::Text(text))
                if NOT_OPTIONS.contains(&parameter.name) && text.starts_with('-') =>
            {
                return Err(format!("{} must not start with -", parameter.name));
            }
            Some(Argument::Text(text))
                if !parameter.choices.is_empty() && !parameter.choices.contains(&text.as_str()) =>
            {
                return Err(format!(
                    "{} must be one of {}",
                    parameter.name,
                    parameter.choices.join(", ")
                ));
            }
            Some(argument) => found.push((parameter.name, argument)),
            None if parameter.required => {
                return Err(format!("{} is required", parameter.name));
            }
            None => {}
        }
    }
    Ok(found)
}

/// A page of a list: where it starts and how long it is, from `cursor` and `limit`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Page {
    pub offset: usize,
    pub limit: usize,
}

/// The page asked for, and the version of the graph its cursor was made for. The version is
/// checked once the answer is in, against the graph the answer came from, so a list is never
/// paged across two versions.
pub fn page(
    paging: &Paging,
    arguments: &[(&str, Argument)],
) -> Result<(Page, Option<String>), String> {
    let mut page = Page {
        offset: 0,
        limit: paging.default_limit,
    };
    let mut made_for = None;
    for (name, argument) in arguments {
        match (*name, argument) {
            ("limit", Argument::Integer(limit)) => {
                if !(0..=MAX_LIMIT as i64).contains(limit) {
                    return Err(format!("limit must be from 0 to {MAX_LIMIT}"));
                }
                page.limit = *limit as usize;
            }
            ("cursor", Argument::Text(cursor)) if !cursor.is_empty() => {
                let not_ours = || "the cursor is not one this server made".to_owned();
                let (version, offset) = cursor.split_once('.').ok_or_else(not_ours)?;
                page.offset = offset.parse().map_err(|_| not_ours())?;
                made_for = Some(version.to_owned());
            }
            _ => {}
        }
    }
    Ok((page, made_for))
}

/// The cursor of the page after `page`, when `total` items leave one.
pub fn next_cursor(page: Page, total: usize, version: &str) -> Option<String> {
    let next = page.offset + page.limit;
    (page.limit > 0 && next < total).then(|| format!("{version}.{next}"))
}

/// Cut the list `paging.items` of an answer down to `page`, and add `next_cursor`.
pub fn paged(answer: &Value, paging: &Paging, page: Page, version: &str) -> Value {
    let mut answer = answer.clone();
    let items = answer
        .get(paging.items)
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let total = items.len();
    let kept: Vec<Value> = items
        .into_iter()
        .skip(page.offset)
        .take(page.limit)
        .collect();
    if let Some(object) = answer.as_object_mut() {
        object.insert(paging.items.to_owned(), Value::from(kept));
        object.insert(
            "next_cursor".to_owned(),
            next_cursor(page, total, version).map_or(Value::Null, Value::from),
        );
    }
    answer
}

/// The problem types the API answers with, each with its HTTP status and title.
pub const PROBLEM_TYPES: &[(&str, u16, &str)] = &[
    ("bad-request", 400, "The request is malformed"),
    (
        "invalid-parameter",
        400,
        "A parameter is missing or not valid",
    ),
    ("unauthorised", 401, "A token is required"),
    (
        "host-not-allowed",
        403,
        "The Host header does not name this server",
    ),
    ("cross-site", 403, "Requests from other sites are refused"),
    ("not-found", 404, "No such route"),
    (
        "no-such-node",
        404,
        "Nothing in the graph has this id or name",
    ),
    ("method-not-allowed", 405, "Only GET and HEAD are allowed"),
    (
        "stale-cursor",
        409,
        "The graph changed since this cursor was made",
    ),
    (
        "not-answerable",
        422,
        "The question cannot be answered from this graph",
    ),
    ("internal-error", 500, "The answer failed"),
    ("busy", 503, "The server is answering too many questions"),
    ("too-many-streams", 503, "Too many event streams are open"),
];

/// The status and title of a problem type.
pub fn problem_type(slug: &str) -> (u16, &'static str) {
    PROBLEM_TYPES
        .iter()
        .find(|(name, _, _)| *name == slug)
        .map_or((500, "The answer failed"), |(_, status, title)| {
            (*status, *title)
        })
}

/// An RFC 9457 problem details object.
pub fn problem(slug: &str, detail: &str) -> (u16, Value) {
    let (status, title) = problem_type(slug);
    let slug = if PROBLEM_TYPES.iter().any(|(name, _, _)| *name == slug) {
        slug
    } else {
        "internal-error"
    };
    (
        status,
        json!({
            "type": format!("{PROBLEMS}{slug}"),
            "title": title,
            "status": status,
            "detail": detail,
        }),
    )
}

fn reference(schema: &str) -> Value {
    json!({ "$ref": format!("#/components/schemas/{schema}") })
}

fn object(required: &[&str], properties: Value) -> Value {
    json!({ "type": "object", "required": required, "properties": properties })
}

fn list_of(schema: &str) -> Value {
    json!({ "type": "array", "items": reference(schema) })
}

fn strings() -> Value {
    json!({ "type": "array", "items": { "type": "string" } })
}

fn objects() -> Value {
    json!({ "type": "array", "items": { "type": "object" } })
}

fn nullable(kind: &str) -> Value {
    json!({ "type": [kind, "null"] })
}

/// The schemas of the answers. Python's answers are the `to_dict()` of the result classes in
/// `konstruct.analysis` and `konstruct.semantics`, documented in the Python API reference; their
/// top-level fields are fixed here, and the API's tests check real answers against them.
fn schemas() -> Map<String, Value> {
    let text = json!({ "type": "string" });
    let integer = json!({ "type": "integer" });
    let boolean = json!({ "type": "boolean" });
    let entries = [
        (
            "Item",
            object(
                &["id", "kind", "label", "name"],
                json!({
                    "id": text, "kind": text, "label": text, "name": text,
                    "relation_kind": text, "path": text, "data_type": text, "description": text,
                    "depth": integer, "via": text, "transform": {}, "verified": {}, "role": {},
                    "command": {}, "through": text,
                    "direction": { "enum": ["forward", "backward"] },
                    "score": { "type": "number" }, "status": {},
                }),
            ),
        ),
        (
            "Walk",
            object(
                &[
                    "direction",
                    "target",
                    "columns",
                    "relations",
                    "joins_and_filters",
                    "filtered_by",
                    "tests",
                    "exposures",
                    "tasks",
                    "terms",
                    "entities",
                    "total",
                    "summary",
                ],
                json!({
                    "direction": { "enum": ["downstream", "upstream"] },
                    "target": reference("Item"),
                    "columns": list_of("Item"), "relations": list_of("Item"),
                    "joins_and_filters": list_of("Item"), "filtered_by": list_of("Item"),
                    "tests": list_of("Item"), "exposures": list_of("Item"),
                    "tasks": list_of("Item"), "terms": list_of("Item"),
                    "entities": { "type": "object", "additionalProperties": strings() },
                    "total": integer, "summary": text,
                }),
            ),
        ),
        (
            "PathResult",
            object(
                &["from", "to", "found", "steps"],
                json!({
                    "from": reference("Item"), "to": reference("Item"), "found": boolean,
                    "steps": list_of("Item"),
                }),
            ),
        ),
        (
            "SearchResult",
            object(
                &["query", "results", "next_cursor"],
                json!({ "query": text, "results": list_of("Item"), "next_cursor": nullable("string") }),
            ),
        ),
        (
            "NodeEntry",
            object(
                &["node", "label", "incoming", "outgoing"],
                json!({
                    "node": object(&["id", "kind", "name"], json!({ "id": text, "kind": text, "name": text })),
                    "label": text,
                    "incoming": list_of("Edge"), "outgoing": list_of("Edge"),
                }),
            ),
        ),
        (
            "Edge",
            object(
                &["source", "target", "kind"],
                json!({ "source": text, "target": text, "kind": text, "attrs": { "type": "object" } }),
            ),
        ),
        (
            "Listing",
            object(
                &["kinds", "items", "next_cursor"],
                json!({ "kinds": strings(), "items": list_of("Item"), "next_cursor": nullable("string") }),
            ),
        ),
        (
            "Resolution",
            object(
                &["name", "nodes"],
                json!({ "name": text, "nodes": list_of("Item") }),
            ),
        ),
        (
            "Description",
            object(
                &[
                    "id",
                    "kind",
                    "label",
                    "name",
                    "origins",
                    "attrs",
                    "upstream",
                    "downstream",
                ],
                json!({
                    "id": text, "kind": text, "label": text, "name": text, "origins": strings(),
                    "attrs": { "type": "object" }, "columns": list_of("Item"),
                    "upstream": strings(), "downstream": strings(), "tests": strings(),
                    "terms": strings(), "conditions": strings(),
                }),
            ),
        ),
        (
            "Explanation",
            object(
                &["column", "label", "complete", "steps"],
                json!({
                    "column": text, "label": text, "expression": nullable("string"),
                    "composed": nullable("string"), "complete": boolean, "steps": objects(),
                }),
            ),
        ),
        (
            "Definitions",
            object(
                &["query", "terms"],
                json!({ "query": text, "terms": objects() }),
            ),
        ),
        (
            "Ontology",
            object(
                &["entities", "measures", "relationships", "rules"],
                json!({
                    "entities": objects(), "measures": objects(), "relationships": objects(),
                    "rules": objects(),
                }),
            ),
        ),
        (
            "Conflicts",
            object(
                &["conflicts", "next_cursor"],
                json!({ "conflicts": objects(), "next_cursor": nullable("string") }),
            ),
        ),
        (
            "QueryResult",
            object(
                &["question", "matches", "edges", "neighbours"],
                json!({
                    "question": text, "matches": list_of("Item"), "edges": objects(),
                    "neighbours": list_of("Item"), "impact": reference("Walk"),
                    "lineage": reference("Walk"), "definitions": objects(),
                }),
            ),
        ),
        (
            "History",
            object(
                &["column", "formulas", "terms"],
                json!({ "column": text, "formulas": objects(), "terms": objects() }),
            ),
        ),
        (
            "Diff",
            object(
                &["base", "relations", "breaking"],
                json!({ "base": text, "relations": objects(), "breaking": objects() }),
            ),
        ),
        (
            "Gate",
            object(
                &["passed", "findings"],
                json!({ "passed": boolean, "findings": objects() }),
            ),
        ),
        (
            "Status",
            object(
                &["root", "config_file", "dbt_projects", "warehouses", "graph"],
                json!({
                    "root": text, "config_file": nullable("string"), "dbt_projects": objects(),
                    "warehouses": objects(), "graph": { "type": "object" },
                }),
            ),
        ),
        (
            "Report",
            object(
                &[
                    "nodes",
                    "edges",
                    "lineage",
                    "hubs",
                    "orphans",
                    "untested",
                    "no_lineage",
                    "documented_missing",
                    "warnings",
                ],
                json!({
                    "nodes": { "type": "object", "additionalProperties": integer },
                    "edges": { "type": "object", "additionalProperties": integer },
                    "lineage": object(&["covered", "columns"], json!({ "covered": integer, "columns": integer })),
                    "hubs": objects(), "orphans": list_of("Item"), "untested": list_of("Item"),
                    "no_lineage": list_of("Item"), "documented_missing": list_of("Item"),
                    "warnings": strings(),
                }),
            ),
        ),
        ("Export", json!({ "type": "string" })),
        (
            "Problem",
            object(
                &["type", "title", "status", "detail"],
                json!({
                    "type": { "type": "string", "format": "uri" }, "title": text,
                    "status": integer, "detail": text,
                }),
            ),
        ),
    ];
    entries
        .into_iter()
        .map(|(name, schema)| (name.to_owned(), schema))
        .collect()
}

fn parameter_schema(parameter: &Parameter) -> Value {
    let base = match parameter.kind {
        Kind::Text => json!({ "type": "string" }),
        Kind::Integer => json!({ "type": "integer" }),
        Kind::Boolean => json!({ "type": "boolean" }),
        Kind::TextList => json!({ "type": "array", "items": { "type": "string" } }),
    };
    if parameter.choices.is_empty() {
        base
    } else {
        json!({ "type": "string", "enum": parameter.choices })
    }
}

/// What any route can answer besides its own responses.
const DEFAULT_PROBLEMS: &str = "Any other problem: bad-request (400), unauthorised (401), \
     host-not-allowed or cross-site (403), method-not-allowed (405), internal-error (500), busy or \
     too-many-streams (503)";

fn problem_response(description: &str) -> Value {
    json!({
        "description": description,
        "content": { "application/problem+json": { "schema": reference("Problem") } },
    })
}

/// The OpenAPI 3.1 document of the API, from [`OPERATIONS`]. Its maps are sorted and it holds no
/// time or machine, so the same build always writes the same document.
pub fn openapi() -> Value {
    let mut paths = Map::new();
    for operation in OPERATIONS {
        let parameters: Vec<Value> = operation
            .parameters
            .iter()
            .map(|parameter| {
                let mut entry = json!({
                    "name": parameter.name,
                    "in": match parameter.place { Place::Path => "path", Place::Query => "query" },
                    "required": parameter.required,
                    "description": parameter.description,
                    "schema": parameter_schema(parameter),
                });
                if parameter.kind == Kind::TextList {
                    entry["style"] = json!("form");
                    entry["explode"] = json!(true);
                }
                entry
            })
            .collect();
        let content: Map<String, Value> = operation
            .media
            .iter()
            .map(|media| {
                (
                    (*media).to_owned(),
                    json!({ "schema": reference(operation.schema) }),
                )
            })
            .collect();
        let mut responses = json!({
            "200": { "description": operation.summary, "content": content },
            "400": problem_response("A parameter is missing or not valid"),
            "422": problem_response("The question cannot be answered from this graph"),
            "default": problem_response(DEFAULT_PROBLEMS),
        });
        if operation.paging.is_some() {
            responses["409"] = problem_response("The graph changed since the cursor was made");
        }
        // Every name of a node is looked up: a path segment, a path's ends, a term or column.
        if operation
            .parameters
            .iter()
            .any(|p| p.place == Place::Path || matches!(p.name, "from" | "to" | "ref"))
        {
            responses["404"] = problem_response("No node has this id, or nothing matches the name");
        }
        paths.insert(
            format!("{PREFIX}{}", operation.path),
            json!({
                "get": {
                    "operationId": operation.id,
                    "summary": operation.summary,
                    "description": operation.description,
                    "tags": [operation.tag],
                    "parameters": parameters,
                    "responses": responses,
                    "x-konstruct-cached": operation.cached,
                }
            }),
        );
    }
    paths.insert(
        format!("{PREFIX}/openapi.json"),
        json!({
            "get": {
                "operationId": "openapi",
                "summary": "This document",
                "description": "The OpenAPI 3.1 document of the API.",
                "tags": ["project"],
                "responses": {
                    "200": {
                        "description": "This document",
                        "content": { "application/json": { "schema": { "type": "object" } } },
                    },
                    "default": problem_response(DEFAULT_PROBLEMS),
                },
                "x-konstruct-cached": true,
            }
        }),
    );
    let problems: Vec<Value> = PROBLEM_TYPES
        .iter()
        .map(|(slug, status, title)| json!({ "type": format!("{PROBLEMS}{slug}"), "status": status, "title": title }))
        .collect();
    json!({
        "openapi": "3.1.0",
        "info": {
            "title": "Konstruct HTTP API",
            "version": API_VERSION,
            "description": "Read-only answers about a Konstruct graph, from `konstruct serve`. Nodes are named by id in the routes that take `{id}`, `from` and `to`, and by any name the command line takes in the others. Errors are RFC 9457 problem details; the problem types are listed in `x-konstruct-problems`. When the server listens beyond this machine, every request needs `Authorization: Bearer <token>`.",
            "license": { "name": "MIT", "identifier": "MIT" },
        },
        "servers": [{ "url": "http://127.0.0.1:8000", "description": "konstruct serve, by default" }],
        "security": [{}, { "bearer": [] }],
        "tags": [
            { "name": "graph", "description": "Nodes, names and search" },
            { "name": "lineage", "description": "Impact, trace, paths and formulas" },
            { "name": "meaning", "description": "Business terms, the ontology and conflicts" },
            { "name": "change", "description": "Changes in git" },
            { "name": "project", "description": "The project, its report and exports" },
        ],
        "paths": paths,
        "components": {
            "schemas": schemas(),
            "securitySchemes": { "bearer": { "type": "http", "scheme": "bearer" } },
        },
        "x-konstruct-problems": problems,
    })
}

/// [`openapi`] as the text that is served and committed: two-space indents, sorted keys and a
/// final newline.
pub fn openapi_text() -> String {
    let mut text = serde_json::to_string_pretty(&openapi()).unwrap_or_default();
    text.push('\n');
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_find_their_operation() {
        let (operation, value) = find("/api/v1/impact/model.a%23b").unwrap();
        assert_eq!((operation.id, value), ("impact", Some("model.a%23b")));
        assert_eq!(find("/api/v1/nodes").unwrap().0.id, "listNodes");
        assert_eq!(
            find("/api/v1/nodes/x/y").unwrap(),
            (&OPERATIONS[0], Some("x/y"))
        );
        assert_eq!(find("/api/v1/export/csv").unwrap().0.id, "export");
        for missing in [
            "/api/v1/nodes/",
            "/api/v1/impact",
            "/api/v2/nodes",
            "/api/v1",
            "/api/v1/searching",
        ] {
            assert!(find(missing).is_none(), "{missing}");
        }
    }

    #[test]
    fn operation_ids_and_paths_are_unique() {
        let mut ids: Vec<&str> = OPERATIONS.iter().map(|o| o.id).collect();
        let mut paths: Vec<&str> = OPERATIONS.iter().map(|o| o.path).collect();
        ids.sort_unstable();
        paths.sort_unstable();
        ids.dedup();
        paths.dedup();
        assert_eq!(ids.len(), OPERATIONS.len());
        assert_eq!(paths.len(), OPERATIONS.len());
        for operation in OPERATIONS {
            assert!(
                schemas().contains_key(operation.schema),
                "{}",
                operation.schema
            );
        }
    }

    #[test]
    fn arguments_are_checked_against_their_kind() {
        let search = find("/api/v1/search").unwrap().0;
        let pairs = |items: &[(&str, &str)]| -> Vec<(String, String)> {
            items
                .iter()
                .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
                .collect()
        };
        let found = arguments(
            search,
            None,
            &pairs(&[
                ("q", "x"),
                ("kind", "model"),
                ("kind", "seed"),
                ("limit", "5"),
            ]),
        )
        .unwrap();
        assert_eq!(found[0], ("q", Argument::Text("x".into())));
        assert_eq!(
            found[1],
            (
                "kind",
                Argument::TextList(vec!["model".into(), "seed".into()])
            )
        );
        assert_eq!(found[2], ("limit", Argument::Integer(5)));
        assert_eq!(
            arguments(search, None, &pairs(&[])),
            Err("q is required".into())
        );
        assert!(arguments(search, None, &pairs(&[("q", "x"), ("limit", "many")])).is_err());
        let export = find("/api/v1/export/x").unwrap().0;
        assert!(arguments(export, Some("pdf".into()), &[]).is_err());
        let ok = arguments(export, Some("dot".into()), &pairs(&[("columns", "true")])).unwrap();
        assert_eq!(ok[1], ("columns", Argument::Boolean(true)));
        assert!(arguments(export, Some("dot".into()), &pairs(&[("columns", "maybe")])).is_err());
        assert_eq!(Argument::TextList(vec!["a".into()]).to_json(), json!(["a"]));
    }

    #[test]
    fn revisions_that_git_would_read_as_options_are_refused() {
        let as_pairs = |key: &str, value: &str| vec![(key.to_owned(), value.to_owned())];
        for (path, name) in [
            ("/api/v1/diff", "base"),
            ("/api/v1/diff", "head"),
            ("/api/v1/gate", "base"),
            ("/api/v1/define", "ref"),
        ] {
            let operation = find(path).unwrap().0;
            let refused = arguments(operation, None, &as_pairs(name, "--output=/tmp/x"));
            assert_eq!(
                refused,
                Err(format!("{name} must not start with -")),
                "{path} {name}"
            );
            assert!(arguments(operation, None, &as_pairs(name, "main")).is_ok());
        }
        let explain = find("/api/v1/explain/x").unwrap().0;
        assert!(arguments(explain, Some("-x".into()), &[]).is_err());
    }

    #[test]
    fn pages_follow_cursors_of_one_version() {
        let paging = Paging {
            items: "items",
            default_limit: 2,
        };
        let answer = json!({ "kinds": [], "items": [1, 2, 3, 4, 5] });
        let (first, none) = page(&paging, &[]).unwrap();
        assert_eq!(none, None);
        let one = paged(&answer, &paging, first, "v1");
        assert_eq!(one["items"], json!([1, 2]));
        assert_eq!(one["next_cursor"], "v1.2");
        let cursor = [("cursor", Argument::Text("v1.4".into()))];
        let (fifth, made_for) = page(&paging, &cursor).unwrap();
        assert_eq!(made_for.as_deref(), Some("v1"));
        let last = paged(&answer, &paging, fifth, "v1");
        assert_eq!(last["items"], json!([5]));
        assert_eq!(last["next_cursor"], Value::Null);
        assert!(page(&paging, &[("cursor", Argument::Text("x".into()))]).is_err());
        assert!(page(&paging, &[("limit", Argument::Integer(5000))]).is_err());
        assert_eq!(
            next_cursor(
                Page {
                    offset: 0,
                    limit: 0
                },
                5,
                "v"
            ),
            None
        );
    }

    #[test]
    fn problems_have_stable_types() {
        let (status, body) = problem("no-such-node", "no node x");
        assert_eq!(status, 404);
        assert_eq!(body["type"], format!("{PROBLEMS}no-such-node"));
        assert_eq!(body["status"], 404);
        let (status, body) = problem("something-else", "x");
        assert_eq!(
            (
                status,
                body["type"].as_str().unwrap().ends_with("internal-error")
            ),
            (500, true)
        );
    }

    #[test]
    fn the_document_is_the_same_every_time() {
        let text = openapi_text();
        assert_eq!(text, openapi_text());
        let document: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(document["openapi"], "3.1.0");
        let paths = document["paths"].as_object().unwrap();
        assert_eq!(paths.len(), OPERATIONS.len() + 1);
        assert!(paths.keys().all(|p| p.starts_with("/api/v1/")));
        assert_eq!(
            document["paths"]["/api/v1/search"]["get"]["x-konstruct-cached"],
            true
        );
        assert_eq!(
            document["paths"]["/api/v1/diff"]["get"]["x-konstruct-cached"],
            false
        );
    }
}
