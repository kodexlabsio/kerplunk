//! Finding a repository's files: one walk of the tree, on every CPU, then any number of
//! questions over what it found (the files of an input, the directories a dbt project may be in);
//! and the record a build keeps of each file it read ([`files`]).
//!
//! The walk lists each directory once, as `os.walk` did: its subdirectories in name order (those
//! that are links, hidden or named in `skip` are neither entered nor listed) and its files in
//! name order, each file being a regular file or a link to one. A link is noted, so that a
//! project that does not trust its links can look at those and only those. Directories are read
//! by a pool of threads and put in order afterwards, so the answer does not depend on which
//! thread was first.

pub mod files;
pub mod glob;

#[cfg(feature = "python")]
pub(crate) mod python;

use std::collections::{HashMap, HashSet};
use std::ffi::{OsStr, OsString};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Condvar, Mutex};

use glob::Pattern;

/// A file in a directory.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct File {
    /// The name, without its directory.
    pub name: OsString,
    /// Whether the name is a link (to a regular file).
    pub link: bool,
}

/// A directory the walk read.
#[derive(Debug, Clone)]
pub struct Directory {
    /// The path from the root, empty for the root. Kept as the file system gave it, so a name
    /// that is not text still leads to its directory.
    pub path: PathBuf,
    /// Its files, in name order.
    pub files: Vec<File>,
}

/// The directories under a root, in the order `os.walk` yields them: a directory, then each of
/// its subdirectories in name order, each with what is under it.
#[derive(Debug)]
pub struct Tree {
    root: PathBuf,
    directories: Vec<Directory>,
}

/// What a question asks of a file's name.
pub enum Names {
    /// The name ends with one of these (`fold`: whatever the case of either; the suffixes are
    /// then lower case).
    Suffix { suffixes: Vec<String>, fold: bool },
    /// The name matches one of these shell patterns.
    Glob(Vec<Pattern>),
}

/// A directory's listing before it is put in order.
struct Listing {
    directories: Vec<OsString>,
    files: Vec<File>,
}

impl Tree {
    /// Walks `root`, entering no directory named in `skip` or beginning with a `.`.
    pub fn walk(root: &Path, skip: &HashSet<OsString>) -> Self {
        Self::walk_with(root, skip, false)
    }

    /// [`Tree::walk`], entering directories that begin with a `.` too when `hidden` is set.
    pub fn walk_with(root: &Path, skip: &HashSet<OsString>, hidden: bool) -> Self {
        let mut listings = walk_all(root, skip, hidden);
        let mut directories = Vec::with_capacity(listings.len());
        // An explicit stack, so a deep tree is not a deep call.
        let mut stack = vec![PathBuf::new()];
        while let Some(path) = stack.pop() {
            let Some(listing) = listings.remove(&path) else {
                continue; // a directory that could not be read is not in the walk
            };
            for name in listing.directories.iter().rev() {
                stack.push(path.join(name));
            }
            directories.push(Directory {
                path,
                files: listing.files,
            });
        }
        Self {
            root: root.to_path_buf(),
            directories,
        }
    }

    /// The directories, in walk order.
    pub fn directories(&self) -> &[Directory] {
        &self.directories
    }

    /// The root the paths are relative to.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// The path of `directory` from the filesystem root.
    pub fn full(&self, directory: &Directory) -> PathBuf {
        if directory.path.as_os_str().is_empty() {
            self.root.clone()
        } else {
            self.root.join(&directory.path)
        }
    }

    /// The files whose names `names` accepts, with their paths from the root of the filesystem
    /// and whether each is a link: in walk order, without the files of the directory `skip`
    /// (a path from the root, as `directories` gives it) and without a path that `exclude`
    /// matches.
    pub fn files(
        &self,
        names: &Names,
        exclude: &[Pattern],
        skip: Option<&Path>,
    ) -> Vec<(PathBuf, bool)> {
        let mut found = Vec::new();
        for directory in &self.directories {
            if skip == Some(directory.path.as_path()) {
                continue;
            }
            let base = self.full(directory);
            for file in &directory.files {
                // A name that is not text is matched as its lossy form, which no pattern a
                // user wrote for a real file name can tell from the original.
                let name = file.name.to_string_lossy();
                if accepts(names, &name) && !excluded(exclude, &directory.path, &name) {
                    found.push((base.join(&file.name), file.link));
                }
            }
        }
        found
    }
}

pub(crate) fn accepts(names: &Names, name: &str) -> bool {
    match names {
        Names::Suffix { suffixes, fold } => {
            if *fold {
                let lower = name.to_lowercase();
                suffixes.iter().any(|s| lower.ends_with(s.as_str()))
            } else {
                suffixes.iter().any(|s| name.ends_with(s.as_str()))
            }
        }
        Names::Glob(patterns) => patterns.iter().any(|p| p.matches(name)),
    }
}

/// Whether a pattern matches the path from the root, which patterns see as text with `/`
/// between the names.
fn excluded(patterns: &[Pattern], directory: &Path, name: &str) -> bool {
    if patterns.is_empty() {
        return false;
    }
    let mut path = String::new();
    for part in directory.components() {
        path.push_str(&part.as_os_str().to_string_lossy());
        path.push('/');
    }
    path.push_str(name);
    patterns.iter().any(|p| p.matches(&path))
}

/// How many threads read directories: the CPUs, up to eight, as the build's workers are.
fn threads() -> usize {
    std::thread::available_parallelism()
        .map_or(1, usize::from)
        .min(8)
}

/// The work shared by the threads.
struct Queue {
    waiting: Vec<PathBuf>,
    /// Directories being read, each of which may add to `waiting`.
    reading: usize,
}

fn walk_all(root: &Path, skip: &HashSet<OsString>, hidden: bool) -> HashMap<PathBuf, Listing> {
    let queue = Mutex::new(Queue {
        waiting: vec![PathBuf::new()],
        reading: 0,
    });
    let wake = Condvar::new();
    let listings = Mutex::new(HashMap::new());
    let work = || {
        loop {
            let path = {
                let mut state = queue.lock().expect("a directory reader panicked");
                loop {
                    if let Some(path) = state.waiting.pop() {
                        state.reading += 1;
                        break path;
                    }
                    if state.reading == 0 {
                        wake.notify_all();
                        return;
                    }
                    state = wake.wait(state).expect("a directory reader panicked");
                }
            };
            let full = if path.as_os_str().is_empty() {
                root.to_path_buf()
            } else {
                root.join(&path)
            };
            let listing = list(&full, skip, hidden);
            let mut state = queue.lock().expect("a directory reader panicked");
            if let Some(listing) = listing {
                state
                    .waiting
                    .extend(listing.directories.iter().map(|name| path.join(name)));
                listings
                    .lock()
                    .expect("a directory reader panicked")
                    .insert(path, listing);
            }
            state.reading -= 1;
            wake.notify_all();
        }
    };
    std::thread::scope(|scope| {
        for _ in 1..threads() {
            // A system that refuses a thread (a limit on processes, a container's) leaves the
            // walk to the threads it did give, the calling one at least: they take the whole
            // queue between them, so the answer is the same.
            if helper(scope, &work).is_err() {
                break;
            }
        }
        work();
    });
    listings.into_inner().expect("a directory reader panicked")
}

#[cfg(test)]
thread_local! {
    /// Makes [`helper`] fail, as a system out of threads does.
    static REFUSE_THREADS: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

fn helper<'scope, 'env, F>(
    scope: &'scope std::thread::Scope<'scope, 'env>,
    work: &'scope F,
) -> std::io::Result<()>
where
    F: Fn() + Sync,
{
    #[cfg(test)]
    if REFUSE_THREADS.with(std::cell::Cell::get) {
        return Err(std::io::Error::other("no threads"));
    }
    std::thread::Builder::new()
        .spawn_scoped(scope, work)
        .map(|_| ())
}

/// One directory, or `None` when it cannot be read.
fn list(directory: &Path, skip: &HashSet<OsString>, hidden: bool) -> Option<Listing> {
    let mut directories = Vec::new();
    let mut files = Vec::new();
    for entry in fs::read_dir(directory).ok()?.flatten() {
        let Ok(kind) = entry.file_type() else {
            continue;
        };
        let name = entry.file_name();
        if kind.is_dir() {
            if !skipped(&name, skip, hidden) {
                directories.push(name);
            }
        } else if kind.is_file() {
            files.push(File { name, link: false });
        } else if kind.is_symlink() {
            // `is_file` follows the link: one to a directory (which is not entered, as
            // `os.walk` does not follow links) or to nothing is not a file.
            if fs::metadata(entry.path()).is_ok_and(|m| m.is_file()) {
                files.push(File { name, link: true });
            }
        }
    }
    directories.sort_unstable_by(|a, b| a.as_encoded_bytes().cmp(b.as_encoded_bytes()));
    files.sort_unstable_by(|a, b| a.name.as_encoded_bytes().cmp(b.name.as_encoded_bytes()));
    Some(Listing { directories, files })
}

fn skipped(name: &OsStr, skip: &HashSet<OsString>, hidden: bool) -> bool {
    (!hidden && name.as_encoded_bytes().first() == Some(&b'.')) || skip.contains(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A directory under the system's temporary one, removed when dropped.
    pub(crate) struct Scratch(pub PathBuf);

    impl Scratch {
        pub(crate) fn new(label: &str) -> Self {
            let path = std::env::temp_dir()
                .join(format!("konstruct-discover-{label}-{}", std::process::id()));
            let _ = fs::remove_dir_all(&path);
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }

        pub(crate) fn file(&self, relative: &str) {
            let path = self.0.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, b"x").unwrap();
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn skip(names: &[&str]) -> HashSet<OsString> {
        names.iter().map(OsString::from).collect()
    }

    fn suffix(s: &str) -> Names {
        Names::Suffix {
            suffixes: vec![s.to_string()],
            fold: false,
        }
    }

    fn relative(tree: &Tree, found: &[(PathBuf, bool)]) -> Vec<String> {
        found
            .iter()
            .map(|(p, _)| {
                p.strip_prefix(tree.root())
                    .unwrap()
                    .to_string_lossy()
                    .into_owned()
            })
            .collect()
    }

    #[test]
    fn directories_come_in_walk_order_with_their_files_in_name_order() {
        let scratch = Scratch::new("order");
        for file in [
            "b/2.sql",
            "a/9.sql",
            "a/1.sql",
            "a/z/3.sql",
            "root.sql",
            "c.sql",
        ] {
            scratch.file(file);
        }
        let tree = Tree::walk(&scratch.0, &skip(&[]));
        let paths: Vec<_> = tree
            .directories()
            .iter()
            .map(|d| d.path.to_str().unwrap())
            .collect();
        assert_eq!(paths, ["", "a", "a/z", "b"]);
        let found = tree.files(&suffix(".sql"), &[], None);
        assert_eq!(
            relative(&tree, &found),
            [
                "c.sql",
                "root.sql",
                "a/1.sql",
                "a/9.sql",
                "a/z/3.sql",
                "b/2.sql"
            ]
        );
    }

    #[test]
    fn hidden_and_skipped_directories_are_not_entered() {
        let scratch = Scratch::new("skip");
        for file in [
            ".git/x.sql",
            "node_modules/y.sql",
            "keep/z.sql",
            ".hidden.sql",
        ] {
            scratch.file(file);
        }
        let tree = Tree::walk(&scratch.0, &skip(&["node_modules"]));
        let found = tree.files(&suffix(".sql"), &[], None);
        assert_eq!(relative(&tree, &found), [".hidden.sql", "keep/z.sql"]);
    }

    #[test]
    fn exclude_matches_the_path_from_the_root_and_skip_drops_one_directorys_files() {
        let scratch = Scratch::new("exclude");
        for file in ["a/x.sql", "b/x.sql", "b/c/x.sql"] {
            scratch.file(file);
        }
        let tree = Tree::walk(&scratch.0, &skip(&[]));
        let exclude = [Pattern::new("a/*", false)];
        let found = tree.files(&suffix(".sql"), &exclude, None);
        assert_eq!(relative(&tree, &found), ["b/x.sql", "b/c/x.sql"]);
        let found = tree.files(&suffix(".sql"), &[], Some(Path::new("b")));
        assert_eq!(relative(&tree, &found), ["a/x.sql", "b/c/x.sql"]);
    }

    #[test]
    fn names_by_pattern_and_by_suffix_without_case() {
        let scratch = Scratch::new("names");
        for file in ["dags.yml", "Meltano.YML", "other.txt"] {
            scratch.file(file);
        }
        let tree = Tree::walk(&scratch.0, &skip(&[]));
        let globbed = Names::Glob(vec![Pattern::new("dags.*", false)]);
        assert_eq!(
            relative(&tree, &tree.files(&globbed, &[], None)),
            ["dags.yml"]
        );
        let folded = Names::Suffix {
            suffixes: vec![".yml".to_string()],
            fold: true,
        };
        assert_eq!(
            relative(&tree, &tree.files(&folded, &[], None)),
            ["Meltano.YML", "dags.yml"]
        );
    }

    #[cfg(unix)]
    #[test]
    fn links_to_files_are_noted_and_links_to_directories_or_nothing_are_not_files() {
        use std::os::unix::fs::symlink;
        let scratch = Scratch::new("links");
        scratch.file("real/a.sql");
        scratch.file("target.sql");
        symlink(scratch.0.join("target.sql"), scratch.0.join("link.sql")).unwrap();
        symlink(scratch.0.join("real"), scratch.0.join("dirlink.sql")).unwrap();
        symlink(scratch.0.join("missing"), scratch.0.join("dead.sql")).unwrap();
        let tree = Tree::walk(&scratch.0, &skip(&[]));
        let found = tree.files(&suffix(".sql"), &[], None);
        let links: Vec<_> = found.iter().map(|(_, link)| *link).collect();
        assert_eq!(
            relative(&tree, &found),
            ["link.sql", "target.sql", "real/a.sql"]
        );
        assert_eq!(links, [true, false, false]);
        let paths: Vec<_> = tree
            .directories()
            .iter()
            .map(|d| d.path.to_str().unwrap())
            .collect();
        assert_eq!(paths, ["", "real"]);
    }

    #[test]
    fn a_wide_tree_is_the_same_whatever_thread_read_what() {
        let scratch = Scratch::new("wide");
        for d in 0..40 {
            for f in 0..5 {
                scratch.file(&format!("d{d:02}/s/f{f}.py"));
            }
        }
        let first = Tree::walk(&scratch.0, &skip(&[]));
        let again = Tree::walk(&scratch.0, &skip(&[]));
        let names = suffix(".py");
        assert_eq!(
            first.files(&names, &[], None),
            again.files(&names, &[], None)
        );
        assert_eq!(first.files(&names, &[], None).len(), 200);
        assert_eq!(first.directories().len(), 1 + 80);
    }

    #[cfg(unix)]
    #[test]
    fn names_that_are_not_text_are_walked_and_found() {
        use std::os::unix::ffi::OsStrExt;
        let scratch = Scratch::new("bytes");
        let odd_directory = OsStr::from_bytes(b"d\xff");
        let odd_file = OsStr::from_bytes(b"\xfe.sql");
        for relative in [
            Path::new(odd_directory).join("x.sql"),
            Path::new(odd_directory).join("inner").join("y.sql"),
            PathBuf::from("plain").join(odd_file),
        ] {
            let path = scratch.0.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, b"x").unwrap();
        }
        let tree = Tree::walk(&scratch.0, &skip(&[]));
        let found: Vec<_> = tree
            .files(&suffix(".sql"), &[], None)
            .into_iter()
            .map(|(path, _)| path.strip_prefix(tree.root()).unwrap().to_path_buf())
            .collect();
        assert_eq!(
            found,
            [
                Path::new(odd_directory).join("x.sql"),
                Path::new(odd_directory).join("inner").join("y.sql"),
                PathBuf::from("plain").join(odd_file),
            ]
        );
    }

    #[test]
    fn a_system_that_refuses_threads_gets_the_same_walk_from_the_calling_one() {
        let scratch = Scratch::new("nothreads");
        for d in 0..20 {
            scratch.file(&format!("d{d:02}/s/f.py"));
        }
        let expected = Tree::walk(&scratch.0, &skip(&[]));
        REFUSE_THREADS.with(|refuse| refuse.set(true));
        let alone = Tree::walk(&scratch.0, &skip(&[]));
        REFUSE_THREADS.with(|refuse| refuse.set(false));
        let names = suffix(".py");
        assert_eq!(alone.files(&names, &[], None).len(), 20);
        assert_eq!(
            alone.files(&names, &[], None),
            expected.files(&names, &[], None)
        );
    }

    #[test]
    fn hidden_directories_are_entered_when_asked() {
        let scratch = Scratch::new("hidden");
        for file in [".h/x.sql", "node_modules/y.sql", "k/z.sql"] {
            scratch.file(file);
        }
        let tree = Tree::walk_with(&scratch.0, &skip(&["node_modules"]), true);
        let found = tree.files(&suffix(".sql"), &[], None);
        assert_eq!(relative(&tree, &found), [".h/x.sql", "k/z.sql"]);
    }
}
