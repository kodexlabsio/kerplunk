// BLAKE2b compression: AArch64 (ARMv8-A).
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
//   AAPCS64, declared extern "C" in Rust (the same on Linux, macOS and Windows on Arm).
//     x0  h       pointer to the eight state words (read and written)
//     x1  block   pointer to the 128-byte block, aligned to 8 (read)
//     x2  work    pointer to the work area (read and written)
//   Returns nothing. Clobbers x3 to x8, caller-saved temporaries, and the flags. AArch64 runs
//   little-endian here, so G loads the message words straight from the block.
//
// Why it is safe
//   It reads h[0..8], block[0..128] and the work area's first 152 bytes, and writes only h and
//   v: all inside the three live objects the Rust caller passes, which do not overlap. It is a
//   leaf that never touches sp or x30, does not use x18 (reserved on Apple platforms and
//   Windows), uses no floating-point or SIMD registers and keeps no state between calls, so it
//   is reentrant and thread safe.
//
// Symbol, section and alignment
//   The plain name konstruct_blake2b_compress, the name Rust links to (with a leading \x01
//   byte in its link_name, which tells LLVM to use the name exactly as written, without the
//   underscore Mach-O symbols otherwise get). `.text` names the code section in ELF, Mach-O
//   and COFF, and `.p2align 2` aligns the entry to the 4-byte instruction size. On ELF targets
//   src/identity/blake2b/mod.rs adds the symbol's `.type` and `.size`. The macro is removed with
//   `.purgem` at the end.

    .macro konstruct_b2_g a, b, c, d, x, y
    ldr     x3, [x2, #\a]
    ldr     x4, [x2, #\b]
    ldr     x5, [x2, #\c]
    ldr     x6, [x2, #\d]
    ldr     x7, [x1, #\x]
    ldr     x8, [x1, #\y]
    add     x3, x3, x4                  // a = a + b + m[x]
    add     x3, x3, x7
    eor     x6, x6, x3                  // d = (d ^ a) >>> 32
    ror     x6, x6, #32
    add     x5, x5, x6                  // c = c + d
    eor     x4, x4, x5                  // b = (b ^ c) >>> 24
    ror     x4, x4, #24
    add     x3, x3, x4                  // a = a + b + m[y]
    add     x3, x3, x8
    eor     x6, x6, x3                  // d = (d ^ a) >>> 16
    ror     x6, x6, #16
    add     x5, x5, x6                  // c = c + d
    eor     x4, x4, x5                  // b = (b ^ c) >>> 63
    ror     x4, x4, #63
    str     x3, [x2, #\a]
    str     x4, [x2, #\b]
    str     x5, [x2, #\c]
    str     x6, [x2, #\d]
    .endm

    .text
    .globl konstruct_blake2b_compress
    .p2align 2
konstruct_blake2b_compress:
    // v[0..8] = h
    ldr     x3, [x0, #0]
    str     x3, [x2, #0]
    ldr     x3, [x0, #8]
    str     x3, [x2, #8]
    ldr     x3, [x0, #16]
    str     x3, [x2, #16]
    ldr     x3, [x0, #24]
    str     x3, [x2, #24]
    ldr     x3, [x0, #32]
    str     x3, [x2, #32]
    ldr     x3, [x0, #40]
    str     x3, [x2, #40]
    ldr     x3, [x0, #48]
    str     x3, [x2, #48]
    ldr     x3, [x0, #56]
    str     x3, [x2, #56]
    // v[8..16] = IV
    movz    x3, #0xc908
    movk    x3, #0xf3bc, lsl #16
    movk    x3, #0xe667, lsl #32
    movk    x3, #0x6a09, lsl #48
    str     x3, [x2, #64]
    movz    x3, #0xa73b
    movk    x3, #0x84ca, lsl #16
    movk    x3, #0xae85, lsl #32
    movk    x3, #0xbb67, lsl #48
    str     x3, [x2, #72]
    movz    x3, #0xf82b
    movk    x3, #0xfe94, lsl #16
    movk    x3, #0xf372, lsl #32
    movk    x3, #0x3c6e, lsl #48
    str     x3, [x2, #80]
    movz    x3, #0x36f1
    movk    x3, #0x5f1d, lsl #16
    movk    x3, #0xf53a, lsl #32
    movk    x3, #0xa54f, lsl #48
    str     x3, [x2, #88]
    movz    x3, #0x82d1
    movk    x3, #0xade6, lsl #16
    movk    x3, #0x527f, lsl #32
    movk    x3, #0x510e, lsl #48
    str     x3, [x2, #96]
    movz    x3, #0x6c1f
    movk    x3, #0x2b3e, lsl #16
    movk    x3, #0x688c, lsl #32
    movk    x3, #0x9b05, lsl #48
    str     x3, [x2, #104]
    movz    x3, #0xbd6b
    movk    x3, #0xfb41, lsl #16
    movk    x3, #0xd9ab, lsl #32
    movk    x3, #0x1f83, lsl #48
    str     x3, [x2, #112]
    movz    x3, #0x2179
    movk    x3, #0x137e, lsl #16
    movk    x3, #0xcd19, lsl #32
    movk    x3, #0x5be0, lsl #48
    str     x3, [x2, #120]
    // v[12] ^= t low, v[13] ^= t high, v[14] ^= f0
    ldr     x3, [x2, #128]
    ldr     x4, [x2, #96]
    eor     x4, x4, x3
    str     x4, [x2, #96]
    ldr     x3, [x2, #136]
    ldr     x4, [x2, #104]
    eor     x4, x4, x3
    str     x4, [x2, #104]
    ldr     x3, [x2, #144]
    ldr     x4, [x2, #112]
    eor     x4, x4, x3
    str     x4, [x2, #112]
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
    ldr     x3, [x2, #0]
    ldr     x4, [x2, #64]
    ldr     x5, [x0, #0]
    eor     x3, x3, x4
    eor     x5, x5, x3
    str     x5, [x0, #0]
    ldr     x3, [x2, #8]
    ldr     x4, [x2, #72]
    ldr     x5, [x0, #8]
    eor     x3, x3, x4
    eor     x5, x5, x3
    str     x5, [x0, #8]
    ldr     x3, [x2, #16]
    ldr     x4, [x2, #80]
    ldr     x5, [x0, #16]
    eor     x3, x3, x4
    eor     x5, x5, x3
    str     x5, [x0, #16]
    ldr     x3, [x2, #24]
    ldr     x4, [x2, #88]
    ldr     x5, [x0, #24]
    eor     x3, x3, x4
    eor     x5, x5, x3
    str     x5, [x0, #24]
    ldr     x3, [x2, #32]
    ldr     x4, [x2, #96]
    ldr     x5, [x0, #32]
    eor     x3, x3, x4
    eor     x5, x5, x3
    str     x5, [x0, #32]
    ldr     x3, [x2, #40]
    ldr     x4, [x2, #104]
    ldr     x5, [x0, #40]
    eor     x3, x3, x4
    eor     x5, x5, x3
    str     x5, [x0, #40]
    ldr     x3, [x2, #48]
    ldr     x4, [x2, #112]
    ldr     x5, [x0, #48]
    eor     x3, x3, x4
    eor     x5, x5, x3
    str     x5, [x0, #48]
    ldr     x3, [x2, #56]
    ldr     x4, [x2, #120]
    ldr     x5, [x0, #56]
    eor     x3, x3, x4
    eor     x5, x5, x3
    str     x5, [x0, #56]
    ret

    .purgem konstruct_b2_g
