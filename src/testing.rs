//! What the assembly routines' tests share: a generator of test bytes and, on Linux, buffers
//! that end where memory stops being readable.

/// A small deterministic generator (xorshift64*), so a failing case can be run again.
pub struct Rng(pub u64);

impl Rng {
    pub fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }

    pub fn below(&mut self, bound: usize) -> usize {
        (self.next() >> 33) as usize % bound
    }

    /// `len` bytes drawn from `alphabet`.
    pub fn bytes(&mut self, alphabet: &[u8], len: usize) -> Vec<u8> {
        (0..len)
            .map(|_| alphabet[self.below(alphabet.len())])
            .collect()
    }
}

/// Bytes of every kind the routines tell apart (letters and digits at the edges of their ranges,
/// the ASCII bytes next to them, upper case, NUL, DEL and bytes above 0x7f), few enough that runs
/// of each kind and repeated bytes form often.
pub const MIXED: &[u8] = b"abcxyz0189AZ_. -/`{@[:\x00\x7f\x80\xbf\xc3\xe9\xff\x9f\xd0\x60";

#[cfg(target_os = "linux")]
pub use guarded::Guarded;

#[cfg(target_os = "linux")]
mod guarded {
    use std::ffi::c_void;

    unsafe extern "C" {
        fn mmap(
            addr: *mut c_void,
            len: usize,
            prot: i32,
            flags: i32,
            fd: i32,
            off: i64,
        ) -> *mut c_void;
        fn mprotect(addr: *mut c_void, len: usize, prot: i32) -> i32;
        fn munmap(addr: *mut c_void, len: usize) -> i32;
        fn sysconf(name: i32) -> i64;
    }

    // The same values on every architecture Konstruct supports.
    const PROT_NONE: i32 = 0;
    const PROT_READ_WRITE: i32 = 3;
    const MAP_PRIVATE_ANONYMOUS: i32 = 0x22;
    const SC_PAGESIZE: i32 = 30;

    /// Readable pages followed by one that is not: bytes placed to end at the boundary make a
    /// routine that reads one byte past them fault instead of passing by chance.
    pub struct Guarded {
        base: *mut u8,
        readable: usize,
        size: usize,
    }

    impl Guarded {
        /// Room for `len` bytes before the unreadable page.
        pub fn new(len: usize) -> Self {
            // SAFETY: plain calls into the C library, checked for failure; the mapping is
            // private to this value and unmapped when it is dropped.
            unsafe {
                let page = usize::try_from(sysconf(SC_PAGESIZE)).expect("a page size");
                let readable = len.div_ceil(page).max(1) * page;
                let size = readable + page;
                let base = mmap(
                    std::ptr::null_mut(),
                    size,
                    PROT_READ_WRITE,
                    MAP_PRIVATE_ANONYMOUS,
                    -1,
                    0,
                );
                assert_ne!(base as isize, -1, "mmap");
                assert_eq!(
                    mprotect(base.cast::<u8>().add(readable).cast(), page, PROT_NONE),
                    0
                );
                Self {
                    base: base.cast(),
                    readable,
                    size,
                }
            }
        }

        /// `bytes` copied so that they end at the unreadable page.
        pub fn at_end(&mut self, bytes: &[u8]) -> &[u8] {
            assert!(bytes.len() <= self.readable);
            // SAFETY: the range is inside the readable part of the mapping, which this value
            // owns.
            unsafe {
                let start = self.base.add(self.readable - bytes.len());
                std::ptr::copy_nonoverlapping(bytes.as_ptr(), start, bytes.len());
                std::slice::from_raw_parts(start, bytes.len())
            }
        }
    }

    impl Drop for Guarded {
        fn drop(&mut self) {
            // SAFETY: unmaps the mapping made in `new`, which nothing borrows any more.
            unsafe {
                munmap(self.base.cast(), self.size);
            }
        }
    }
}
