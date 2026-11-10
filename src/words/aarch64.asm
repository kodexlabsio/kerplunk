// Word boundaries in a byte string: AArch64 (ARMv8-A) with NEON, GNU syntax.
//
// What it does
//   Finds the runs of word bytes (ASCII `a` to `z` and `0` to `9`) in the `len` bytes at `data`
//   and writes, in order, the offset of each run's first byte and the offset just past its last,
//   as 64-bit integers from `bounds`. Returns how many offsets it wrote: twice the number of
//   words. The search index splits names, descriptions and queries into words with it
//   (src/words/mod.rs).
//
// How
//   Sixteen bytes at a time: b - 'a' < 26 and b - '0' < 10, compared unsigned with `cmhi`, give
//   0xff for each word byte. NEON has no instruction that gathers one bit per byte, so `shrn`
//   narrows each pair of bytes to one, leaving a 64-bit mask with four bits per byte (all set
//   for a word byte). A word starts or ends wherever a byte differs from the one before it, so
//   mask XOR (mask << 4, with the previous block's last byte shifted in), keeping one bit of
//   each four, has a bit for each boundary; boundaries alternate between starts and ends.
//   `rbit` and `clz` find each in turn.
//   The last 1 to 15 bytes are covered by one more block that ends at `len` and overlaps the one
//   before, with the boundaries already written masked out, so nothing past `len` is read; a
//   text shorter than 16 bytes is classified one byte at a time.
//
// Calling convention
//   AAPCS64, the procedure call standard every AArch64 operating system uses (Linux, macOS and
//   Windows alike for these arguments), declared extern "C" in Rust.
//     x0  data    pointer to the first byte (need not be aligned; unused when len is 0)
//     x1  len     number of bytes
//     x2  bounds  where the offsets go: room for len + 1 of them, 8-byte aligned
//     x0          the number of offsets written (returned)
//   Clobbers x2 to x10, v0 to v2, v16 to v19 and the condition flags, all of which AAPCS64 lets
//   a callee change. It leaves v8 to v15 alone, whose low halves a callee must preserve, and
//   does not touch x18, the platform register on macOS and Windows.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: each 16-byte load starts at an offset at most
//   len - 16, and a text shorter than that is read a byte at a time. It writes only bounds[0] to bounds[n - 1],
//   where n, the count it returns, is at most len + 1, as words and separators alternate. The
//   Rust caller passes a slice and a vector with that much spare room. It never touches the
//   stack or the link register (it is a leaf, so it needs no frame record and no unwind data)
//   and keeps no state between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_word_bounds on ELF, Mach-O and COFF alike: Rust links
//   to it by that exact name, as a leading \x01 byte in its link_name tells LLVM not to add the
//   underscore Mach-O symbols otherwise get. `.text` names the code section in all three object
//   formats, and `.p2align 2` aligns the entry to 4 bytes, the size of every A64 instruction.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_word_bounds
    .p2align 2
konstruct_word_bounds:
    mov     x3, x2                      // the first offset's slot, to count the offsets
    mov     x4, #0                      // x4: offset of the block or byte being classified
    mov     x5, #0                      // x5: 1 while inside a word, else 0
    cmp     x1, #16
    b.lo    6f                          // fewer than 16 bytes: classify them one at a time
    movi    v16.16b, #0x61              // 'a'
    movi    v17.16b, #26                // letters
    movi    v18.16b, #0x30              // '0'
    movi    v19.16b, #10                // digits
    mov     x10, #-1                    // x10: the bytes of the block not yet classified
2:
    ldr     q0, [x0, x4]
    sub     v1.16b, v0.16b, v16.16b
    cmhi    v1.16b, v17.16b, v1.16b     // 0xff where b - 'a' < 26, unsigned: a letter
    sub     v2.16b, v0.16b, v18.16b
    cmhi    v2.16b, v19.16b, v2.16b     // 0xff where b - '0' < 10: a digit
    orr     v1.16b, v1.16b, v2.16b
    shrn    v1.8b, v1.8h, #4
    fmov    x6, d1                      // bits 4k to 4k + 3: byte k is a word byte
    neg     x7, x5
    and     x7, x7, #0xf                // the previous byte, as a mask of four bits
    orr     x7, x7, x6, lsl #4          // the mask a byte later
    eor     x7, x7, x6
    and     x7, x7, #0x1111111111111111
    and     x7, x7, x10                 // bit 4k: a word starts or ends at byte k
    lsr     x5, x6, #63                 // whether the block ends inside a word
    cbz     x7, 4f
3:
    rbit    x8, x7
    clz     x8, x8                      // the next boundary in the block, times four
    add     x8, x4, x8, lsr #2
    str     x8, [x2], #8
    sub     x9, x7, #1
    and     x7, x7, x9                  // clear it
    cbnz    x7, 3b
4:
    add     x4, x4, #16
    add     x9, x4, #16
    cmp     x9, x1
    b.ls    2b                          // another whole block
    cmp     x4, x1
    b.hs    8f                          // no bytes left
    sub     x9, x9, x1                  // bytes of the last block already classified
    sub     x4, x1, #16                 // the last block ends with the bytes
    lsl     x9, x9, #2
    mov     x10, #-1
    lsl     x10, x10, x9                // keep only the bytes not yet classified
    b       2b
6:
    cbz     x1, 8f
7:
    ldrb    w6, [x0, x4]
    sub     w7, w6, #0x61
    cmp     w7, #26
    cset    w7, lo                      // a letter
    sub     w6, w6, #0x30
    cmp     w6, #10
    cset    w6, lo                      // a digit
    orr     w6, w6, w7
    cmp     x6, x5
    b.eq    5f
    str     x4, [x2], #8                // a word starts or ends here
    mov     x5, x6
5:
    add     x4, x4, #1
    cmp     x4, x1
    b.lo    7b
8:
    cbz     x5, 9f
    str     x1, [x2], #8                // the last word ends with the bytes
9:
    sub     x0, x2, x3
    lsr     x0, x0, #3                  // offsets written
    ret
