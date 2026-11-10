// Substring test: RISC-V RV64GC.
//
// What it does
//   Returns 1 when the `needle_len` bytes at `needle` occur in the `haystack_len` bytes at
//   `haystack`, else 0. An empty needle occurs in every haystack, and one longer than the
//   haystack in none. The search index tests labels against query words with it
//   (src/substring/mod.rs).
//
// How
//   With n = needle_len, a match can start at offsets 0 to haystack_len - n, the candidates.
//   One candidate at a time, as RV64GC has no vector instructions: the byte at the candidate is
//   compared with the needle's first byte and the byte n - 1 further on with its last, both held
//   in registers, and only a candidate where both match is compared in full, byte by byte.
//
// Calling convention
//   The standard RISC-V ELF psABI, LP64D, declared extern "C" in Rust.
//     a0  haystack      pointer to its first byte (need not be aligned)
//     a1  haystack_len  number of bytes
//     a2  needle        pointer to its first byte (need not be aligned)
//     a3  needle_len    number of bytes
//     a0                1 when the needle occurs, else 0 (returned)
//   Clobbers a1 and t0 to t6, all caller-saved temporaries or argument registers.
//
// Why it is safe
//   It reads only haystack[0] to haystack[haystack_len - 1] and needle[0] to
//   needle[needle_len - 1]: the needle only when it is not empty and no longer than the
//   haystack, the first bytes at candidates 0 to haystack_len - n, and the last bytes n - 1
//   further, which end at haystack_len - 1. It writes no memory, never touches sp or ra (it is a
//   leaf that returns through ra untouched), uses no floating-point or vector registers and
//   keeps no state between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_contains, the name Rust links to (with a leading \x01
//   byte in its link_name, which tells LLVM to use the name exactly as written). `.text` is the
//   ELF code section, and `.p2align 2` aligns the entry to 4 bytes, the size of a base
//   instruction; RV64GC includes the C extension, so the assembler writes the instructions that
//   have one in their 2-byte compressed form.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_contains
    .p2align 2
konstruct_contains:
    beqz    a3, 8f                      // an empty needle occurs everywhere
    bltu    a1, a3, 9f                  // a needle longer than the haystack occurs nowhere
    sub     a1, a1, a3
    add     a1, a0, a1                  // a1: the last candidate
    addi    t0, a3, -1                  // t0: the offset of the needle's last byte
    lbu     t1, 0(a2)                   // t1: the needle's first byte
    add     t2, a2, t0
    lbu     t2, 0(t2)                   // t2: its last
2:
    lbu     t3, 0(a0)
    bne     t3, t1, 5f
    add     t3, a0, t0
    lbu     t3, 0(t3)
    bne     t3, t2, 5f
    li      t4, 1                       // t4: the next byte to compare
3:
    bgeu    t4, t0, 8f                  // every byte between the first and last matches
    add     t5, a0, t4
    lbu     t5, 0(t5)
    add     t6, a2, t4
    lbu     t6, 0(t6)
    bne     t5, t6, 5f
    addi    t4, t4, 1
    j       3b
5:
    addi    a0, a0, 1
    bgeu    a1, a0, 2b                  // another candidate
9:
    li      a0, 0
    ret
8:
    li      a0, 1
    ret
