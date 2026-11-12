// JSON string escaping: little-endian PowerPC64 (POWER8 and later), GNU syntax.
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
//   The 64-bit ELFv2 ABI, declared extern "C" in Rust.
//     r3  src  pointer to the first byte (need not be aligned; unused when len is 0)
//     r4  len  number of bytes
//     r5  dst  where the output goes, 6 * len bytes of room
//     r3       the address after the last byte written (returned)
//   Clobbers r3 to r11 and cr0, all volatile. It uses no TOC and no stack.
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
//   section, and `.p2align 4` aligns the entry to 16 bytes, as compilers align function entries for POWER.
//   As it uses no TOC, its global and local entry points are the same address and it needs no
//   `.localentry`.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size`.

    .text
    .globl konstruct_json_escape
    .p2align 4
konstruct_json_escape:
    lis     9, 0x8080
    ori     9, 9, 0x8080
    rldimi  9, 9, 32, 0                 // 0x8080808080808080
    lis     10, 0x0101
    ori     10, 10, 0x0101
    rldimi  10, 10, 32, 0               // 0x0101010101010101
2:
    cmpldi  4, 8
    blt     0, 5f
    ld      6, 0(3)
    sldi    8, 10, 5                    // 0x2020202020202020
    subf    7, 8, 6
    andc    7, 7, 6                     // some byte is below 0x20
    mulli   8, 10, 0x22
    xor     8, 8, 6
    subf    11, 10, 8
    andc    11, 11, 8
    or      7, 7, 11                    // or equal to a quote
    mulli   8, 10, 0x5c
    xor     8, 8, 6
    subf    11, 10, 8
    andc    11, 11, 8
    or      7, 7, 11                    // or equal to a backslash
    and.    7, 7, 9
    bne     0, 5f
    std     6, 0(5)                     // the eight bytes are clean
    addi    5, 5, 8
    addi    3, 3, 8
    addi    4, 4, -8
    b       2b
5:
    cmpdi   4, 0
    beq     0, 9f
    lbz     6, 0(3)
    addi    3, 3, 1
    addi    4, 4, -1
    cmpwi   6, 0x22
    beq     0, 6f
    cmpwi   6, 0x5c
    beq     0, 7f
    cmpwi   6, 0x20
    blt     0, 8f
    stb     6, 0(5)
    addi    5, 5, 1
    b       2b
6:
    li      7, 0x225c                   // \"
    b       10f
7:
    li      7, 0x5c5c                   // \\
    b       10f
8:
    li      7, 0x625c                   // \b
    cmpwi   6, 8
    beq     0, 10f
    li      7, 0x745c                   // \t
    cmpwi   6, 9
    beq     0, 10f
    li      7, 0x6e5c                   // \n
    cmpwi   6, 10
    beq     0, 10f
    li      7, 0x665c                   // \f
    cmpwi   6, 12
    beq     0, 10f
    li      7, 0x725c                   // \r
    cmpwi   6, 13
    beq     0, 10f
    lis     7, 0x3030
    ori     7, 7, 0x755c                // \u00, the backslash first in memory
    stw     7, 0(5)
    srwi    7, 6, 4
    addi    7, 7, 0x30                  // the high digit is 0 or 1
    stb     7, 4(5)
    andi.   7, 6, 15
    addi    8, 7, 0x30
    addi    11, 7, 0x57                 // a to f for 10 to 15
    cmpwi   7, 10
    blt     0, 11f
    mr      8, 11
11:
    stb     8, 5(5)
    addi    5, 5, 6
    b       2b
10:
    sth     7, 0(5)
    addi    5, 5, 2
    b       2b
9:
    mr      3, 5
    blr
