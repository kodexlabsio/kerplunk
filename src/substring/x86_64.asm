// Substring test: x86_64 with SSE2, Intel syntax.
//
// What it does
//   Returns 1 when the `needle_len` bytes at `needle` occur in the `haystack_len` bytes at
//   `haystack`, else 0. An empty needle occurs in every haystack, and one longer than the
//   haystack in none. The search index tests labels against query words with it
//   (src/substring/mod.rs).
//
// How
//   With n = needle_len, a match can start at offsets 0 to haystack_len - n, the candidates.
//   Sixteen candidates at a time: the bytes at the candidates are compared with the needle's
//   first byte, and the bytes n - 1 further on with its last, and `pmovmskb` of both comparisons
//   ANDed gives a mask of the candidates where both match. Each is then compared in full, byte by
//   byte, as `bsf` finds it. When fewer than sixteen candidates are left, one more block ends at
//   the last candidate and overlaps the one before (a candidate seen twice fails twice). A
//   haystack with fewer than sixteen candidates is tested one candidate at a time.
//
// Calling convention
//   System V AMD64 on every operating system: Rust declares the function extern "sysv64", so
//   Windows callers use it too, not the Microsoft x64 convention.
//     rdi  haystack      pointer to its first byte (need not be aligned)
//     rsi  haystack_len  number of bytes
//     rdx  needle        pointer to its first byte (need not be aligned)
//     rcx  needle_len    number of bytes
//     rax                1 when the needle occurs, else 0 (returned)
//   Clobbers rsi, rdi, r8 to r11, xmm0, xmm1, xmm4, xmm5 and the flags, all of which System V
//   lets a callee change; under System V every vector register is caller-saved.
//
// Why it is safe
//   It reads only haystack[0] to haystack[haystack_len - 1] and needle[0] to
//   needle[needle_len - 1]: the needle only when it is not empty and no longer than the
//   haystack, the first-byte loads at candidates 0 to haystack_len - n, and the last-byte loads
//   n - 1 bytes further, which end at haystack_len - 1. It writes no memory, touches no stack (it
//   is a leaf: no pushes, no frame, so Windows needs no unwind data for it) and keeps no state
//   between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_contains on ELF, Mach-O and COFF alike: Rust links to
//   it by that exact name, as a leading \x01 byte in its link_name tells LLVM not to add the
//   underscore Mach-O symbols otherwise get. `.text` names the code section in all three object
//   formats, and `.p2align 4` starts the function and its main loop on 16-byte boundaries.
//   Local labels are numbered from 2: in Intel syntax a label made only of 0 and 1 reads as a
//   binary number.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_contains
    .p2align 4
konstruct_contains:
    mov     eax, 1
    test    rcx, rcx
    jz      9f                          // an empty needle occurs everywhere
    xor     eax, eax
    cmp     rcx, rsi
    ja      9f                          // a needle longer than the haystack occurs nowhere
    sub     rsi, rcx
    add     rsi, rdi                    // rsi: the last candidate
    lea     rax, [rdi + 15]
    cmp     rax, rsi
    ja      7f                          // fewer than 16 candidates: one at a time
    movzx   eax, byte ptr [rdx]
    imul    eax, eax, 0x01010101
    movd    xmm4, eax
    pshufd  xmm4, xmm4, 0               // xmm4: the needle's first byte, sixteen times
    movzx   eax, byte ptr [rdx + rcx - 1]
    imul    eax, eax, 0x01010101
    movd    xmm5, eax
    pshufd  xmm5, xmm5, 0               // xmm5: its last byte
    .p2align 4
2:                                      // rdi: the block's first candidate
    movdqu  xmm0, xmmword ptr [rdi]
    movdqu  xmm1, xmmword ptr [rdi + rcx - 1]
    pcmpeqb xmm0, xmm4
    pcmpeqb xmm1, xmm5
    pand    xmm0, xmm1
    pmovmskb r8d, xmm0                  // bit k: candidate k matches the first and last bytes
    test    r8d, r8d
    jz      5f
3:
    bsf     r9d, r8d
    add     r9, rdi                     // r9: the candidate
    mov     r10d, 1                     // r10: the next byte to compare
4:
    lea     rax, [r10 + 1]
    cmp     rax, rcx
    jae     8f                          // every byte between the first and last matches
    movzx   eax, byte ptr [r9 + r10]
    cmp     al, byte ptr [rdx + r10]
    jne     6f
    inc     r10
    jmp     4b
6:
    lea     eax, [r8 - 1]
    and     r8d, eax                    // the next candidate in the block
    jnz     3b
5:
    add     rdi, 16
    lea     rax, [rdi + 15]
    cmp     rax, rsi
    jbe     2b                          // another whole block
    cmp     rdi, rsi
    ja      13f                         // no candidates left
    lea     rdi, [rsi - 15]             // the last block ends with the last candidate
    jmp     2b
7:                                      // one candidate at a time
    movzx   eax, byte ptr [rdi]
    cmp     al, byte ptr [rdx]
    jne     14f
    movzx   eax, byte ptr [rdi + rcx - 1]
    cmp     al, byte ptr [rdx + rcx - 1]
    jne     14f
    mov     r10d, 1
12:
    lea     rax, [r10 + 1]
    cmp     rax, rcx
    jae     8f
    movzx   eax, byte ptr [rdi + r10]
    cmp     al, byte ptr [rdx + r10]
    jne     14f
    inc     r10
    jmp     12b
14:
    inc     rdi
    cmp     rdi, rsi
    jbe     7b
13:
    xor     eax, eax
    ret
8:
    mov     eax, 1
9:
    ret
