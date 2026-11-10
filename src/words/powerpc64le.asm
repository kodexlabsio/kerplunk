// Word boundaries in a byte string: 64-bit little-endian Power, POWER8 vector instructions.
//
// What it does
//   Finds the runs of word bytes (ASCII `a` to `z` and `0` to `9`) in the `len` bytes at `data`
//   and writes, in order, the offset of each run's first byte and the offset just past its last,
//   as 64-bit integers from `bounds`. Returns how many offsets it wrote: twice the number of
//   words. The search index splits names, descriptions and queries into words with it
//   (src/words/mod.rs).
//
// How
//   Sixteen bytes at a time: b - 'a' < 26 and b - '0' < 10, compared unsigned with `vcmpgtub`,
//   give 0xff for each word byte, and `vbpermq` (POWER8) gathers one bit of each byte into a
//   16-bit mask. `lxvd2x` loads each doubleword in little-endian order, so the bytes sit in the
//   register as two reversed halves; the bit indices `vbpermq` takes are arranged to match, so
//   bit k of the mask is byte k in memory. A word starts or ends wherever a byte differs from
//   the one before it, so mask XOR (mask << 1, with the previous block's last byte shifted in)
//   has a bit for each boundary; boundaries alternate between starts and ends. POWER8 has no
//   count of trailing zeros (`cnttzd` is POWER9), so each boundary is isolated with x AND -x and
//   located with `cntlzd`.
//   The last 1 to 15 bytes are covered by one more block that ends at `len` and overlaps the one
//   before, with the boundaries already written masked out, so nothing past `len` is read; a
//   text shorter than 16 bytes is classified one byte at a time.
//
// Calling convention
//   The 64-bit ELF ABI version 2 (ELFv2), which every little-endian Power Linux uses, declared
//   extern "C" in Rust.
//     r3  data    pointer to the first byte (need not be aligned; unused when len is 0)
//     r4  len     number of bytes
//     r5  bounds  where the offsets go: room for len + 1 of them, 8-byte aligned
//     r3          the number of offsets written (returned)
//   Clobbers r0, r5 to r12, v0 to v8 (VSX registers 32 to 40), cr0 and cr1, all volatile
//   under ELFv2. It leaves v20 to v31, which a callee must preserve, alone, and does not use
//   VRSAVE, which ELFv2 does not require a callee to maintain.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: each 16-byte load starts at an offset at most
//   len - 16, and a text shorter than that is read a byte at a time. It writes only bounds[0] to bounds[n - 1],
//   where n, the count it returns, is at most len + 1, as words and separators alternate. The
//   Rust caller passes a slice and a vector with that much spare room. It has no stack frame and
//   never touches r1, r2 (the TOC pointer, which it does not need) or the link register, through
//   which it returns, and keeps no state between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_word_bounds, the name Rust links to (with a leading
//   \x01 byte in its link_name, which tells LLVM to use the name exactly as written). As it uses
//   no TOC, its global and local entry points are the same address and it needs no
//   `.localentry`. `.text` is the ELF code section; `.p2align 4` aligns the entry to 16 bytes,
//   as compilers align function entries for POWER's instruction fetch.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_word_bounds
    .p2align 4
konstruct_word_bounds:
    mr      6, 5                        // the first offset's slot, to count the offsets
    li      7, 0                        // r7: offset of the block or byte being classified
    li      8, 0                        // r8: 1 while inside a word, else 0
    cmpldi  4, 16
    blt     0, 6f                       // fewer than 16 bytes: classify them one at a time
    li      9, 0x61
    mtvsrwz 34, 9
    vspltb  2, 2, 7                     // v2: 'a' in each byte
    li      9, 26
    mtvsrwz 35, 9
    vspltb  3, 3, 7                     // v3: 26 letters
    li      9, 0x30
    mtvsrwz 36, 9
    vspltb  4, 4, 7                     // v4: '0'
    vspltisb 5, 10                      // v5: 10 digits
    li      9, 0
    lvsl    6, 0, 9                     // v6: bytes 0 to 15
    vspltisb 7, 3
    vslb    6, 6, 7                     // the index of the first bit of each byte, 0 to 120,
    xxswapd 38, 38                      // with the halves swapped as lxvd2x leaves the data
    li      12, -1
    clrldi  12, 12, 48                  // r12: the bytes of the block not yet classified
2:
    lxvd2x  32, 3, 7                    // v0: the next sixteen bytes
    vsububm 1, 0, 2
    vcmpgtub 1, 3, 1                    // 0xff where b - 'a' < 26, unsigned: a letter
    vsububm 8, 0, 4
    vcmpgtub 8, 5, 8                    // 0xff where b - '0' < 10: a digit
    vor     1, 1, 8
    vbpermq 1, 1, 6
    mfvsrd  9, 33                       // bit k: byte k is a word byte
    sldi    10, 9, 1
    or      10, 10, 8                   // the mask a byte later, the previous byte shifted in
    xor     10, 10, 9
    srdi    8, 9, 15                    // whether the block ends inside a word
    and.    10, 10, 12                  // bit k: a word starts or ends at byte k
    beq     0, 4f
3:
    neg     11, 10
    and     11, 11, 10                  // the lowest boundary alone
    cntlzd  11, 11
    subfic  11, 11, 63                  // its position in the block
    add     11, 11, 7
    std     11, 0(5)
    addi    5, 5, 8
    addi    11, 10, -1
    and.    10, 10, 11                  // clear it
    bne     0, 3b
4:
    addi    7, 7, 16
    addi    9, 7, 16
    cmpld   9, 4
    ble     0, 2b                       // another whole block
    cmpld   7, 4
    bge     0, 8f                       // no bytes left
    subf    9, 4, 9                     // bytes of the last block already classified
    addi    7, 4, -16                   // the last block ends with the bytes
    li      12, -1
    sld     12, 12, 9
    clrldi  12, 12, 48                  // keep only the bytes not yet classified
    b       2b
6:
    cmpldi  4, 0
    beq     0, 8f
7:
    lbzx    9, 3, 7
    addi    10, 9, -0x61
    cmpldi  0, 10, 26                   // cr0: a letter, if below
    addi    11, 9, -0x30
    cmpldi  1, 11, 10                   // cr1: a digit, if below
    li      10, 1
    blt     0, 9f
    blt     1, 9f
    li      10, 0
9:
    cmpld   10, 8
    beq     0, 5f
    std     7, 0(5)                     // a word starts or ends here
    addi    5, 5, 8
    mr      8, 10
5:
    addi    7, 7, 1
    cmpld   7, 4
    blt     0, 7b
8:
    cmpldi  8, 0
    beq     0, 10f
    std     4, 0(5)                     // the last word ends with the bytes
    addi    5, 5, 8
10:
    subf    3, 6, 5
    srdi    3, 3, 3                     // offsets written
    blr
