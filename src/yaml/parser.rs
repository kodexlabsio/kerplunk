//! The events of a YAML text: PyYAML's parser, a state machine over the tokens of the scanner. It
//! resolves tags against the `%TAG` directives, joins an anchor and a tag to the node they
//! describe, and refuses what is not a document at the token PyYAML refuses it at. It is a pull
//! parser with its own stack, so nesting costs no call depth.

use std::borrow::Cow;
use std::collections::HashMap;

use super::scanner::{Directive, Scanner, Tok, Token};
use super::{Failure, Mark, Stage, repr};

/// One step of the document, as the composer reads it.
#[derive(Debug)]
pub enum Event<'a> {
    StreamStart,
    StreamEnd,
    /// Where the document begins.
    DocumentStart(Mark),
    DocumentEnd,
    Alias {
        name: Cow<'a, str>,
        mark: Mark,
    },
    Scalar {
        anchor: Option<Cow<'a, str>>,
        /// The tag as a URI, `!` when the document wrote the non-specific tag.
        tag: Option<Cow<'a, str>>,
        /// Whether the scalar's tag is found from what it says: it is plain and has no tag, or
        /// has the tag `!`.
        implicit: bool,
        value: Cow<'a, str>,
        /// Whether the value has an escaped surrogate that no pair completes.
        lone: bool,
        mark: Mark,
    },
    SequenceStart {
        anchor: Option<Cow<'a, str>>,
        tag: Option<Cow<'a, str>>,
        mark: Mark,
    },
    SequenceEnd,
    MappingStart {
        anchor: Option<Cow<'a, str>>,
        tag: Option<Cow<'a, str>>,
        mark: Mark,
    },
    MappingEnd,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    StreamStart,
    StreamEnd,
    Directive,
    DocumentStart,
    DocumentEnd,
    BlockSequenceStart,
    BlockMappingStart,
    BlockEnd,
    FlowSequenceStart,
    FlowSequenceEnd,
    FlowMappingStart,
    FlowMappingEnd,
    BlockEntry,
    FlowEntry,
    Key,
    Value,
    Alias,
    Anchor,
    Tag,
    Scalar,
}

impl Kind {
    fn of(token: &Tok<'_>) -> Kind {
        match token {
            Tok::StreamStart => Kind::StreamStart,
            Tok::StreamEnd => Kind::StreamEnd,
            Tok::Directive(_) => Kind::Directive,
            Tok::DocumentStart => Kind::DocumentStart,
            Tok::DocumentEnd => Kind::DocumentEnd,
            Tok::BlockSequenceStart => Kind::BlockSequenceStart,
            Tok::BlockMappingStart => Kind::BlockMappingStart,
            Tok::BlockEnd => Kind::BlockEnd,
            Tok::FlowSequenceStart => Kind::FlowSequenceStart,
            Tok::FlowSequenceEnd => Kind::FlowSequenceEnd,
            Tok::FlowMappingStart => Kind::FlowMappingStart,
            Tok::FlowMappingEnd => Kind::FlowMappingEnd,
            Tok::BlockEntry => Kind::BlockEntry,
            Tok::FlowEntry => Kind::FlowEntry,
            Tok::Key => Kind::Key,
            Tok::Value => Kind::Value,
            Tok::Alias(_) => Kind::Alias,
            Tok::Anchor(_) => Kind::Anchor,
            Tok::Tag(..) => Kind::Tag,
            Tok::Scalar { .. } => Kind::Scalar,
        }
    }

    /// How PyYAML's messages name the token.
    fn id(self) -> &'static str {
        match self {
            Kind::StreamStart => "<stream start>",
            Kind::StreamEnd => "<stream end>",
            Kind::Directive => "<directive>",
            Kind::DocumentStart => "<document start>",
            Kind::DocumentEnd => "<document end>",
            Kind::BlockSequenceStart => "<block sequence start>",
            Kind::BlockMappingStart => "<block mapping start>",
            Kind::BlockEnd => "<block end>",
            Kind::FlowSequenceStart => "[",
            Kind::FlowSequenceEnd => "]",
            Kind::FlowMappingStart => "{",
            Kind::FlowMappingEnd => "}",
            Kind::BlockEntry => "-",
            Kind::FlowEntry => ",",
            Kind::Key => "?",
            Kind::Value => ":",
            Kind::Alias => "<alias>",
            Kind::Anchor => "<anchor>",
            Kind::Tag => "<tag>",
            Kind::Scalar => "<scalar>",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum State {
    StreamStart,
    ImplicitDocumentStart,
    DocumentStart,
    DocumentEnd,
    DocumentContent,
    BlockNode,
    BlockSequenceFirstEntry,
    BlockSequenceEntry,
    IndentlessSequenceEntry,
    BlockMappingFirstKey,
    BlockMappingKey,
    BlockMappingValue,
    FlowSequenceFirstEntry,
    FlowSequenceEntry,
    FlowSequenceEntryMappingKey,
    FlowSequenceEntryMappingValue,
    FlowSequenceEntryMappingEnd,
    FlowMappingFirstKey,
    FlowMappingKey,
    FlowMappingValue,
    FlowMappingEmptyValue,
    End,
}

/// The pull parser.
pub struct Parser<'a> {
    scanner: Scanner<'a>,
    /// The token the parser looked at and has not taken, and what kind it is.
    ahead: Option<(Kind, Token<'a>)>,
    state: State,
    states: Vec<State>,
    marks: Vec<Mark>,
    handles: HashMap<String, String>,
    version: bool,
}

type Step<'a> = Result<Event<'a>, Failure>;

impl<'a> Parser<'a> {
    pub fn new(text: &'a str, digits: usize) -> Parser<'a> {
        Parser {
            scanner: Scanner::new(text, digits),
            ahead: None,
            state: State::StreamStart,
            states: Vec::new(),
            marks: Vec::new(),
            handles: HashMap::new(),
            version: false,
        }
    }

    /// The scanner's position, which is where a problem the parser finds at the end is.
    pub fn mark(&self) -> Mark {
        self.scanner.mark()
    }

    fn fill(&mut self) -> Result<(), Failure> {
        if self.ahead.is_none() {
            let token = self.scanner.get_token()?;
            self.ahead = Some((Kind::of(&token.tok), token));
        }
        Ok(())
    }

    fn kind(&mut self) -> Result<Kind, Failure> {
        if let Some((kind, _)) = &self.ahead {
            return Ok(*kind);
        }
        self.fill()?;
        Ok(self
            .ahead
            .as_ref()
            .map_or(Kind::StreamEnd, |(kind, _)| *kind))
    }

    fn is(&mut self, kinds: &[Kind]) -> Result<bool, Failure> {
        Ok(kinds.contains(&self.kind()?))
    }

    fn start(&mut self) -> Result<Mark, Failure> {
        self.fill()?;
        Ok(self
            .ahead
            .as_ref()
            .map_or_else(Mark::default, |(_, t)| t.start))
    }

    fn take(&mut self) -> Result<Token<'a>, Failure> {
        self.fill()?;
        self.ahead
            .take()
            .map(|(_, token)| token)
            .ok_or_else(|| Failure::new(Stage::Parser, "no token", None))
    }

    /// A refusal at the next token, which `context` says what was being read when it came.
    fn refuse(&mut self, context: Option<(&str, Mark)>, problem: String) -> Failure {
        let at = self.start().unwrap_or_default();
        match context {
            Some((context, mark)) => Failure::within(Stage::Parser, context, mark, problem, at),
            None => Failure::new(Stage::Parser, problem, Some(at)),
        }
    }

    /// The next event. After the stream's end it is the end again.
    pub fn next_event(&mut self) -> Step<'a> {
        match self.state {
            State::StreamStart => {
                let _ = self.take()?;
                self.state = State::ImplicitDocumentStart;
                Ok(Event::StreamStart)
            }
            State::ImplicitDocumentStart => self.implicit_document_start(),
            State::DocumentStart => self.document_start(),
            State::DocumentEnd => self.document_end(),
            State::DocumentContent => self.document_content(),
            State::BlockNode => self.node(true, false),
            State::BlockSequenceFirstEntry => {
                let token = self.take()?;
                self.marks.push(token.start);
                self.block_sequence_entry()
            }
            State::BlockSequenceEntry => self.block_sequence_entry(),
            State::IndentlessSequenceEntry => self.indentless_sequence_entry(),
            State::BlockMappingFirstKey => {
                let token = self.take()?;
                self.marks.push(token.start);
                self.block_mapping_key()
            }
            State::BlockMappingKey => self.block_mapping_key(),
            State::BlockMappingValue => self.block_mapping_value(),
            State::FlowSequenceFirstEntry => {
                let token = self.take()?;
                self.marks.push(token.start);
                self.flow_sequence_entry(true)
            }
            State::FlowSequenceEntry => self.flow_sequence_entry(false),
            State::FlowSequenceEntryMappingKey => self.flow_sequence_entry_mapping_key(),
            State::FlowSequenceEntryMappingValue => self.flow_sequence_entry_mapping_value(),
            State::FlowSequenceEntryMappingEnd => {
                self.state = State::FlowSequenceEntry;
                Ok(Event::MappingEnd)
            }
            State::FlowMappingFirstKey => {
                let token = self.take()?;
                self.marks.push(token.start);
                self.flow_mapping_key(true)
            }
            State::FlowMappingKey => self.flow_mapping_key(false),
            State::FlowMappingValue => self.flow_mapping_value(),
            State::FlowMappingEmptyValue => {
                self.state = State::FlowMappingKey;
                let mark = self.start()?;
                Ok(Self::empty_scalar(mark))
            }
            State::End => Ok(Event::StreamEnd),
        }
    }

    fn pop(&mut self) {
        self.state = self.states.pop().unwrap_or(State::End);
    }

    fn empty_scalar(mark: Mark) -> Event<'a> {
        Event::Scalar {
            anchor: None,
            tag: None,
            implicit: true,
            value: Cow::Borrowed(""),
            lone: false,
            mark,
        }
    }

    fn implicit_document_start(&mut self) -> Step<'a> {
        if self.is(&[Kind::Directive, Kind::DocumentStart, Kind::StreamEnd])? {
            return self.document_start();
        }
        let mark = self.start()?;
        self.default_handles();
        self.states.push(State::DocumentEnd);
        self.state = State::BlockNode;
        Ok(Event::DocumentStart(mark))
    }

    fn default_handles(&mut self) {
        self.handles.clear();
        self.handles.insert("!".into(), "!".into());
        self.handles
            .insert("!!".into(), "tag:yaml.org,2002:".into());
    }

    fn document_start(&mut self) -> Step<'a> {
        while self.is(&[Kind::DocumentEnd])? {
            let _ = self.take()?;
        }
        if self.is(&[Kind::StreamEnd])? {
            let _ = self.take()?;
            self.state = State::End;
            return Ok(Event::StreamEnd);
        }
        let mark = self.start()?;
        self.directives()?;
        if !self.is(&[Kind::DocumentStart])? {
            let found = self.kind()?;
            return Err(self.refuse(
                None,
                format!("expected '<document start>', but found '{}'", found.id()),
            ));
        }
        let _ = self.take()?;
        self.states.push(State::DocumentEnd);
        self.state = State::DocumentContent;
        Ok(Event::DocumentStart(mark))
    }

    fn directives(&mut self) -> Result<(), Failure> {
        self.version = false;
        self.handles.clear();
        while self.is(&[Kind::Directive])? {
            let token = self.take()?;
            let Tok::Directive(directive) = token.tok else {
                continue;
            };
            match directive {
                Directive::Yaml { major } => {
                    if self.version {
                        return Err(Failure::new(
                            Stage::Parser,
                            "found duplicate YAML directive",
                            Some(token.start),
                        ));
                    }
                    if major != 1 {
                        return Err(Failure::new(
                            Stage::Parser,
                            "found incompatible YAML document (version 1.* is required)",
                            Some(token.start),
                        ));
                    }
                    self.version = true;
                }
                Directive::Tag { handle, prefix } => {
                    if self.handles.contains_key(handle.as_ref()) {
                        return Err(Failure::new(
                            Stage::Parser,
                            format!("duplicate tag handle {}", repr(&handle)),
                            Some(token.start),
                        ));
                    }
                    self.handles
                        .insert(handle.into_owned(), prefix.into_owned());
                }
                Directive::Other => {}
            }
        }
        for (handle, prefix) in [("!", "!"), ("!!", "tag:yaml.org,2002:")] {
            self.handles
                .entry(handle.to_owned())
                .or_insert_with(|| prefix.to_owned());
        }
        Ok(())
    }

    fn document_end(&mut self) -> Step<'a> {
        if self.is(&[Kind::DocumentEnd])? {
            let _ = self.take()?;
        }
        self.state = State::DocumentStart;
        Ok(Event::DocumentEnd)
    }

    fn document_content(&mut self) -> Step<'a> {
        if self.is(&[
            Kind::Directive,
            Kind::DocumentStart,
            Kind::DocumentEnd,
            Kind::StreamEnd,
        ])? {
            let mark = self.start()?;
            self.pop();
            return Ok(Self::empty_scalar(mark));
        }
        self.node(true, false)
    }

    fn node(&mut self, block: bool, indentless: bool) -> Step<'a> {
        if self.is(&[Kind::Alias])? {
            let token = self.take()?;
            self.pop();
            let Tok::Alias(name) = token.tok else {
                unreachable!("an alias token")
            };
            return Ok(Event::Alias {
                name,
                mark: token.start,
            });
        }
        let mut anchor: Option<Cow<'a, str>> = None;
        let mut tag: Option<(Option<Cow<'a, str>>, Cow<'a, str>)> = None;
        let mut start: Option<Mark> = None;
        let mut tag_mark: Option<Mark> = None;
        if self.is(&[Kind::Anchor])? {
            let token = self.take()?;
            start = Some(token.start);
            if let Tok::Anchor(name) = token.tok {
                anchor = Some(name);
            }
            if self.is(&[Kind::Tag])? {
                let token = self.take()?;
                tag_mark = Some(token.start);
                if let Tok::Tag(handle, suffix) = token.tok {
                    tag = Some((handle, suffix));
                }
            }
        } else if self.is(&[Kind::Tag])? {
            let token = self.take()?;
            start = Some(token.start);
            tag_mark = Some(token.start);
            if let Tok::Tag(handle, suffix) = token.tok {
                tag = Some((handle, suffix));
            }
            if self.is(&[Kind::Anchor])? {
                let token = self.take()?;
                if let Tok::Anchor(name) = token.tok {
                    anchor = Some(name);
                }
            }
        }
        let tag: Option<Cow<'a, str>> = match tag {
            None => None,
            Some((None, suffix)) => Some(suffix),
            Some((Some(handle), suffix)) => match self.handles.get(handle.as_ref()) {
                Some(prefix) => Some(Cow::Owned(format!("{prefix}{suffix}"))),
                None => {
                    let here = start.unwrap_or_default();
                    return Err(Failure::within(
                        Stage::Parser,
                        "while parsing a node",
                        here,
                        format!("found undefined tag handle {}", repr(&handle)),
                        tag_mark.unwrap_or(here),
                    ));
                }
            },
        };
        let begin = match start {
            Some(mark) => mark,
            None => self.start()?,
        };
        let implicit = tag.as_deref().is_none_or(|t| t == "!");
        if indentless && self.is(&[Kind::BlockEntry])? {
            self.state = State::IndentlessSequenceEntry;
            return Ok(Event::SequenceStart {
                anchor,
                tag,
                mark: begin,
            });
        }
        match self.kind()? {
            Kind::Scalar => {
                let token = self.take()?;
                let Tok::Scalar { value, plain, lone } = token.tok else {
                    unreachable!("a scalar token")
                };
                let implicit = (plain && tag.is_none()) || tag.as_deref() == Some("!");
                self.pop();
                Ok(Event::Scalar {
                    anchor,
                    tag,
                    implicit,
                    value,
                    lone,
                    mark: begin,
                })
            }
            Kind::FlowSequenceStart => {
                self.state = State::FlowSequenceFirstEntry;
                Ok(Event::SequenceStart {
                    anchor,
                    tag,
                    mark: begin,
                })
            }
            Kind::FlowMappingStart => {
                self.state = State::FlowMappingFirstKey;
                Ok(Event::MappingStart {
                    anchor,
                    tag,
                    mark: begin,
                })
            }
            Kind::BlockSequenceStart if block => {
                self.state = State::BlockSequenceFirstEntry;
                Ok(Event::SequenceStart {
                    anchor,
                    tag,
                    mark: begin,
                })
            }
            Kind::BlockMappingStart if block => {
                self.state = State::BlockMappingFirstKey;
                Ok(Event::MappingStart {
                    anchor,
                    tag,
                    mark: begin,
                })
            }
            found => {
                if anchor.is_some() || tag.is_some() {
                    // An empty scalar is allowed even if a tag or an anchor is given.
                    self.pop();
                    return Ok(Event::Scalar {
                        anchor,
                        tag,
                        implicit,
                        value: Cow::Borrowed(""),
                        lone: false,
                        mark: begin,
                    });
                }
                let what = if block { "block" } else { "flow" };
                let context = format!("while parsing a {what} node");
                Err(self.refuse(
                    Some((&context, begin)),
                    format!("expected the node content, but found '{}'", found.id()),
                ))
            }
        }
    }

    fn block_sequence_entry(&mut self) -> Step<'a> {
        if self.is(&[Kind::BlockEntry])? {
            let token = self.take()?;
            if !self.is(&[Kind::BlockEntry, Kind::BlockEnd])? {
                self.states.push(State::BlockSequenceEntry);
                return self.node(true, false);
            }
            self.state = State::BlockSequenceEntry;
            return Ok(Self::empty_scalar(token.end));
        }
        if !self.is(&[Kind::BlockEnd])? {
            let found = self.kind()?;
            let begin = self.marks.last().copied().unwrap_or_default();
            return Err(self.refuse(
                Some(("while parsing a block collection", begin)),
                format!("expected <block end>, but found '{}'", found.id()),
            ));
        }
        let _ = self.take()?;
        self.pop();
        self.marks.pop();
        Ok(Event::SequenceEnd)
    }

    fn indentless_sequence_entry(&mut self) -> Step<'a> {
        if self.is(&[Kind::BlockEntry])? {
            let token = self.take()?;
            if !self.is(&[Kind::BlockEntry, Kind::Key, Kind::Value, Kind::BlockEnd])? {
                self.states.push(State::IndentlessSequenceEntry);
                return self.node(true, false);
            }
            self.state = State::IndentlessSequenceEntry;
            return Ok(Self::empty_scalar(token.end));
        }
        self.pop();
        Ok(Event::SequenceEnd)
    }

    fn block_mapping_key(&mut self) -> Step<'a> {
        if self.is(&[Kind::Key])? {
            let token = self.take()?;
            if !self.is(&[Kind::Key, Kind::Value, Kind::BlockEnd])? {
                self.states.push(State::BlockMappingValue);
                return self.node(true, true);
            }
            self.state = State::BlockMappingValue;
            return Ok(Self::empty_scalar(token.end));
        }
        if !self.is(&[Kind::BlockEnd])? {
            let found = self.kind()?;
            let begin = self.marks.last().copied().unwrap_or_default();
            return Err(self.refuse(
                Some(("while parsing a block mapping", begin)),
                format!("expected <block end>, but found '{}'", found.id()),
            ));
        }
        let _ = self.take()?;
        self.pop();
        self.marks.pop();
        Ok(Event::MappingEnd)
    }

    fn block_mapping_value(&mut self) -> Step<'a> {
        if self.is(&[Kind::Value])? {
            let token = self.take()?;
            if !self.is(&[Kind::Key, Kind::Value, Kind::BlockEnd])? {
                self.states.push(State::BlockMappingKey);
                return self.node(true, true);
            }
            self.state = State::BlockMappingKey;
            return Ok(Self::empty_scalar(token.end));
        }
        self.state = State::BlockMappingKey;
        let mark = self.start()?;
        Ok(Self::empty_scalar(mark))
    }

    fn flow_sequence_entry(&mut self, first: bool) -> Step<'a> {
        if !self.is(&[Kind::FlowSequenceEnd])? {
            if !first {
                if self.is(&[Kind::FlowEntry])? {
                    let _ = self.take()?;
                } else {
                    let found = self.kind()?;
                    let begin = self.marks.last().copied().unwrap_or_default();
                    return Err(self.refuse(
                        Some(("while parsing a flow sequence", begin)),
                        format!("expected ',' or ']', but got '{}'", found.id()),
                    ));
                }
            }
            if self.is(&[Kind::Key])? {
                let mark = self.start()?;
                self.state = State::FlowSequenceEntryMappingKey;
                return Ok(Event::MappingStart {
                    anchor: None,
                    tag: None,
                    mark,
                });
            } else if !self.is(&[Kind::FlowSequenceEnd])? {
                self.states.push(State::FlowSequenceEntry);
                return self.node(false, false);
            }
        }
        let _ = self.take()?;
        self.pop();
        self.marks.pop();
        Ok(Event::SequenceEnd)
    }

    fn flow_sequence_entry_mapping_key(&mut self) -> Step<'a> {
        let token = self.take()?;
        if !self.is(&[Kind::Value, Kind::FlowEntry, Kind::FlowSequenceEnd])? {
            self.states.push(State::FlowSequenceEntryMappingValue);
            return self.node(false, false);
        }
        self.state = State::FlowSequenceEntryMappingValue;
        Ok(Self::empty_scalar(token.end))
    }

    fn flow_sequence_entry_mapping_value(&mut self) -> Step<'a> {
        if self.is(&[Kind::Value])? {
            let token = self.take()?;
            if !self.is(&[Kind::FlowEntry, Kind::FlowSequenceEnd])? {
                self.states.push(State::FlowSequenceEntryMappingEnd);
                return self.node(false, false);
            }
            self.state = State::FlowSequenceEntryMappingEnd;
            return Ok(Self::empty_scalar(token.end));
        }
        self.state = State::FlowSequenceEntryMappingEnd;
        let mark = self.start()?;
        Ok(Self::empty_scalar(mark))
    }

    fn flow_mapping_key(&mut self, first: bool) -> Step<'a> {
        if !self.is(&[Kind::FlowMappingEnd])? {
            if !first {
                if self.is(&[Kind::FlowEntry])? {
                    let _ = self.take()?;
                } else {
                    let found = self.kind()?;
                    let begin = self.marks.last().copied().unwrap_or_default();
                    return Err(self.refuse(
                        Some(("while parsing a flow mapping", begin)),
                        format!("expected ',' or '}}', but got '{}'", found.id()),
                    ));
                }
            }
            if self.is(&[Kind::Key])? {
                let token = self.take()?;
                if !self.is(&[Kind::Value, Kind::FlowEntry, Kind::FlowMappingEnd])? {
                    self.states.push(State::FlowMappingValue);
                    return self.node(false, false);
                }
                self.state = State::FlowMappingValue;
                return Ok(Self::empty_scalar(token.end));
            } else if !self.is(&[Kind::FlowMappingEnd])? {
                self.states.push(State::FlowMappingEmptyValue);
                return self.node(false, false);
            }
        }
        let _ = self.take()?;
        self.pop();
        self.marks.pop();
        Ok(Event::MappingEnd)
    }

    fn flow_mapping_value(&mut self) -> Step<'a> {
        if self.is(&[Kind::Value])? {
            let token = self.take()?;
            if !self.is(&[Kind::FlowEntry, Kind::FlowMappingEnd])? {
                self.states.push(State::FlowMappingKey);
                return self.node(false, false);
            }
            self.state = State::FlowMappingKey;
            return Ok(Self::empty_scalar(token.end));
        }
        self.state = State::FlowMappingKey;
        let mark = self.start()?;
        Ok(Self::empty_scalar(mark))
    }
}
