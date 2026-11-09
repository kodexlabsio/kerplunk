// FNV-1a, 64-bit, over a byte slice: AArch64 (ARMv8-A), GNU syntax.
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
//   AAPCS64, the procedure call standard every AArch64 operating system uses (Linux, macOS and
//   Windows alike for integer arguments), declared extern "C" in Rust.
//     x0  data   pointer to the first byte (need not be aligned; unused when len is 0)
//     x1  len    number of bytes
//     x2  state  the hash so far
//     x0         the hash after the bytes (returned)
//   Clobbers x1 to x4 and the condition flags, all of which AAPCS64 lets a callee change. It
//   does not touch x18, the platform register on macOS and Windows.
//
// Why it is safe
//   It reads exactly the bytes data[0] to data[len - 1], one at a time with a post-indexed
//   byte load, and reads nothing when len is 0; the Rust caller passes a slice's pointer and
//   length, so every byte read is inside one live allocation. It writes no memory and never
//   touches the stack or the link register (it is a leaf, so it needs no frame record and no
//   unwind data), uses no SIMD registers and keeps no state between calls, so it is reentrant
//   and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_fnv1a64 on ELF, Mach-O and COFF alike: Rust links to
//   it by that exact name, as a leading \x01 byte in its link_name tells LLVM not to add the
//   underscore Mach-O symbols otherwise get. `.text` names the code section in all three object
//   formats, and `.p2align 2` aligns the entry to 4 bytes, the size of every A64 instruction.
//   On ELF targets src/fnv/mod.rs adds the symbol's `.type` and `.size` after the routine.

    .text
    .globl konstruct_fnv1a64
    .p2align 2
konstruct_fnv1a64:
    mov     x3, #0x01b3                 // the 64-bit FNV prime, 0x00000100000001b3:
    movk    x3, #0x0100, lsl #32        // low half-word, then bits 32 to 47
    cbz     x1, 3f                      // nothing to hash
2:
    ldrb    w4, [x0], #1                // next byte, zero-extended; advance data
    eor     x2, x2, x4                  // FNV-1a mixes the byte in first...
    mul     x2, x2, x3                  // ...then multiplies, keeping the low 64 bits
    subs    x1, x1, #1
    b.ne    2b
3:
    mov     x0, x2                      // return the hash
    ret
