//! The nodes of a document: PyYAML's composer, and the limits that make an untrusted file safe
//! to read. An alias refers to the node of its anchor, so a document is a graph, and a few hundred
//! bytes of aliases to aliases can stand for millions of values; the limits are checked as nodes
//! are made, before anything expands them:
//!
//! - nesting depth is counted through aliases, so a deep node cannot be reached by a short path;
//! - the values that aliases add are counted for a whole stream, one budget for all its
//!   documents, so that many small documents are no more than one large one.
//!
//! The composer is a loop over the parser's events with its own stack, so a deep document costs
//! no call depth here.

use std::borrow::Cow;
use std::collections::HashMap;

use super::parser::{Event, Parser};
use super::resolve::{Tag, resolve};
use super::{Failure, Limits, Mark, Stage, grouped, repr};

pub type NodeId = u32;

/// A value of the document.
#[derive(Debug)]
pub struct Node<'a> {
    pub tag: Tag,
    /// Where the node starts: its anchor or tag, if it has them, and its content otherwise.
    pub mark: Mark,
    pub body: Body<'a>,
    /// Whether an alias refers to the node, so that the value built for it is kept.
    pub shared: bool,
    /// Whether a scalar has an escaped surrogate that no pair completes, which no value can hold.
    pub lone: bool,
}

#[derive(Debug)]
pub enum Body<'a> {
    Scalar(Cow<'a, str>),
    Sequence(Vec<NodeId>),
    Mapping(Vec<(NodeId, NodeId)>),
}

impl Body<'_> {
    /// What PyYAML's messages call the kind of node.
    pub fn id(&self) -> &'static str {
        match self {
            Body::Scalar(_) => "scalar",
            Body::Sequence(_) => "sequence",
            Body::Mapping(_) => "mapping",
        }
    }
}

/// One document: its nodes, and the root among them.
#[derive(Debug)]
pub struct Document<'a> {
    pub nodes: Vec<Node<'a>>,
    pub root: NodeId,
}

/// Saturation of the number of values a node stands for.
const SATURATED: u64 = u64::MAX / 4;

struct Frame {
    id: NodeId,
    /// The key of the pair being read, once read.
    key: Option<NodeId>,
}

/// A document as it is built.
struct Build<'a> {
    nodes: Vec<Node<'a>>,
    /// For each node, the values it stands for with aliases expanded; a collection that is still
    /// being read stands for a saturated number, so that an alias to its own ancestor is a
    /// document of too many values.
    size: Vec<u64>,
    /// For each node, its nesting with aliases expanded: a scalar is 0, a collection is one more
    /// than its deepest member.
    depth: Vec<usize>,
    anchors: HashMap<Cow<'a, str>, NodeId>,
    aliases: Vec<(NodeId, Mark)>,
    stack: Vec<Frame>,
    root: Option<NodeId>,
}

impl<'a> Build<'a> {
    fn new() -> Build<'a> {
        Build {
            nodes: Vec::new(),
            size: Vec::new(),
            depth: Vec::new(),
            anchors: HashMap::new(),
            aliases: Vec::new(),
            stack: Vec::new(),
            root: None,
        }
    }

    fn push(&mut self, node: Node<'a>, size: u64) -> Result<NodeId, Failure> {
        let id = NodeId::try_from(self.nodes.len())
            .map_err(|_| Failure::new(Stage::Limit, "too many values to read", Some(node.mark)))?;
        self.nodes.push(node);
        self.size.push(size);
        self.depth.push(0);
        Ok(id)
    }

    fn register(
        &mut self,
        anchor: Option<Cow<'a, str>>,
        id: NodeId,
        mark: Mark,
    ) -> Result<(), Failure> {
        let Some(name) = anchor else { return Ok(()) };
        if let Some(&first) = self.anchors.get(&name) {
            return Err(Failure::within(
                Stage::Composer,
                format!("found duplicate anchor {}; first occurrence", repr(&name)),
                self.nodes[first as usize].mark,
                "second occurrence",
                mark,
            ));
        }
        self.anchors.insert(name, id);
        Ok(())
    }

    /// Puts `child` where the collection being read expects its next member.
    fn attach(&mut self, child: NodeId) {
        let Some(frame) = self.stack.last_mut() else {
            self.root = Some(child);
            return;
        };
        match &mut self.nodes[frame.id as usize].body {
            Body::Sequence(items) => items.push(child),
            Body::Mapping(pairs) => match frame.key.take() {
                None => frame.key = Some(child),
                Some(key) => pairs.push((key, child)),
            },
            Body::Scalar(_) => {}
        }
    }

    /// A collection has all its members: what it stands for, and how deep it is.
    fn finish(&mut self, id: NodeId) {
        let mut total: u64 = 1;
        let mut deepest = 0;
        let mut add = |member: NodeId, size: &[u64], depth: &[usize]| {
            total = total.saturating_add(size[member as usize]).min(SATURATED);
            deepest = deepest.max(depth[member as usize]);
        };
        match &self.nodes[id as usize].body {
            Body::Sequence(items) => {
                for &item in items {
                    add(item, &self.size, &self.depth);
                }
            }
            Body::Mapping(pairs) => {
                for &(key, value) in pairs {
                    add(key, &self.size, &self.depth);
                    add(value, &self.size, &self.depth);
                }
            }
            Body::Scalar(_) => {}
        }
        self.size[id as usize] = total;
        self.depth[id as usize] = deepest + 1;
    }
}

/// The documents of a stream, one at a time, sharing one budget of alias values.
pub struct Composer<'t> {
    parser: Parser<'t>,
    limits: Limits,
    /// How many values aliases may still add in this stream.
    left: u64,
    started: bool,
    /// The event read ahead of the document being asked for.
    ahead: Option<Event<'t>>,
    /// Whether the budget is for a stream of documents, which its message says.
    stream: bool,
}

impl<'t> Composer<'t> {
    pub fn new(text: &'t str, limits: Limits, stream: bool) -> Composer<'t> {
        Composer {
            parser: Parser::new(text, limits.digits),
            left: limits.max_values,
            limits,
            started: false,
            ahead: None,
            stream,
        }
    }

    fn event(&mut self) -> Result<Event<'t>, Failure> {
        match self.ahead.take() {
            Some(event) => Ok(event),
            None => self.parser.next_event(),
        }
    }

    /// The next document of the stream, or None at its end.
    pub fn next_document(&mut self) -> Result<Option<Document<'t>>, Failure> {
        if !self.started {
            self.started = true;
            let _ = self.event()?;
        }
        match self.event()? {
            Event::StreamEnd => {
                self.ahead = Some(Event::StreamEnd);
                Ok(None)
            }
            Event::DocumentStart(_) => self.document().map(Some),
            _ => Err(Failure::new(
                Stage::Composer,
                "expected the start of a document",
                None,
            )),
        }
    }

    /// The only document of the stream, None when it is empty. A second is refused.
    pub fn single(&mut self) -> Result<Option<Document<'t>>, Failure> {
        let Some(document) = self.next_document()? else {
            return Ok(None);
        };
        match self.event()? {
            Event::DocumentStart(mark) => Err(Failure::within(
                Stage::Composer,
                "expected a single document in the stream",
                document.nodes[document.root as usize].mark,
                "but found another document",
                mark,
            )),
            _ => Ok(Some(document)),
        }
    }

    fn document(&mut self) -> Result<Document<'t>, Failure> {
        let max_depth = self.limits.max_depth;
        let mut build = Build::new();
        loop {
            let child: NodeId = match self.event()? {
                Event::DocumentEnd => break,
                Event::StreamStart | Event::StreamEnd | Event::DocumentStart(_) => {
                    return Err(Failure::new(
                        Stage::Composer,
                        "found the start of a document inside a document",
                        None,
                    ));
                }
                Event::Alias { name, mark } => {
                    let Some(&target) = build.anchors.get(&name) else {
                        return Err(Failure::new(
                            Stage::Composer,
                            format!("found undefined alias {}", repr(&name)),
                            Some(mark),
                        ));
                    };
                    build.nodes[target as usize].shared = true;
                    build.aliases.push((target, mark));
                    target
                }
                Event::Scalar {
                    anchor,
                    tag,
                    implicit,
                    value,
                    lone,
                    mark,
                } => {
                    let tag = match tag.as_deref() {
                        None | Some("!") => {
                            if implicit {
                                resolve(&value)
                            } else {
                                Tag::Str
                            }
                        }
                        Some(uri) => Tag::from_uri(uri),
                    };
                    let id = build.push(
                        Node {
                            tag,
                            mark,
                            body: Body::Scalar(value),
                            shared: false,
                            lone,
                        },
                        1,
                    )?;
                    build.register(anchor, id, mark)?;
                    id
                }
                Event::SequenceStart { anchor, tag, mark } => {
                    self.open(
                        &mut build,
                        anchor,
                        tag,
                        mark,
                        Tag::Seq,
                        Body::Sequence(Vec::new()),
                    )?;
                    continue;
                }
                Event::MappingStart { anchor, tag, mark } => {
                    self.open(
                        &mut build,
                        anchor,
                        tag,
                        mark,
                        Tag::Map,
                        Body::Mapping(Vec::new()),
                    )?;
                    continue;
                }
                Event::SequenceEnd | Event::MappingEnd => {
                    let Some(frame) = build.stack.pop() else {
                        return Err(Failure::new(
                            Stage::Composer,
                            "found the end of a collection outside one",
                            None,
                        ));
                    };
                    build.finish(frame.id);
                    if build.depth[frame.id as usize] > max_depth {
                        return Err(Failure::new(
                            Stage::Limit,
                            "nested too deeply to read",
                            Some(build.nodes[frame.id as usize].mark),
                        ));
                    }
                    continue;
                }
            };
            build.attach(child);
        }
        let Some(root) = build.root else {
            return Err(Failure::new(
                Stage::Composer,
                "a document without a node",
                None,
            ));
        };
        self.charge(&build, root)?;
        Ok(Document {
            nodes: build.nodes,
            root,
        })
    }

    /// Starts a sequence or a mapping, which is attached where it begins, as an anchor may be
    /// aliased from inside it.
    fn open(
        &self,
        build: &mut Build<'t>,
        anchor: Option<Cow<'t, str>>,
        tag: Option<Cow<'t, str>>,
        mark: Mark,
        default: Tag,
        body: Body<'t>,
    ) -> Result<(), Failure> {
        if build.stack.len() >= self.limits.max_depth {
            return Err(Failure::new(
                Stage::Limit,
                "nested too deeply to read",
                Some(mark),
            ));
        }
        let tag = match tag.as_deref() {
            None | Some("!") => default,
            Some(uri) => Tag::from_uri(uri),
        };
        let id = build.push(
            Node {
                tag,
                mark,
                body,
                shared: false,
                lone: false,
            },
            SATURATED,
        )?;
        build.register(anchor, id, mark)?;
        // The new node is the parent of what follows, and the child of what is open now.
        build.attach(id);
        build.stack.push(Frame { id, key: None });
        Ok(())
    }

    /// Spends the budget on what the aliases of the document add, or refuses the document.
    fn charge(&mut self, build: &Build<'t>, root: NodeId) -> Result<(), Failure> {
        let written = build.nodes.len() as u64;
        let limit = self.left.saturating_add(written);
        let total = build.size[root as usize];
        if total <= limit {
            self.left -= total.saturating_sub(written).min(self.left);
            return Ok(());
        }
        // The alias that adds the most is where the problem is.
        let mut worst: Option<(u64, Mark)> = None;
        for &(target, mark) in &build.aliases {
            let added = build.size[target as usize].min(limit + 1);
            if worst.is_none_or(|(best, _)| added > best) {
                worst = Some((added, mark));
            }
        }
        let problem = if self.stream {
            format!(
                "the aliases of the documents stand for more than {} values together",
                grouped(self.limits.max_values)
            )
        } else {
            format!(
                "its aliases stand for more than {} values",
                grouped(self.limits.max_values)
            )
        };
        Err(Failure::new(
            Stage::Limit,
            problem,
            worst.map(|(_, mark)| mark),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn limits(max_values: u64, max_depth: usize) -> Limits {
        Limits {
            max_values,
            max_depth,
            ..Limits::DEFAULT
        }
    }

    fn single(text: &str, limits: Limits) -> Result<Option<Document<'_>>, Failure> {
        Composer::new(text, limits, false).single()
    }

    #[test]
    fn an_alias_refers_to_the_node_of_its_anchor() {
        let document = single("a: &x [1, 2]\nb: *x\n", Limits::DEFAULT)
            .unwrap()
            .unwrap();
        let Body::Mapping(pairs) = &document.nodes[document.root as usize].body else {
            panic!("a mapping");
        };
        assert_eq!(pairs[0].1, pairs[1].1);
        assert!(document.nodes[pairs[0].1 as usize].shared);
    }

    #[test]
    fn a_tag_and_an_anchor_move_the_start_of_a_node() {
        let document = single("a: !!str &x\n  - 1\n", Limits::DEFAULT)
            .unwrap()
            .unwrap();
        let Body::Mapping(pairs) = &document.nodes[document.root as usize].body else {
            panic!("a mapping");
        };
        let value = &document.nodes[pairs[0].1 as usize];
        assert_eq!((value.mark.line, value.mark.column), (0, 3));
        assert_eq!(value.tag, Tag::Str);
    }

    #[test]
    fn what_aliases_add_is_counted_and_what_is_written_is_not() {
        // 99 values and ten copies of them stand for 1,000 values added, and eleven for more.
        let list = format!("s: &s [{}]\n", vec!["x"; 99].join(", "));
        let ten: String = (0..10).map(|n| format!("c{n}: *s\n")).collect();
        assert!(single(&format!("{list}{ten}"), limits(1_000, 250)).is_ok());
        let eleven: String = (0..11).map(|n| format!("c{n}: *s\n")).collect();
        let refused = single(&format!("{list}{eleven}"), limits(1_000, 250)).unwrap_err();
        assert_eq!(
            refused.problem,
            "its aliases stand for more than 1,000 values"
        );
        assert_eq!(refused.stage, Stage::Limit);
        let written: String = (0..5_000).map(|n| format!("k{n}: v\n")).collect();
        assert!(single(&written, limits(1_000, 250)).is_ok());
    }

    #[test]
    fn a_node_that_holds_itself_stands_for_too_many_values() {
        let refused = single("a: &a [*a]\n", Limits::DEFAULT).unwrap_err();
        assert_eq!(refused.stage, Stage::Limit);
    }

    #[test]
    fn a_stream_has_one_budget_for_all_its_documents() {
        let document = "s: &s [a, b, c, d, e, f, g, h]\nt: [*s, *s, *s]\n";
        let stream = format!("{document}---\n{document}---\n{document}");
        let mut composer = Composer::new(&stream, limits(40, 250), true);
        // Each document adds 3 * 9 = 27 values to its own 12: two would not fit in 40 together.
        assert!(composer.next_document().unwrap().is_some());
        let refused = composer.next_document().unwrap_err();
        assert_eq!(
            refused.problem,
            "the aliases of the documents stand for more than 40 values together"
        );
        // The same documents, each read alone, are within 40.
        for text in [document, document, document] {
            assert!(single(text, limits(40, 250)).is_ok());
        }
    }

    #[test]
    fn nesting_is_counted_through_aliases() {
        let mut text = String::from("a0: &a0 [x]\n");
        for level in 1..=10 {
            text.push_str(&format!("a{level}: &a{level} [*a{}]\n", level - 1));
        }
        assert!(single(&text, limits(1_000_000, 12)).is_ok());
        let refused = single(&text, limits(1_000_000, 10)).unwrap_err();
        assert_eq!(refused.problem, "nested too deeply to read");
        let written = format!("{}{}", "[".repeat(300), "]".repeat(300));
        let refused = single(&written, limits(1_000_000, 250)).unwrap_err();
        assert_eq!(refused.problem, "nested too deeply to read");
        assert_eq!(refused.problem_mark.unwrap().line, 0);
    }

    #[test]
    fn a_second_document_is_refused_where_one_is_wanted() {
        let refused = single("a: 1\n---\nb: 2\n", Limits::DEFAULT).unwrap_err();
        assert_eq!(refused.stage, Stage::Composer);
        assert_eq!(refused.problem, "but found another document");
        assert!(single("", Limits::DEFAULT).unwrap().is_none());
    }

    #[test]
    fn an_anchor_is_defined_once_and_an_alias_needs_one() {
        let twice = single("a: &x 1\nb: &x 2\n", Limits::DEFAULT).unwrap_err();
        assert_eq!(twice.problem, "second occurrence");
        assert_eq!(
            crate::yaml::plain(twice.context.as_deref().unwrap()),
            "found duplicate anchor 'x'; first occurrence"
        );
        let undefined = single("a: *x\n", Limits::DEFAULT).unwrap_err();
        assert_eq!(
            crate::yaml::plain(&undefined.problem),
            "found undefined alias 'x'"
        );
    }
}

#[cfg(test)]
mod timing {
    use super::*;
    use std::time::Instant;

    #[test]
    #[ignore]
    fn compose_a_file() {
        let path = std::env::var("KONSTRUCT_YAML_FILE").expect("a file");
        let bytes = std::fs::read(path).unwrap();
        let text = crate::yaml::reader::decode(&bytes).unwrap();
        for _ in 0..3 {
            let document = Composer::new(&text, Limits::DEFAULT, false)
                .single()
                .unwrap()
                .unwrap();
            std::hint::black_box(document.nodes.len());
        }
    }

    /// `KONSTRUCT_YAML_FILE=schema.yml cargo test --release --lib yaml::compose::timing -- --ignored --nocapture`
    #[test]
    #[ignore]
    fn read_a_file() {
        let path = std::env::var("KONSTRUCT_YAML_FILE").expect("a file");
        let bytes = std::fs::read(path).unwrap();
        let text = crate::yaml::reader::decode(&bytes).unwrap();
        for _ in 0..5 {
            let start = Instant::now();
            let reader = Instant::now();
            crate::yaml::reader::check(&text).unwrap();
            let check = reader.elapsed();
            let mut scanner = crate::yaml::scanner::Scanner::new(&text);
            let scan = Instant::now();
            let mut count = 0;
            while let Ok(token) = scanner.get_token() {
                count += 1;
                if matches!(token.tok, crate::yaml::scanner::Tok::StreamEnd) {
                    break;
                }
            }
            let scanned = scan.elapsed();
            let parse = Instant::now();
            let mut parser = crate::yaml::parser::Parser::new(&text);
            let mut events = 0;
            loop {
                let event = parser.next_event().unwrap();
                events += 1;
                if matches!(event, crate::yaml::parser::Event::StreamEnd) {
                    break;
                }
            }
            println!("parse alone {:?} ({events} events)", parse.elapsed());
            let document = Composer::new(&text, Limits::DEFAULT, false)
                .single()
                .unwrap()
                .unwrap();
            println!(
                "{} bytes: check {:?}, scan {:?} ({count} tokens), scan+parse+compose {:?}, {} nodes",
                bytes.len(),
                check,
                scanned,
                start.elapsed() - check,
                document.nodes.len()
            );
        }
    }
}
