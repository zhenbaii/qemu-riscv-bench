/* Local experiment: RVV state transitions and a libc-free scalar control. */
    .option norvc
    .section .text
    .global _start
_start:
    ld t0, 0(sp)
    li t1, 2
    bne t0, t1, bad_argument
    ld t0, 16(sp)
    li s0, 0
    li t3, 922337203685477580
parse_digit:
    lbu t1, 0(t0)
    beqz t1, parsed
    addi t1, t1, -48
    li t2, 9
    bgtu t1, t2, bad_argument
    bgtu s0, t3, bad_argument
    bne s0, t3, accumulate
    li t2, 7
    bgtu t1, t2, bad_argument
accumulate:
    li t2, 10
    mul s0, s0, t2
    add s0, s0, t1
    addi t0, t0, 1
    j parse_digit
parsed:
    beqz s0, bad_argument
    li s1, 0
    mv t0, s0
    j benchmark_loop

    /* Keep the hot loop within one guest page for eligible goto_tb edges. */
    .p2align 12
    .global benchmark_loop
benchmark_loop:
#if VECTOR_CASE
    .global vector_config_first
vector_config_first:
    vsetivli t1, 4, e32, m1, ta, ma
    .global vector_successor_first
vector_successor_first:
    vmv.v.x v0, s1
    vadd.vi v0, v0, 1
    vmv.x.s s1, v0
    .global vector_config_second
vector_config_second:
    vsetivli t1, 8, e16, m1, ta, ma
    .global vector_successor_second
vector_successor_second:
#else
    /* Same wrapping 32-bit accumulator, without any vector instruction. */
    addiw s1, s1, 1
#endif
    addi t0, t0, -1
    bnez t0, benchmark_loop
    addi sp, sp, -32
    sd s1, 0(sp)
#if VECTOR_CASE
    csrr t0, vl
    csrr t1, vtype
#else
    li t0, 0
    li t1, 0
#endif
    sd t0, 8(sp)
    sd t1, 16(sp)
    li a0, 1
    mv a1, sp
    li a2, 24
    li a7, 64
    ecall
    li t0, 24
    bne a0, t0, bad_argument
    li a0, 0
    li a7, 93
    ecall
bad_argument:
    li a0, 2
    li a7, 93
    ecall

    .section .note.GNU-stack,"",@progbits
