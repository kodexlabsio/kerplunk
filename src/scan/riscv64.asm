// Orchestrator words: RISC-V RV64GC.
//
// What it does
//   Looks through the `len` bytes at `data` for the words "airflow" and "dagster", and returns
//   which of them occur: bit 0 is set when "airflow" does and bit 1 when "dagster" does. They
//   are the first things Konstruct asks of every Python file in a repository, to find the files
//   that declare Airflow DAGs and Dagster definitions (src/scan/mod.rs), so nearly every file is
//   scanned from end to end and found to hold neither.
//
// How
//   One candidate at a time, as RV64GC has no vector instructions: the byte at the candidate k,
//   for k from 0 to len - 7, is compared with `a` and `d`, the first letters of the two words,
//   and only a candidate that starts like one is compared with the rest of it, byte by byte,
//   the last letter first. The scan ends as soon as both words are found. It reads one byte at
//   a time, as a riscv64 machine may not do unaligned loads of more.
//
// Calling convention
//   The standard RISC-V ELF psABI, LP64D, declared extern "C" in Rust.
//     a0  data  pointer to its first byte (need not be aligned)
//     a1  len   number of bytes
//     a0        the words found (returned)
//   Clobbers a1, a2 and t0 to t3, all caller-saved temporaries or argument registers.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: the first bytes at candidates 0 to len - 7, and the
//   six bytes after a candidate, which end at data[len - 1] for the last. It writes no memory,
//   never touches sp or ra (it is a leaf that returns through ra untouched), uses no
//   floating-point or vector registers and keeps no state between calls, so it is reentrant and
//   thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_gate_words, the name Rust links to (with a leading \x01
//   byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is the
//   ELF code section, and `.p2align 2` aligns the entry to 4 bytes, the size of a base
//   instruction; RV64GC includes the C extension, so the assembler writes the instructions that
//   have one in their 2-byte compressed form.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_gate_words
    .p2align 2
konstruct_gate_words:
    li      a2, 0                       // a2: the words found
    li      t0, 7
    bltu    a1, t0, 9f                  // fewer than seven bytes hold neither word
    addi    a1, a1, -7
    add     a1, a0, a1                  // a1: the last candidate
    li      t1, 0x61                    // t1: "a"
    li      t2, 0x64                    // t2: "d"
2:
    lbu     t0, 0(a0)
    beq     t0, t1, 3f
    beq     t0, t2, 4f
5:
    addi    a0, a0, 1
    bgeu    a1, a0, 2b                  // another candidate
9:
    mv      a0, a2
    ret
3:                                      // a candidate that starts like "airflow"
    lbu     t0, 6(a0)
    li      t3, 0x77                    // "w"
    bne     t0, t3, 5b
    lbu     t0, 1(a0)
    li      t3, 0x69                    // "i"
    bne     t0, t3, 5b
    lbu     t0, 2(a0)
    li      t3, 0x72                    // "r"
    bne     t0, t3, 5b
    lbu     t0, 3(a0)
    li      t3, 0x66                    // "f"
    bne     t0, t3, 5b
    lbu     t0, 4(a0)
    li      t3, 0x6c                    // "l"
    bne     t0, t3, 5b
    lbu     t0, 5(a0)
    li      t3, 0x6f                    // "o"
    bne     t0, t3, 5b
    ori     a2, a2, 1
    j       6f
4:                                      // a candidate that starts like "dagster"
    lbu     t0, 6(a0)
    li      t3, 0x72                    // "r"
    bne     t0, t3, 5b
    lbu     t0, 1(a0)
    li      t3, 0x61                    // "a"
    bne     t0, t3, 5b
    lbu     t0, 2(a0)
    li      t3, 0x67                    // "g"
    bne     t0, t3, 5b
    lbu     t0, 3(a0)
    li      t3, 0x73                    // "s"
    bne     t0, t3, 5b
    lbu     t0, 4(a0)
    li      t3, 0x74                    // "t"
    bne     t0, t3, 5b
    lbu     t0, 5(a0)
    li      t3, 0x65                    // "e"
    bne     t0, t3, 5b
    ori     a2, a2, 2
6:
    li      t0, 3
    bne     a2, t0, 5b
    j       9b                          // both found
