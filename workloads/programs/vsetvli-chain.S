/* Compare TB dispatch after a full VL and a shorter VL with VLEN=128. */
.option norvc
.section .text
.globl _start
_start:
    li t0, 100000000
    li t1, 16
    li t2, 7
    .p2align 6
.Lloop:
    vsetvli t3, t1, e8, m1, ta, ma
    vsetvli t4, t2, e8, m1, ta, ma
    addi t0, t0, -1
    bnez t0, .Lloop

    bne t3, t1, .Lfail
    bne t4, t2, .Lfail
    csrr t5, vl
    bne t5, t2, .Lfail
    li a0, 0
    j .Lexit
.Lfail:
    li a0, 1
.Lexit:
    li a7, 93
    ecall
.section .note.GNU-stack,"",@progbits
