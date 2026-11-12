// JSON string escaping: AArch64 (ARMv8-A), GNU syntax.
//
// What it does
//   Copies the `len` bytes at `src` to `dst` with the escapes a JSON string needs and returns
//   the address just past the last byte written. A quote becomes \" and a backslash \\; the
//   control characters below 0x20 become \b, \t, \n, \f and \r, or \u00xx with lower-case hex
//   for the others; every other byte, DEL and the bytes of non-ASCII characters included, is
//   copied as it is. The output is the one orjson writes, which graph.json has always been.
//   `dst` needs room for 6 * len bytes. The bytes are UTF-8 text, but the routine looks at one
//   byte at a time and changes only ASCII, so it does not need them to be.
//   Konstruct writes every string of graph.json and of its other JSON files with it
//   (src/write/mod.rs).
//
// How
//   Eight bytes at a time, as a 64-bit word. A word with no byte below 0x20 and none equal to
//   0x22 or 0x5c is stored whole. "Some byte of w is below 0x20" is
//   (w - 0x2020...20) & ~w & 0x8080...80, and "some byte of w is equal to c" is the same test
//   (x - 0x0101...01) & ~x & 0x8080...80 on x = w ^ (c in every byte); both are exact for the
//   question whether there is any such byte, and they take no notice of byte order. If there is
//   one, or fewer than eight bytes are left, one byte is looked at on its own and then the word
//   test is made again from the next.
//
// Calling convention
//   AAPCS64, the procedure call standard every AArch64 operating system uses (Linux, macOS and
//   Windows alike for these arguments), declared extern "C" in Rust.
//     x0  src  pointer to the first byte (need not be aligned; unused when len is 0)
//     x1  len  number of bytes
//     x2  dst  where the output goes, 6 * len bytes of room
//     x0       the address after the last byte written (returned)
//   Clobbers x1 to x13 and the flags, all of which AAPCS64 lets a callee change. It does not
//   touch x18, the platform register on macOS and Windows, uses no SIMD registers and does not
//   use the stack.
//
// Why it is safe
//   It reads only src[0] to src[len - 1]: a word load is made only while eight or more bytes
//   are left, and the rest is read a byte at a time. It writes only dst[0] to dst[6 * len - 1]:
//   a word store replaces the eight bytes it read, and no byte writes more than six.
//   It keeps no state between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_escape: Rust links to it by that exact name, as a leading \x01
//   byte in its link_name tells LLVM to use the name exactly as written. `.text` is the ELF code
//   section, and `.p2align 2` aligns the entry to 4 bytes, the size of every A64 instruction.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size`.

    .text
    .globl konstruct_json_escape
    .p2align 2
konstruct_json_escape:
    mov     x9, #0x8080808080808080
    mov     x10, #0x0101010101010101
    mov     x11, #0x2020202020202020
    mov     x12, #0x2222222222222222
    mov     x13, #0x5c5c                // 0x5c is not a bit-mask immediate in every byte
    orr     x13, x13, x13, lsl #16
    orr     x13, x13, x13, lsl #32      // 0x5c5c5c5c5c5c5c5c
2:
    cmp     x1, #8
    b.lo    5f
    ldr     x3, [x0]
    sub     x4, x3, x11
    bic     x4, x4, x3                  // some byte is below 0x20
    eor     x5, x3, x12
    sub     x6, x5, x10
    bic     x6, x6, x5
    orr     x4, x4, x6                  // or equal to a quote
    eor     x5, x3, x13
    sub     x6, x5, x10
    bic     x6, x6, x5
    orr     x4, x4, x6                  // or equal to a backslash
    tst     x4, x9
    b.ne    5f
    str     x3, [x2], #8                // the eight bytes are clean
    add     x0, x0, #8
    sub     x1, x1, #8
    b       2b
5:
    cbz     x1, 9f
    ldrb    w3, [x0], #1
    sub     x1, x1, #1
    cmp     w3, #0x22
    b.eq    6f
    cmp     w3, #0x5c
    b.eq    7f
    cmp     w3, #0x20
    b.lo    8f
    strb    w3, [x2], #1
    b       2b
6:
    mov     w4, #0x225c                 // \"
    b       10f
7:
    mov     w4, #0x5c5c                 // \\
    b       10f
8:
    mov     w4, #0x625c                 // \b
    cmp     w3, #8
    b.eq    10f
    mov     w4, #0x745c                 // \t
    cmp     w3, #9
    b.eq    10f
    mov     w4, #0x6e5c                 // \n
    cmp     w3, #10
    b.eq    10f
    mov     w4, #0x665c                 // \f
    cmp     w3, #12
    b.eq    10f
    mov     w4, #0x725c                 // \r
    cmp     w3, #13
    b.eq    10f
    mov     w4, #0x755c                 // \u00
    movk    w4, #0x3030, lsl #16
    str     w4, [x2]
    lsr     w5, w3, #4
    add     w5, w5, #0x30               // the high digit is 0 or 1
    strb    w5, [x2, #4]
    and     w5, w3, #15
    add     w6, w5, #0x30
    add     w7, w5, #0x57               // a to f for 10 to 15
    cmp     w5, #10
    csel    w6, w7, w6, hs
    strb    w6, [x2, #5]
    add     x2, x2, #6
    b       2b
10:
    strh    w4, [x2], #2
    b       2b
9:
    mov     x0, x2
    ret
