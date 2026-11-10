// Substring test: 64-bit little-endian Power, POWER8 vector instructions.
//
// What it does
//   Returns 1 when the `needle_len` bytes at `needle` occur in the `haystack_len` bytes at
//   `haystack`, else 0. An empty needle occurs in every haystack, and one longer than the
//   haystack in none. The search index tests labels against query words with it
//   (src/substring/mod.rs).
//
// How
//   With n = needle_len, a match can start at offsets 0 to haystack_len - n, the candidates.
//   Sixteen candidates at a time: the bytes at the candidates are compared with the needle's
//   first byte, and the bytes n - 1 further on with its last, and `vbpermq` (POWER8) gathers the
//   two comparisons ANDed into a 16-bit mask of the candidates where both match. `lxvd2x` loads
//   each doubleword in little-endian order, so the bit indices `vbpermq` takes are arranged to
//   match and bit k is candidate k. Each candidate is then compared in full, byte by byte; POWER8
//   has no count of trailing zeros, so each is isolated with x AND -x and located with `cntlzd`.
//   When fewer than sixteen candidates are left, one more block ends at the last candidate and
//   overlaps the one before (a candidate seen twice fails twice). A haystack with fewer than
//   sixteen candidates is tested one candidate at a time.
//
// Calling convention
//   The 64-bit ELF ABI version 2 (ELFv2), which every little-endian Power Linux uses, declared
//   extern "C" in Rust.
//     r3  haystack      pointer to its first byte (need not be aligned)
//     r4  haystack_len  number of bytes
//     r5  needle        pointer to its first byte (need not be aligned)
//     r6  needle_len    number of bytes
//     r3                1 when the needle occurs, else 0 (returned)
//   Clobbers r0, r4 and r7 to r12, v0 to v7 (VSX registers 32 to 39) and cr0, all volatile
//   under ELFv2. It leaves v20 to v31, which a callee must preserve, alone, and does not use
//   VRSAVE, which ELFv2 does not require a callee to maintain.
//
// Why it is safe
//   It reads only haystack[0] to haystack[haystack_len - 1] and needle[0] to
//   needle[needle_len - 1]: the needle only when it is not empty and no longer than the
//   haystack, the first-byte loads at candidates 0 to haystack_len - n, and the last-byte loads
//   n - 1 bytes further, which end at haystack_len - 1. It writes no memory, has no stack frame
//   and never touches r1, r2 (the TOC pointer, which it does not need) or the link register,
//   through which it returns, and keeps no state between calls, so it is reentrant and thread
//   safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_contains, the name Rust links to (with a leading \x01
//   byte in its link_name, which tells LLVM to use the name exactly as written). As it uses no
//   TOC, its global and local entry points are the same address and it needs no `.localentry`.
//   `.text` is the ELF code section; `.p2align 4` aligns the entry to 16 bytes, as compilers
//   align function entries for POWER's instruction fetch.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_contains
    .p2align 4
konstruct_contains:
    cmpldi  6, 0
    beq     0, 8f                       // an empty needle occurs everywhere
    cmpld   6, 4
    bgt     0, 9f                       // a needle longer than the haystack occurs nowhere
    subf    4, 6, 4
    add     4, 3, 4                     // r4: the last candidate
    addi    7, 6, -1                    // r7: the offset of the needle's last byte
    lbz     8, 0(5)                     // r8: the needle's first byte
    lbzx    9, 5, 7                     // r9: its last
    addi    10, 3, 15
    cmpld   10, 4
    bgt     0, 7f                       // fewer than 16 candidates: one at a time
    mtvsrwz 36, 8
    vspltb  4, 4, 7                     // v4: the first byte, sixteen times
    mtvsrwz 37, 9
    vspltb  5, 5, 7                     // v5: the last byte
    li      10, 0
    lvsl    6, 0, 10                    // v6: bytes 0 to 15
    vspltisb 7, 3
    vslb    6, 6, 7                     // the index of the first bit of each byte, 0 to 120,
    xxswapd 38, 38                      // with the halves swapped as lxvd2x leaves the data
2:                                      // r3: the block's first candidate
    lxvd2x  32, 0, 3
    lxvd2x  33, 3, 7
    vcmpequb 0, 0, 4
    vcmpequb 1, 1, 5
    vand    0, 0, 1
    vbpermq 0, 0, 6
    mfvsrd  10, 32                      // bit k: candidate k matches the first and last bytes
    cmpldi  10, 0
    beq     0, 5f
3:
    neg     11, 10
    and     11, 11, 10
    cntlzd  11, 11
    subfic  11, 11, 63
    add     11, 3, 11                   // r11: the candidate
    li      12, 1                       // r12: the next byte to compare
4:
    cmpld   12, 7
    bge     0, 8f                       // every byte between the first and last matches
    lbzx    0, 11, 12
    lbzx    9, 5, 12
    cmpld   0, 9
    bne     0, 6f
    addi    12, 12, 1
    b       4b
6:
    addi    11, 10, -1
    and.    10, 10, 11                  // the next candidate in the block
    bne     0, 3b
5:
    addi    3, 3, 16
    addi    10, 3, 15
    cmpld   10, 4
    ble     0, 2b                       // another whole block
    cmpld   3, 4
    bgt     0, 9f                       // no candidates left
    addi    3, 4, -15                   // the last block ends with the last candidate
    b       2b
7:                                      // one candidate at a time
    lbz     0, 0(3)
    cmpld   0, 8
    bne     0, 11f
    lbzx    0, 3, 7
    cmpld   0, 9
    bne     0, 11f
    li      12, 1
10:
    cmpld   12, 7
    bge     0, 8f
    lbzx    0, 3, 12
    lbzx    10, 5, 12
    cmpld   0, 10
    bne     0, 11f
    addi    12, 12, 1
    b       10b
11:
    addi    3, 3, 1
    cmpld   3, 4
    ble     0, 7b
9:
    li      3, 0
    blr
8:
    li      3, 1
    blr
