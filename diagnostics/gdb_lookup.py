"""Untimed x86-64 host diagnostics; guest gdbstub is disabled."""
import collections
import gdb
import json
import os
import shlex

cfg = json.load(open(os.environ['QEMU_BENCH_GDB_CONFIG']))
counts = {name: collections.Counter() for name in ('helper', 'state_lookup', 'dispatcher')}
failures = []
exit_status = []
for command in ('set pagination off', 'set confirm off', 'set auto-load off',
                'set disable-randomization off', 'handle SIGSEGV nostop noprint pass',
                'handle SIGBUS nostop noprint pass'):
    gdb.execute(command)
gdb.execute('set debuginfod enabled off')
gdb.execute('file ' + shlex.quote(cfg['qemu']))
gdb.execute('starti ' + shlex.join(['-cpu', cfg['cpu'], cfg['guest'], str(cfg['iterations'])])
            + ' > ' + shlex.quote(cfg['stdout']))


class Entry(gdb.Breakpoint):
    def __init__(self, name, symbol):
        super().__init__('*' + symbol, internal=True)
        self.counter_name = name

    def stop(self):
        try:
            if self.counter_name == 'helper':
                env = gdb.parse_and_eval('(CPURISCVState *)$rdi')
                pc = int(env.dereference()['pc'])
            else:
                # get_tb_cpu_state returns a 24-byte struct: hidden sret
                # pointer is in RDI, CPUState *cs is the second ABI argument.
                reg = '$rsi' if self.counter_name == 'state_lookup' else '$rdi'
                cpu = gdb.parse_and_eval('(RISCVCPU *)' + reg)
                pc = int(cpu.dereference()['env']['pc'])
            counts[self.counter_name][pc] += 1
            return False
        except Exception as error:
            failures.append(str(error))
            return True


breakpoints = [Entry('helper', 'helper_lookup_tb_ptr'),
               Entry('state_lookup', 'riscv_get_tb_cpu_state'),
               Entry('dispatcher', 'cpu_tb_exec')]
gdb.events.exited.connect(lambda event: exit_status.append(event.exit_code))
gdb.execute('continue')
result = dict(method='native host GDB entry breakpoints, no guest gdbstub',
              iterations=cfg['iterations'], timing_usable=False, config=cfg,
              counts={name: {hex(pc): n for pc, n in sorted(values.items())}
                      for name, values in counts.items()},
              target_successor={name: values[cfg['successor_pc']]
                                for name, values in counts.items()},
              exit_status=exit_status, errors=failures)
with open(cfg['output'], 'w') as stream:
    json.dump(result, stream, indent=2)
    stream.write('\n')
if failures or exit_status != [0]:
    raise gdb.GdbError(str(result))
