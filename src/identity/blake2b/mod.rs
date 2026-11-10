//! Keyed BLAKE2b (RFC 7693), the pseudo-random function behind every random value the identity
//! test draws. A keyed BLAKE2b is a PRF: without the key its outputs cannot be told from
//! independent uniform values, which is what the Schwartz-Zippel argument needs of the points
//! and the uninterpreted functions.
//!
//! The compression function is hand-written assembly for each architecture Konstruct
//! supports, in this module's folder; this module pads, counts and finalises around it.

macro_rules! routine {
    ($file:literal) => {
        #[cfg(any(target_vendor = "apple", target_os = "windows"))]
        core::arch::global_asm!(include_str!($file));
        #[cfg(not(any(target_vendor = "apple", target_os = "windows")))]
        core::arch::global_asm!(
            include_str!($file),
            ".type konstruct_blake2b_compress, %function",
            ".size konstruct_blake2b_compress, . - konstruct_blake2b_compress",
        );
    };
}

#[cfg(target_arch = "x86_64")]
routine!("x86_64.asm");
#[cfg(target_arch = "aarch64")]
routine!("aarch64.asm");
#[cfg(target_arch = "riscv64")]
routine!("riscv64.asm");
#[cfg(all(target_arch = "powerpc64", target_endian = "little"))]
routine!("powerpc64le.asm");
#[cfg(target_arch = "s390x")]
routine!("s390x.asm");
#[cfg(target_arch = "loongarch64")]
routine!("loongarch64.asm");

/// What the compression routine works in, besides the state and the block: the sixteen words
/// it mixes, the byte counter, the finalisation flag and, for a big-endian machine, the block
/// as little-endian words. Its layout is the routines' contract: `v` at 0, the counter at 128
/// and 136, the flag at 144 and the words at 152.
#[repr(C, align(8))]
struct Work {
    v: [u64; 16],
    counter: [u64; 2],
    /// All ones for the last block, else zero.
    last: u64,
    words: [u64; 16],
}

/// A block of input, aligned so that the routines can load it as 64-bit words.
#[derive(Clone)]
#[repr(C, align(8))]
struct Block([u8; BLOCK]);

#[cfg(target_arch = "x86_64")]
unsafe extern "sysv64" {
    #[link_name = "\u{1}konstruct_blake2b_compress"]
    fn konstruct_blake2b_compress(h: *mut u64, block: *const u8, work: *mut Work);
}

#[cfg(not(target_arch = "x86_64"))]
unsafe extern "C" {
    #[link_name = "\u{1}konstruct_blake2b_compress"]
    fn konstruct_blake2b_compress(h: *mut u64, block: *const u8, work: *mut Work);
}

/// The initialisation vector, which the routines also hold.
const IV: [u64; 8] = [
    0x6a09_e667_f3bc_c908,
    0xbb67_ae85_84ca_a73b,
    0x3c6e_f372_fe94_f82b,
    0xa54f_f53a_5f1d_36f1,
    0x510e_527f_ade6_82d1,
    0x9b05_688c_2b3e_6c1f,
    0x1f83_d9ab_fb41_bd6b,
    0x5be0_cd19_137e_2179,
];

const BLOCK: usize = 128;

/// An incremental BLAKE2b computation.
#[derive(Clone)]
pub struct Blake2b {
    h: [u64; 8],
    buffer: Block,
    filled: usize,
    counter: u128,
    length: usize,
}

fn compress(h: &mut [u64; 8], block: &Block, counter: u128, last: bool) {
    let mut work = Work {
        v: [0; 16],
        counter: [counter as u64, (counter >> 64) as u64],
        last: if last { u64::MAX } else { 0 },
        words: [0; 16],
    };
    // SAFETY: the routine reads the eight words at h, the 128 bytes at block (aligned to 8)
    // and the work area, writes only h and the work area, and follows the declared calling
    // convention; all three are live, distinct and exclusively borrowed or owned here.
    unsafe { konstruct_blake2b_compress(h.as_mut_ptr(), block.0.as_ptr(), &raw mut work) }
}

impl Blake2b {
    /// A computation of a `length`-byte digest (1 to 64) keyed with `key` (at most 64 bytes).
    pub fn new(key: &[u8], length: usize) -> Blake2b {
        assert!((1..=64).contains(&length) && key.len() <= 64);
        let mut h = IV;
        h[0] ^= 0x0101_0000 ^ ((key.len() as u64) << 8) ^ length as u64;
        let mut state = Blake2b {
            h,
            buffer: Block([0; BLOCK]),
            filled: 0,
            counter: 0,
            length,
        };
        if !key.is_empty() {
            state.update(key);
            state.filled = BLOCK;
        }
        state
    }

    /// Compresses a full buffer now rather than when more data arrives: for a keyed state
    /// that is cloned for many messages, each of which adds at least one byte, so that the key
    /// block is compressed once. Finishing a settled state with nothing added gives a wrong
    /// digest, so it panics.
    pub fn settle(&mut self) {
        if self.filled == BLOCK {
            self.counter += BLOCK as u128;
            compress(&mut self.h, &self.buffer, self.counter, false);
            self.filled = 0;
        }
    }

    pub fn update(&mut self, mut data: &[u8]) {
        while !data.is_empty() {
            // The last block is compressed only at the end, flagged as last, so a full buffer
            // waits until more data arrives.
            if self.filled == BLOCK {
                self.counter += BLOCK as u128;
                compress(&mut self.h, &self.buffer, self.counter, false);
                self.filled = 0;
            }
            let take = data.len().min(BLOCK - self.filled);
            self.buffer.0[self.filled..self.filled + take].copy_from_slice(&data[..take]);
            self.filled += take;
            data = &data[take..];
        }
    }

    /// The digest, written into `out`, whose length must be the one given to `new`.
    pub fn finish_into(mut self, out: &mut [u8]) {
        assert_eq!(out.len(), self.length);
        assert!(
            self.filled > 0 || self.counter == 0,
            "a settled state finished with nothing added"
        );
        self.counter += self.filled as u128;
        self.buffer.0[self.filled..].fill(0);
        compress(&mut self.h, &self.buffer, self.counter, true);
        let mut bytes = [0u8; 64];
        for (chunk, word) in bytes.as_chunks_mut::<8>().0.iter_mut().zip(self.h) {
            chunk.copy_from_slice(&word.to_le_bytes());
        }
        out.copy_from_slice(&bytes[..self.length]);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }

    fn unhex(text: &str) -> Vec<u8> {
        (0..text.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
            .collect()
    }

    fn digest(key: &[u8], data: &[u8], length: usize) -> String {
        let mut state = Blake2b::new(key, length);
        state.update(data);
        let mut out = vec![0; length];
        state.finish_into(&mut out);
        hex(&out)
    }

    #[test]
    fn known_answers() {
        // The BLAKE2 authors' keyed test vectors: 64-byte key 00..3f, inputs 00.. of every
        // length from 0 to 255, 64-byte digests.
        let text = include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/blake2b-kat.txt"
        ));
        let mut count = 0;
        for entry in text.split("\n\n").filter(|e| !e.trim().is_empty()) {
            let field = |name: &str| {
                entry
                    .lines()
                    .find_map(|line| line.strip_prefix(name))
                    .map(|value| value.trim().to_string())
                    .unwrap_or_else(|| panic!("no {name} in {entry:?}"))
            };
            let (input, key, hash) = (unhex(&field("in:")), unhex(&field("key:")), field("hash:"));
            assert_eq!(
                digest(&key, &input, 64),
                hash,
                "input of {} bytes",
                input.len()
            );
            count += 1;
        }
        assert_eq!(count, 256);
    }

    #[test]
    fn unkeyed_multi_block_and_short_digests() {
        // From Python's hashlib.blake2b(data, key=key, digest_size=n), over i mod 251.
        let data: Vec<u8> = (0..1000).map(|i| (i % 251) as u8).collect();
        let key: Vec<u8> = (0..64).collect();
        for (key, length, size, expected) in [
            (&b""[..], 0, 64, UNKEYED[0]),
            (b"", 1, 64, UNKEYED[1]),
            (b"", 127, 64, UNKEYED[2]),
            (b"", 128, 64, UNKEYED[3]),
            (b"", 129, 64, UNKEYED[4]),
            (b"", 256, 64, UNKEYED[5]),
            (b"", 257, 64, UNKEYED[6]),
            (b"", 1000, 64, UNKEYED[7]),
            (b"", 1000, 1, "b7"),
            (b"", 1000, 20, "fc9a2426db78846a07219bc181a52bae9a62eacc"),
            (b"", 1000, 48, SIZE_48),
            (
                &key,
                1000,
                33,
                "d2bab9dca5bf1e86af75f38ed695053f297fd844998ae26f4f8bb3122ad1fc2b6d",
            ),
            (&[7u8][..], 300, 64, ONE_BYTE_KEY),
            (&key, 0, 1, "05"),
        ] {
            assert_eq!(
                digest(key, &data[..length], size),
                expected,
                "{length} bytes, {size}"
            );
        }
        assert_eq!(
            digest(b"", b"abc", 64),
            "ba80a53f981c4d0d6a2797b69f12f6e94c212f14685ac4b74b12bb6fdbffa2d1\
             7d87c5392aab792dc252d5de4533cc9518d38aa8dbf1925ab92386edd4009923"
        );
    }

    #[test]
    fn updates_in_pieces_give_the_same_digest() {
        let data: Vec<u8> = (0..=255).cycle().take(1000).collect();
        let whole = digest(b"key", &data, 32);
        for cut in [0, 1, 127, 128, 129, 256, 999, 1000] {
            let mut state = Blake2b::new(b"key", 32);
            state.update(&data[..cut]);
            state.update(&data[cut..]);
            let mut out = [0u8; 32];
            state.finish_into(&mut out);
            assert_eq!(hex(&out), whole, "cut at {cut}");
        }
    }

    #[test]
    fn a_settled_keyed_state_gives_the_same_digests() {
        let key: Vec<u8> = (0..64).collect();
        let mut settled = Blake2b::new(&key, 64);
        settled.settle();
        for length in [1, 127, 128, 129, 300] {
            let data: Vec<u8> = (0..length).map(|i| i as u8).collect();
            let mut state = settled.clone();
            state.update(&data);
            let mut out = [0u8; 64];
            state.finish_into(&mut out);
            assert_eq!(hex(&out), digest(&key, &data, 64), "{length} bytes");
        }
    }

    #[test]
    #[should_panic(expected = "settled state finished with nothing added")]
    fn a_settled_state_needs_data() {
        let mut settled = Blake2b::new(b"key", 64);
        settled.settle();
        settled.finish_into(&mut [0u8; 64]);
    }

    #[test]
    fn the_work_area_is_laid_out_as_the_routines_expect() {
        let work = Work {
            v: [0; 16],
            counter: [0; 2],
            last: 0,
            words: [0; 16],
        };
        let base = &raw const work as usize;
        assert_eq!(&raw const work.counter as usize - base, 128);
        assert_eq!(&raw const work.last as usize - base, 144);
        assert_eq!(&raw const work.words as usize - base, 152);
        assert_eq!(IV[0], 0x6a09_e667_f3bc_c908);
    }

    const UNKEYED: [&str; 8] = [
        "786a02f742015903c6c6fd852552d272912f4740e15847618a86e217f71f5419d25e1031afee585313896444934eb04b903a685b1448b755d56f701afe9be2ce",
        "2fa3f686df876995167e7c2e5d74c4c7b6e48f8068fe0e44208344d480f7904c36963e44115fe3eb2a3ac8694c28bcb4f5a0f3276f2e79487d8219057a506e4b",
        "b6292669ccd38d5f01caae96ba272c76a879a45743afa0725d83b9ebb26665b731f1848c52f11972b6644f554c064fa90780dbbbf3a89d4fc31f67df3e5857ef",
        "2319e3789c47e2daa5fe807f61bec2a1a6537fa03f19ff32e87eecbfd64b7e0e8ccff439ac333b040f19b0c4ddd11a61e24ac1fe0f10a039806c5dcc0da3d115",
        "f59711d44a031d5f97a9413c065d1e614c417ede998590325f49bad2fd444d3e4418be19aec4e11449ac1a57207898bc57d76a1bcf3566292c20c683a5c4648f",
        "93463ac058b6163eb43be3f5bb32b28541498f4e3366f1effe253ad44e1e076e41c3616046027c82a7124f8f4746668ad10b12e8e25a95ac8f3151df01cd5a93",
        "9ca40e2ddee9436dbbd08efc65dbaf4870059f5eb3d76efd20241ae5bf13c60f250b882ea5c564838257a3fc95c496819ace2c6490b55b268535208dfc31822c",
        "c11e1c0340bd7e5a1b275f1230c962fad215ecb1391486e74e31b960a2f2996381a5fad092da06841d5f26e38f6ecfeaf441acbcd1c2de61aef121e7927175f5",
    ];
    const SIZE_48: &str = "f0a7a4bb3c3290f432e513caa227ab3bf933c4c8c167193dff1cb10a0b992f042f5679e477f00c551e2cf2bec8101f1e";
    const ONE_BYTE_KEY: &str = "b770382e1c8877b631af303aaf84e4a32df893c309b59414dc3ef109cb8edf2b16e901c5639ede0fb7470a42692d47c1d6e44508e86b593b58d44aa401ce50d0";
}
