// JSON string escaping: RISC-V RV64GC.
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
//   One byte at a time, with the three comparisons a byte is tested by. RV64GC has no vector
//   instructions a riscv64 machine is bound to have, and a word load at an address that is not
//   a multiple of eight is slow on hardware that traps it, so this is the scalar loop, as the
//   word scan of src/words/ is.
//
// Calling convention
//   The standard RISC-V ELF psABI, LP64D, declared extern "C" in Rust.
//     a0  src  pointer to the first byte (unused when len is 0)
//     a1  len  number of bytes
//     a2  dst  where the output goes, 6 * len bytes of room
//     a0       the address after the last byte written (returned)
//   Clobbers a0 to a4, t0 and t1, all caller-saved argument registers or temporaries. It uses no
//   floating-point or vector registers and does not use the stack.
//
// Why it is safe
//   It reads only src[0] to src[len - 1], a byte at a time. It writes only dst[0] to
//   dst[6 * len - 1], as no byte writes more than six. It keeps no state between calls, so it is
//   reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_escape: Rust links to it by that exact name, as a leading \x01
//   byte in its link_name tells LLVM to use the name exactly as written. `.text` is the ELF code
//   section, and `.p2align 2` aligns the entry to 4 bytes, the size of a base instruction.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size`.

    .text
    .globl konstruct_json_escape
    .p2align 2
konstruct_json_escape:
2:
    beqz    a1, 9f
    lbu     t0, 0(a0)
    addi    a0, a0, 1
    addi    a1, a1, -1
    li      t1, 0x22
    beq     t0, t1, 6f
    li      t1, 0x5c
    beq     t0, t1, 7f
    sltiu   t1, t0, 0x20
    bnez    t1, 8f
    sb      t0, 0(a2)
    addi    a2, a2, 1
    j       2b
6:
    li      t1, 0x225c                  // \"
    j       10f
7:
    li      t1, 0x5c5c                  // \\
    j       10f
8:
    li      t1, 0x625c                  // \b
    li      a3, 8
    beq     t0, a3, 10f
    li      t1, 0x745c                  // \t
    li      a3, 9
    beq     t0, a3, 10f
    li      t1, 0x6e5c                  // \n
    li      a3, 10
    beq     t0, a3, 10f
    li      t1, 0x665c                  // \f
    li      a3, 12
    beq     t0, a3, 10f
    li      t1, 0x725c                  // \r
    li      a3, 13
    beq     t0, a3, 10f
    li      t1, 0x5c
    sb      t1, 0(a2)                   // \u00
    li      t1, 0x75
    sb      t1, 1(a2)
    li      t1, 0x30
    sb      t1, 2(a2)
    sb      t1, 3(a2)
    srli    t1, t0, 4
    addi    t1, t1, 0x30                // the high digit is 0 or 1
    sb      t1, 4(a2)
    andi    t1, t0, 15
    addi    t1, t1, 0x30
    li      a3, 10
    andi    a4, t0, 15
    bltu    a4, a3, 11f
    addi    t1, t1, 0x27                // a to f for 10 to 15
11:
    sb      t1, 5(a2)
    addi    a2, a2, 6
    j       2b
10:
    sb      t1, 0(a2)                   // the two bytes of the escape, the backslash first
    srli    t1, t1, 8
    sb      t1, 1(a2)
    addi    a2, a2, 2
    j       2b
9:
    mv      a0, a2
    ret
