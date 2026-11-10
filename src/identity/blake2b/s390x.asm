// BLAKE2b compression: IBM Z (z/Architecture, s390x).
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
//   The s390x ELF ABI (z/Architecture, 64-bit), declared extern "C" in Rust.
//     r2  h       pointer to the eight state words (read and written)
//     r3  block   pointer to the 128-byte block, aligned to 8 (read)
//     r4  work    pointer to the work area (read and written)
//   Returns nothing. Clobbers r0, r1, r5 and the condition code. G needs a fifth register, so
//   r6, which the ABI makes the callee preserve, is saved in the caller's register save area
//   (48(r15) holds r6, as the ABI lays it out) and restored before returning. A rotation
//   right by n is rllg (rotate left) by 64 - n.
//   z/Architecture is big-endian and BLAKE2b reads its message as little-endian words, so the
//   routine first loads each word byte-reversed (lrvg) into the work area's words at 152, and
//   G adds from there. h, v, the counter and the flag are native words on both sides.
//
// Why it is safe
//   It reads h[0..8], block[0..128] and the work area's first 152 bytes, and writes only h, v
//   and the words at 152..280: all inside the three live objects the Rust caller passes,
//   which do not overlap. Besides restoring r6, it never touches r15 (the stack pointer) or
//   r14, the return address it branches to, and writes the stack only at r6's slot in the
//   save area the caller provides. It uses no floating-point or vector registers and keeps no
//   state between calls, so it is reentrant and thread safe. llihf and oilf are from the
//   extended-immediate facility and lrvg from z/Architecture itself, all in the z10 baseline.
//
// Symbol, section and alignment
//   The plain name konstruct_blake2b_compress, the name Rust links to. `.text` is the ELF code
//   section, and `.p2align 3` aligns the entry to 8 bytes. On ELF targets
//   src/identity/blake2b/mod.rs adds the symbol's `.type` and `.size`. The macro is removed with
//   `.purgem` at the end.

    .macro konstruct_b2_g a, b, c, d, x, y
    lg      %r0, \a(%r4)
    lg      %r1, \b(%r4)
    lg      %r5, \c(%r4)
    lg      %r6, \d(%r4)
    algr    %r0, %r1                    // a = a + b + m[x]
    alg     %r0, 152+\x(%r4)
    xgr     %r6, %r0                    // d = (d ^ a) >>> 32
    rllg    %r6, %r6, 32
    algr    %r5, %r6                    // c = c + d
    xgr     %r1, %r5                    // b = (b ^ c) >>> 24
    rllg    %r1, %r1, 40
    algr    %r0, %r1                    // a = a + b + m[y]
    alg     %r0, 152+\y(%r4)
    xgr     %r6, %r0                    // d = (d ^ a) >>> 16
    rllg    %r6, %r6, 48
    algr    %r5, %r6                    // c = c + d
    xgr     %r1, %r5                    // b = (b ^ c) >>> 63
    rllg    %r1, %r1, 1
    stg     %r0, \a(%r4)
    stg     %r1, \b(%r4)
    stg     %r5, \c(%r4)
    stg     %r6, \d(%r4)
    .endm

    .text
    .globl konstruct_blake2b_compress
    .p2align 3
konstruct_blake2b_compress:
    stg     %r6, 48(%r15)               // r6 is the callee's to preserve
    // The message words, little-endian
    lrvg    %r0, 0(%r3)
    stg     %r0, 152(%r4)
    lrvg    %r0, 8(%r3)
    stg     %r0, 160(%r4)
    lrvg    %r0, 16(%r3)
    stg     %r0, 168(%r4)
    lrvg    %r0, 24(%r3)
    stg     %r0, 176(%r4)
    lrvg    %r0, 32(%r3)
    stg     %r0, 184(%r4)
    lrvg    %r0, 40(%r3)
    stg     %r0, 192(%r4)
    lrvg    %r0, 48(%r3)
    stg     %r0, 200(%r4)
    lrvg    %r0, 56(%r3)
    stg     %r0, 208(%r4)
    lrvg    %r0, 64(%r3)
    stg     %r0, 216(%r4)
    lrvg    %r0, 72(%r3)
    stg     %r0, 224(%r4)
    lrvg    %r0, 80(%r3)
    stg     %r0, 232(%r4)
    lrvg    %r0, 88(%r3)
    stg     %r0, 240(%r4)
    lrvg    %r0, 96(%r3)
    stg     %r0, 248(%r4)
    lrvg    %r0, 104(%r3)
    stg     %r0, 256(%r4)
    lrvg    %r0, 112(%r3)
    stg     %r0, 264(%r4)
    lrvg    %r0, 120(%r3)
    stg     %r0, 272(%r4)
    // v[0..8] = h
    lg      %r0, 0(%r2)
    stg     %r0, 0(%r4)
    lg      %r0, 8(%r2)
    stg     %r0, 8(%r4)
    lg      %r0, 16(%r2)
    stg     %r0, 16(%r4)
    lg      %r0, 24(%r2)
    stg     %r0, 24(%r4)
    lg      %r0, 32(%r2)
    stg     %r0, 32(%r4)
    lg      %r0, 40(%r2)
    stg     %r0, 40(%r4)
    lg      %r0, 48(%r2)
    stg     %r0, 48(%r4)
    lg      %r0, 56(%r2)
    stg     %r0, 56(%r4)
    // v[8..16] = IV
    llihf   %r0, 0x6a09e667
    oilf    %r0, 0xf3bcc908
    stg     %r0, 64(%r4)
    llihf   %r0, 0xbb67ae85
    oilf    %r0, 0x84caa73b
    stg     %r0, 72(%r4)
    llihf   %r0, 0x3c6ef372
    oilf    %r0, 0xfe94f82b
    stg     %r0, 80(%r4)
    llihf   %r0, 0xa54ff53a
    oilf    %r0, 0x5f1d36f1
    stg     %r0, 88(%r4)
    llihf   %r0, 0x510e527f
    oilf    %r0, 0xade682d1
    stg     %r0, 96(%r4)
    llihf   %r0, 0x9b05688c
    oilf    %r0, 0x2b3e6c1f
    stg     %r0, 104(%r4)
    llihf   %r0, 0x1f83d9ab
    oilf    %r0, 0xfb41bd6b
    stg     %r0, 112(%r4)
    llihf   %r0, 0x5be0cd19
    oilf    %r0, 0x137e2179
    stg     %r0, 120(%r4)
    // v[12] ^= t low, v[13] ^= t high, v[14] ^= f0
    lg      %r0, 96(%r4)
    xg      %r0, 128(%r4)
    stg     %r0, 96(%r4)
    lg      %r0, 104(%r4)
    xg      %r0, 136(%r4)
    stg     %r0, 104(%r4)
    lg      %r0, 112(%r4)
    xg      %r0, 144(%r4)
    stg     %r0, 112(%r4)
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
    lg      %r0, 0(%r4)
    xg      %r0, 64(%r4)
    xg      %r0, 0(%r2)
    stg     %r0, 0(%r2)
    lg      %r0, 8(%r4)
    xg      %r0, 72(%r4)
    xg      %r0, 8(%r2)
    stg     %r0, 8(%r2)
    lg      %r0, 16(%r4)
    xg      %r0, 80(%r4)
    xg      %r0, 16(%r2)
    stg     %r0, 16(%r2)
    lg      %r0, 24(%r4)
    xg      %r0, 88(%r4)
    xg      %r0, 24(%r2)
    stg     %r0, 24(%r2)
    lg      %r0, 32(%r4)
    xg      %r0, 96(%r4)
    xg      %r0, 32(%r2)
    stg     %r0, 32(%r2)
    lg      %r0, 40(%r4)
    xg      %r0, 104(%r4)
    xg      %r0, 40(%r2)
    stg     %r0, 40(%r2)
    lg      %r0, 48(%r4)
    xg      %r0, 112(%r4)
    xg      %r0, 48(%r2)
    stg     %r0, 48(%r2)
    lg      %r0, 56(%r4)
    xg      %r0, 120(%r4)
    xg      %r0, 56(%r2)
    stg     %r0, 56(%r2)
    lg      %r6, 48(%r15)
    br      %r14

    .purgem konstruct_b2_g
