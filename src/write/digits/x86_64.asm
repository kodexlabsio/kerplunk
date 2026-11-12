// An integer's decimal digits: x86_64, Intel syntax.
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
//   System V AMD64, declared extern "sysv64" in Rust.
//     rdi  value
//     rsi  buf    20 bytes
//     rax         the number of digits (returned)
//   Clobbers rax, rcx, rdx, rdi, r8 to r10 and the flags. It does not use the stack.
//
// Why it is safe
//   It writes only buf[0] to buf[19], and reads no memory. It keeps no state, so it is
//   reentrant and thread safe.
//
// Symbol, section and alignment
//   As above: the plain name konstruct_json_digits, `.text`, `.p2align 4`.

    .text
    .globl konstruct_json_digits
    .p2align 4
konstruct_json_digits:
    lea     r8, [rsi + 20]              // the next digit goes before r8
    movabs  r9, 0xcccccccccccccccd
2:
    mov     rax, rdi
    mul     r9                          // rdx: the high half of value * 0xcccccccccccccccd
    shr     rdx, 3                      // value / 10
    lea     rcx, [rdx + rdx * 4]
    add     rcx, rcx                    // 10 * (value / 10)
    mov     r10, rdi
    sub     r10, rcx                    // value % 10
    add     r10b, 0x30
    dec     r8
    mov     byte ptr [r8], r10b
    mov     rdi, rdx
    test    rdi, rdi
    jnz     2b
    lea     rax, [rsi + 20]
    sub     rax, r8
    ret
