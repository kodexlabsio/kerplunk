//! The nodes of a tree in the order Python's `ast.walk` yields them: breadth first, each node's
//! children in the order of its fields.
//!
//! The readers depend on that order: it decides which of two jobs, SQL statements or `return`s
//! comes first. Ruff's tree differs from Python's in a few places, so [`Node`] has the nodes as
//! Python has them: `elif` is an `If` inside the `orelse` of the one before it, parameters are an
//! `arguments` node of `arg` nodes, and a decorator is its expression. Nodes with no children,
//! such as operators and contexts, are left out, as they cannot change the order of the others.

use std::collections::VecDeque;

use ruff_python_ast::{
    Alias, Comprehension, ElifElseClause, ExceptHandler, Expr, InterpolatedElement,
    InterpolatedStringElement, InterpolatedStringFormatSpec, Keyword, MatchCase, Parameter,
    Parameters, Pattern, Stmt, StmtFunctionDef, TypeParam, WithItem,
};

/// A node of Python's tree.
#[derive(Clone, Copy)]
pub enum Node<'a> {
    Stmt(&'a Stmt),
    /// A function definition, as a node to start a walk from.
    Function(&'a StmtFunctionDef),
    Expr(&'a Expr),
    /// An `elif` clause and what follows it: the `If` that is the `orelse` of the one before.
    Elif(&'a [ElifElseClause]),
    Arguments(&'a Parameters),
    Arg(&'a Parameter),
    Keyword(&'a Keyword),
    Alias(&'a Alias),
    WithItem(&'a WithItem),
    Comprehension(&'a Comprehension),
    Handler(&'a ExceptHandler),
    MatchCase(&'a MatchCase),
    Pattern(&'a Pattern),
    TypeParam(&'a TypeParam),
    /// A `FormattedValue` of an f-string, or an `Interpolation` of a t-string.
    Interpolation(&'a InterpolatedElement),
    /// The `JoinedStr` that is a format specification.
    Spec(&'a InterpolatedStringFormatSpec),
}

/// The statements that follow an `if`'s body: the `else` clause's, or the `elif` that stands for
/// them.
#[derive(Clone, Copy)]
pub enum Orelse<'a> {
    Stmts(&'a [Stmt]),
    Elif(&'a [ElifElseClause]),
}

impl<'a> Orelse<'a> {
    /// What follows the body of an `if` whose clauses are `clauses`.
    pub fn of(clauses: &'a [ElifElseClause]) -> Self {
        match clauses.first() {
            None => Self::Stmts(&[]),
            Some(first) if first.test.is_none() => Self::Stmts(&first.body),
            Some(_) => Self::Elif(clauses),
        }
    }
}

fn expr<'a>(queue: &mut VecDeque<Node<'a>>, found: &'a Expr) {
    queue.push_back(Node::Expr(found));
}

fn optional<'a>(queue: &mut VecDeque<Node<'a>>, found: Option<&'a Expr>) {
    if let Some(found) = found {
        expr(queue, found);
    }
}

fn exprs<'a>(queue: &mut VecDeque<Node<'a>>, found: &'a [Expr]) {
    queue.extend(found.iter().map(Node::Expr));
}

fn stmts<'a>(queue: &mut VecDeque<Node<'a>>, found: &'a [Stmt]) {
    queue.extend(found.iter().map(Node::Stmt));
}

fn orelse<'a>(queue: &mut VecDeque<Node<'a>>, found: Orelse<'a>) {
    match found {
        Orelse::Stmts(body) => stmts(queue, body),
        Orelse::Elif(clauses) => queue.push_back(Node::Elif(clauses)),
    }
}

fn type_params<'a>(queue: &mut VecDeque<Node<'a>>, found: Option<&'a ruff_python_ast::TypeParams>) {
    if let Some(found) = found {
        queue.extend(found.type_params.iter().map(Node::TypeParam));
    }
}

fn parameters<'a>(queue: &mut VecDeque<Node<'a>>, found: &'a Parameters) {
    queue.push_back(Node::Arguments(found));
}

fn interpolations<'a>(
    queue: &mut VecDeque<Node<'a>>,
    elements: impl Iterator<Item = &'a InterpolatedStringElement>,
) {
    for element in elements {
        if let InterpolatedStringElement::Interpolation(found) = element {
            queue.push_back(Node::Interpolation(found));
        }
    }
}

fn function<'a>(queue: &mut VecDeque<Node<'a>>, def: &'a StmtFunctionDef) {
    parameters(queue, &def.parameters);
    stmts(queue, &def.body);
    queue.extend(def.decorator_list.iter().map(|d| Node::Expr(&d.expression)));
    optional(queue, def.returns.as_deref());
    type_params(queue, def.type_params.as_deref());
}

fn statement<'a>(queue: &mut VecDeque<Node<'a>>, found: &'a Stmt) {
    match found {
        Stmt::FunctionDef(def) => function(queue, def),
        Stmt::ClassDef(class) => {
            if let Some(arguments) = &class.arguments {
                exprs(queue, &arguments.args);
                queue.extend(arguments.keywords.iter().map(Node::Keyword));
            }
            stmts(queue, &class.body);
            queue.extend(
                class
                    .decorator_list
                    .iter()
                    .map(|d| Node::Expr(&d.expression)),
            );
            type_params(queue, class.type_params.as_deref());
        }
        Stmt::Return(s) => optional(queue, s.value.as_deref()),
        Stmt::Delete(s) => exprs(queue, &s.targets),
        Stmt::TypeAlias(s) => {
            expr(queue, &s.name);
            type_params(queue, s.type_params.as_deref());
            expr(queue, &s.value);
        }
        Stmt::Assign(s) => {
            exprs(queue, &s.targets);
            expr(queue, &s.value);
        }
        Stmt::AugAssign(s) => {
            expr(queue, &s.target);
            expr(queue, &s.value);
        }
        Stmt::AnnAssign(s) => {
            expr(queue, &s.target);
            expr(queue, &s.annotation);
            optional(queue, s.value.as_deref());
        }
        Stmt::For(s) => {
            expr(queue, &s.target);
            expr(queue, &s.iter);
            stmts(queue, &s.body);
            stmts(queue, &s.orelse);
        }
        Stmt::While(s) => {
            expr(queue, &s.test);
            stmts(queue, &s.body);
            stmts(queue, &s.orelse);
        }
        Stmt::If(s) => {
            expr(queue, &s.test);
            stmts(queue, &s.body);
            orelse(queue, Orelse::of(&s.elif_else_clauses));
        }
        Stmt::With(s) => {
            queue.extend(s.items.iter().map(Node::WithItem));
            stmts(queue, &s.body);
        }
        Stmt::Match(s) => {
            expr(queue, &s.subject);
            queue.extend(s.cases.iter().map(Node::MatchCase));
        }
        Stmt::Raise(s) => {
            optional(queue, s.exc.as_deref());
            optional(queue, s.cause.as_deref());
        }
        Stmt::Try(s) => {
            stmts(queue, &s.body);
            queue.extend(s.handlers.iter().map(Node::Handler));
            stmts(queue, &s.orelse);
            stmts(queue, &s.finalbody);
        }
        Stmt::Assert(s) => {
            expr(queue, &s.test);
            optional(queue, s.msg.as_deref());
        }
        Stmt::Import(s) => queue.extend(s.names.iter().map(Node::Alias)),
        Stmt::ImportFrom(s) => queue.extend(s.names.iter().map(Node::Alias)),
        Stmt::Expr(s) => expr(queue, &s.value),
        Stmt::Global(_)
        | Stmt::Nonlocal(_)
        | Stmt::Pass(_)
        | Stmt::Break(_)
        | Stmt::Continue(_)
        | Stmt::IpyEscapeCommand(_) => {}
    }
}

fn expression<'a>(queue: &mut VecDeque<Node<'a>>, found: &'a Expr) {
    match found {
        Expr::BoolOp(e) => exprs(queue, &e.values),
        Expr::Named(e) => {
            expr(queue, &e.target);
            expr(queue, &e.value);
        }
        Expr::BinOp(e) => {
            expr(queue, &e.left);
            expr(queue, &e.right);
        }
        Expr::UnaryOp(e) => expr(queue, &e.operand),
        Expr::Lambda(e) => {
            if let Some(found) = &e.parameters {
                parameters(queue, found);
            }
            expr(queue, &e.body);
        }
        Expr::If(e) => {
            expr(queue, &e.test);
            expr(queue, &e.body);
            expr(queue, &e.orelse);
        }
        Expr::Dict(e) => {
            queue.extend(
                e.items
                    .iter()
                    .filter_map(|item| item.key.as_ref())
                    .map(Node::Expr),
            );
            queue.extend(e.items.iter().map(|item| Node::Expr(&item.value)));
        }
        Expr::Set(e) => exprs(queue, &e.elts),
        Expr::ListComp(e) => {
            expr(queue, &e.elt);
            queue.extend(e.generators.iter().map(Node::Comprehension));
        }
        Expr::SetComp(e) => {
            expr(queue, &e.elt);
            queue.extend(e.generators.iter().map(Node::Comprehension));
        }
        Expr::DictComp(e) => {
            optional(queue, e.key.as_deref());
            expr(queue, &e.value);
            queue.extend(e.generators.iter().map(Node::Comprehension));
        }
        Expr::Generator(e) => {
            expr(queue, &e.elt);
            queue.extend(e.generators.iter().map(Node::Comprehension));
        }
        Expr::Await(e) => expr(queue, &e.value),
        Expr::Yield(e) => optional(queue, e.value.as_deref()),
        Expr::YieldFrom(e) => expr(queue, &e.value),
        Expr::Compare(e) => {
            expr(queue, &e.left);
            exprs(queue, &e.comparators);
        }
        Expr::Call(e) => {
            expr(queue, &e.func);
            exprs(queue, &e.arguments.args);
            queue.extend(e.arguments.keywords.iter().map(Node::Keyword));
        }
        Expr::FString(e) => {
            for part in e.value.as_slice() {
                if let ruff_python_ast::FStringPart::FString(f) = part {
                    interpolations(queue, f.elements.iter());
                }
            }
        }
        Expr::TString(e) => {
            for t in e.value.iter() {
                interpolations(queue, t.elements.iter());
            }
        }
        Expr::Attribute(e) => expr(queue, &e.value),
        Expr::Subscript(e) => {
            expr(queue, &e.value);
            expr(queue, &e.slice);
        }
        Expr::Starred(e) => expr(queue, &e.value),
        Expr::List(e) => exprs(queue, &e.elts),
        Expr::Tuple(e) => exprs(queue, &e.elts),
        Expr::Slice(e) => {
            optional(queue, e.lower.as_deref());
            optional(queue, e.upper.as_deref());
            optional(queue, e.step.as_deref());
        }
        Expr::StringLiteral(_)
        | Expr::BytesLiteral(_)
        | Expr::NumberLiteral(_)
        | Expr::BooleanLiteral(_)
        | Expr::NoneLiteral(_)
        | Expr::EllipsisLiteral(_)
        | Expr::Name(_)
        | Expr::IpyEscapeCommand(_) => {}
    }
}

fn pattern<'a>(queue: &mut VecDeque<Node<'a>>, found: &'a Pattern) {
    match found {
        Pattern::MatchValue(p) => expr(queue, &p.value),
        Pattern::MatchSequence(p) => queue.extend(p.patterns.iter().map(Node::Pattern)),
        Pattern::MatchMapping(p) => {
            exprs(queue, &p.keys);
            queue.extend(p.patterns.iter().map(Node::Pattern));
        }
        Pattern::MatchClass(p) => {
            expr(queue, &p.cls);
            queue.extend(p.arguments.patterns.iter().map(Node::Pattern));
            queue.extend(
                p.arguments
                    .keywords
                    .iter()
                    .map(|k| Node::Pattern(&k.pattern)),
            );
        }
        Pattern::MatchAs(p) => {
            if let Some(inner) = &p.pattern {
                queue.push_back(Node::Pattern(inner));
            }
        }
        Pattern::MatchOr(p) => queue.extend(p.patterns.iter().map(Node::Pattern)),
        Pattern::MatchSingleton(_) | Pattern::MatchStar(_) => {}
    }
}

impl<'a> Node<'a> {
    /// Adds this node's children to the back of `queue`, in the order of Python's fields.
    fn children(self, queue: &mut VecDeque<Node<'a>>) {
        match self {
            Node::Stmt(found) => statement(queue, found),
            Node::Function(found) => function(queue, found),
            Node::Expr(found) => expression(queue, found),
            Node::Elif(clauses) => {
                let first = &clauses[0];
                optional(queue, first.test.as_ref());
                stmts(queue, &first.body);
                orelse(queue, Orelse::of(&clauses[1..]));
            }
            Node::Arguments(found) => {
                queue.extend(found.posonlyargs.iter().map(|a| Node::Arg(&a.parameter)));
                queue.extend(found.args.iter().map(|a| Node::Arg(&a.parameter)));
                if let Some(vararg) = &found.vararg {
                    queue.push_back(Node::Arg(vararg));
                }
                queue.extend(found.kwonlyargs.iter().map(|a| Node::Arg(&a.parameter)));
                // Python lists the defaults of the keyword-only parameters, then the kwarg, then
                // the defaults of the others.
                for arg in &found.kwonlyargs {
                    optional(queue, arg.default.as_deref());
                }
                if let Some(kwarg) = &found.kwarg {
                    queue.push_back(Node::Arg(kwarg));
                }
                for arg in found.posonlyargs.iter().chain(&found.args) {
                    optional(queue, arg.default.as_deref());
                }
            }
            Node::Arg(found) => optional(queue, found.annotation.as_deref()),
            Node::Keyword(found) => expr(queue, &found.value),
            Node::Alias(_) => {}
            Node::WithItem(found) => {
                expr(queue, &found.context_expr);
                optional(queue, found.optional_vars.as_deref());
            }
            Node::Comprehension(found) => {
                expr(queue, &found.target);
                expr(queue, &found.iter);
                exprs(queue, &found.ifs);
            }
            Node::Handler(ExceptHandler::ExceptHandler(found)) => {
                optional(queue, found.type_.as_deref());
                stmts(queue, &found.body);
            }
            Node::MatchCase(found) => {
                queue.push_back(Node::Pattern(&found.pattern));
                optional(queue, found.guard.as_deref());
                stmts(queue, &found.body);
            }
            Node::Pattern(found) => pattern(queue, found),
            Node::TypeParam(found) => match found {
                TypeParam::TypeVar(p) => {
                    optional(queue, p.bound.as_deref());
                    optional(queue, p.default.as_deref());
                }
                TypeParam::TypeVarTuple(p) => optional(queue, p.default.as_deref()),
                TypeParam::ParamSpec(p) => optional(queue, p.default.as_deref()),
            },
            Node::Interpolation(found) => {
                expr(queue, &found.expression);
                if let Some(spec) = &found.format_spec {
                    queue.push_back(Node::Spec(spec));
                }
            }
            Node::Spec(found) => interpolations(queue, found.elements.iter()),
        }
    }
}

/// Calls `visit` on `roots` and then on every node below them, as `ast.walk` does for the nodes
/// it starts from, in the order it yields them.
pub fn walk<'a>(roots: impl IntoIterator<Item = Node<'a>>, mut visit: impl FnMut(Node<'a>)) {
    let mut queue: VecDeque<Node<'a>> = roots.into_iter().collect();
    while let Some(node) = queue.pop_front() {
        visit(node);
        node.children(&mut queue);
    }
}
