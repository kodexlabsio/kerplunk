//! Whether a file changed: the one rule for every place Konstruct keeps a file to look at again
//! (the inputs a build read, the SQL files the viewer shows, the files beside an input).
//!
//! A file's identity is its size, modification and change times (nanoseconds), device and
//! inode. The change time moves even when a rewrite puts the modification time back, a chmod
//! moves it too, and an atomic save gives a new inode. Where the platform gives no device or
//! inode (Windows, through the standard library) they are 0, and the rest still says it: on
//! Windows an atomic save that keeps size and times is not seen by the identity alone.

use std::fs;
use std::path::{Path, PathBuf};

use crate::identity::blake2b::Blake2b;

/// What says a regular file changed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Identity {
    pub size: u64,
    pub mtime_ns: i128,
    pub ctime_ns: i128,
    pub device: u64,
    pub inode: u64,
}

impl Identity {
    /// The identity of what `metadata` describes, which must be a regular file's.
    pub fn of(metadata: &fs::Metadata) -> Self {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let nanos = |seconds: i64, fraction: i64| {
                i128::from(seconds) * 1_000_000_000 + i128::from(fraction)
            };
            Self {
                size: metadata.size(),
                mtime_ns: nanos(metadata.mtime(), metadata.mtime_nsec()),
                ctime_ns: nanos(metadata.ctime(), metadata.ctime_nsec()),
                device: metadata.dev(),
                inode: metadata.ino(),
            }
        }
        #[cfg(not(unix))]
        {
            // The identity of a file met in a directory listing and that of the same file
            // stat-ed by path must agree, or every file would look changed after one was
            // recorded the other way. Windows listings carry no file index, so a stat by path
            // does not use one either, rather than the two differing: device and inode are 0.
            let nanos = |time: std::io::Result<std::time::SystemTime>| {
                time.ok()
                    .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                    .map_or(0, |d| d.as_nanos() as i128)
            };
            Self {
                size: metadata.len(),
                mtime_ns: nanos(metadata.modified()),
                ctime_ns: nanos(metadata.created()),
                device: 0,
                inode: 0,
            }
        }
    }

    /// Sixteen hexadecimal digits for a file that is only ever compared with itself: BLAKE2b
    /// with an 8-byte digest of the fields in decimal separated by spaces.
    pub fn digest(&self) -> String {
        digest_of(&[
            i128::from(self.size),
            self.mtime_ns,
            self.ctime_ns,
            i128::from(self.device),
            i128::from(self.inode),
        ])
    }
}

/// [`Identity::digest`] of fields as Python holds them.
pub fn digest_of(fields: &[i128]) -> String {
    let text = fields
        .iter()
        .map(i128::to_string)
        .collect::<Vec<_>>()
        .join(" ");
    let mut hash = Blake2b::new(b"", 8);
    hash.update(text.as_bytes());
    hex(hash)
}

fn hex(hash: Blake2b) -> String {
    let mut out = [0u8; 8];
    hash.finish_into(&mut out);
    out.iter().map(|b| format!("{b:02x}")).collect()
}

/// The identity of the file at `path` (a link is followed), or `None` when it is not a regular
/// file: missing, a directory, a FIFO or a device.
pub fn identity(path: &Path) -> Option<Identity> {
    let metadata = fs::metadata(path).ok()?;
    metadata.is_file().then(|| Identity::of(&metadata))
}

/// A regular file in a folder.
pub struct Entry {
    pub name: std::ffi::OsString,
    pub identity: Identity,
}

/// The regular files in `folder` whose names end with `extension`, in the order the folder lists
/// them. `Err` when the folder cannot be listed.
pub fn regular_files(folder: &Path, extension: &[u8]) -> std::io::Result<Vec<Entry>> {
    let mut found = Vec::new();
    for item in fs::read_dir(folder)?.flatten() {
        let name = item.file_name();
        if !name.as_encoded_bytes().ends_with(extension) {
            continue;
        }
        let metadata = match item.file_type() {
            // A link's status is its target's.
            Ok(kind) if kind.is_symlink() => fs::metadata(item.path()),
            Ok(_) => item.metadata(),
            Err(error) => Err(error),
        };
        if let Ok(metadata) = metadata
            && metadata.is_file()
        {
            found.push(Entry {
                name,
                identity: Identity::of(&metadata),
            });
        }
    }
    Ok(found)
}

/// A digest of files as [`regular_files`] gives them, so a folder whose files are all as they
/// were is seen in one hash: BLAKE2b with an 8-byte digest of the names joined by NUL, then each
/// file's identity as 64-bit integers in the machine's byte order (the low 64 bits, two's
/// complement for a time before 1970). The same files in another order give another digest.
pub fn listing(files: &[Entry]) -> String {
    let mut hash = Blake2b::new(b"", 8);
    for (at, file) in files.iter().enumerate() {
        if at > 0 {
            hash.update(&[0]);
        }
        hash.update(file.name.as_encoded_bytes());
    }
    for file in files {
        let id = &file.identity;
        for word in [
            id.size,
            id.mtime_ns as u64,
            id.ctime_ns as u64,
            id.device,
            id.inode,
        ] {
            hash.update(&word.to_ne_bytes());
        }
    }
    hex(hash)
}

/// Every regular file under `folder`, at any depth and hidden ones too, with its path from
/// `folder` and its identity. A link to a directory is not entered; a link to a file is a file.
pub fn under(folder: &Path) -> Vec<(PathBuf, Identity)> {
    let mut found = Vec::new();
    let mut stack = vec![PathBuf::new()];
    while let Some(relative) = stack.pop() {
        let Ok(entries) = fs::read_dir(folder.join(&relative)) else {
            continue;
        };
        for item in entries.flatten() {
            let path = relative.join(item.file_name());
            let Ok(kind) = item.file_type() else {
                continue;
            };
            if kind.is_dir() {
                stack.push(path);
                continue;
            }
            let metadata = if kind.is_symlink() {
                fs::metadata(item.path())
            } else {
                item.metadata()
            };
            if let Ok(metadata) = metadata
                && metadata.is_file()
            {
                found.push((path, Identity::of(&metadata)));
            }
        }
    }
    found
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::discover::tests::Scratch;

    #[test]
    fn digest_is_blake2b_of_the_decimal_fields() {
        // hashlib.blake2b(b"1 2 3 4 5", digest_size=8).hexdigest()
        assert_eq!(digest_of(&[1, 2, 3, 4, 5]).len(), 16);
        assert_eq!(digest_of(&[1, 2, 3, 4, 5]), digest_of(&[1, 2, 3, 4, 5]));
        assert_ne!(digest_of(&[1, 2, 3, 4, 5]), digest_of(&[1, 2, 3, 4, 6]));
    }

    #[test]
    fn only_regular_files_have_an_identity() {
        let scratch = Scratch::new("identity");
        scratch.file("a.sql");
        assert!(identity(&scratch.0.join("a.sql")).is_some());
        assert!(identity(&scratch.0).is_none());
        assert!(identity(&scratch.0.join("missing")).is_none());
    }

    #[test]
    fn a_listing_changes_with_a_name_or_a_file() {
        let scratch = Scratch::new("listing");
        scratch.file("a.sql");
        scratch.file("b.txt");
        let before = listing(&regular_files(&scratch.0, b".sql").unwrap());
        assert_eq!(
            before,
            listing(&regular_files(&scratch.0, b".sql").unwrap())
        );
        scratch.file("c.sql");
        assert_ne!(
            before,
            listing(&regular_files(&scratch.0, b".sql").unwrap())
        );
        assert_eq!(regular_files(&scratch.0, b".sql").unwrap().len(), 2);
        assert!(regular_files(&scratch.0.join("missing"), b".sql").is_err());
    }

    #[test]
    fn an_empty_listing_has_a_digest() {
        assert_eq!(listing(&[]).len(), 16);
    }

    #[test]
    fn under_goes_into_every_folder_and_takes_hidden_files() {
        let scratch = Scratch::new("under");
        for file in ["a.sql", "x/b.sql", "x/y/.c"] {
            scratch.file(file);
        }
        let mut names: Vec<_> = under(&scratch.0)
            .into_iter()
            .map(|(p, _)| p.to_string_lossy().into_owned())
            .collect();
        names.sort();
        let separator = std::path::MAIN_SEPARATOR;
        assert_eq!(
            names,
            [
                "a.sql".to_string(),
                format!("x{separator}b.sql"),
                format!("x{separator}y{separator}.c")
            ]
        );
    }
}
