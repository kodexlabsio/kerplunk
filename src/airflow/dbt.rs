//! The dbt commands in a shell command, as a task that runs `dbt run --select staging` states
//! them: split on the shell's separators and into words as `shlex` does, and read from each
//! `dbt <command>` its selection flags.

use crate::pysyntax::text;

/// One `dbt <command>` and what it selects.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DbtInvocation {
    pub command: String,
    pub selectors: Vec<String>,
    pub excludes: Vec<String>,
    pub project_dir: Option<String>,
    pub unsupported: Vec<String>,
}

pub const DBT_COMMANDS: [&str; 6] = ["run", "build", "test", "seed", "snapshot", "compile"];

/// The parts of `command` between `&&`, `||`, `;`, a line end and `|`.
fn segments(command: &str) -> Vec<&str> {
    let bytes = command.as_bytes();
    let mut parts = Vec::new();
    let (mut start, mut at) = (0, 0);
    while at < bytes.len() {
        let width = match bytes[at] {
            b'&' if bytes.get(at + 1) == Some(&b'&') => 2,
            b'|' if bytes.get(at + 1) == Some(&b'|') => 2,
            b'|' | b';' | b'\n' => 1,
            _ => 0,
        };
        if width == 0 {
            at += 1;
        } else {
            parts.push(&command[start..at]);
            at += width;
            start = at;
        }
    }
    parts.push(&command[start..]);
    parts
}

#[derive(Clone, Copy, PartialEq)]
enum State {
    Space,
    Word,
    Quote(char),
    Escape(char),
}

/// `shlex.split(text, posix=True)`: `None` where it raises `ValueError`, for an unclosed quote or
/// a trailing escape.
fn shlex_split(text: &str) -> Option<Vec<String>> {
    let mut words = Vec::new();
    let mut token = String::new();
    let mut state = State::Space;
    let mut quoted = false;
    // The state an escape returns to: the word, or the double quote it was inside.
    let mut escaped = State::Space;
    let mut chars = text.chars();
    loop {
        let next = chars.next();
        match state {
            State::Space | State::Word => match next {
                None => {
                    if state == State::Word && (!token.is_empty() || quoted) {
                        words.push(std::mem::take(&mut token));
                    }
                    return Some(words);
                }
                Some(' ' | '\t' | '\r' | '\n') => {
                    state = State::Space;
                    if !token.is_empty() || quoted {
                        words.push(std::mem::take(&mut token));
                        quoted = false;
                    }
                }
                Some('\\') => {
                    escaped = State::Word;
                    state = State::Escape('\\');
                }
                Some(c @ ('\'' | '"')) => state = State::Quote(c),
                Some(c) => {
                    token.push(c);
                    state = State::Word;
                }
            },
            State::Quote(quote) => {
                quoted = true;
                match next {
                    None => return None,
                    Some(c) if c == quote => state = State::Word,
                    Some('\\') if quote == '"' => {
                        escaped = State::Quote(quote);
                        state = State::Escape('\\');
                    }
                    Some(c) => token.push(c),
                }
            }
            State::Escape(escape) => {
                let c = next?;
                if let State::Quote(quote) = escaped
                    && c != quote
                    && c != escape
                {
                    token.push(escape);
                }
                token.push(c);
                state = escaped;
            }
        }
    }
}

/// `Path(word).name`: the last component, as a path with `/` for its separator reads it.
fn file_name(word: &str) -> &str {
    let separators: &[char] = if cfg!(windows) { &['/', '\\'] } else { &['/'] };
    word.rsplit(separators)
        .find(|part| !part.is_empty() && *part != ".")
        .unwrap_or("")
}

#[derive(Clone, Copy)]
enum Collect {
    Nothing,
    Selectors,
    Excludes,
}

fn is_select(flag: &str) -> bool {
    matches!(flag, "--select" | "-s" | "--models" | "-m")
}

/// The `dbt <command>` invocations in a shell command, with their selection flags.
pub fn parse_dbt_commands(command: &str) -> Vec<DbtInvocation> {
    let mut invocations = Vec::new();
    for segment in segments(command) {
        let words = shlex_split(segment)
            .unwrap_or_else(|| text::split_whitespace(segment).map(str::to_owned).collect());
        for (index, word) in words.iter().enumerate() {
            if file_name(word) != "dbt" || index + 1 >= words.len() {
                continue;
            }
            let rest = &words[index + 1..];
            let subcommand = rest.iter().find(|w| !w.starts_with('-'));
            let Some(subcommand) = subcommand.filter(|s| DBT_COMMANDS.contains(&s.as_str())) else {
                break;
            };
            invocations.push(invocation(subcommand, rest));
            break;
        }
    }
    invocations
}

fn invocation(subcommand: &str, rest: &[String]) -> DbtInvocation {
    let mut found = DbtInvocation {
        command: subcommand.to_owned(),
        ..DbtInvocation::default()
    };
    let mut current = Collect::Nothing;
    let mut position = 0;
    while position < rest.len() {
        let word = &rest[position];
        let (flag, inline) = match word.split_once('=') {
            Some((flag, inline)) => (flag, inline),
            None => (word.as_str(), ""),
        };
        if is_select(flag) {
            current = Collect::Selectors;
        } else if flag == "--exclude" {
            current = Collect::Excludes;
        } else if flag == "--project-dir" && position + 1 < rest.len() {
            found.project_dir = Some(if inline.is_empty() {
                rest[position + 1].clone()
            } else {
                inline.to_owned()
            });
            current = Collect::Nothing;
            if inline.is_empty() {
                position += 1;
            }
        } else if matches!(flag, "--selector" | "--state" | "--defer") || flag.starts_with("state:")
        {
            found.unsupported.push(word.clone());
            current = Collect::Nothing;
        } else if word.starts_with('-') {
            current = Collect::Nothing;
        } else if !matches!(current, Collect::Nothing) {
            if word.starts_with("state:")
                || word.starts_with("source_status:")
                || word.starts_with("result:")
            {
                found.unsupported.push(word.clone());
            } else {
                collect(&mut found, current).push(word.clone());
            }
        }
        position += 1;
        if !inline.is_empty()
            && (is_select(flag) || flag == "--exclude")
            && !matches!(current, Collect::Nothing)
        {
            collect(&mut found, current).push(inline.to_owned());
        }
    }
    found
}

fn collect(found: &mut DbtInvocation, which: Collect) -> &mut Vec<String> {
    match which {
        Collect::Excludes => &mut found.excludes,
        _ => &mut found.selectors,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn one(command: &str) -> DbtInvocation {
        parse_dbt_commands(command).remove(0)
    }

    #[test]
    fn a_command_is_split_where_the_shell_splits_it() {
        assert_eq!(
            segments("a && b || c; d\ne | f & g"),
            ["a ", " b ", " c", " d", "e ", " f & g"]
        );
    }

    #[test]
    fn words_are_split_as_shlex_splits_them() {
        let split = |text| shlex_split(text);
        assert_eq!(
            split("dbt run --select 'a b' \"c d\" e\\ f"),
            Some(
                vec!["dbt", "run", "--select", "a b", "c d", "e f"]
                    .into_iter()
                    .map(String::from)
                    .collect()
            )
        );
        assert_eq!(
            split("echo ''"),
            Some(vec!["echo".to_string(), String::new()])
        );
        assert_eq!(split("a\"b c\"d"), Some(vec!["ab cd".to_string()]));
        assert_eq!(split("\"a\\$b\\\"c\""), Some(vec!["a\\$b\"c".to_string()]));
        assert_eq!(split("a 'b"), None);
        assert_eq!(split("a \\"), None);
        assert_eq!(split("  "), Some(vec![]));
    }

    #[test]
    fn selection_flags_are_read() {
        let found = one("cd /opt && dbt run --select staging customers --exclude tag:slow -x");
        assert_eq!(found.command, "run");
        assert_eq!(found.selectors, ["staging", "customers"]);
        assert_eq!(found.excludes, ["tag:slow"]);
        let found = one("/usr/bin/dbt build -s=a --project-dir=/x state:modified+ --state target");
        assert_eq!(found.selectors, ["a"]);
        assert_eq!(found.project_dir.as_deref(), Some("/x"));
        assert_eq!(found.unsupported, ["state:modified+", "--state"]);
        assert!(parse_dbt_commands("dbt deps").is_empty());
        assert!(parse_dbt_commands("echo dbt").is_empty());
    }
}
