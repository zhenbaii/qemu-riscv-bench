# QEMU RISC-V benchmark

[English](README.md)

## 复现补丁结果

现有 recipe 需要 x86-64 Linux、Python 3.12+、QEMU 构建依赖、
`riscv64-linux-gnu-gcc` 和 `riscv64-linux-gnu-gcc-14`（14.2.0）。
采样需要宿主机的 `perf`。

准备一份干净的 QEMU checkout，确保基线和补丁两个 commit 都在其中。
将它放在 bench 仓库旁的 `../qemu`，也可以用 `QEMU_SRC` 指定路径。
以下命令在 bench 仓库根目录运行。

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt

./reproduce.sh BASELINE_COMMIT PATCHED_COMMIT --recipe vsetvli-v2
```

脚本会构建两份 QEMU，并绑定到第一个允许使用的逻辑 CPU。
`vsetvli-v1` 和 `vsetvli-v2` 跑相同的专项程序和 LZ4、zlib、SQLite 构建。
首次运行通用负载会构建并缓存 guest `cc1`。加 `--dry-run` 可预览，
用 `--affinity CPU` 可指定绑核。

## 使用已有的 QEMU 二进制

```sh
./run.sh --list

./run.sh --config experiments/vsetvli-v1-mail.yml zlib \
  --qemu baseline=/path/to/baseline/qemu-riscv64 \
  --qemu candidate=/path/to/patched/qemu-riscv64
```

单独跑专项程序时，用同一组 `--qemu` 参数运行
`./run.sh --config experiments/vsetvli-v1-program.yml`。
自定义负载见 [YAML 配置说明](docs/configuration.md)。

Recipe 会在终端汇总结果。报告和原始样本保存在 `data/programs/` 和 `data/runs/`。

[测量流程](docs/protocol.md) · [来源说明](PROVENANCE.md)
