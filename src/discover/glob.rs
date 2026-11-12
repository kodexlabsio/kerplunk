//! Shell patterns as Python's `fnmatch` reads them: `*` matches any run of characters, `/`
//! included, `?` any one, `[abc]`, `[a-z]` and `[!abc]` a set, and a `[` with no closing `]` is
//! itself. Matching is over characters, not bytes, and a pattern is read once and used for every
//! name, so a repository of a hundred thousand files costs a pass over the names.

/// One step of a pattern.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Step {
    Char(char),
    One,
    Run,
    Set {
        negated: bool,
        ranges: Vec<(char, char)>,
    },
}

/// A pattern read once.
#[derive(Debug, Clone)]
pub struct Pattern {
    steps: Vec<Step>,
    fold: bool,
}

impl Pattern {
    /// Reads `pattern`. With `fold`, letters match whatever their case, as `fnmatch` does where
    /// file names do not distinguish them.
    pub fn new(pattern: &str, fold: bool) -> Self {
        let text: Vec<char> = if fold {
            pattern.chars().flat_map(char::to_lowercase).collect()
        } else {
            pattern.chars().collect()
        };
        let mut steps = Vec::new();
        let mut at = 0;
        while at < text.len() {
            let c = text[at];
            at += 1;
            match c {
                '*' => {
                    // Several in a row are one, and one is all the matcher needs.
                    if steps.last() != Some(&Step::Run) {
                        steps.push(Step::Run);
                    }
                }
                '?' => steps.push(Step::One),
                '[' => {
                    let mut end = at;
                    if text.get(end) == Some(&'!') {
                        end += 1;
                    }
                    if text.get(end) == Some(&']') {
                        end += 1;
                    }
                    while end < text.len() && text[end] != ']' {
                        end += 1;
                    }
                    if end >= text.len() {
                        steps.push(Step::Char('['));
                    } else {
                        steps.push(set(&text[at..end]));
                        at = end + 1;
                    }
                }
                other => steps.push(Step::Char(other)),
            }
        }
        Self { steps, fold }
    }

    /// Whether the whole of `name` matches.
    pub fn matches(&self, name: &str) -> bool {
        if self.fold {
            let folded: Vec<char> = name.chars().flat_map(char::to_lowercase).collect();
            self.run(&folded)
        } else {
            let chars: Vec<char> = name.chars().collect();
            self.run(&chars)
        }
    }

    /// Greedy matching with one place to go back to: the last `*` takes one more character each
    /// time the rest fails, so a pattern never costs more than its length times the name's.
    fn run(&self, name: &[char]) -> bool {
        let (mut s, mut n) = (0, 0);
        let mut back: Option<(usize, usize)> = None;
        while n < name.len() {
            match self.steps.get(s) {
                Some(Step::Run) => {
                    back = Some((s, n));
                    s += 1;
                    continue;
                }
                Some(step) if step_takes(step, name[n]) => {
                    s += 1;
                    n += 1;
                    continue;
                }
                _ => {}
            }
            match back {
                Some((star, taken)) => {
                    s = star + 1;
                    n = taken + 1;
                    back = Some((star, taken + 1));
                }
                None => return false,
            }
        }
        self.steps[s..].iter().all(|step| *step == Step::Run)
    }
}

fn step_takes(step: &Step, c: char) -> bool {
    match step {
        Step::Char(expected) => *expected == c,
        Step::One => true,
        Step::Run => false,
        Step::Set { negated, ranges } => {
            ranges.iter().any(|&(low, high)| low <= c && c <= high) != *negated
        }
    }
}

/// The set written between `[` and `]`: a leading `!` negates it, a `-` between two characters
/// makes a range (one that runs backwards matches nothing), and a `-` at either end is itself.
fn set(body: &[char]) -> Step {
    let (negated, body) = match body.split_first() {
        Some(('!', rest)) => (true, rest),
        _ => (false, body),
    };
    let mut ranges = Vec::new();
    let mut at = 0;
    while at < body.len() {
        if at + 2 < body.len() && body[at + 1] == '-' {
            ranges.push((body[at], body[at + 2]));
            at += 3;
        } else {
            ranges.push((body[at], body[at]));
            at += 1;
        }
    }
    Step::Set { negated, ranges }
}

#[cfg(test)]
mod tests {
    use super::Pattern;

    fn yes(pattern: &str, name: &str) -> bool {
        Pattern::new(pattern, false).matches(name)
    }

    #[test]
    fn star_runs_over_slashes_and_may_be_empty() {
        assert!(yes("*.sql", "a.sql"));
        assert!(yes("*.sql", ".sql"));
        assert!(yes("models/*", "models/a/b.sql"));
        assert!(!yes("*.sql", "a.sqlx"));
        assert!(yes("a*b*c", "aXXbYYc"));
        assert!(!yes("a*b*c", "aXXbYY"));
    }

    #[test]
    fn question_mark_takes_exactly_one_character() {
        assert!(yes("a?c", "abc"));
        assert!(!yes("a?c", "ac"));
        assert!(yes("?", "é"));
    }

    #[test]
    fn sets_ranges_and_negation() {
        assert!(yes("[abc]x", "bx"));
        assert!(!yes("[!abc]x", "bx"));
        assert!(yes("[!abc]x", "dx"));
        assert!(yes("[a-c]x", "bx"));
        assert!(!yes("[a-c]x", "dx"));
        assert!(yes("[]]", "]"));
        assert!(yes("[a-]", "-"));
    }

    #[test]
    fn a_bracket_with_no_close_is_itself() {
        assert!(yes("a[b", "a[b"));
        assert!(!yes("a[b", "ab"));
    }

    #[test]
    fn a_pattern_of_stars_does_not_blow_up() {
        let name = "a".repeat(5000);
        assert!(!yes("a*a*a*a*a*a*a*b", &name));
    }

    #[test]
    fn folding_ignores_case_in_both() {
        assert!(Pattern::new("*.SQL", true).matches("Model.sql"));
        assert!(!Pattern::new("*.SQL", false).matches("Model.sql"));
    }
}
