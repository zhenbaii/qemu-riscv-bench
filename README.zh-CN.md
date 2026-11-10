# QEMU RISC-V benchmark

通用项目构建负载和指令 microbenchmark 共用运行入口，支持指定多份 QEMU、
自动绑核、交替 A/B 测量及结果校验。完整依赖见 [英文 README](README.md)。

## 复现邮件结果

准备一份干净的 QEMU checkout，确保其中已有基线和应用补丁后的两个 commit。
默认使用 bench 仓库旁边的 `../qemu`，其他位置用 `QEMU_SRC` 指定。

```sh
./reproduce.sh "$baseline_commit" "$patched_commit" --recipe vsetvli-v1
```

脚本用同一套配置依次构建两个 commit 的静态 `qemu-riscv64`，并恢复原 checkout。
`vsetvli-v1` recipe 先跑[专项程序和 perf 采样](experiments/vsetvli-v1-program.yml)，
再跑[邮件中的 LZ4、zlib、SQLite 构建](experiments/vsetvli-v1-mail.yml)。
两段完成后，终端在 `Combined results` 下集中显示两份摘要。
构建负载使用原实验的 amd64 GCC 14.2.0 依赖锁，需要 `riscv64-linux-gnu-gcc-14`。
两段默认都绑定启动进程允许使用的编号最小的逻辑 CPU，并把实际绑核写入 manifest。
可用 `--affinity CPU` 指定其他 CPU，用 `--dry-run` 预览两组计划。
自定义实验或单个负载用 `run.sh`。

## 目录

```text
workloads/
  packages/              # SQLite、LZ4、zlib、zlib-ng 的版本、来源、SHA-256、recipe
  micro/                 # vsetvli、vsetivli 的 CPU 要求、测试项及输入校验和
  programs/              # 每次优化可添加自己的 guest 程序和负载定义
experiments/             # QEMU、基线 label、工具链、绑核、测量轮数等实验配置
reproduce.sh             # 传两个 QEMU commit，用 --recipe 选择整组负载
bench.py                 # 统一入口：合并配置、预览、选择运行器
configuration.py         # YAML 读取、配置快照、CPU 拓扑和绑核
run.py                   # 通用项目构建运行器
programs.py              # 编译并直接运行自定义 guest 程序
vsetvli.py               # vsetvli 小测试运行器
microbench.py            # vsetivli 小测试运行器
results/                 # 保留原提交身份的历史结果
data/                    # 实际运行时产生的缓存、日志和结果，Git 忽略
```

## 安装与预览

在仓库根目录运行：

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt

./run.sh --list
./run.sh --list-cpus
./run.sh --config experiments/packages.example.yml --dry-run
```

这些列表和预览命令不会启动 benchmark。`--dry-run` 也不会下载、构建、执行 QEMU、
创建结果目录或改变 affinity；输出 JSON 中的 `effective` 为 `null`，实际运行时才读回生效值。

## 实验配置

复制示例后，填写自己的路径和 CPU 集合：

```sh
cp experiments/packages.example.yml experiments/packages.local.yml
```

`packages.local.yml` 中的主要字段如下，其余字段保留示例默认值即可：

```yaml
workloads: [sqlite, lz4, zlib]
reference: baseline
qemu:
  baseline:
    path: /absolute/path/before/qemu-riscv64
    commit: null
  candidate:
    path: /absolute/path/after/qemu-riscv64
    commit: null
host:
  affinity: [2]
build:
  compiler: rvv
  jobs: 1
measurement:
  warmups: 1
  repetitions: 10
```

CPU 2 仅为示例。可以用 `--list-cpus` 查看当前允许使用的逻辑 CPU、物理核及 SMT
同核线程；`affinity: null` 表示继承已有设置。负载 YAML 不保存本机 CPU 编号。

配置中的相对文件路径以该 YAML 所在目录为基准；命令行路径以当前目录为基准。
命令行参数优先。`--qemu LABEL=PATH` 只覆盖对应 label 的路径，其他 QEMU 配置保留。

```sh
# 仅预览覆盖后的配置
./run.sh --config experiments/packages.local.yml --affinity 2 --jobs 1 --dry-run

# vsetvli 专用测试的配置预览
./run.sh --config experiments/vsetvli.example.yml --dry-run
```

需要真正执行时，去掉 `--dry-run`。这一步才会准备依赖并启动所选负载。
项目构建默认一次性准备 GCC 14.2.0 的 RVV guest 编译器，需要匹配的交叉驱动、
额外构建时间和数 GB 磁盘；也可以提供已有的 guest `cc1`。

## 自动绑核和记录

实际运行时，bench 检查指定 CPU 是否包含在继承的可用集合内，设置 affinity 并读回核对。
失败会报错，不会悄悄换核。测量进程及后续启动的 make、编译器、QEMU 子进程继承此设置。
bench 读取并记录 governor、频率范围、boost、SMT 等主机信息。

每个结果目录保存 `manifest.json`，包含展开后的实验参数、负载 YAML 内容和校验和、
QEMU 身份，以及请求、继承和实际生效的 affinity；计时样本、日志和产物检查另行保存。
绑核后，其他进程和同核 SMT 线程仍可能争用资源。

项目构建的计时范围为：宿主 make/GCC driver/交叉 binutils，加上 QEMU 下执行的
RISC-V `cc1`。它与整个 guest 用户空间内构建的计时范围不同。

配置字段详解见 [docs/configuration.md](docs/configuration.md)。历史结果见
[历史实验说明](docs/historical-vsetvli.md)，代码来源见 [PROVENANCE.md](PROVENANCE.md)。

## 自定义专项负载

在 `workloads/programs/` 放一个自包含的 C 或汇编文件，以及描述源码、命令行参数和
预期标准输出的 YAML。可以从 [RVV 数组加法示例](workloads/programs/rvv-add.yml) 开始，
在实验 YAML 的 `workloads` 中选择用例。

```sh
./run.sh --config experiments/programs.example.yml \
  --qemu baseline=/path/to/before/qemu-riscv64 \
  --qemu candidate=/path/to/after/qemu-riscv64 --affinity 7
```

宿主交叉编译器先为每个用例生成一份静态 ELF，两版 QEMU 再直接运行同一份 ELF。
计时包括 QEMU 启动和 guest 执行。结果保存到 `data/programs/`，包含源码和 ELF 的副本及
哈希、逐轮计时、标准输出和日志。每轮检查退出码和标准输出。编译参数、绑核、预热和轮数
由实验配置控制，示例用 `-O3 -march=rv64gcv`。具体字段见 [配置说明](docs/configuration.md)。

[vsetvli v1 专项预设](experiments/vsetvli-v1-program.yml)在热循环中交替设置满 VL 和较短 VL。
它包含在 `reproduce.sh ... --recipe vsetvli-v1` 中。已有两份 QEMU 时，也可单独运行专项预设。

```sh
./run.sh --config experiments/vsetvli-v1-program.yml \
  --qemu "baseline=$baseline_qemu" --qemu "candidate=$patched_qemu"
```

先完成五对正式计时，再对两份 QEMU 各做一次宿主 perf 采样。摘要记录耗时变化和
`helper_lookup_tb_ptr` 自身的用户态周期占比，运行目录保留原生 perf 文本报告和 `perf.data`。
