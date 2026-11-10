// BLAKE2b compression: x86_64, Intel syntax.
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
//   System V AMD64 on every operating system: Rust declares the function extern "sysv64".
//     rdi  h       pointer to the eight state words (read and written)
//     rsi  block   pointer to the 128-byte block, aligned to 8 (read)
//     rdx  work    pointer to the work area (read and written)
//   Returns nothing. Clobbers rax, rcx, r8, r9 and the flags, which System V lets a callee
//   change. x86_64 is little-endian, so G adds the message words straight from the block.
//
// Why it is safe
//   It reads h[0..8], block[0..128] and the work area's first 152 bytes, and writes only h and
//   v: all inside the three live objects the Rust caller passes, which do not overlap. It
//   touches no stack (a leaf with no pushes or frame, so Windows needs no unwind data), uses
//   no vector registers and keeps no state between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The plain name konstruct_blake2b_compress on ELF, Mach-O and COFF, as the FNV routine
//   explains; `.p2align 4` starts it on a 16-byte boundary. On ELF targets
//   src/identity/blake2b/mod.rs adds the symbol's `.type` and `.size` after the routine. The macro
//   konstruct_b2_g is removed with `.purgem` at the end, so it does not leak into other
//   assembly in the crate.

    .macro konstruct_b2_g a, b, c, d, x, y
    mov     rax, qword ptr [rdx + \a]
    mov     rcx, qword ptr [rdx + \b]
    mov     r8, qword ptr [rdx + \c]
    mov     r9, qword ptr [rdx + \d]
    add     rax, rcx                    // a = a + b + m[x]
    add     rax, qword ptr [rsi + \x]
    xor     r9, rax                     // d = (d ^ a) >>> 32
    ror     r9, 32
    add     r8, r9                      // c = c + d
    xor     rcx, r8                     // b = (b ^ c) >>> 24
    ror     rcx, 24
    add     rax, rcx                    // a = a + b + m[y]
    add     rax, qword ptr [rsi + \y]
    xor     r9, rax                     // d = (d ^ a) >>> 16
    ror     r9, 16
    add     r8, r9                      // c = c + d
    xor     rcx, r8                     // b = (b ^ c) >>> 63
    ror     rcx, 63
    mov     qword ptr [rdx + \a], rax
    mov     qword ptr [rdx + \b], rcx
    mov     qword ptr [rdx + \c], r8
    mov     qword ptr [rdx + \d], r9
    .endm

    .text
    .globl konstruct_blake2b_compress
    .p2align 4
konstruct_blake2b_compress:
    // v[0..8] = h
    mov     rax, qword ptr [rdi + 0]
    mov     qword ptr [rdx + 0], rax
    mov     rax, qword ptr [rdi + 8]
    mov     qword ptr [rdx + 8], rax
    mov     rax, qword ptr [rdi + 16]
    mov     qword ptr [rdx + 16], rax
    mov     rax, qword ptr [rdi + 24]
    mov     qword ptr [rdx + 24], rax
    mov     rax, qword ptr [rdi + 32]
    mov     qword ptr [rdx + 32], rax
    mov     rax, qword ptr [rdi + 40]
    mov     qword ptr [rdx + 40], rax
    mov     rax, qword ptr [rdi + 48]
    mov     qword ptr [rdx + 48], rax
    mov     rax, qword ptr [rdi + 56]
    mov     qword ptr [rdx + 56], rax
    // v[8..16] = IV
    movabs  rax, 0x6a09e667f3bcc908
    mov     qword ptr [rdx + 64], rax
    movabs  rax, 0xbb67ae8584caa73b
    mov     qword ptr [rdx + 72], rax
    movabs  rax, 0x3c6ef372fe94f82b
    mov     qword ptr [rdx + 80], rax
    movabs  rax, 0xa54ff53a5f1d36f1
    mov     qword ptr [rdx + 88], rax
    movabs  rax, 0x510e527fade682d1
    mov     qword ptr [rdx + 96], rax
    movabs  rax, 0x9b05688c2b3e6c1f
    mov     qword ptr [rdx + 104], rax
    movabs  rax, 0x1f83d9abfb41bd6b
    mov     qword ptr [rdx + 112], rax
    movabs  rax, 0x5be0cd19137e2179
    mov     qword ptr [rdx + 120], rax
    // v[12] ^= t low, v[13] ^= t high, v[14] ^= f0
    mov     rax, qword ptr [rdx + 128]
    xor     qword ptr [rdx + 96], rax
    mov     rax, qword ptr [rdx + 136]
    xor     qword ptr [rdx + 104], rax
    mov     rax, qword ptr [rdx + 144]
    xor     qword ptr [rdx + 112], rax
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
    mov     rax, qword ptr [rdx + 0]
    xor     rax, qword ptr [rdx + 64]
    xor     qword ptr [rdi + 0], rax
    mov     rax, qword ptr [rdx + 8]
    xor     rax, qword ptr [rdx + 72]
    xor     qword ptr [rdi + 8], rax
    mov     rax, qword ptr [rdx + 16]
    xor     rax, qword ptr [rdx + 80]
    xor     qword ptr [rdi + 16], rax
    mov     rax, qword ptr [rdx + 24]
    xor     rax, qword ptr [rdx + 88]
    xor     qword ptr [rdi + 24], rax
    mov     rax, qword ptr [rdx + 32]
    xor     rax, qword ptr [rdx + 96]
    xor     qword ptr [rdi + 32], rax
    mov     rax, qword ptr [rdx + 40]
    xor     rax, qword ptr [rdx + 104]
    xor     qword ptr [rdi + 40], rax
    mov     rax, qword ptr [rdx + 48]
    xor     rax, qword ptr [rdx + 112]
    xor     qword ptr [rdi + 48], rax
    mov     rax, qword ptr [rdx + 56]
    xor     rax, qword ptr [rdx + 120]
    xor     qword ptr [rdi + 56], rax
    ret

    .purgem konstruct_b2_g
