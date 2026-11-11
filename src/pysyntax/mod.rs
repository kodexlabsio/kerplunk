//! Python source read as a syntax tree and never run: the foundation of the readers of Airflow
//! DAG files and Dagster definitions ([`crate::airflow`], [`crate::dagster`]).
//!
//! The tree comes from Ruff's parser, which is written in Rust, handles the syntax of every
//! Python from 3.7 to 3.15 and only ever builds a tree: nothing in it imports a module, evaluates
//! an expression or reaches the file system. What the readers see of it is what Python's own `ast`
//! module shows, so [`walk`] orders the nodes as `ast.walk` does, [`value`] folds constants as
//! Python holds them and [`text`] has the string rules the readers depend on.

pub mod nodes;
#[cfg(feature = "python")]
pub mod python;
pub mod text;
pub mod value;
pub mod walk;

use std::borrow::Cow;
use std::cell::Cell;
use std::rc::Rc;
use std::sync::mpsc::{RecvError, Sender};
use std::sync::{Arc, Mutex, OnceLock};

use ruff_python_ast::ModModule;
use ruff_python_ast::token::TokenKind;
use ruff_python_parser::{LexicalErrorType, Mode, ParseErrorType, lexer};
use ruff_text_size::TextSize;

/// A file that cannot be read as Python: what Python's `SyntaxError` carries.
#[derive(Debug, PartialEq, Eq)]
pub struct Failure {
    pub message: String,
    pub line: usize,
    /// Whether Python would raise `IndentationError`, a kind of `SyntaxError`.
    pub indentation: bool,
}

impl Failure {
    pub fn new(message: impl Into<String>, line: usize) -> Self {
        Self {
            message: message.into(),
            line,
            indentation: false,
        }
    }

    /// The failure of a file whose expressions nest deeper than the readers follow.
    pub fn too_deep() -> Self {
        Self::new("expressions are nested too deeply to read", 1)
    }
}

/// How deep the readers follow an expression, as Python's own recursion limit stops them.
const MAX_DEPTH: usize = 1000;

/// The depth of the walk a reader is in, which refuses a tree nested deeper than it can follow
/// without exhausting the stack. A reader asks [`Depth::enter`] at each level of a recursion and
/// finishes with [`Depth::exceeded`].
#[derive(Clone, Default)]
pub struct Depth {
    level: Rc<Cell<usize>>,
    exceeded: Rc<Cell<bool>>,
}

/// One level of depth, given back when it is dropped.
pub struct Level(Rc<Cell<usize>>);

impl Drop for Level {
    fn drop(&mut self) {
        self.0.set(self.0.get() - 1);
    }
}

impl Depth {
    /// One level deeper, or `None` when that is too deep, which [`Depth::exceeded`] remembers.
    pub fn enter(&self) -> Option<Level> {
        if self.level.get() >= MAX_DEPTH {
            self.exceeded.set(true);
            return None;
        }
        self.level.set(self.level.get() + 1);
        Some(Level(Rc::clone(&self.level)))
    }

    /// Whether a level was refused.
    pub fn exceeded(&self) -> bool {
        self.exceeded.get()
    }
}

/// `text` as Python reads a file: `\r\n` and a lone `\r` end a line like `\n` does.
pub fn universal_newlines(text: &str) -> Cow<'_, str> {
    if text.contains('\r') {
        Cow::Owned(text.replace("\r\n", "\n").replace('\r', "\n"))
    } else {
        Cow::Borrowed(text)
    }
}

/// The text of a file with the line ends Python's tokenizer sees, and where each line starts.
pub struct Source {
    text: String,
    line_starts: Vec<u32>,
}

impl Source {
    /// Reads `raw` as Python does: `\r\n` and a lone `\r` end a line like `\n` does, in a string
    /// literal as well.
    pub fn new(raw: &str) -> Self {
        let text = universal_newlines(raw);
        let mut line_starts = vec![0u32];
        line_starts.extend(
            text.bytes()
                .enumerate()
                .filter(|&(_, byte)| byte == b'\n')
                .map(|(at, _)| u32::try_from(at + 1).unwrap_or(u32::MAX)),
        );
        Self {
            text: text.into_owned(),
            line_starts,
        }
    }

    pub fn text(&self) -> &str {
        &self.text
    }

    /// The line, counted from 1, that holds `offset`.
    pub fn line(&self, offset: TextSize) -> usize {
        self.line_starts
            .partition_point(|&start| start <= offset.to_u32())
    }
}

/// The bracket depth beyond which Python itself refuses a file.
const MAX_BRACKETS: usize = 200;
/// The longest run of prefix operators that a large file may hold. The parser grows its stack on
/// the heap, so what a run costs is memory, about 4 KiB for each operator.
const MAX_CHAIN: usize = 5000;
/// The most `**`, `lambda` and `else` that may follow one another, up to a comma, a bracket or the
/// end of a line, in a large file: they nest to the right, so each is another level of the parser's
/// stack, at about 4 KiB. The old reader stopped at about this many.
const MAX_RIGHT_NESTED: usize = 20_000;
/// What the parser may take of the stack for each level of nesting, with room to spare (it takes
/// about 4 KiB for a prefix operator).
const STACK_PER_LEVEL: usize = 16 << 10;

/// Refuses a file whose nesting could exhaust the parser's stack. A file of `n` bytes cannot nest
/// deeper than `n` levels, so [`parse`] looks only at files too large for the stack to be sure.
fn check_nesting(text: &str) -> Result<(), Failure> {
    let mut lexer = lexer::lex(text, Mode::Module);
    let (mut depth, mut prefixes, mut rights) = (0usize, 0usize, 0usize);
    loop {
        let kind = lexer.next_token();
        match kind {
            TokenKind::EndOfFile => return Ok(()),
            TokenKind::Lpar | TokenKind::Lsqb | TokenKind::Lbrace => depth += 1,
            TokenKind::Rpar | TokenKind::Rsqb | TokenKind::Rbrace => {
                depth = depth.saturating_sub(1);
            }
            _ => {}
        }
        match kind {
            TokenKind::Minus
            | TokenKind::Plus
            | TokenKind::Tilde
            | TokenKind::Not
            | TokenKind::Await => prefixes += 1,
            TokenKind::NonLogicalNewline | TokenKind::Comment => {}
            _ => prefixes = 0,
        }
        match kind {
            TokenKind::DoubleStar | TokenKind::Lambda | TokenKind::Else => rights += 1,
            TokenKind::Comma
            | TokenKind::Lpar
            | TokenKind::Rpar
            | TokenKind::Lsqb
            | TokenKind::Rsqb
            | TokenKind::Lbrace
            | TokenKind::Rbrace
            | TokenKind::Newline
            | TokenKind::NonLogicalNewline => rights = 0,
            _ => {}
        }
        if depth > MAX_BRACKETS || prefixes > MAX_CHAIN || rights > MAX_RIGHT_NESTED {
            return Err(Failure::too_deep());
        }
    }
}

/// Parses `source`. `stack` is the size of the stack the caller runs on (see [`with_deep_stack`]).
pub fn parse(source: &Source, stack: usize) -> Result<ModModule, Failure> {
    let text = source.text();
    if text.contains('\0') {
        return Err(Failure::new(
            "source code string cannot contain null bytes",
            1,
        ));
    }
    if text.len() > stack / STACK_PER_LEVEL {
        check_nesting(text)?;
    }
    match ruff_python_parser::parse_module(text) {
        Ok(parsed) => Ok(parsed.into_syntax()),
        Err(error) => {
            let message = error.error.to_string();
            let indentation = matches!(
                error.error,
                ParseErrorType::UnexpectedIndentation
                    | ParseErrorType::Lexical(LexicalErrorType::IndentationError)
            ) || message.starts_with("Expected an indented block");
            Err(Failure {
                message,
                line: source.line(error.location.start()),
                indentation,
            })
        }
    }
}

/// A job for a thread with a large stack, given the size of the stack.
type Job = Box<dyn FnOnce(usize) + Send>;

/// The queue of the threads that run [`with_deep_stack`]'s work.
struct Pool {
    jobs: Mutex<Sender<Job>>,
}

/// Starts the threads, each with the largest stack the system lets it have, or `None` when it
/// lets none of them have one that is any use.
fn start_pool() -> Option<Pool> {
    const STACKS: [usize; 3] = [512 << 20, 128 << 20, 32 << 20];
    let (sender, receiver) = std::sync::mpsc::channel::<Job>();
    let receiver = Arc::new(Mutex::new(receiver));
    let threads = std::thread::available_parallelism().map_or(1, |n| n.get().min(4));
    let mut chosen = None;
    for _ in 0..threads {
        for stack in STACKS
            .into_iter()
            .filter(|s| chosen.is_none_or(|c| *s <= c))
        {
            let receiver = Arc::clone(&receiver);
            let spawned = std::thread::Builder::new()
                .name("konstruct-python".into())
                .stack_size(stack)
                .spawn(move || {
                    while let Ok(job) = receiver
                        .lock()
                        .map(|queue| queue.recv())
                        .unwrap_or(Err(RecvError))
                    {
                        job(stack);
                    }
                });
            if spawned.is_ok() {
                chosen = Some(stack);
                break;
            }
        }
    }
    chosen.map(|_| Pool {
        jobs: Mutex::new(sender),
    })
}

/// Runs `work` on a thread with a large stack, giving it the stack's size. The parser and the
/// walks over a tree recurse once for each level of nesting, and dropping a tree does too, so a
/// file built to nest deeply could otherwise overflow the stack of the calling thread. The stack
/// is memory the process reserves and the kernel commits only as it is used; the threads are
/// kept, as making one for each file costs far more than reading it.
pub fn with_deep_stack<T: Send + 'static>(work: impl FnOnce(usize) -> T + Send + 'static) -> T {
    static POOL: OnceLock<Option<Pool>> = OnceLock::new();
    let Some(pool) = POOL.get_or_init(start_pool) else {
        // No thread could be made: the caller's own stack is what there is, and `parse` refuses
        // what that could not hold.
        return work(1 << 20);
    };
    let (done, result) = std::sync::mpsc::channel();
    let job: Job = Box::new(move |stack| {
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| work(stack)));
        // The caller is waiting for this; if it is gone there is nothing to tell.
        let _ = done.send(outcome);
    });
    let sent = pool
        .jobs
        .lock()
        .map(|jobs| jobs.send(job).is_ok())
        .unwrap_or(false);
    match sent.then(|| result.recv()) {
        Some(Ok(Ok(done))) => done,
        Some(Ok(Err(panic))) => std::panic::resume_unwind(panic),
        _ => panic!("the threads that read Python stopped"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lines_count_as_python_counts_them() {
        let source = Source::new("a = 1\r\nb = '''x\ry'''\n");
        assert_eq!(source.text(), "a = 1\nb = '''x\ny'''\n");
        assert_eq!(source.line(TextSize::new(0)), 1);
        assert_eq!(source.line(TextSize::new(6)), 2);
        assert_eq!(source.line(TextSize::new(16)), 3);
    }

    #[test]
    fn a_file_is_read_or_refused_with_its_line() {
        with_deep_stack(|stack| {
            assert!(parse(&Source::new("x = 1\n"), stack).is_ok());
            let failure =
                parse(&Source::new("x = 1\nwith y as z:\n    oops(\n"), stack).unwrap_err();
            assert!(failure.line >= 2, "{failure:?}");
            let nul = parse(&Source::new("x = 1\0\n"), stack).unwrap_err();
            assert!(nul.message.contains("null"));
        });
    }

    #[test]
    fn nesting_beyond_what_python_reads_is_refused_before_it_is_parsed() {
        let deep = format!("x = {}1{}\n", "(".repeat(300), ")".repeat(300));
        assert_eq!(check_nesting(&deep), Err(Failure::too_deep()));
        assert_eq!(check_nesting("x = ((1))\n"), Ok(()));
        let minus = format!("x = {}1\n", "-".repeat(MAX_CHAIN + 1));
        assert_eq!(check_nesting(&minus), Err(Failure::too_deep()));
    }

    #[test]
    fn long_lists_of_ternaries_and_lambdas_and_chains_of_them_are_not_refused() {
        let ternaries = format!(
            "x = [{}]\n",
            "{'id': 1, 'v': 1 if a else 2}, ".repeat(9_000)
        );
        assert_eq!(check_nesting(&ternaries), Ok(()));
        let lambdas = format!("x = [{}]\n", "lambda: 1, ".repeat(9_000));
        assert_eq!(check_nesting(&lambdas), Ok(()));
        let chained = format!("x = {}3\n", "1 if 2 else ".repeat(20_000));
        assert_eq!(check_nesting(&chained), Ok(()));
    }

    #[test]
    fn a_hostile_right_nested_chain_is_refused_but_real_lists_of_them_are_not() {
        let powers = format!("x = a{}\n", "**a".repeat(MAX_RIGHT_NESTED + 1));
        assert_eq!(check_nesting(&powers), Err(Failure::too_deep()));
        let lambdas = format!("x = {}1\n", "lambda: ".repeat(MAX_RIGHT_NESTED + 1));
        assert_eq!(check_nesting(&lambdas), Err(Failure::too_deep()));
        let ternaries = format!("x = {}3\n", "1 if 2 else ".repeat(MAX_RIGHT_NESTED + 1));
        assert_eq!(check_nesting(&ternaries), Err(Failure::too_deep()));
        let listed = format!("x = [{}]\n", "a**b, ".repeat(100_000));
        assert_eq!(check_nesting(&listed), Ok(()));
    }

    #[test]
    fn indentation_errors_are_told_apart() {
        with_deep_stack(|stack| {
            for text in [
                "x = 1\n  y = 2\n",
                "if a:\npass\n",
                "if a:\n        x\n    y\n",
            ] {
                let failure = parse(&Source::new(text), stack).unwrap_err();
                assert!(failure.indentation, "{text:?}: {failure:?}");
            }
            assert!(
                !parse(&Source::new("x = (\n"), stack)
                    .unwrap_err()
                    .indentation
            );
        });
    }

    #[test]
    fn a_long_run_of_prefix_operators_is_refused_or_read_but_never_overflows_the_stack() {
        for count in [1_000, 30_000, 120_000, 400_000] {
            let text = format!("x = {}1\n", "-".repeat(count));
            let outcome = with_deep_stack(move |stack| {
                let source = Source::new(&text);
                parse(&source, stack).map(|_| ())
            });
            if count > 100_000 {
                assert_eq!(outcome, Err(Failure::too_deep()), "{count}");
            } else {
                assert_eq!(outcome, Ok(()), "{count}");
            }
        }
    }

    #[test]
    fn the_work_runs_on_a_deep_stack_and_its_panics_come_back() {
        assert!(with_deep_stack(|stack| stack >= 32 << 20));
        let panicked = std::panic::catch_unwind(|| with_deep_stack(|_| panic!("failed")));
        assert!(panicked.is_err());
    }
}
