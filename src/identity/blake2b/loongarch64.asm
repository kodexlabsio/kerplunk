// BLAKE2b compression: LoongArch64.
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
//   The LoongArch ELF psABI, LP64D, declared extern "C" in Rust.
//     a0  h       pointer to the eight state words (read and written)
//     a1  block   pointer to the 128-byte block, aligned to 8 (read)
//     a2  work    pointer to the work area (read and written)
//   Returns nothing. Clobbers t0 to t5, caller-saved temporaries. Each 64-bit constant is
//   built from four immediates: lu12i.w sets bits 12 to 31, ori bits 0 to 11, lu32i.d bits
//   32 to 51 and lu52i.d bits 52 to 63. LoongArch is little-endian, so G loads the message
//   words straight from the block.
//
// Why it is safe
//   It reads h[0..8], block[0..128] and the work area's first 152 bytes, and writes only h and
//   v: all inside the three live objects the Rust caller passes, which do not overlap. Every
//   access is an aligned doubleword. It is a leaf that never touches sp or ra, uses no
//   floating-point or vector registers and keeps no state between calls, so it is reentrant
//   and thread safe.
//
// Symbol, section and alignment
//   The plain name konstruct_blake2b_compress, the name Rust links to. `.text` is the ELF code
//   section, and `.p2align 2` aligns the entry to the 4-byte instruction size. On ELF targets
//   src/identity/blake2b/mod.rs adds the symbol's `.type` and `.size`. The macro is removed with
//   `.purgem` at the end.

    .macro konstruct_b2_g a, b, c, d, x, y
    ld.d    $t0, $a2, \a
    ld.d    $t1, $a2, \b
    ld.d    $t2, $a2, \c
    ld.d    $t3, $a2, \d
    ld.d    $t4, $a1, \x
    ld.d    $t5, $a1, \y
    add.d   $t0, $t0, $t1               // a = a + b + m[x]
    add.d   $t0, $t0, $t4
    xor     $t3, $t3, $t0               // d = (d ^ a) >>> 32
    rotri.d $t3, $t3, 32
    add.d   $t2, $t2, $t3               // c = c + d
    xor     $t1, $t1, $t2               // b = (b ^ c) >>> 24
    rotri.d $t1, $t1, 24
    add.d   $t0, $t0, $t1               // a = a + b + m[y]
    add.d   $t0, $t0, $t5
    xor     $t3, $t3, $t0               // d = (d ^ a) >>> 16
    rotri.d $t3, $t3, 16
    add.d   $t2, $t2, $t3               // c = c + d
    xor     $t1, $t1, $t2               // b = (b ^ c) >>> 63
    rotri.d $t1, $t1, 63
    st.d    $t0, $a2, \a
    st.d    $t1, $a2, \b
    st.d    $t2, $a2, \c
    st.d    $t3, $a2, \d
    .endm

    .text
    .globl konstruct_blake2b_compress
    .p2align 2
konstruct_blake2b_compress:
    // v[0..8] = h
    ld.d    $t0, $a0, 0
    st.d    $t0, $a2, 0
    ld.d    $t0, $a0, 8
    st.d    $t0, $a2, 8
    ld.d    $t0, $a0, 16
    st.d    $t0, $a2, 16
    ld.d    $t0, $a0, 24
    st.d    $t0, $a2, 24
    ld.d    $t0, $a0, 32
    st.d    $t0, $a2, 32
    ld.d    $t0, $a0, 40
    st.d    $t0, $a2, 40
    ld.d    $t0, $a0, 48
    st.d    $t0, $a2, 48
    ld.d    $t0, $a0, 56
    st.d    $t0, $a2, 56
    // v[8..16] = IV
    lu12i.w $t0, -50228
    ori     $t0, $t0, 0x908
    lu32i.d $t0, -399769
    lu52i.d $t0, $t0, 1696
    st.d    $t0, $a2, 64
    lu12i.w $t0, -504662
    ori     $t0, $t0, 0x73b
    lu32i.d $t0, 503429
    lu52i.d $t0, $t0, -1098
    st.d    $t0, $a2, 72
    lu12i.w $t0, -5809
    ori     $t0, $t0, 0x82b
    lu32i.d $t0, -68750
    lu52i.d $t0, $t0, 966
    st.d    $t0, $a2, 80
    lu12i.w $t0, 389587
    ori     $t0, $t0, 0x6f1
    lu32i.d $t0, -2758
    lu52i.d $t0, $t0, -1452
    st.d    $t0, $a2, 88
    lu12i.w $t0, -336280
    ori     $t0, $t0, 0x2d1
    lu32i.d $t0, -109953
    lu52i.d $t0, $t0, 1296
    st.d    $t0, $a2, 96
    lu12i.w $t0, 177126
    ori     $t0, $t0, 0xc1f
    lu32i.d $t0, 354444
    lu52i.d $t0, $t0, -1616
    st.d    $t0, $a2, 104
    lu12i.w $t0, -19429
    ori     $t0, $t0, 0xd6b
    lu32i.d $t0, 252331
    lu52i.d $t0, $t0, 504
    st.d    $t0, $a2, 112
    lu12i.w $t0, 79842
    ori     $t0, $t0, 0x179
    lu32i.d $t0, 52505
    lu52i.d $t0, $t0, 1470
    st.d    $t0, $a2, 120
    // v[12] ^= t low, v[13] ^= t high, v[14] ^= f0
    ld.d    $t0, $a2, 128
    ld.d    $t1, $a2, 96
    xor     $t1, $t1, $t0
    st.d    $t1, $a2, 96
    ld.d    $t0, $a2, 136
    ld.d    $t1, $a2, 104
    xor     $t1, $t1, $t0
    st.d    $t1, $a2, 104
    ld.d    $t0, $a2, 144
    ld.d    $t1, $a2, 112
    xor     $t1, $t1, $t0
    st.d    $t1, $a2, 112
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
    ld.d    $t0, $a2, 0
    ld.d    $t1, $a2, 64
    ld.d    $t2, $a0, 0
    xor     $t0, $t0, $t1
    xor     $t2, $t2, $t0
    st.d    $t2, $a0, 0
    ld.d    $t0, $a2, 8
    ld.d    $t1, $a2, 72
    ld.d    $t2, $a0, 8
    xor     $t0, $t0, $t1
    xor     $t2, $t2, $t0
    st.d    $t2, $a0, 8
    ld.d    $t0, $a2, 16
    ld.d    $t1, $a2, 80
    ld.d    $t2, $a0, 16
    xor     $t0, $t0, $t1
    xor     $t2, $t2, $t0
    st.d    $t2, $a0, 16
    ld.d    $t0, $a2, 24
    ld.d    $t1, $a2, 88
    ld.d    $t2, $a0, 24
    xor     $t0, $t0, $t1
    xor     $t2, $t2, $t0
    st.d    $t2, $a0, 24
    ld.d    $t0, $a2, 32
    ld.d    $t1, $a2, 96
    ld.d    $t2, $a0, 32
    xor     $t0, $t0, $t1
    xor     $t2, $t2, $t0
    st.d    $t2, $a0, 32
    ld.d    $t0, $a2, 40
    ld.d    $t1, $a2, 104
    ld.d    $t2, $a0, 40
    xor     $t0, $t0, $t1
    xor     $t2, $t2, $t0
    st.d    $t2, $a0, 40
    ld.d    $t0, $a2, 48
    ld.d    $t1, $a2, 112
    ld.d    $t2, $a0, 48
    xor     $t0, $t0, $t1
    xor     $t2, $t2, $t0
    st.d    $t2, $a0, 48
    ld.d    $t0, $a2, 56
    ld.d    $t1, $a2, 120
    ld.d    $t2, $a0, 56
    xor     $t0, $t0, $t1
    xor     $t2, $t2, $t0
    st.d    $t2, $a0, 56
    ret

    .purgem konstruct_b2_g
