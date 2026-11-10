//! The random values the identity test is built on, all drawn from one keyed BLAKE2b.
//!
//! Every value is a function of the key and of what it stands for (a column's name, a function
//! applied to arguments with given values), so under one key the same formula always evaluates
//! to the same point. Fingerprints use the fixed key [`SEED`], so they are reproducible across
//! runs and machines; comparisons use a fresh key, so that no formula can be built to collide.
//! The key also draws the three primes of the [`Field`].

use super::blake2b::Blake2b;
use super::field::{Field, LANES, Lanes};

/// The key fingerprints use, so that they agree between builds. Changing it, or anything about
/// how a formula is evaluated, changes every fingerprint: it is versioned.
pub const SEED: &[u8] = b"konstruct formula identity 3";

/// What a value stands for. Each kind hashes under its own tag, so a column named like a
/// function cannot collide with it, and two atoms standing for different things made from the
/// same inputs (the presence of a product and of a COALESCE of the same arguments) stay apart.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Tag {
    Column = 1,
    Presence,
    Literal,
    Function,
    FunctionPresence,
    Symmetric,
    SymmetricPresence,
    Aggregate,
    AggregatePresence,
    Window,
    WindowPresence,
    Indicator,
    RowSum,
    Rows,
    Inverse,
    Nested,
    /// A row-level expression kept whole, its expansion past the limit.
    Opaque,
    /// An aggregate's argument times its presence, kept whole past the limit.
    Weighted,
    /// Past the limit: the presence of a product of operands, of a CASE and of a COALESCE.
    BothPresence,
    CasePresence,
    CoalescePresence,
    /// Whether a row of the nullable side of an outer join matched.
    Outer,
    Fingerprint,
    Draw,
    Prime,
    Context,
}

/// The input of one draw, framed so that no two different inputs give the same bytes.
#[derive(Clone)]
pub struct Feed(Blake2b);

impl Feed {
    pub fn text(&mut self, text: &str) -> &mut Feed {
        self.bytes(text.as_bytes())
    }

    pub fn bytes(&mut self, bytes: &[u8]) -> &mut Feed {
        self.0.update(&(bytes.len() as u64).to_le_bytes());
        self.0.update(bytes);
        self
    }

    pub fn lanes(&mut self, lanes: &Lanes) -> &mut Feed {
        for lane in lanes.0 {
            self.0.update(&lane.to_le_bytes());
        }
        self
    }

    pub fn number(&mut self, n: u64) -> &mut Feed {
        self.0.update(&n.to_le_bytes());
        self
    }
}

#[derive(Clone)]
pub struct Oracle {
    key: [u8; 32],
    /// The keyed state for 64-byte draws, with the key block already compressed.
    keyed: Blake2b,
    pub field: Field,
}

impl Default for Oracle {
    fn default() -> Oracle {
        Oracle::new(SEED)
    }
}

fn digest_of(key: &[u8], tag: Tag, feed: impl FnOnce(&mut Feed), out: &mut [u8]) {
    let mut state = Feed(Blake2b::new(key, out.len()));
    state.0.update(&[tag as u8]);
    feed(&mut state);
    state.0.finish_into(out);
}

impl Oracle {
    /// The oracle a key gives: any bytes, hashed into the BLAKE2b key.
    pub fn new(seed: &[u8]) -> Oracle {
        let key = key_of(seed);
        // The primes are drawn before the field exists, from the key alone.
        let mut keyed = Blake2b::new(&key, 64);
        keyed.settle();
        let mut oracle = Oracle {
            key,
            keyed,
            field: Field::of(3, 5, 7),
        };
        oracle.field = Field::draw(&oracle);
        oracle
    }

    fn digest<const N: usize>(&self, tag: Tag, feed: impl FnOnce(&mut Feed)) -> [u8; N] {
        let mut out = [0; N];
        digest_of(&self.key, tag, feed, &mut out);
        out
    }

    /// A uniform element of each lane's field for the input `feed` writes: words from
    /// BLAKE2b in counter mode, each lane rejecting those at or above its prime.
    pub fn lanes(&self, tag: Tag, feed: impl FnOnce(&mut Feed)) -> Lanes {
        // Every draw adds its tag, so the settled state is never finished empty.
        let mut state = Feed(self.keyed.clone());
        state.0.update(&[tag as u8]);
        feed(&mut state);
        let words = (0u64..).flat_map(move |block| {
            let mut copy = state.clone();
            copy.number(block);
            let mut bytes = [0u8; 64];
            copy.0.finish_into(&mut bytes);
            (0..8).map(move |i| {
                u64::from_le_bytes(bytes[8 * i..8 * i + 8].try_into().expect("eight bytes"))
            })
        });
        self.field.uniform(words)
    }

    /// The 128-bit fingerprint of what `feed` writes.
    pub fn fingerprint(&self, feed: impl FnOnce(&mut Feed)) -> u128 {
        u128::from_le_bytes(self.digest(Tag::Fingerprint, feed))
    }

    /// The `index`th pseudo-random word of the stream `stream`, under this oracle's key.
    pub fn draw(&self, stream: u64, index: u64) -> u64 {
        u64::from_le_bytes(self.digest(Tag::Draw, |f| {
            f.number(stream).number(index);
        }))
    }

    /// A digest of a context, to key the aggregates that run under it.
    pub fn context(&self, context: &str) -> [u8; 16] {
        self.digest(Tag::Context, |f| {
            f.text(context);
        })
    }
}

/// The `index`th word of the stream `stream` under the fixed key, whatever key evaluates: the
/// search for a counterexample draws from it, so its result does not depend on the key.
pub fn fixed_draw(stream: u64, index: u64) -> u64 {
    static KEY: std::sync::OnceLock<[u8; 32]> = std::sync::OnceLock::new();
    let key = KEY.get_or_init(|| key_of(SEED));
    let mut out = [0u8; 8];
    digest_of(
        key,
        Tag::Draw,
        |f| {
            f.number(stream).number(index);
        },
        &mut out,
    );
    u64::from_le_bytes(out)
}

fn key_of(seed: &[u8]) -> [u8; 32] {
    let mut state = Blake2b::new(b"", 32);
    state.update(seed);
    let mut key = [0; 32];
    state.finish_into(&mut key);
    key
}

const _: () = assert!(LANES == 3);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn draws_are_reproducible_and_separated_by_tag() {
        let oracle = Oracle::default();
        let a = oracle.lanes(Tag::Column, |f| {
            f.text("orders.amount");
        });
        assert_eq!(
            a,
            Oracle::default().lanes(Tag::Column, |f| {
                f.text("orders.amount");
            })
        );
        let b = oracle.lanes(Tag::Literal, |f| {
            f.text("orders.amount");
        });
        assert_ne!(a, b);
        assert_ne!(a.0[0], a.0[1]);
        let other = Oracle::new(b"another seed").lanes(Tag::Column, |f| {
            f.text("orders.amount");
        });
        assert_ne!(a, other);
        // Framing: ("ab", "c") and ("a", "bc") are different inputs.
        let ab_c = oracle.lanes(Tag::Function, |f| {
            f.text("ab").text("c");
        });
        let a_bc = oracle.lanes(Tag::Function, |f| {
            f.text("a").text("bc");
        });
        assert_ne!(ab_c, a_bc);
        assert_ne!(oracle.draw(1, 0), oracle.draw(1, 1));
        assert_ne!(oracle.draw(1, 0), oracle.draw(2, 0));
        let print = |l: &Lanes| {
            oracle.fingerprint(|f| {
                f.lanes(l);
            })
        };
        assert_eq!(print(&a), print(&a));
        assert_ne!(print(&a), print(&b));
        assert_eq!(fixed_draw(3, 4), Oracle::default().draw(3, 4));
        assert_ne!(fixed_draw(3, 4), Oracle::new(b"another key").draw(3, 4));
    }

    #[test]
    fn fixed_key_gives_fixed_fingerprints() {
        // A change here changes every fingerprint Konstruct has written: bump SEED with it.
        let oracle = Oracle::default();
        let column = oracle.lanes(Tag::Column, |f| {
            f.text("a");
        });
        let print = oracle.fingerprint(|f| {
            f.lanes(&column);
        });
        assert_eq!(format!("{print:032x}"), PINNED);
    }

    const PINNED: &str = "890930a431c6b3b3c8502a3a646c1b5f";
}
