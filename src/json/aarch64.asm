// The JSON scan: AArch64 (ARMv8-A) with NEON, GNU syntax.
//
// What it does
//   Scans the `len` bytes at `data`, which start at position `base` of a document, and writes
//   as 32-bit integers from `positions`, in order, the position of every structural character
//   (braces, brackets, colon and comma) outside strings, every quote that is not escaped, and
//   the first byte of every other run of bytes outside strings that are not whitespace
//   (src/json/mod.rs defines the scan exactly). Returns how many positions it wrote. The state
//   in `scan` carries over from one call to the next: whether the last byte was inside a
//   string, whether the next one is escaped, whether the last byte continued a number or word,
//   and the position of the first control character JSON does not allow. Every call but a
//   document's last must be given a multiple of 64 bytes. Konstruct reads graph.json with it
//   (src/json/mod.rs).
//
// How
//   Sixty-four bytes at a time, as simdjson does. Each 16 are compared with the quote, the
//   backslash, the structural characters (b | 0x20 is 0x7b or 0x7d for either brace or
//   bracket), the whitespace bytes, and 0x20 > b (`cmhi`, unsigned: a control character). NEON
//   has no instruction that gathers one bit per byte, so each comparison is ANDed with the bit
//   its byte stands for in its group of eight (1, 2, 4, ... 128), and three rounds of `addp`
//   add the 64 bytes of four comparisons down to 8, a 64-bit mask with bit k for byte k. Then,
//   in the general registers:
//   - Escaped bytes: with p the backslashes not themselves escaped by the last block, the bytes
//     each odd run of backslashes escapes are ((p << 1 | ODD) - p) ^ ODD ^ (backslashes |
//     carry), where ODD has the odd bits set: the subtraction runs a borrow along each run, so
//     whether a run is odd or even decides the bit after it.
//   - Strings: the prefix XOR of the unescaped quotes (x ^= x << 1, << 2, ... << 32) has a bit
//     set from each opening quote up to its closing quote, and its top bit carries over.
//   - Every other token: the bytes that are not whitespace, structural, quotes or control
//     characters outside strings, wherever the byte before is not one too.
//   `rbit` and `clz` then find each position to write. The last 1 to 63 bytes of a call are
//   copied into the 64 bytes at the end of `scan`, after spaces, which change nothing that is
//   reported, and scanned there, so nothing past `len` is read.
//
// Calling convention
//   AAPCS64, the procedure call standard every AArch64 operating system uses (Linux, macOS and
//   Windows alike for these arguments), declared extern "C" in Rust.
//     x0  data       pointer to the first byte (need not be aligned; unused when len is 0)
//     x1  len        number of bytes
//     x2  base       the position of the first byte in the document
//     x3  scan       the state, 168 bytes, 8-byte aligned (`Scan` in src/json/mod.rs)
//     x4  positions  where the positions go: room for len of them, 4-byte aligned
//     x0             the number of positions written (returned)
//   Clobbers x1 to x17, v0 to v7, v16 to v31 and the condition flags, all of which AAPCS64 lets
//   a callee change. It leaves v8 to v15 alone, whose low halves a callee must preserve, and
//   does not touch x18, the platform register on macOS and Windows.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: each 64-byte load is of a block that ends at most
//   at len, and the last bytes are copied a byte at a time. It writes only `scan` and
//   positions[0] to positions[n - 1], where n, the count it returns, is at most len, as no byte
//   is reported twice. The Rust caller passes a slice, the state and a vector with that much
//   spare room. It never touches the stack or the link register (it is a leaf, so it needs no
//   frame record and no unwind data) and keeps its state only in `scan`, so it is reentrant
//   and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_scan on ELF, Mach-O and COFF alike: Rust links to
//   it by that exact name, as a leading \x01 byte in its link_name tells LLVM not to add the
//   underscore Mach-O symbols otherwise get. `.text` names the code section in all three object
//   formats, and `.p2align 2` aligns the entry to 4 bytes, the size of every A64 instruction.
//   The macros konstruct_json_mask and konstruct_json_classes are named so that they cannot
//   meet another routine's in the crate's assembly, and removed after use.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    // The 64 comparisons in v4 to v7, one bit each, into the general register \dst.
    .macro konstruct_json_mask dst
    and     v4.16b, v4.16b, v26.16b
    and     v5.16b, v5.16b, v26.16b
    and     v6.16b, v6.16b, v26.16b
    and     v7.16b, v7.16b, v26.16b
    addp    v4.16b, v4.16b, v5.16b
    addp    v6.16b, v6.16b, v7.16b
    addp    v4.16b, v4.16b, v6.16b
    addp    v4.16b, v4.16b, v4.16b
    fmov    \dst, d4
    .endm

    // The structural characters and the whitespace of the 16 bytes in \x, into \s and \w.
    .macro konstruct_json_classes x, s, w
    orr     v27.16b, \x, v18.16b        // b | 0x20
    cmeq    v28.16b, v27.16b, v19.16b   // an opening brace or bracket
    cmeq    v27.16b, v27.16b, v20.16b   // a closing brace or bracket
    cmeq    v29.16b, \x, v21.16b        // colon
    cmeq    v30.16b, \x, v22.16b        // comma
    orr     v27.16b, v27.16b, v28.16b
    orr     v29.16b, v29.16b, v30.16b
    orr     \s, v27.16b, v29.16b
    cmeq    v27.16b, \x, v18.16b        // space
    cmeq    v28.16b, \x, v23.16b        // tab
    cmeq    v29.16b, \x, v24.16b        // line feed
    cmeq    v30.16b, \x, v25.16b        // carriage return
    orr     v27.16b, v27.16b, v28.16b
    orr     v29.16b, v29.16b, v30.16b
    orr     \w, v27.16b, v29.16b
    .endm

    .text
    .globl konstruct_json_scan
    .p2align 2
konstruct_json_scan:
    sub     x16, x2, x0                 // x16: the position of the byte at address a is a + x16
    add     x1, x0, x1                  // x1: the end of the bytes
    mov     x2, x4                      // the first position's slot, to count the positions
    ldr     x5, [x3]                    // x5: all ones inside a string
    ldr     x6, [x3, #8]                // x6: 1 when the next byte is escaped
    ldr     x7, [x3, #16]               // x7: 1 when the last byte was in a number or word
    mov     x15, #0xaaaaaaaaaaaaaaaa    // x15: the odd bits
    movi    v16.16b, #0x22              // quote
    movi    v17.16b, #0x5c              // backslash
    movi    v18.16b, #0x20              // space, and the bit that folds brackets onto braces
    movi    v19.16b, #0x7b              // opening brace
    movi    v20.16b, #0x7d              // closing brace
    movi    v21.16b, #0x3a              // colon
    movi    v22.16b, #0x2c              // comma
    movi    v23.16b, #0x09              // tab
    movi    v24.16b, #0x0a              // line feed
    movi    v25.16b, #0x0d              // carriage return
    mov     x9, #0x0201
    movk    x9, #0x0804, lsl #16
    movk    x9, #0x2010, lsl #32
    movk    x9, #0x8040, lsl #48
    dup     v26.2d, x9                  // the bit each byte stands for in its group of eight
2:
    sub     x9, x1, x0                  // bytes left
    cmp     x9, #64
    b.lo    5f
    ldp     q0, q1, [x0]
    ldp     q2, q3, [x0, #32]
    cmeq    v4.16b, v0.16b, v16.16b
    cmeq    v5.16b, v1.16b, v16.16b
    cmeq    v6.16b, v2.16b, v16.16b
    cmeq    v7.16b, v3.16b, v16.16b
    konstruct_json_mask x10             // x10: quotes
    cmeq    v4.16b, v0.16b, v17.16b
    cmeq    v5.16b, v1.16b, v17.16b
    cmeq    v6.16b, v2.16b, v17.16b
    cmeq    v7.16b, v3.16b, v17.16b
    konstruct_json_mask x11             // x11: backslashes
    cmhi    v4.16b, v18.16b, v0.16b
    cmhi    v5.16b, v18.16b, v1.16b
    cmhi    v6.16b, v18.16b, v2.16b
    cmhi    v7.16b, v18.16b, v3.16b
    konstruct_json_mask x13             // x13: control characters, below 0x20
    konstruct_json_classes v0.16b, v4.16b, v31.16b
    konstruct_json_classes v1.16b, v5.16b, v0.16b
    konstruct_json_classes v2.16b, v6.16b, v1.16b
    konstruct_json_classes v3.16b, v7.16b, v2.16b
    konstruct_json_mask x12             // x12: structural characters
    mov     v4.16b, v31.16b
    mov     v5.16b, v0.16b
    mov     v6.16b, v1.16b
    mov     v7.16b, v2.16b
    konstruct_json_mask x14             // x14: whitespace
    // The bytes escaped: each odd run of backslashes escapes the byte after it.
    bic     x8, x11, x6                 // backslashes not escaped by the last block
    orr     x9, x15, x8, lsl #1
    sub     x9, x9, x8
    eor     x9, x9, x15                 // the escaped bytes and the backslashes that escape
    orr     x8, x11, x6
    eor     x8, x8, x9                  // the escaped bytes
    and     x9, x9, x11
    lsr     x6, x9, #63                 // whether the block's last byte escapes the next
    bic     x10, x10, x8                // x10: the quotes that are not escaped
    // Inside strings: from each opening quote up to its closing quote.
    eor     x8, x10, x10, lsl #1
    eor     x8, x8, x8, lsl #2
    eor     x8, x8, x8, lsl #4
    eor     x8, x8, x8, lsl #8
    eor     x8, x8, x8, lsl #16
    eor     x8, x8, x8, lsl #32
    eor     x8, x8, x5                  // x8: inside a string
    asr     x5, x8, #63                 // whether the block ends inside one
    // Control characters inside strings, or outside them and not whitespace.
    orn     x9, x8, x14
    ands    x9, x9, x13
    b.ne    7f
6:
    // The bytes of numbers and words: none of the above, outside strings.
    orr     x13, x13, x14
    orr     x13, x13, x12
    orr     x13, x13, x10
    orr     x13, x13, x8
    mvn     x13, x13
    bic     x12, x12, x8                // x12: structural characters outside strings
    orr     x9, x7, x13, lsl #1         // the bytes after a byte of a number or word
    lsr     x7, x13, #63
    bic     x9, x13, x9                 // the first byte of each number or word
    orr     x9, x9, x12
    orr     x9, x9, x10                 // every position to write
    cbz     x9, 4f
    add     x8, x0, x16                 // the position of the block's first byte
3:
    rbit    x10, x9
    clz     x10, x10
    add     x10, x10, x8
    str     w10, [x4], #4
    sub     x11, x9, #1
    ands    x9, x9, x11                 // clear it
    b.ne    3b
4:
    add     x0, x0, #64
    b       2b
7:
    ldr     x17, [x3, #24]
    cmn     x17, #1
    b.ne    6b                          // one is noted already
    rbit    x9, x9
    clz     x9, x9
    add     x9, x9, x0
    add     x9, x9, x16
    str     x9, [x3, #24]               // the first
    b       6b
5:
    cbz     x9, 8f                      // no bytes left
    add     x10, x3, #104               // the last bytes go to the end of scan
    add     x16, x16, x0
    sub     x16, x16, x10               // positions as they were, from the copy
    mov     x11, #0x2020202020202020
    stp     x11, x11, [x10]             // spaces after them
    stp     x11, x11, [x10, #16]
    stp     x11, x11, [x10, #32]
    stp     x11, x11, [x10, #48]
    mov     x11, #0
9:
    ldrb    w12, [x0, x11]
    strb    w12, [x10, x11]
    add     x11, x11, #1
    cmp     x11, x9
    b.lo    9b
    mov     x0, x10
    add     x1, x10, #64                // one block left: the copy
    b       2b
8:
    str     x5, [x3]                    // the state, for the next call
    str     x6, [x3, #8]
    str     x7, [x3, #16]
    sub     x0, x4, x2
    lsr     x0, x0, #2                  // positions written
    ret

    .purgem konstruct_json_mask
    .purgem konstruct_json_classes
