// Word boundaries in a byte string: RISC-V RV64GC.
//
// What it does
//   Finds the runs of word bytes (ASCII `a` to `z` and `0` to `9`) in the `len` bytes at `data`
//   and writes, in order, the offset of each run's first byte and the offset just past its last,
//   as 64-bit integers from `bounds`. Returns how many offsets it wrote: twice the number of
//   words. The search index splits names, descriptions and queries into words with it
//   (src/words/mod.rs).
//
// How
//   One byte at a time, as RV64GC has no vector instructions and no instruction to find a set
//   bit in a mask: b - 'a' < 26 or b - '0' < 10, compared unsigned with `sltiu`, says whether
//   the byte is a word byte, and an offset is written wherever that differs from the byte
//   before. The classification has no branches; the loop branches only to write an offset.
//
// Calling convention
//   The standard RISC-V ELF psABI, LP64D, declared extern "C" in Rust.
//     a0  data    pointer to the first byte (need not be aligned; unused when len is 0)
//     a1  len     number of bytes
//     a2  bounds  where the offsets go: room for len + 1 of them, 8-byte aligned
//     a0          the number of offsets written (returned)
//   Clobbers a2, a3 and t0 to t4, all caller-saved temporaries or argument registers.
//
// Why it is safe
//   It reads exactly the bytes data[0] to data[len - 1], one at a time, and reads nothing when
//   len is 0. It writes only bounds[0] to bounds[n - 1], where n, the count it returns, is at
//   most len + 1, as words and separators alternate. The Rust caller passes a slice and a vector
//   with that much spare room. It never touches sp or ra (it is a leaf that returns through ra
//   untouched), uses no floating-point or vector registers and keeps no state between calls, so
//   it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_word_bounds, the name Rust links to (with a leading
//   \x01 byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is
//   the ELF code section, and `.p2align 2` aligns the entry to 4 bytes, the size of a base
//   instruction; RV64GC includes the C extension, so the assembler writes the instructions that
//   have one in their 2-byte compressed form.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_word_bounds
    .p2align 2
konstruct_word_bounds:
    mv      t0, a2                      // the first offset's slot, to count the offsets
    mv      t1, a0                      // t1: the byte being classified
    add     a3, a0, a1                  // a3: the end of the bytes
    li      t2, 0                       // t2: 1 while inside a word, else 0
    beq     t1, a3, 3f
2:
    lbu     t3, 0(t1)
    addi    t4, t3, -0x61
    sltiu   t4, t4, 26                  // a letter: b - 'a' < 26, unsigned
    addi    t3, t3, -0x30
    sltiu   t3, t3, 10                  // a digit
    or      t3, t3, t4
    beq     t3, t2, 4f
    sub     t4, t1, a0
    sd      t4, 0(a2)                   // a word starts or ends here
    addi    a2, a2, 8
    mv      t2, t3
4:
    addi    t1, t1, 1
    bne     t1, a3, 2b
3:
    beqz    t2, 5f
    sd      a1, 0(a2)                   // the last word ends with the bytes
    addi    a2, a2, 8
5:
    sub     a0, a2, t0
    srli    a0, a0, 3                   // offsets written
    ret
