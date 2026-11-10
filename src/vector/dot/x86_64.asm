// Inner product of two signed byte vectors: x86_64 with SSE2, Intel syntax.
//
// What it does
//   Returns the sum of a[i] * b[i] for i in 0..len, with a and b read as signed bytes, as a
//   32-bit integer that wraps modulo 2^32 (exact while len < 2^17, as |a[i] * b[i]| <= 2^14).
//   The vector index scores every candidate with it (src/vector/dot/mod.rs).
//
// Instruction set
//   SSE2 only, which every x86_64 processor has: one implementation for every machine the
//   wheels run on, with no run-time dispatch. AVX2 is not in the x86_64 baseline, and Rosetta
//   2, which runs the x86_64 macOS wheel on Apple silicon, has none. FMA does not apply: the
//   work is integer multiply-adds, which SSE2 has as `pmaddwd`.
//
// Method
//   Sixteen bytes of each vector at a time. Each byte is widened to 16 bits with its sign
//   (unpack a register with itself, then shift each 16-bit lane right arithmetically by 8),
//   `pmaddwd` multiplies the 16-bit lanes and adds neighbouring products into 32-bit lanes,
//   and two accumulators take the low and high halves. The 32-bit lanes are added together at
//   the end and the last len % 16 bytes are added one at a time.
//
// Calling convention
//   System V AMD64 on every operating system: Rust declares the function extern "sysv64", so
//   Windows callers use it too, not the Microsoft x64 convention.
//     rdi  a    pointer to the first vector (need not be aligned; unused when len is 0)
//     rsi  b    pointer to the second vector, likewise
//     rdx  len  number of bytes in each
//     eax       the inner product (returned)
//   Clobbers rcx, rdx, rsi, rdi, r8, xmm0 to xmm5 and the flags, all of which System V lets a
//   callee change.
//
// Why it is safe
//   It reads exactly a[0..len] and b[0..len], with unaligned loads (`movdqu`) and byte loads
//   for the tail, and reads nothing when len is 0; the Rust caller passes two slices of equal
//   length, so every byte read is inside a live allocation. It writes no memory, touches no
//   stack (a leaf with no frame, so Windows needs no unwind data), and keeps no state between
//   calls, so it is reentrant and thread safe.
//
// Symbol, section and alignment
//   The label is the plain name konstruct_dot_i8 on ELF, Mach-O and COFF alike, as with
//   konstruct_fnv1a64 (src/fnv/x86_64.asm). `.p2align 4` aligns the entry and the loop to
//   16 bytes. Local labels are numbered from 2: in Intel syntax 0 and 1 read as binary.
//   On ELF targets src/vector/dot/mod.rs adds the symbol's `.type` and `.size` after the
//   routine.

    .text
    .globl konstruct_dot_i8
    .p2align 4
konstruct_dot_i8:
    pxor    xmm4, xmm4                  // the low halves' 32-bit sums
    pxor    xmm5, xmm5                  // the high halves'
    mov     rcx, rdx
    shr     rcx, 4                      // whole blocks of 16 bytes
    jz      3f
    .p2align 4
2:
    movdqu  xmm0, xmmword ptr [rdi]     // 16 bytes of a
    movdqu  xmm1, xmmword ptr [rsi]     // and of b
    movdqa  xmm2, xmm0
    punpcklbw xmm2, xmm2                // each low byte of a twice: x, x
    psraw   xmm2, 8                     // the 16-bit lane x * 257 >> 8 is x, sign extended
    punpckhbw xmm0, xmm0                // the high bytes likewise
    psraw   xmm0, 8
    movdqa  xmm3, xmm1
    punpcklbw xmm3, xmm3                // and of b
    psraw   xmm3, 8
    punpckhbw xmm1, xmm1
    psraw   xmm1, 8
    pmaddwd xmm2, xmm3                  // a0*b0 + a1*b1, ..., four 32-bit sums
    pmaddwd xmm0, xmm1
    paddd   xmm4, xmm2
    paddd   xmm5, xmm0
    add     rdi, 16
    add     rsi, 16
    dec     rcx
    jnz     2b
3:
    paddd   xmm4, xmm5                  // the two accumulators, then their four lanes
    pshufd  xmm5, xmm4, 0x4e            // lanes 2, 3, 0, 1
    paddd   xmm4, xmm5
    pshufd  xmm5, xmm4, 0xb1            // lanes 1, 0, 3, 2
    paddd   xmm4, xmm5
    movd    eax, xmm4
    and     rdx, 15                     // the bytes after the last whole block
    jz      5f
4:
    movsx   ecx, byte ptr [rdi]
    movsx   r8d, byte ptr [rsi]
    imul    ecx, r8d
    add     eax, ecx
    inc     rdi
    inc     rsi
    dec     rdx
    jnz     4b
5:
    ret
