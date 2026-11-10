// BLAKE2b compression: RISC-V RV64GC.
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
//   The standard RISC-V ELF psABI, LP64D, declared extern "C" in Rust.
//     a0  h       pointer to the eight state words (read and written)
//     a1  block   pointer to the 128-byte block, aligned to 8 (read)
//     a2  work    pointer to the work area (read and written)
//   Returns nothing. Clobbers t0 to t6, caller-saved temporaries. RV64GC has no rotate
//   instruction (that is the Zbb extension), so each rotation is two shifts and an OR. RISC-V
//   is little-endian, so G loads the message words straight from the block.
//
// Why it is safe
//   It reads h[0..8], block[0..128] and the work area's first 152 bytes, and writes only h and
//   v: all inside the three live objects the Rust caller passes, which do not overlap. Every
//   load and store is of an aligned 64-bit word. It is a leaf that never touches sp or ra,
//   uses no floating-point or vector registers and keeps no state between calls, so it is
//   reentrant and thread safe.
//
// Symbol, section and alignment
//   The plain name konstruct_blake2b_compress, the name Rust links to. `.text` is the ELF code
//   section, and `.p2align 2` aligns the entry to 4 bytes, the size of the base instructions
//   used. On ELF targets src/identity/blake2b/mod.rs adds the symbol's `.type` and `.size`. The
//   macro is removed with `.purgem` at the end.

    .macro konstruct_b2_g a, b, c, d, x, y
    ld      t0, \a(a2)
    ld      t1, \b(a2)
    ld      t2, \c(a2)
    ld      t3, \d(a2)
    ld      t4, \x(a1)
    ld      t5, \y(a1)
    add     t0, t0, t1                  // a = a + b + m[x]
    add     t0, t0, t4
    xor     t3, t3, t0                  // d = (d ^ a) >>> 32
    srli    t6, t3, 32
    slli    t3, t3, 32
    or      t3, t3, t6
    add     t2, t2, t3                  // c = c + d
    xor     t1, t1, t2                  // b = (b ^ c) >>> 24
    srli    t6, t1, 24
    slli    t1, t1, 40
    or      t1, t1, t6
    add     t0, t0, t1                  // a = a + b + m[y]
    add     t0, t0, t5
    xor     t3, t3, t0                  // d = (d ^ a) >>> 16
    srli    t6, t3, 16
    slli    t3, t3, 48
    or      t3, t3, t6
    add     t2, t2, t3                  // c = c + d
    xor     t1, t1, t2                  // b = (b ^ c) >>> 63
    srli    t6, t1, 63
    slli    t1, t1, 1
    or      t1, t1, t6
    sd      t0, \a(a2)
    sd      t1, \b(a2)
    sd      t2, \c(a2)
    sd      t3, \d(a2)
    .endm

    .text
    .globl konstruct_blake2b_compress
    .p2align 2
konstruct_blake2b_compress:
    // v[0..8] = h
    ld      t0, 0(a0)
    sd      t0, 0(a2)
    ld      t0, 8(a0)
    sd      t0, 8(a2)
    ld      t0, 16(a0)
    sd      t0, 16(a2)
    ld      t0, 24(a0)
    sd      t0, 24(a2)
    ld      t0, 32(a0)
    sd      t0, 32(a2)
    ld      t0, 40(a0)
    sd      t0, 40(a2)
    ld      t0, 48(a0)
    sd      t0, 48(a2)
    ld      t0, 56(a0)
    sd      t0, 56(a2)
    // v[8..16] = IV
    li      t0, 0x6a09e667f3bcc908
    sd      t0, 64(a2)
    li      t0, 0xbb67ae8584caa73b
    sd      t0, 72(a2)
    li      t0, 0x3c6ef372fe94f82b
    sd      t0, 80(a2)
    li      t0, 0xa54ff53a5f1d36f1
    sd      t0, 88(a2)
    li      t0, 0x510e527fade682d1
    sd      t0, 96(a2)
    li      t0, 0x9b05688c2b3e6c1f
    sd      t0, 104(a2)
    li      t0, 0x1f83d9abfb41bd6b
    sd      t0, 112(a2)
    li      t0, 0x5be0cd19137e2179
    sd      t0, 120(a2)
    // v[12] ^= t low, v[13] ^= t high, v[14] ^= f0
    ld      t0, 128(a2)
    ld      t1, 96(a2)
    xor     t1, t1, t0
    sd      t1, 96(a2)
    ld      t0, 136(a2)
    ld      t1, 104(a2)
    xor     t1, t1, t0
    sd      t1, 104(a2)
    ld      t0, 144(a2)
    ld      t1, 112(a2)
    xor     t1, t1, t0
    sd      t1, 112(a2)
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
    ld      t0, 0(a2)
    ld      t1, 64(a2)
    ld      t2, 0(a0)
    xor     t0, t0, t1
    xor     t2, t2, t0
    sd      t2, 0(a0)
    ld      t0, 8(a2)
    ld      t1, 72(a2)
    ld      t2, 8(a0)
    xor     t0, t0, t1
    xor     t2, t2, t0
    sd      t2, 8(a0)
    ld      t0, 16(a2)
    ld      t1, 80(a2)
    ld      t2, 16(a0)
    xor     t0, t0, t1
    xor     t2, t2, t0
    sd      t2, 16(a0)
    ld      t0, 24(a2)
    ld      t1, 88(a2)
    ld      t2, 24(a0)
    xor     t0, t0, t1
    xor     t2, t2, t0
    sd      t2, 24(a0)
    ld      t0, 32(a2)
    ld      t1, 96(a2)
    ld      t2, 32(a0)
    xor     t0, t0, t1
    xor     t2, t2, t0
    sd      t2, 32(a0)
    ld      t0, 40(a2)
    ld      t1, 104(a2)
    ld      t2, 40(a0)
    xor     t0, t0, t1
    xor     t2, t2, t0
    sd      t2, 40(a0)
    ld      t0, 48(a2)
    ld      t1, 112(a2)
    ld      t2, 48(a0)
    xor     t0, t0, t1
    xor     t2, t2, t0
    sd      t2, 48(a0)
    ld      t0, 56(a2)
    ld      t1, 120(a2)
    ld      t2, 56(a0)
    xor     t0, t0, t1
    xor     t2, t2, t0
    sd      t2, 56(a0)
    ret

    .purgem konstruct_b2_g
