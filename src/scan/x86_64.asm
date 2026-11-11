// Orchestrator words: x86_64 with SSE2, Intel syntax.
//
// What it does
//   Looks through the `len` bytes at `data` for the words "airflow" and "dagster", and returns
//   which of them occur: bit 0 is set when "airflow" does and bit 1 when "dagster" does. They
//   are the first things Konstruct asks of every Python file in a repository, to find the files
//   that declare Airflow DAGs and Dagster definitions (src/scan/mod.rs), so nearly every file is
//   scanned from end to end and found to hold neither.
//
// How
//   Both words have seven bytes, so a candidate at offset k, for k from 0 to len - 7, is an
//   occurrence of "airflow" when its first byte is `a` and the byte six on is `w`, and of
//   "dagster" when they are `d` and `r`. Sixteen candidates at a time: the bytes at the
//   candidates and the bytes six further on are compared with those four letters, the two
//   comparisons for each word ANDed and the two words ORed, and `pmovmskb` gives a mask of the
//   candidates that match one of the pairs. Each of those is checked in full by two overlapping
//   four-byte loads, at the candidate and three bytes on, against the word's two halves. The scan
//   ends as soon as both words are found. When fewer than sixteen candidates are left, one more
//   block ends at the last candidate and overlaps the one before (a candidate seen twice is
//   found twice, which changes nothing). Fewer than sixteen candidates in all are checked one at
//   a time.
//
// Calling convention
//   System V AMD64 on every operating system: Rust declares the function extern "sysv64", so
//   Windows callers use it too, not the Microsoft x64 convention.
//     rdi  data  pointer to its first byte (need not be aligned)
//     rsi  len   number of bytes
//     rax        the words found (returned)
//   Clobbers rcx, rdx, rsi, rdi, r8, r9, xmm0 to xmm7 and the flags, all of which System V lets
//   a callee change; under System V every vector register is caller-saved.
//
// Why it is safe
//   It reads only data[0] to data[len - 1]: the vector loads at candidates 0 to len - 7 and six
//   bytes on cover up to data[len - 1] (the last block ends with the last candidate), and the
//   four-byte loads at a candidate and three bytes on end at its sixth byte. It writes no
//   memory, touches no stack (it is a leaf: no pushes, no frame, so Windows needs no unwind data
//   for it) and keeps no state between calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_gate_words on ELF, Mach-O and COFF alike: Rust links to
//   it by that exact name, as a leading \x01 byte in its link_name tells LLVM not to add the
//   underscore Mach-O symbols otherwise get. `.text` names the code section in all three object
//   formats, and `.p2align 4` starts the function and its main loop on 16-byte boundaries.
//   Local labels are numbered from 2 and avoid those made only of 0 and 1, which Intel syntax
//   reads as binary numbers.
//   On ELF targets the routine! macro in src/lib.rs adds the symbol's `.type` and `.size` after
//   the routine.

    .text
    .globl konstruct_gate_words
    .p2align 4
konstruct_gate_words:
    xor     eax, eax                    // eax: the words found
    cmp     rsi, 7
    jb      9f                          // fewer than seven bytes hold neither word
    lea     rsi, [rdi + rsi - 7]        // rsi: the last candidate
    lea     rcx, [rdi + 15]
    cmp     rcx, rsi
    ja      7f                          // fewer than 16 candidates: one at a time
    mov     ecx, 0x61616161
    movd    xmm4, ecx
    pshufd  xmm4, xmm4, 0               // xmm4: "a", sixteen times
    mov     ecx, 0x77777777
    movd    xmm5, ecx
    pshufd  xmm5, xmm5, 0               // xmm5: "w"
    mov     ecx, 0x64646464
    movd    xmm6, ecx
    pshufd  xmm6, xmm6, 0               // xmm6: "d"
    mov     ecx, 0x72727272
    movd    xmm7, ecx
    pshufd  xmm7, xmm7, 0               // xmm7: "r"
    .p2align 4
2:                                      // rdi: the block's first candidate
    movdqu  xmm0, xmmword ptr [rdi]
    movdqu  xmm1, xmmword ptr [rdi + 6]
    movdqa  xmm2, xmm0
    movdqa  xmm3, xmm1
    pcmpeqb xmm0, xmm4
    pcmpeqb xmm1, xmm5
    pand    xmm0, xmm1                  // candidates that start and end like "airflow"
    pcmpeqb xmm2, xmm6
    pcmpeqb xmm3, xmm7
    pand    xmm2, xmm3                  // and like "dagster"
    por     xmm0, xmm2
    pmovmskb ecx, xmm0                  // bit k: candidate k starts and ends like a word
    test    ecx, ecx
    jz      5f
3:
    bsf     edx, ecx
    add     rdx, rdi                    // rdx: the candidate
    mov     r8d, dword ptr [rdx]        // its first four bytes
    mov     r9d, dword ptr [rdx + 3]    // and its last four
    cmp     r8d, 0x66726961             // "airf"
    jne     4f
    cmp     r9d, 0x776f6c66             // "flow"
    jne     4f
    or      eax, 1
    jmp     6f
4:
    cmp     r8d, 0x73676164             // "dags"
    jne     6f
    cmp     r9d, 0x72657473             // "ster"
    jne     6f
    or      eax, 2
6:
    cmp     eax, 3
    je      9f                          // both found
    lea     edx, [rcx - 1]
    and     ecx, edx                    // the next candidate in the block
    jnz     3b
5:
    add     rdi, 16
    lea     rcx, [rdi + 15]
    cmp     rcx, rsi
    jbe     2b                          // another whole block
    cmp     rdi, rsi
    ja      9f                          // no candidates left
    lea     rdi, [rsi - 15]             // the last block ends with the last candidate
    jmp     2b
7:                                      // one candidate at a time
    mov     r8d, dword ptr [rdi]
    mov     r9d, dword ptr [rdi + 3]
    cmp     r8d, 0x66726961
    jne     12f
    cmp     r9d, 0x776f6c66
    jne     12f
    or      eax, 1
    jmp     13f
12:
    cmp     r8d, 0x73676164
    jne     13f
    cmp     r9d, 0x72657473
    jne     13f
    or      eax, 2
13:
    cmp     eax, 3
    je      9f
    inc     rdi
    cmp     rdi, rsi
    jbe     7b
9:
    ret
