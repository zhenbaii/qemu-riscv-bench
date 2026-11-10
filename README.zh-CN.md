# QEMU RISC-V benchmark

[English](README.md)

## 复现补丁结果

需要 Linux、Python 3.12+、QEMU 构建依赖和 RISC-V Linux 交叉工具链。
采样需要宿主机的 `perf`。

通用负载使用 `build.compiler: rvv` 时，bench 会在首次计时前下载 GCC，
构建并缓存 guest `cc1`。宿主机需要 RISC-V GCC 14.2.0 交叉编译器、
`gcc`、`g++`、`make` 和 `dpkg-deb`。

准备一份干净的 QEMU checkout，确保基线和补丁两个 commit 都在其中。
将它放在 bench 仓库旁的 `../qemu`，也可以用 `QEMU_SRC` 指定路径。
以下命令在 bench 仓库根目录运行。

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt

./reproduce.sh BASELINE_COMMIT PATCHED_COMMIT --recipe recipe-name
```

把 `recipe-name` 换成 [`recipes/`](recipes/) 下去掉 `.yml` 后缀的文件名。
脚本会构建两份 QEMU，并运行所选 recipe。加 `--dry-run` 可预览，
用 `--affinity CPU` 可覆盖 recipe 的绑核设置。

## 使用已有的 QEMU 二进制

```sh
./run.sh --list

./run.sh --config experiments/packages.example.yml zlib \
  --qemu baseline=/path/to/baseline/qemu-riscv64 \
  --qemu candidate=/path/to/patched/qemu-riscv64
```

运行 guest 程序时，用同一组 `--qemu` 参数运行
`./run.sh --config experiments/programs.example.yml`。
自定义负载和 recipe 见 [YAML 配置说明](docs/configuration.md)。

Recipe 会在终端汇总结果。报告和原始样本保存在 `data/`。

[测量流程](docs/protocol.md) · [来源说明](PROVENANCE.md)
