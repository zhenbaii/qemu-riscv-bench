/* AI-assisted local RVV experiment: no upstream contribution. */
.option norvc
.section .text
.global _start
_start:
    ld t0, 0(sp)
    li t1, 2
    bne t0, t1, fail
    ld t0, 16(sp)
    li s0, 0
parse:
    lbu t1, 0(t0)
    beqz t1, parsed
    addi t1, t1, -48
    li t2, 9
    bgtu t1, t2, fail
    li t2, 10
    mul s0, s0, t2
    add s0, s0, t1
    addi t0, t0, 1
    j parse
parsed:
    beqz s0, fail
    mv s3, s0
    li s1, 0
    li a0, 8
    vsetvli t1, a0, e8, mf2, tu, mu
    vmv.v.i v0, 0
    j benchmark_loop
    .p2align 12
.global benchmark_loop
benchmark_loop:
    xori a0, a0, 8
.global vector_config
vector_config:
    vsetvli t1, a0, e8, mf2, tu, mu
.global vector_successor
vector_successor:
    add s1, s1, t1
    vadd.vi v0, v0, 1
    addi s0, s0, -1
    bnez s0, benchmark_loop
    addi sp, sp, -64
    sd s1, 0(sp)
    csrr t0, vl
    sd t0, 8(sp)
    csrr t0, vtype
    sd t0, 16(sp)
    csrr t0, vstart
    sd t0, 24(sp)
    li a0, 8
    vsetvli t1, a0, e8, mf2, tu, mu
    addi a1, sp, 32
    vse8.v v0, (a1)
    li a0, 1
    mv a1, sp
    li a2, 40
    li a7, 64
    ecall
    li t0, 40
    bne a0, t0, fail
    li a0, 0
    li a7, 93
    ecall
fail:
    li a0, 2
    li a7, 93
    ecall
.section .note.GNU-stack,"",@progbits
