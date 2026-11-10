// Substring test: AArch64 (ARMv8-A) with NEON, GNU syntax.
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
//   first byte, and the bytes n - 1 further on with its last, and the two comparisons ANDed,
//   narrowed by `shrn` to four bits a candidate, give a 64-bit mask of the candidates where both
//   match. Each is then compared in full, byte by byte, as `rbit` and `clz` find it. When fewer
//   than sixteen candidates are left, one more block ends at the last candidate and overlaps the
//   one before (a candidate seen twice fails twice). A haystack with fewer than sixteen
//   candidates is tested one candidate at a time.
//
// Calling convention
//   AAPCS64, the procedure call standard every AArch64 operating system uses (Linux, macOS and
//   Windows alike for these arguments), declared extern "C" in Rust.
//     x0  haystack      pointer to its first byte (need not be aligned)
//     x1  haystack_len  number of bytes
//     x2  needle        pointer to its first byte (need not be aligned)
//     x3  needle_len    number of bytes
//     x0                1 when the needle occurs, else 0 (returned)
//   Clobbers x1 and x4 to x12, v0, v1, v4, v5 and the condition flags, all of which AAPCS64
//   lets a callee change. It leaves v8 to v15, whose low halves a callee must preserve, alone,
//   and does not touch x18, the platform register on macOS and Windows.
//
// Why it is safe
//   It reads only haystack[0] to haystack[haystack_len - 1] and needle[0] to
//   needle[needle_len - 1]: the needle only when it is not empty and no longer than the
//   haystack, the first-byte loads at candidates 0 to haystack_len - n, and the last-byte loads
//   n - 1 bytes further, which end at haystack_len - 1. It writes no memory, never touches the
//   stack or the link register (it is a leaf, so it needs no frame record and no unwind data)
//   and keeps no state between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_contains on ELF, Mach-O and COFF alike: Rust links to
//   it by that exact name, as a leading \x01 byte in its link_name tells LLVM not to add the
//   underscore Mach-O symbols otherwise get. `.text` names the code section in all three object
//   formats, and `.p2align 2` aligns the entry to 4 bytes, the size of every A64 instruction.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_contains
    .p2align 2
konstruct_contains:
    cbz     x3, 8f                      // an empty needle occurs everywhere
    cmp     x3, x1
    b.hi    9f                          // a needle longer than the haystack occurs nowhere
    sub     x1, x1, x3
    add     x1, x0, x1                  // x1: the last candidate
    sub     x4, x3, #1                  // x4: the offset of the needle's last byte
    ldrb    w5, [x2]                    // w5: the needle's first byte
    ldrb    w6, [x2, x4]                // w6: its last
    add     x7, x0, #15
    cmp     x7, x1
    b.hi    7f                          // fewer than 16 candidates: one at a time
    dup     v4.16b, w5
    dup     v5.16b, w6
2:                                      // x0: the block's first candidate
    ldr     q0, [x0]
    ldr     q1, [x0, x4]
    cmeq    v0.16b, v0.16b, v4.16b
    cmeq    v1.16b, v1.16b, v5.16b
    and     v0.16b, v0.16b, v1.16b
    shrn    v0.8b, v0.8h, #4
    fmov    x7, d0
    and     x7, x7, #0x8888888888888888 // bit 4k + 3: candidate k matches the first and last
    cbz     x7, 5f
3:
    rbit    x8, x7
    clz     x8, x8
    add     x8, x0, x8, lsr #2          // x8: the candidate
    mov     x9, #1                      // x9: the next byte to compare
4:
    cmp     x9, x4
    b.hs    8f                          // every byte between the first and last matches
    ldrb    w10, [x8, x9]
    ldrb    w11, [x2, x9]
    cmp     w10, w11
    b.ne    6f
    add     x9, x9, #1
    b       4b
6:
    sub     x12, x7, #1
    and     x7, x7, x12                 // the next candidate in the block
    cbnz    x7, 3b
5:
    add     x0, x0, #16
    add     x7, x0, #15
    cmp     x7, x1
    b.ls    2b                          // another whole block
    cmp     x0, x1
    b.hi    9f                          // no candidates left
    sub     x0, x1, #15                 // the last block ends with the last candidate
    b       2b
7:                                      // one candidate at a time
    ldrb    w10, [x0]
    cmp     w10, w5
    b.ne    11f
    ldrb    w10, [x0, x4]
    cmp     w10, w6
    b.ne    11f
    mov     x9, #1
10:
    cmp     x9, x4
    b.hs    8f
    ldrb    w10, [x0, x9]
    ldrb    w11, [x2, x9]
    cmp     w10, w11
    b.ne    11f
    add     x9, x9, #1
    b       10b
11:
    add     x0, x0, #1
    cmp     x0, x1
    b.ls    7b
9:
    mov     x0, #0
    ret
8:
    mov     x0, #1
    ret
