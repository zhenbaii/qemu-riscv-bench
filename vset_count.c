/* Local benchmark instrumentation; counts guest RISC-V vector configuration. */
#include <qemu-plugin.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;
static _Atomic uint64_t counts[3];
static char *output_dir;

static void count_instruction(unsigned int cpu_index, void *userdata)
{
    (void)cpu_index;
    atomic_fetch_add_explicit(&counts[(uintptr_t)userdata], 1, memory_order_relaxed);
}

static void translated(struct qemu_plugin_tb *tb, void *userdata)
{
    (void)userdata;
    for (size_t i = 0; i < qemu_plugin_tb_n_insns(tb); i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        unsigned char bytes[4];
        if (qemu_plugin_insn_data(insn, bytes, sizeof(bytes)) != 4) {
            continue;
        }
        uint32_t word = (uint32_t)bytes[0] | (uint32_t)bytes[1] << 8 |
                        (uint32_t)bytes[2] << 16 | (uint32_t)bytes[3] << 24;
        int kind = -1;
        if ((word & 0xc000707f) == 0xc0007057) {
            kind = 0; /* vsetivli */
        } else if ((word & 0x8000707f) == 0x00007057) {
            kind = 1; /* vsetvli */
        } else if ((word & 0xfe00707f) == 0x80007057) {
            kind = 2; /* vsetvl */
        }
        if (kind >= 0) {
            qemu_plugin_register_vcpu_insn_exec_cb(insn, count_instruction,
                QEMU_PLUGIN_CB_NO_REGS, (void *)(uintptr_t)kind);
        }
    }
}

static void finish(void *userdata)
{
    (void)userdata;
    char *path = g_strdup_printf("%s/%ld.json", output_dir, (long)getpid());
    FILE *stream = fopen(path, "w");
    if (stream) {
        fprintf(stream, "{\"vsetivli\":%" PRIu64 ",\"vsetvli\":%" PRIu64
                ",\"vsetvl\":%" PRIu64 "}\n", atomic_load(&counts[0]),
                atomic_load(&counts[1]), atomic_load(&counts[2]));
        fclose(stream);
    } else {
        perror(path);
    }
    g_free(path);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
    const qemu_info_t *info, int argc, char **argv)
{
    if (strcmp(info->target_name, "riscv64") || argc != 1 ||
        strncmp(argv[0], "out=", 4)) {
        fprintf(stderr, "vset_count requires a riscv64 user-mode QEMU and out=DIR\n");
        return -1;
    }
    output_dir = g_strdup(argv[0] + 4);
    qemu_plugin_register_vcpu_tb_trans_cb(id, translated, NULL);
    qemu_plugin_register_atexit_cb(id, finish, NULL);
    return 0;
}
