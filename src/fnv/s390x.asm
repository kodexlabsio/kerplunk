// FNV-1a, 64-bit, over a byte slice: IBM Z (z/Architecture, s390x).
//
// What it does
//   Continues an FNV-1a 64 hash over `len` bytes at `data`, starting from `state`, and returns
//   the new state. For each byte: state = (state XOR byte) * 0x100000001b3, wrapping modulo
//   2^64. Starting from the offset basis 0xcbf29ce484222325 gives the published FNV-1a 64 hash
//   of the bytes; passing a previous result continues it, so a hasher can feed a key in parts
//   (a string's bytes, then its 0xff terminator) and get the hash of the whole.
//   The graph index hashes every node id it interns with it (src/fnv/mod.rs, Fnv1a).
//
// Calling convention
//   The s390x ELF ABI (z/Architecture, 64-bit), declared extern "C" in Rust.
//     r2  data   pointer to the first byte (need not be aligned; unused when len is 0)
//     r3  len    number of bytes
//     r4  state  the hash so far
//     r2         the hash after the bytes (returned)
//   Clobbers r0, r1, r3 and r4, which the ABI lets a callee change, and the condition code.
//   `brctg` decrements len and branches while it is not zero.
//
// Why it is safe
//   It reads exactly the bytes data[0] to data[len - 1], one at a time with a zero-extending
//   byte load, and reads nothing when len is 0; the Rust caller passes a slice's pointer and
//   length, so every byte read is inside one live allocation. It writes no memory, needs no
//   register save area and never touches r15 (the stack pointer) or r14, the return address
//   it branches to. It uses no floating-point or vector registers and keeps no state between
//   calls, so it is reentrant and thread safe. z/Architecture is big-endian, which does not
//   matter here: the hash reads single bytes in memory order on every architecture.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_fnv1a64, the name Rust links to (with a leading \x01
//   byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is the
//   ELF code section, and `.p2align 3` aligns the entry to 8 bytes, as compilers align
//   function entries on this architecture.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_fnv1a64
    .p2align 3
konstruct_fnv1a64:
    ltgr    %r3, %r3                    // nothing to hash?
    je      3f
    llihl   %r1, 0x100                  // the 64-bit FNV prime, 0x00000100000001b3:
    oill    %r1, 0x1b3                  // 0x100 << 32, with 0x1b3 in the low half-word
2:
    llgc    %r0, 0(%r2)                 // next byte, zero-extended to 64 bits
    xgr     %r4, %r0                    // FNV-1a mixes the byte in first...
    msgr    %r4, %r1                    // ...then multiplies, keeping the low 64 bits
    aghi    %r2, 1
    brctg   %r3, 2b
3:
    lgr     %r2, %r4                    // return the hash
    br      %r14
