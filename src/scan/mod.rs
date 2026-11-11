//! Whether a Python file declares Airflow DAGs or Dagster definitions, decided from its bytes.
//!
//! Discovery asks it of every Python file in a repository, so it is a scan over all of them.
//! Both answers begin with a word, "airflow" or "dagster", that nearly every file lacks, and
//! finding those is hand-written assembly for each architecture Konstruct supports, in
//! `src/scan/`, each using the vector instructions its baseline guarantees, as `src/words/` does
//! and for the same reasons:
//!
//! - x86_64: SSE2, with `pmovmskb` for the mask of candidates.
//! - aarch64: NEON, with a narrowing shift for a mask of four bits per position.
//! - riscv64: scalar RV64GC, one position at a time.
//! - powerpc64le: POWER8's VMX and VSX, with `vbpermq` for the mask.
//! - s390x: the vector facility of z13, with `vfene` for the first candidate and `vll` for the
//!   last positions.
//! - loongarch64: LSX, with `vmskltz.b` for the mask.
//!
//! The marker words that follow are looked for only in a file that has the first, which is rare,
//! with [`crate::substring`], itself assembly.

// The routines take 64-bit pointers and lengths, so a 32-bit ABI on one of these architectures
// (x86_64's x32, aarch64's ILP32) is refused as well: it would pass 32-bit ones.
#[cfg(not(all(
    target_pointer_width = "64",
    any(
        target_arch = "x86_64",
        target_arch = "aarch64",
        target_arch = "riscv64",
        all(target_arch = "powerpc64", target_endian = "little"),
        target_arch = "s390x",
        target_arch = "loongarch64",
    ),
)))]
compile_error!(
    "Konstruct supports x86_64, aarch64, riscv64, powerpc64le, s390x and loongarch64, with 64-bit pointers"
);

#[cfg(target_arch = "x86_64")]
routine!(include_str!("x86_64.asm"), "konstruct_gate_words");

#[cfg(target_arch = "aarch64")]
routine!(include_str!("aarch64.asm"), "konstruct_gate_words");

#[cfg(target_arch = "riscv64")]
routine!(include_str!("riscv64.asm"), "konstruct_gate_words");

#[cfg(all(target_arch = "powerpc64", target_endian = "little"))]
routine!(include_str!("powerpc64le.asm"), "konstruct_gate_words");

#[cfg(target_arch = "s390x")]
routine!(include_str!("s390x.asm"), "konstruct_gate_words");

#[cfg(target_arch = "loongarch64")]
routine!(include_str!("loongarch64.asm"), "konstruct_gate_words");

// System V on x86_64 whatever the operating system, so one routine serves Windows as well.
#[cfg(target_arch = "x86_64")]
unsafe extern "sysv64" {
    #[link_name = "\u{1}konstruct_gate_words"]
    fn konstruct_gate_words(data: *const u8, len: usize) -> usize;
}

// Every other architecture has one C calling convention on every operating system it runs.
#[cfg(not(target_arch = "x86_64"))]
unsafe extern "C" {
    #[link_name = "\u{1}konstruct_gate_words"]
    fn konstruct_gate_words(data: *const u8, len: usize) -> usize;
}

/// Which of the two words a text holds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Gates {
    pub airflow: bool,
    pub dagster: bool,
}

/// Whether "airflow" and "dagster" occur in `bytes`.
pub fn gates(bytes: &[u8]) -> Gates {
    // SAFETY: the routine reads only bytes inside the slice, writes no memory and follows the
    // declared calling convention; it returns bit 0 for "airflow" and bit 1 for "dagster".
    let found = unsafe { konstruct_gate_words(bytes.as_ptr(), bytes.len()) };
    Gates {
        airflow: found & 1 != 0,
        dagster: found & 2 != 0,
    }
}

fn mentions(bytes: &[u8], word: &[u8]) -> bool {
    crate::substring::contains(bytes, word)
}

/// Whether a file's text mentions Airflow and declares a DAG, as `airflow` and `DAG` or `@dag`.
pub fn looks_like_dag_file(bytes: &[u8]) -> bool {
    gates(bytes).airflow && (mentions(bytes, b"DAG") || mentions(bytes, b"@dag"))
}

/// The words that mark a file that mentions Dagster as one that defines something.
const DAGSTER_MARKERS: [&[u8]; 10] = [
    b"@asset",
    b"@multi_asset",
    b"@dbt_assets",
    b"define_asset_job",
    b"Definitions(",
    b"@op",
    b"@job",
    b"@graph",
    b"sensor",
    b"@schedule",
];

/// Whether a file's text mentions Dagster and one of the words that declare its definitions.
pub fn looks_like_dagster_file(bytes: &[u8]) -> bool {
    gates(bytes).dagster && DAGSTER_MARKERS.iter().any(|marker| mentions(bytes, marker))
}

/// What [`classify`] found in a file: the bit of each kind of file it looks like.
pub const DAG_FILE: u8 = 1;
pub const DAGSTER_FILE: u8 = 2;

/// Both answers for a text, scanning it once.
pub fn classify(bytes: &[u8]) -> u8 {
    let found = gates(bytes);
    let dag = found.airflow && (mentions(bytes, b"DAG") || mentions(bytes, b"@dag"));
    let dagster = found.dagster && DAGSTER_MARKERS.iter().any(|marker| mentions(bytes, marker));
    (u8::from(dag) * DAG_FILE) | (u8::from(dagster) * DAGSTER_FILE)
}

/// What [`classify`] says of each file in `paths`, which are read in parallel; a file that cannot
/// be read has neither bit.
pub fn classify_files<P: AsRef<std::path::Path> + Sync>(paths: &[P]) -> Vec<u8> {
    use std::sync::atomic::{AtomicU8, AtomicUsize, Ordering};
    /// Below this many files, starting threads costs more than it saves.
    const PARALLEL_FROM: usize = 64;
    let found: Vec<AtomicU8> = paths.iter().map(|_| AtomicU8::new(0)).collect();
    let next = AtomicUsize::new(0);
    let work = || {
        loop {
            let at = next.fetch_add(1, Ordering::Relaxed);
            let Some(path) = paths.get(at) else { return };
            if let Ok(bytes) = std::fs::read(path) {
                found[at].store(classify(&bytes), Ordering::Relaxed);
            }
        }
    };
    let threads = if paths.len() < PARALLEL_FROM {
        1
    } else {
        std::thread::available_parallelism().map_or(1, |n| n.get().min(8))
    };
    if threads == 1 {
        work();
    } else {
        std::thread::scope(|scope| {
            for _ in 0..threads {
                scope.spawn(work);
            }
        });
    }
    found.into_iter().map(AtomicU8::into_inner).collect()
}

#[cfg(feature = "python")]
pub mod python;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::{MIXED, Rng};
    use proptest::prelude::*;

    /// The words by comparing every position in full.
    fn reference(bytes: &[u8]) -> Gates {
        Gates {
            airflow: bytes.windows(7).any(|w| w == b"airflow"),
            dagster: bytes.windows(7).any(|w| w == b"dagster"),
        }
    }

    /// `bytes` as Python's `in` reads it: the specification of the two predicates.
    fn dag_reference(bytes: &[u8]) -> bool {
        let has = |word: &[u8]| bytes.windows(word.len()).any(|w| w == word);
        has(b"airflow") && (has(b"DAG") || has(b"@dag"))
    }

    fn dagster_reference(bytes: &[u8]) -> bool {
        let has = |word: &[u8]| bytes.windows(word.len()).any(|w| w == word);
        has(b"dagster") && DAGSTER_MARKERS.iter().any(|marker| has(marker))
    }

    /// Checks `bytes` at every offset in `alignments` of a buffer.
    fn check_aligned(bytes: &[u8], alignments: std::ops::Range<usize>) {
        let expected = reference(bytes);
        let mut buffer = vec![0u8; bytes.len() + alignments.end];
        for offset in alignments {
            buffer[offset..offset + bytes.len()].copy_from_slice(bytes);
            let at = &buffer[offset..offset + bytes.len()];
            assert_eq!(gates(at), expected, "{bytes:?} at offset {offset}");
        }
    }

    #[test]
    fn examples() {
        assert_eq!(
            gates(b""),
            Gates {
                airflow: false,
                dagster: false
            }
        );
        assert_eq!(
            gates(b"airflo"),
            Gates {
                airflow: false,
                dagster: false
            }
        );
        assert_eq!(
            gates(b"airflow"),
            Gates {
                airflow: true,
                dagster: false
            }
        );
        assert_eq!(
            gates(b"dagster"),
            Gates {
                airflow: false,
                dagster: true
            }
        );
        assert_eq!(
            gates(b"from airflow import DAG"),
            Gates {
                airflow: true,
                dagster: false
            }
        );
        assert_eq!(
            gates(b"airflow dagster"),
            Gates {
                airflow: true,
                dagster: true
            }
        );
        assert_eq!(
            gates(b"dagsterairflow"),
            Gates {
                airflow: true,
                dagster: true
            }
        );
        assert!(looks_like_dag_file(b"from airflow import DAG"));
        assert!(looks_like_dag_file(b"from airflow.sdk import dag\n@dag()"));
        assert!(!looks_like_dag_file(b"import pandas"));
        assert!(!looks_like_dag_file(b"import airflow"));
        assert!(!looks_like_dag_file(b"DAG"));
        assert!(looks_like_dagster_file(
            b"import dagster\n@asset\ndef a(): pass"
        ));
        assert!(looks_like_dagster_file(b"from dagster import sensor"));
        assert!(!looks_like_dagster_file(b"import dagster"));
        assert!(!looks_like_dagster_file(b"@asset"));
        assert_eq!(
            classify(b"airflow DAG dagster @op"),
            DAG_FILE | DAGSTER_FILE
        );
    }

    #[test]
    fn a_word_at_every_position_of_every_length() {
        for word in [&b"airflow"[..], b"dagster"] {
            for len in 7..=80 {
                for at in 0..=len - 7 {
                    for background in [b'x', b'a', b'd', b'w', b'r', 0u8, 0xff] {
                        let mut bytes = vec![background; len];
                        bytes[at..at + 7].copy_from_slice(word);
                        check_aligned(&bytes, 0..3);
                    }
                }
            }
        }
    }

    #[test]
    fn near_misses_are_not_words() {
        for word in [&b"airflow"[..], b"dagster"] {
            for len in [7, 15, 22, 23, 24, 40, 100] {
                for at in [0, 3, len - 7].into_iter().filter(|at| at + 7 <= len) {
                    for broken in 0..7 {
                        for replacement in [b'x', word[(broken + 1) % 7], 0u8] {
                            let mut bytes = vec![b'.'; len];
                            bytes[at..at + 7].copy_from_slice(word);
                            bytes[at + broken] = replacement;
                            if replacement != word[broken] {
                                check_aligned(&bytes, 0..2);
                            }
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn every_length_to_300_at_every_alignment() {
        let mut rng = Rng(0x5eed);
        for len in 0..=300 {
            for bytes in [
                rng.bytes(MIXED, len),
                rng.bytes(b"airflowdgste ", len),
                rng.bytes(b"ad", len),
            ] {
                check_aligned(&bytes, 0..17);
            }
        }
    }

    #[test]
    fn the_predicates_agree_with_the_specification() {
        let mut rng = Rng(0xc0de);
        let parts: [&[u8]; 16] = [
            b"airflow",
            b"dagster",
            b"DAG",
            b"@dag",
            b"@asset",
            b"@op",
            b"sensor",
            b"Definitions(",
            b"@graph",
            b"import ",
            b"\n",
            b" ",
            b"x = 1",
            b"@job",
            b"@schedule",
            b"define_asset_job",
        ];
        for _ in 0..4000 {
            let mut bytes = Vec::new();
            for _ in 0..rng.below(8) {
                bytes.extend_from_slice(parts[rng.below(parts.len())]);
                bytes.extend_from_slice(b" ");
            }
            assert_eq!(
                looks_like_dag_file(&bytes),
                dag_reference(&bytes),
                "{bytes:?}"
            );
            assert_eq!(
                looks_like_dagster_file(&bytes),
                dagster_reference(&bytes),
                "{bytes:?}"
            );
            let both = classify(&bytes);
            assert_eq!(both & DAG_FILE != 0, dag_reference(&bytes));
            assert_eq!(both & DAGSTER_FILE != 0, dagster_reference(&bytes));
        }
    }

    #[test]
    fn reads_nothing_past_the_end() {
        let mut rng = Rng(7);
        let mut guarded = crate::testing::Guarded::new(512);
        for len in 0..=300 {
            for word in [&b"airflow"[..], b"dagster", b"airflox"] {
                let mut bytes = rng.bytes(MIXED, len);
                if len >= 7 {
                    let at = len - 7;
                    bytes[at..].copy_from_slice(word);
                }
                let at_end = guarded.at_end(&bytes);
                assert_eq!(gates(at_end), reference(&bytes), "{bytes:?}");
            }
            let plain = rng.bytes(b"adwr", len);
            assert_eq!(gates(guarded.at_end(&plain)), reference(&plain));
        }
    }

    #[test]
    fn files_are_classified_in_parallel_and_unreadable_ones_have_neither_bit() {
        let directory = std::env::temp_dir().join(format!("konstruct-scan-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let mut paths = Vec::new();
        let mut expected = Vec::new();
        for index in 0..200 {
            let (text, bits): (&[u8], u8) = match index % 4 {
                0 => (b"from airflow import DAG\n", DAG_FILE),
                1 => (b"from dagster import asset\n@asset\n", DAGSTER_FILE),
                2 => (b"import airflow\n", 0),
                _ => (b"\xff\xfe airflow @dag \xff", DAG_FILE),
            };
            let path = directory.join(format!("f{index}.py"));
            std::fs::write(&path, text).unwrap();
            paths.push(path);
            expected.push(bits);
        }
        paths.push(directory.join("missing.py"));
        expected.push(0);
        assert_eq!(classify_files(&paths), expected);
        assert_eq!(classify_files(&paths[..3]), expected[..3]);
        assert_eq!(classify_files::<std::path::PathBuf>(&[]), Vec::<u8>::new());
        std::fs::remove_dir_all(&directory).unwrap();
    }

    proptest! {
        #[test]
        fn gates_agree_with_the_reference(bytes in proptest::collection::vec(
            prop_oneof![Just(b'a'), Just(b'd'), Just(b'w'), Just(b'r'), any::<u8>()], 0..200)) {
            prop_assert_eq!(gates(&bytes), reference(&bytes));
        }
    }
}
