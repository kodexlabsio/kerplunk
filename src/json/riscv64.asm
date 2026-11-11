// The JSON scan: RISC-V RV64GC.
//
// What it does
//   Scans the `len` bytes at `data`, which start at position `base` of a document, and writes
//   as 32-bit integers from `positions`, in order, the position of every structural character
//   (braces, brackets, colon and comma) outside strings, every quote that is not escaped, and
//   the first byte of every other run of bytes outside strings that are not whitespace
//   (src/json/mod.rs defines the scan exactly). Returns how many positions it wrote. The state
//   in `scan` carries over from one call to the next: whether the last byte was inside a
//   string, whether the next one is escaped, whether the last byte continued a number or word,
//   and the position of the first control character JSON does not allow. Konstruct reads
//   graph.json with it (src/json/mod.rs).
//
// How
//   One byte at a time, as RV64GC has no vector instructions and no instruction to find a set
//   bit in a mask, following the definition in src/json/mod.rs: a backslash escapes the next
//   byte unless it is itself escaped; an unescaped quote opens or closes a string; inside one
//   only a control character matters; outside, a structural character is written, whitespace
//   ends a number or word, and any other byte is written when it starts one. A brace or a
//   bracket is b | 0x20 equal to 0x7b or 0x7d.
//
// Calling convention
//   The standard RISC-V ELF psABI, LP64D, declared extern "C" in Rust.
//     a0  data       pointer to the first byte (need not be aligned; unused when len is 0)
//     a1  len        number of bytes
//     a2  base       the position of the first byte in the document
//     a3  scan       the state, 168 bytes, 8-byte aligned (`Scan` in src/json/mod.rs)
//     a4  positions  where the positions go: room for len of them, 4-byte aligned
//     a0             the number of positions written (returned)
//   Clobbers a1, a2, a4 to a7 and t0 to t6, all caller-saved temporaries or argument
//   registers.
//
// Why it is safe
//   It reads exactly the bytes data[0] to data[len - 1], one at a time, and reads nothing when
//   len is 0. It writes only `scan` and positions[0] to positions[n - 1], where n, the count it
//   returns, is at most len, as no byte is written twice. The Rust caller passes a slice, the
//   state and a vector with that much spare room. It never touches sp or ra (it is a leaf that
//   returns through ra untouched), uses no floating-point or vector registers and keeps its
//   state only in `scan`, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_scan, the name Rust links to (with a leading
//   \x01 byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is
//   the ELF code section, and `.p2align 2` aligns the entry to 4 bytes, the size of a base
//   instruction; RV64GC includes the C extension, so the assembler writes the instructions that
//   have one in their 2-byte compressed form.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_json_scan
    .p2align 2
konstruct_json_scan:
    add     a5, a0, a1                  // a5: the end of the bytes
    sub     a2, a2, a0                  // a2: the position of the byte at address a is a + a2
    mv      a6, a4                      // the first position's slot, to count the positions
    ld      t0, 0(a3)
    snez    t0, t0                      // t0: 1 inside a string
    ld      t1, 8(a3)                   // t1: 1 when the next byte is escaped
    ld      t2, 16(a3)                  // t2: 1 when the last byte was in a number or word
    li      t4, 0x22                    // quote
    li      t5, 0x5c                    // backslash
    li      t6, 0x20                    // space, and the bit that folds brackets onto braces
2:
    beq     a0, a5, 8f                  // no bytes left
    lbu     t3, 0(a0)
    mv      a1, t1                      // a1: whether this byte is escaped
    xor     t1, t3, t5
    seqz    t1, t1
    seqz    a7, a1
    and     t1, t1, a7                  // whether it escapes the next: an unescaped backslash
    bnez    a1, 3f                      // an escaped quote neither opens nor closes a string
    beq     t3, t4, 6f
3:
    bnez    t0, 4f
    ori     a7, t3, 0x20
    addi    a7, a7, -0x7b
    beqz    a7, 5f                      // an opening brace or bracket
    addi    a7, a7, -2
    beqz    a7, 5f                      // a closing one
    addi    a7, t3, -0x3a
    beqz    a7, 5f                      // colon
    addi    a7, t3, -0x2c
    beqz    a7, 5f                      // comma
    beq     t3, t6, 7f                  // space
    bgeu    t3, t6, 9f                  // anything else from 0x20 up: a number or word
    addi    a7, t3, -0x09
    beqz    a7, 7f                      // tab
    addi    a7, t3, -0x0a
    beqz    a7, 7f                      // line feed
    addi    a7, t3, -0x0d
    beqz    a7, 7f                      // carriage return
    j       10f                         // any other control character
4:
    bgeu    t3, t6, 7f                  // inside a string only control characters matter
    j       10f
5:
    add     a7, a0, a2                  // a structural character
    sw      a7, 0(a4)
    addi    a4, a4, 4
    j       7f
6:
    add     a7, a0, a2                  // a quote
    sw      a7, 0(a4)
    addi    a4, a4, 4
    xori    t0, t0, 1
    j       7f
9:
    bnez    t2, 11f                     // inside a number or word
    add     a7, a0, a2                  // the first byte of one
    sw      a7, 0(a4)
    addi    a4, a4, 4
11:
    li      t2, 1
    j       12f
10:
    ld      a7, 24(a3)
    addi    a7, a7, 1
    bnez    a7, 7f                      // one is noted already
    add     a7, a0, a2
    sd      a7, 24(a3)                  // the first
7:
    li      t2, 0
12:
    addi    a0, a0, 1
    j       2b
8:
    neg     t0, t0
    sd      t0, 0(a3)                   // the state, for the next call: all ones in a string
    sd      t1, 8(a3)
    sd      t2, 16(a3)
    sub     a0, a4, a6
    srli    a0, a0, 2                   // positions written
    ret
