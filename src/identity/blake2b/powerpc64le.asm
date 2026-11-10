// BLAKE2b compression: 64-bit little-endian Power ISA (powerpc64le, POWER8 and later).
//
// What it does
//   The BLAKE2b compression function F of RFC 7693, section 3.2: mixes one 128-byte block into
//   the eight-word state h, given the byte counter t and the finalisation flag f0. The keyed
//   BLAKE2b behind the formula identity test (src/identity/blake2b/mod.rs) pads, counts and
//   finalises around it. The routine copies h and the initialisation vector into the work
//   area's sixteen words v, XORs the counter into v[12] and v[13] and the flag into v[14], runs
//   the twelve rounds of eight G steps on v, then folds the result back: h[i] ^= v[i] ^ v[i+8].
//   G is a macro over byte offsets into v and into the message words, so the round schedule
//   (SIGMA, with rounds 10 and 11 repeating 0 and 1) is spelled out below as the steps it makes.
//
// The work area (a struct Work, laid out in src/identity/blake2b/mod.rs)
//     0..128    v[0..16], the words being mixed (written, then read back)
//     128, 136  the counter t, low and high words (read)
//     144       f0: all ones for the last block, else zero (read)
//     152..280  the message as little-endian words, for big-endian machines (unused here
//               unless said below)
//
// Calling convention
//   The ELFv2 ABI that powerpc64le Linux uses, declared extern "C" in Rust.
//     r3  h       pointer to the eight state words (read and written)
//     r4  block   pointer to the 128-byte block, aligned to 8 (read)
//     r5  work    pointer to the work area (read and written)
//   Returns nothing. Clobbers r6 to r12, volatile registers. Registers are written as bare
//   numbers, as the FNV routine's are. A rotation right by n is rotldi by 64 - n. The machine
//   is little-endian, so G loads the message words straight from the block. Each 64-bit
//   constant is built 16 bits at a time from zero, with li, oris, ori and a shift.
//
// Why it is safe
//   It reads h[0..8], block[0..128] and the work area's first 152 bytes, and writes only h and
//   v: all inside the three live objects the Rust caller passes, which do not overlap. Every
//   access is an aligned doubleword. It is a leaf: no stack frame, r1 and the link register
//   untouched, no TOC use, no floating-point or vector registers, no state kept between calls,
//   so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The plain name konstruct_blake2b_compress, the name Rust links to. `.text` is the ELF code
//   section, and `.p2align 4` aligns the entry to 16 bytes. On ELF targets
//   src/identity/blake2b/mod.rs adds the symbol's `.type` and `.size`. The macro is removed with
//   `.purgem` at the end.

    .macro konstruct_b2_g a, b, c, d, x, y
    ld      6, \a(5)
    ld      7, \b(5)
    ld      8, \c(5)
    ld      9, \d(5)
    ld      10, \x(4)
    ld      11, \y(4)
    add     6, 6, 7                     // a = a + b + m[x]
    add     6, 6, 10
    xor     9, 9, 6                     // d = (d ^ a) >>> 32
    rotldi  9, 9, 32
    add     8, 8, 9                     // c = c + d
    xor     7, 7, 8                     // b = (b ^ c) >>> 24
    rotldi  7, 7, 40
    add     6, 6, 7                     // a = a + b + m[y]
    add     6, 6, 11
    xor     9, 9, 6                     // d = (d ^ a) >>> 16
    rotldi  9, 9, 48
    add     8, 8, 9                     // c = c + d
    xor     7, 7, 8                     // b = (b ^ c) >>> 63
    rotldi  7, 7, 1
    std     6, \a(5)
    std     7, \b(5)
    std     8, \c(5)
    std     9, \d(5)
    .endm

    .text
    .globl konstruct_blake2b_compress
    .p2align 4
konstruct_blake2b_compress:
    // v[0..8] = h
    ld      6, 0(3)
    std     6, 0(5)
    ld      6, 8(3)
    std     6, 8(5)
    ld      6, 16(3)
    std     6, 16(5)
    ld      6, 24(3)
    std     6, 24(5)
    ld      6, 32(3)
    std     6, 32(5)
    ld      6, 40(3)
    std     6, 40(5)
    ld      6, 48(3)
    std     6, 48(5)
    ld      6, 56(3)
    std     6, 56(5)
    // v[8..16] = IV
    li      6, 0
    oris    6, 6, 0x6a09
    ori     6, 6, 0xe667
    sldi    6, 6, 32
    oris    6, 6, 0xf3bc
    ori     6, 6, 0xc908
    std     6, 64(5)
    li      6, 0
    oris    6, 6, 0xbb67
    ori     6, 6, 0xae85
    sldi    6, 6, 32
    oris    6, 6, 0x84ca
    ori     6, 6, 0xa73b
    std     6, 72(5)
    li      6, 0
    oris    6, 6, 0x3c6e
    ori     6, 6, 0xf372
    sldi    6, 6, 32
    oris    6, 6, 0xfe94
    ori     6, 6, 0xf82b
    std     6, 80(5)
    li      6, 0
    oris    6, 6, 0xa54f
    ori     6, 6, 0xf53a
    sldi    6, 6, 32
    oris    6, 6, 0x5f1d
    ori     6, 6, 0x36f1
    std     6, 88(5)
    li      6, 0
    oris    6, 6, 0x510e
    ori     6, 6, 0x527f
    sldi    6, 6, 32
    oris    6, 6, 0xade6
    ori     6, 6, 0x82d1
    std     6, 96(5)
    li      6, 0
    oris    6, 6, 0x9b05
    ori     6, 6, 0x688c
    sldi    6, 6, 32
    oris    6, 6, 0x2b3e
    ori     6, 6, 0x6c1f
    std     6, 104(5)
    li      6, 0
    oris    6, 6, 0x1f83
    ori     6, 6, 0xd9ab
    sldi    6, 6, 32
    oris    6, 6, 0xfb41
    ori     6, 6, 0xbd6b
    std     6, 112(5)
    li      6, 0
    oris    6, 6, 0x5be0
    ori     6, 6, 0xcd19
    sldi    6, 6, 32
    oris    6, 6, 0x137e
    ori     6, 6, 0x2179
    std     6, 120(5)
    // v[12] ^= t low, v[13] ^= t high, v[14] ^= f0
    ld      6, 128(5)
    ld      7, 96(5)
    xor     7, 7, 6
    std     7, 96(5)
    ld      6, 136(5)
    ld      7, 104(5)
    xor     7, 7, 6
    std     7, 104(5)
    ld      6, 144(5)
    ld      7, 112(5)
    xor     7, 7, 6
    std     7, 112(5)
    // Round 0
    konstruct_b2_g 0, 32, 64, 96, 0, 8
    konstruct_b2_g 8, 40, 72, 104, 16, 24
    konstruct_b2_g 16, 48, 80, 112, 32, 40
    konstruct_b2_g 24, 56, 88, 120, 48, 56
    konstruct_b2_g 0, 40, 80, 120, 64, 72
    konstruct_b2_g 8, 48, 88, 96, 80, 88
    konstruct_b2_g 16, 56, 64, 104, 96, 104
    konstruct_b2_g 24, 32, 72, 112, 112, 120
    // Round 1
    konstruct_b2_g 0, 32, 64, 96, 112, 80
    konstruct_b2_g 8, 40, 72, 104, 32, 64
    konstruct_b2_g 16, 48, 80, 112, 72, 120
    konstruct_b2_g 24, 56, 88, 120, 104, 48
    konstruct_b2_g 0, 40, 80, 120, 8, 96
    konstruct_b2_g 8, 48, 88, 96, 0, 16
    konstruct_b2_g 16, 56, 64, 104, 88, 56
    konstruct_b2_g 24, 32, 72, 112, 40, 24
    // Round 2
    konstruct_b2_g 0, 32, 64, 96, 88, 64
    konstruct_b2_g 8, 40, 72, 104, 96, 0
    konstruct_b2_g 16, 48, 80, 112, 40, 16
    konstruct_b2_g 24, 56, 88, 120, 120, 104
    konstruct_b2_g 0, 40, 80, 120, 80, 112
    konstruct_b2_g 8, 48, 88, 96, 24, 48
    konstruct_b2_g 16, 56, 64, 104, 56, 8
    konstruct_b2_g 24, 32, 72, 112, 72, 32
    // Round 3
    konstruct_b2_g 0, 32, 64, 96, 56, 72
    konstruct_b2_g 8, 40, 72, 104, 24, 8
    konstruct_b2_g 16, 48, 80, 112, 104, 96
    konstruct_b2_g 24, 56, 88, 120, 88, 112
    konstruct_b2_g 0, 40, 80, 120, 16, 48
    konstruct_b2_g 8, 48, 88, 96, 40, 80
    konstruct_b2_g 16, 56, 64, 104, 32, 0
    konstruct_b2_g 24, 32, 72, 112, 120, 64
    // Round 4
    konstruct_b2_g 0, 32, 64, 96, 72, 0
    konstruct_b2_g 8, 40, 72, 104, 40, 56
    konstruct_b2_g 16, 48, 80, 112, 16, 32
    konstruct_b2_g 24, 56, 88, 120, 80, 120
    konstruct_b2_g 0, 40, 80, 120, 112, 8
    konstruct_b2_g 8, 48, 88, 96, 88, 96
    konstruct_b2_g 16, 56, 64, 104, 48, 64
    konstruct_b2_g 24, 32, 72, 112, 24, 104
    // Round 5
    konstruct_b2_g 0, 32, 64, 96, 16, 96
    konstruct_b2_g 8, 40, 72, 104, 48, 80
    konstruct_b2_g 16, 48, 80, 112, 0, 88
    konstruct_b2_g 24, 56, 88, 120, 64, 24
    konstruct_b2_g 0, 40, 80, 120, 32, 104
    konstruct_b2_g 8, 48, 88, 96, 56, 40
    konstruct_b2_g 16, 56, 64, 104, 120, 112
    konstruct_b2_g 24, 32, 72, 112, 8, 72
    // Round 6
    konstruct_b2_g 0, 32, 64, 96, 96, 40
    konstruct_b2_g 8, 40, 72, 104, 8, 120
    konstruct_b2_g 16, 48, 80, 112, 112, 104
    konstruct_b2_g 24, 56, 88, 120, 32, 80
    konstruct_b2_g 0, 40, 80, 120, 0, 56
    konstruct_b2_g 8, 48, 88, 96, 48, 24
    konstruct_b2_g 16, 56, 64, 104, 72, 16
    konstruct_b2_g 24, 32, 72, 112, 64, 88
    // Round 7
    konstruct_b2_g 0, 32, 64, 96, 104, 88
    konstruct_b2_g 8, 40, 72, 104, 56, 112
    konstruct_b2_g 16, 48, 80, 112, 96, 8
    konstruct_b2_g 24, 56, 88, 120, 24, 72
    konstruct_b2_g 0, 40, 80, 120, 40, 0
    konstruct_b2_g 8, 48, 88, 96, 120, 32
    konstruct_b2_g 16, 56, 64, 104, 64, 48
    konstruct_b2_g 24, 32, 72, 112, 16, 80
    // Round 8
    konstruct_b2_g 0, 32, 64, 96, 48, 120
    konstruct_b2_g 8, 40, 72, 104, 112, 72
    konstruct_b2_g 16, 48, 80, 112, 88, 24
    konstruct_b2_g 24, 56, 88, 120, 0, 64
    konstruct_b2_g 0, 40, 80, 120, 96, 16
    konstruct_b2_g 8, 48, 88, 96, 104, 56
    konstruct_b2_g 16, 56, 64, 104, 8, 32
    konstruct_b2_g 24, 32, 72, 112, 80, 40
    // Round 9
    konstruct_b2_g 0, 32, 64, 96, 80, 16
    konstruct_b2_g 8, 40, 72, 104, 64, 32
    konstruct_b2_g 16, 48, 80, 112, 56, 48
    konstruct_b2_g 24, 56, 88, 120, 8, 40
    konstruct_b2_g 0, 40, 80, 120, 120, 88
    konstruct_b2_g 8, 48, 88, 96, 72, 112
    konstruct_b2_g 16, 56, 64, 104, 24, 96
    konstruct_b2_g 24, 32, 72, 112, 104, 0
    // Round 10
    konstruct_b2_g 0, 32, 64, 96, 0, 8
    konstruct_b2_g 8, 40, 72, 104, 16, 24
    konstruct_b2_g 16, 48, 80, 112, 32, 40
    konstruct_b2_g 24, 56, 88, 120, 48, 56
    konstruct_b2_g 0, 40, 80, 120, 64, 72
    konstruct_b2_g 8, 48, 88, 96, 80, 88
    konstruct_b2_g 16, 56, 64, 104, 96, 104
    konstruct_b2_g 24, 32, 72, 112, 112, 120
    // Round 11
    konstruct_b2_g 0, 32, 64, 96, 112, 80
    konstruct_b2_g 8, 40, 72, 104, 32, 64
    konstruct_b2_g 16, 48, 80, 112, 72, 120
    konstruct_b2_g 24, 56, 88, 120, 104, 48
    konstruct_b2_g 0, 40, 80, 120, 8, 96
    konstruct_b2_g 8, 48, 88, 96, 0, 16
    konstruct_b2_g 16, 56, 64, 104, 88, 56
    konstruct_b2_g 24, 32, 72, 112, 40, 24
    // h[i] ^= v[i] ^ v[i + 8]
    ld      6, 0(5)
    ld      7, 64(5)
    ld      8, 0(3)
    xor     6, 6, 7
    xor     8, 8, 6
    std     8, 0(3)
    ld      6, 8(5)
    ld      7, 72(5)
    ld      8, 8(3)
    xor     6, 6, 7
    xor     8, 8, 6
    std     8, 8(3)
    ld      6, 16(5)
    ld      7, 80(5)
    ld      8, 16(3)
    xor     6, 6, 7
    xor     8, 8, 6
    std     8, 16(3)
    ld      6, 24(5)
    ld      7, 88(5)
    ld      8, 24(3)
    xor     6, 6, 7
    xor     8, 8, 6
    std     8, 24(3)
    ld      6, 32(5)
    ld      7, 96(5)
    ld      8, 32(3)
    xor     6, 6, 7
    xor     8, 8, 6
    std     8, 32(3)
    ld      6, 40(5)
    ld      7, 104(5)
    ld      8, 40(3)
    xor     6, 6, 7
    xor     8, 8, 6
    std     8, 40(3)
    ld      6, 48(5)
    ld      7, 112(5)
    ld      8, 48(3)
    xor     6, 6, 7
    xor     8, 8, 6
    std     8, 48(3)
    ld      6, 56(5)
    ld      7, 120(5)
    ld      8, 56(3)
    xor     6, 6, 7
    xor     8, 8, 6
    std     8, 56(3)
    blr

    .purgem konstruct_b2_g
