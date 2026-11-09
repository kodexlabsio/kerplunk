// FNV-1a, 64-bit, over a byte slice: 64-bit little-endian Power (POWER8 and later).
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
//   The 64-bit ELF ABI version 2 (ELFv2), which every little-endian Power Linux uses, declared
//   extern "C" in Rust.
//     r3  data   pointer to the first byte (need not be aligned; unused when len is 0)
//     r4  len    number of bytes
//     r5  state  the hash so far
//     r3         the hash after the bytes (returned)
//   Clobbers r5, r6, r7, the count register and cr0, all volatile under ELFv2. The loop
//   counts down in the count register, so `bdnz` both decrements and branches.
//
// Why it is safe
//   It reads exactly the bytes data[0] to data[len - 1], one at a time with a zero-extending
//   byte load, and reads nothing when len is 0; the Rust caller passes a slice's pointer and
//   length, so every byte read is inside one live allocation. It writes no memory, has no stack
//   frame and never touches r1, r2 (the TOC pointer, which it does not need) or the link
//   register, through which it returns. It uses no floating-point or vector registers and keeps
//   no state between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_fnv1a64, the name Rust links to (with a leading \x01
//   byte in its link_name, which tells LLVM to use the name exactly as written). As it uses no
//   TOC, its global and local entry points are the same address and it needs no
//   `.localentry`. `.text` is the ELF code section; `.p2align 4` aligns the entry to 16 bytes,
//   as compilers align function entries for POWER's instruction fetch.
//   On ELF targets src/fnv/mod.rs adds the symbol's `.type` and `.size` after the routine.

    .text
    .globl konstruct_fnv1a64
    .p2align 4
konstruct_fnv1a64:
    cmpdi   4, 0                        // nothing to hash?
    beq     0, 3f
    li      6, 1                        // the 64-bit FNV prime, 0x00000100000001b3:
    sldi    6, 6, 40                    // 1 << 40,
    ori     6, 6, 0x1b3                 // with 0x1b3 in the low half-word
    mtctr   4                           // len iterations
2:
    lbz     7, 0(3)                     // next byte, zero-extended
    xor     5, 5, 7                     // FNV-1a mixes the byte in first...
    mulld   5, 5, 6                     // ...then multiplies, keeping the low 64 bits
    addi    3, 3, 1
    bdnz    2b
3:
    mr      3, 5                        // return the hash
    blr
