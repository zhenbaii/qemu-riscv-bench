#!/usr/bin/env python3
"""Run or inspect a YAML-defined QEMU experiment. --dry-run has no workload side effects."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys

import configuration as config

MODULES = {'packages': 'run', 'vsetvli': 'vsetvli', 'vsetivli': 'microbench'}
SECTIONS = {
    'host': {'affinity'},
    'build': {'cross_cc', 'compiler', 'bootstrap_jobs', 'guest_cc1', 'guest_sysroot',
              'toolchain_lock', 'bootstrap_lock', 'jobs', 'march', 'cflags'},
    'guest': {'cpu'},
    'measurement': {'warmups', 'repetitions', 'timeout', 'min_seconds', 'iterations'},
    'storage': {'data_dir', 'output', 'offline', 'source_dir'},
    'diagnostics': {'perf', 'lookup_counts', 'diagnose_only', 'profile_vset',
                    'profile_qemu', 'plugin_include'},
}
PATH_FIELDS = {'guest_cc1', 'guest_sysroot', 'toolchain_lock', 'bootstrap_lock',
               'data_dir', 'output', 'source_dir', 'profile_qemu', 'plugin_include'}
BOOL_FIELDS = {'offline', 'perf', 'lookup_counts', 'diagnose_only', 'profile_vset'}


def file_path(value, parent):
    if not isinstance(value, str) or not value:
        raise ValueError('Configured paths must be nonempty strings, or null when unspecified')
    path = Path(value).expanduser()
    return str((parent / path).resolve() if not path.is_absolute() else path.resolve())


def assignments(values, description):
    result = {}
    for value in values or []:
        label, sep, entry = value.partition('=')
        if not sep or not re.fullmatch(r'[a-zA-Z0-9_-]+', label) or not entry:
            raise ValueError(f'{description} expects LABEL=VALUE')
        if label in result:
            raise ValueError(f'Duplicate {description} label: {label}')
        result[label] = entry
    return result


def load_experiment(path):
    if path is None:
        return {}, None
    path = path.expanduser().resolve()
    document = config.read_yaml(path)
    config.only_keys(document, {'schema', 'name', 'runner', 'workloads', 'reference', 'qemu', *SECTIONS}, str(path))
    if not isinstance(document.get('name'), str) or not document['name']:
        raise ValueError('Experiment name must be a nonempty string')
    for name, allowed in SECTIONS.items():
        config.only_keys(document.get(name, {}), allowed, name)
    if document.get('runner') not in MODULES:
        raise ValueError(f'Experiment runner must be one of {list(MODULES)}')
    qemus = document.get('qemu', {})
    config.only_keys(qemus, qemus.keys() if isinstance(qemus, dict) else (), 'qemu')
    for label, value in qemus.items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', label):
            raise ValueError(f'Invalid QEMU label: {label}')
        config.only_keys(value, ('path', 'commit'), f'qemu.{label}')
        if value.get('path') is not None:
            file_path(value['path'], path.parent)
        if value.get('commit') is not None and (not isinstance(value['commit'], str) or
                not re.fullmatch(r'[0-9a-fA-F]{40}', value['commit'])):
            raise ValueError(f'qemu.{label}.commit must be a full SHA40 string or null')
    return document, {'path': str(path), 'definition': document,
                       'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def resolve(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
        epilog='Runner options such as --repetitions, --jobs, --case and --iterations may follow these options.')
    parser.add_argument('--config', type=Path, help='experiment YAML; relative paths inside resolve from this file')
    parser.add_argument('--runner', choices=list(MODULES), help='default: configured runner, otherwise packages')
    parser.add_argument('--list', action='store_true', help='list YAML workload definitions; no execution')
    parser.add_argument('--list-cpus', action='store_true', help='read host CPU topology and inherited mask; no changes')
    parser.add_argument('--dry-run', action='store_true', help='print resolved JSON without downloading, building or running QEMU')
    parser.add_argument('--qemu', action='append', metavar='LABEL=PATH', help='override a configured path by label')
    parser.add_argument('--qemu-commit', action='append', metavar='LABEL=SHA40')
    parser.add_argument('--reference', help='QEMU label used as statistical reference')
    parser.add_argument('--affinity', help='host CPU IDs, e.g. 2 or 2-3,6; otherwise config or inherited')
    common, forwarded = parser.parse_known_args(argv)
    if common.list:
        return {'action': 'list'}
    if common.list_cpus:
        return {'action': 'list-cpus'}
    document, record = load_experiment(common.config)
    runner = common.runner or document.get('runner', 'packages')
    if document and common.runner and common.runner != document['runner']:
        raise ValueError('--runner conflicts with the experiment runner; choose another configuration')
    module = importlib.import_module(MODULES[runner])
    parent = Path(record['path']).parent if record else Path.cwd()
    names = document.get('workloads', [])
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise ValueError('Experiment workloads must be a list of workload IDs')
    catalog = config.documents()
    if any(name not in catalog for name in names):
        raise ValueError(f'Unknown workloads: {sorted(set(names) - set(catalog))}')
    if runner == 'packages' and any(catalog[name]['definition']['kind'] != 'package-build' for name in names):
        raise ValueError('The packages runner accepts only package-build workloads')
    if runner != 'packages' and names not in ([], [runner]):
        raise ValueError(f'The {runner} runner requires workloads: [{runner}]')
    settings = {}
    for section in SECTIONS:
        settings.update(document.get(section, {}))
    settings.pop('affinity', None)
    configuration_argv = []
    explicit_options = {arg.partition('=')[0] for arg in forwarded if arg.startswith('--')}
    for key, value in settings.items():
        option = '--' + key.replace('_', '-')
        if option in explicit_options or value is None:
            continue
        if key in BOOL_FIELDS:
            if type(value) is not bool:
                raise ValueError(f'{key} must be true or false')
            if value:
                configuration_argv.append(option)
        else:
            if key in PATH_FIELDS:
                value = file_path(value, parent)
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ValueError(f'{key} must be a scalar value')
            # The = form also supports compiler flags beginning with a dash.
            configuration_argv.append(f'{option}={value}')
    runner_argv = [*configuration_argv, *forwarded]
    args = (module.arguments(runner_argv, defaults={'packages': names}) if runner == 'packages'
            else module.arguments(runner_argv))
    qemus = {}
    for label, value in document.get('qemu', {}).items():
        qemus[label] = dict(value, path=file_path(value['path'], parent) if value.get('path') else None)
    for label, path in assignments(common.qemu, '--qemu').items():
        qemus.setdefault(label, {})['path'] = file_path(path, Path.cwd())
    for label, commit in assignments(common.qemu_commit, '--qemu-commit').items():
        if label not in qemus:
            raise ValueError(f'--qemu-commit refers to unknown label: {label}')
        if not re.fullmatch(r'[0-9a-fA-F]{40}', commit):
            raise ValueError('--qemu-commit requires a full SHA40')
        qemus[label]['commit'] = commit
    reference = common.reference or document.get('reference')
    if reference is not None and reference not in qemus:
        raise ValueError(f'Unknown reference QEMU label: {reference}')
    configured = [(label, value) for label, value in qemus.items() if value.get('path')]
    if reference and configured and not qemus[reference].get('path'):
        raise ValueError(f'The reference QEMU {reference} has no path')
    configured.sort(key=lambda item: item[0] != reference if reference else False)
    args.qemu = [f'{label}={value["path"]}' for label, value in configured] or None
    args.qemu_commit = [f'{label}={value["commit"]}' for label, value in configured if value.get('commit')]
    args.reference = reference
    affinity = common.affinity if common.affinity is not None else document.get('host', {}).get('affinity')
    requested = config.parse_affinity(affinity)
    args.affinity = ','.join(map(str, sorted(requested))) if requested is not None else None
    args.experiment_record = record
    selected = args.packages if runner == 'packages' else [runner]
    plan = config.manifest(args, [], selected,
                           dict(requested=sorted(requested) if requested is not None else None,
                                inherited=sorted(os.sched_getaffinity(0)), effective=None), runner=runner)
    plan.update(action='dry-run' if common.dry_run else 'run',
                qemu=[dict(label=label, **value) for label, value in qemus.items()],
                reference=reference or next(iter(qemus), None),
                note='Dry-run does not inspect/execute QEMU binaries or apply affinity; runtime records actual identities and effective mask.')
    return dict(action=plan['action'], plan=plan, module=module, args=args)


def main(argv=None):
    resolved = resolve(argv)
    if resolved['action'] == 'list':
        for name, item in config.documents().items():
            value = item['definition']
            print(f"{name:12} {value['kind']:14} {value.get('source', {}).get('version', '')}")
        return 0
    if resolved['action'] == 'list-cpus':
        print(json.dumps(config.cpu_inventory(), indent=2))
        return 0
    if resolved['action'] == 'dry-run':
        print(json.dumps(resolved['plan'], indent=2))
        return 0
    missing = [item['label'] for item in resolved['plan']['qemu'] if not item.get('path')]
    if missing:
        raise ValueError(f'Set QEMU paths in the experiment or with --qemu LABEL=PATH: {missing}')
    return resolved['module'].main(prepared_args=resolved['args'])


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError) as error:
        print(f'bench: {error}', file=sys.stderr)
        sys.exit(1)
