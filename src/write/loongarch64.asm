// JSON string escaping: LoongArch64, GNU syntax.
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
//   The LoongArch ELF psABI, LP64D, declared extern "C" in Rust.
//     $a0  src  pointer to the first byte (need not be aligned; unused when len is 0)
//     $a1  len  number of bytes
//     $a2  dst  where the output goes, 6 * len bytes of room
//     $a0       the address after the last byte written (returned)
//   Clobbers $a0 to $a7 and $t0 to $t4, all caller-saved. It uses no floating-point or vector
//   registers and does not use the stack.
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
//   section, and `.p2align 2` aligns the entry to 4 bytes, the size of every LoongArch instruction.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size`.

    .text
    .globl konstruct_json_escape
    .p2align 2
konstruct_json_escape:
    li.d    $a3, 0x8080808080808080
    li.d    $a4, 0x0101010101010101
    li.d    $a5, 0x2020202020202020
    li.d    $a6, 0x2222222222222222
    li.d    $a7, 0x5c5c5c5c5c5c5c5c
2:
    li.w    $t0, 8
    bltu    $a1, $t0, 5f
    ld.d    $t0, $a0, 0
    sub.d   $t1, $t0, $a5
    andn    $t1, $t1, $t0               // some byte is below 0x20
    xor     $t2, $t0, $a6
    sub.d   $t3, $t2, $a4
    andn    $t3, $t3, $t2
    or      $t1, $t1, $t3               // or equal to a quote
    xor     $t2, $t0, $a7
    sub.d   $t3, $t2, $a4
    andn    $t3, $t3, $t2
    or      $t1, $t1, $t3               // or equal to a backslash
    and     $t1, $t1, $a3
    bnez    $t1, 5f
    st.d    $t0, $a2, 0                 // the eight bytes are clean
    addi.d  $a2, $a2, 8
    addi.d  $a0, $a0, 8
    addi.d  $a1, $a1, -8
    b       2b
5:
    beqz    $a1, 9f
    ld.bu   $t0, $a0, 0
    addi.d  $a0, $a0, 1
    addi.d  $a1, $a1, -1
    li.w    $t1, 0x22
    beq     $t0, $t1, 6f
    li.w    $t1, 0x5c
    beq     $t0, $t1, 7f
    li.w    $t1, 0x20
    bltu    $t0, $t1, 8f
    st.b    $t0, $a2, 0
    addi.d  $a2, $a2, 1
    b       2b
6:
    li.w    $t1, 0x225c                 // \"
    b       10f
7:
    li.w    $t1, 0x5c5c                 // \\
    b       10f
8:
    li.w    $t1, 0x625c                 // \b
    li.w    $t2, 8
    beq     $t0, $t2, 10f
    li.w    $t1, 0x745c                 // \t
    li.w    $t2, 9
    beq     $t0, $t2, 10f
    li.w    $t1, 0x6e5c                 // \n
    li.w    $t2, 10
    beq     $t0, $t2, 10f
    li.w    $t1, 0x665c                 // \f
    li.w    $t2, 12
    beq     $t0, $t2, 10f
    li.w    $t1, 0x725c                 // \r
    li.w    $t2, 13
    beq     $t0, $t2, 10f
    li.w    $t1, 0x3030755c             // \u00
    st.w    $t1, $a2, 0
    srli.d  $t1, $t0, 4
    addi.d  $t1, $t1, 0x30              // the high digit is 0 or 1
    st.b    $t1, $a2, 4
    andi    $t1, $t0, 15
    addi.d  $t2, $t1, 0x30
    addi.d  $t3, $t1, 0x57              // a to f for 10 to 15
    li.w    $t4, 10
    bltu    $t1, $t4, 11f
    move    $t2, $t3
11:
    st.b    $t2, $a2, 5
    addi.d  $a2, $a2, 6
    b       2b
10:
    st.h    $t1, $a2, 0
    addi.d  $a2, $a2, 2
    b       2b
9:
    move    $a0, $a2
    ret
