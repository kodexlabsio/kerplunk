// An integer's decimal digits: s390x (z/Architecture, z13 and later), GNU syntax.
//
// What it does
//   Writes the decimal digits of the unsigned 64-bit `value` to the end of the 20 bytes at
//   `buf`, with no leading zeros (0 is "0"), and returns how many digits there are; they are
//   buf[20 - n] to buf[19]. 20 bytes hold the largest value, 18446744073709551615.
//   Konstruct writes every integer of its JSON files with it (src/write/mod.rs).
//
// How
//   Each step divides by ten as a multiplication: the high half of value * 0xcccccccccccccccd,
//   shifted right by 3, is value / 10 for every 64-bit value; the remainder is value - 10 * q.
//
// Calling convention
//   The s390x ELF ABI, declared extern "C" in Rust.
//     r2  value
//     r3  buf    20 bytes
//     r2         the number of digits (returned)
//   Clobbers r0 to r5 and the condition code. It does not use the stack.
//
// Why it is safe
//   It writes only buf[0] to buf[19] and reads no memory. It keeps no state, so it is reentrant
//   and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_json_digits: Rust links to it by that exact name, as a leading \x01
//   byte in its link_name tells LLVM to use the name exactly as written. `.text` is the ELF code
//   section, and `.p2align 3` aligns the entry to 8 bytes, as compilers align function entries on this
//   architecture.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size`.

    .text
    .globl konstruct_json_digits
    .p2align 3
konstruct_json_digits:
    la      %r5, 20(%r3)                // the next digit goes before r5
    lghi    %r4, 10
    lgr     %r1, %r2                    // the dividend is r0:r1, a 128-bit number
2:
    lghi    %r0, 0
    dlgr    %r0, %r4                    // r1: value / 10, r0: value % 10
    ahi     %r0, 0x30
    aghi    %r5, -1
    stc     %r0, 0(%r5)
    ltgr    %r1, %r1
    jne     2b
    la      %r2, 20(%r3)
    sgr     %r2, %r5
    br      %r14
