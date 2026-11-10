"""YAML workload definitions, experiment records, and host CPU affinity."""
from __future__ import annotations

import hashlib
from functools import lru_cache
import json
import os
from pathlib import Path
import re

try:
    import yaml
except ImportError as error:
    raise RuntimeError('Install configuration dependencies: python3 -m pip install -r requirements.txt') from error

HERE = Path(__file__).resolve().parent
WORKLOADS = HERE / 'workloads'


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValueError('YAML mapping keys must be strings')
        if key in result:
            raise ValueError(f'Duplicate YAML key: {key}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def read_yaml(path: Path) -> dict:
    try:
        value = yaml.load(path.read_text(), Loader=UniqueLoader)
    except yaml.YAMLError as error:
        raise ValueError(f'Invalid YAML in {path}: {error}') from error
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1:
        raise ValueError(f'{path}: expected a mapping with schema: 1')
    return value


def only_keys(value, allowed, description):
    if not isinstance(value, dict):
        raise ValueError(f'{description} must be a mapping')
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f'{description}: unknown keys: {sorted(unknown)}')


@lru_cache(maxsize=None)
def documents(directory=WORKLOADS) -> dict:
    found = {}
    for path in sorted(Path(directory).rglob('*.yml')):
        value = read_yaml(path)
        name = value.get('id')
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
            raise ValueError(f'{path}: invalid workload id')
        if name in found:
            raise ValueError(f'Duplicate workload id: {name}')
        if value.get('kind') == 'package-build':
            only_keys(value, ('schema', 'id', 'kind', 'source', 'recipe', 'artifacts', 'validation'), str(path))
            source = value.get('source')
            only_keys(source, ('version', 'url', 'sha256', 'mirrors'), f'{path}: source')
            if not all(isinstance(source.get(k), str) and source[k] for k in ('version', 'url', 'sha256')):
                raise ValueError(f'{path}: source version, url and sha256 must be strings')
            if not re.fullmatch(r'[0-9a-f]{64}', source['sha256']):
                raise ValueError(f'{path}: source sha256 must contain 64 lowercase hex digits')
            if value.get('recipe') not in ('lz4', 'zlib', 'sqlite', 'zlib-ng'):
                raise ValueError(f'{path}: unknown build recipe')
            if value.get('validation') not in ('lz4-roundtrip', 'zlib-example', 'sqlite-integrity', 'zlib-ng-roundtrip'):
                raise ValueError(f'{path}: unknown validation recipe')
            artifacts = value.get('artifacts')
            if not isinstance(artifacts, list) or not artifacts or any(
                    not isinstance(a, str) or Path(a).is_absolute() or '..' in Path(a).parts for a in artifacts):
                raise ValueError(f'{path}: artifacts must be nonempty relative paths')
            mirrors = source.get('mirrors', [])
            if not isinstance(mirrors, list) or any(not isinstance(url, str) for url in mirrors):
                raise ValueError(f'{path}: mirrors must be a list of URLs')
        elif value.get('kind') == 'guest-program':
            only_keys(value, ('schema', 'id', 'kind', 'source', 'args', 'expected_stdout'), str(path))
            source = value.get('source')
            only_keys(source, ('path',), f'{path}: source')
            filename = source.get('path')
            if not isinstance(filename, str) or Path(filename).suffix not in ('.c', '.S', '.s'):
                raise ValueError(f'{path}: source.path must name a .c, .S or .s file')
            args = value.get('args', [])
            if not isinstance(args, list) or any(not isinstance(arg, str) or '\0' in arg for arg in args):
                raise ValueError(f'{path}: args must be a list of strings')
            if not isinstance(value.get('expected_stdout'), str):
                raise ValueError(f'{path}: expected_stdout must be a string')
        elif value.get('kind') == 'micro':
            only_keys(value, ('schema', 'id', 'kind', 'runner', 'cpu', 'source', 'cases'), str(path))
            if value.get('runner') not in ('vsetivli', 'vsetvli') or not isinstance(value.get('cpu'), str):
                raise ValueError(f'{path}: expected a known micro runner and a guest CPU string')
            if not isinstance(value.get('cases'), (dict, list)) or not value['cases']:
                raise ValueError(f'{path}: expected nonempty cases')
        else:
            raise ValueError(f'{path}: unknown workload kind')
        found[name] = {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'definition': value}
    return found


def package_registry() -> dict:
    return {name: dict(item['definition']['source'],
                       **{key: item['definition'][key] for key in ('recipe', 'artifacts', 'validation')})
            for name, item in documents().items() if item['definition']['kind'] == 'package-build'}


def micro_definition(name: str) -> dict:
    value = documents()[name]['definition']
    if value['kind'] != 'micro':
        raise ValueError(f'{name} is not a micro workload')
    return value


def parse_affinity(value) -> set[int] | None:
    if value is None:
        return None
    if isinstance(value, list):
        if not value or any(type(cpu) is not int or cpu < 0 for cpu in value):
            raise ValueError('host.affinity must be null or a nonempty list of nonnegative CPU IDs')
        return set(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError('affinity must be a CPU list such as 2 or 2-3,6')
    result = set()
    for part in value.split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?', part):
            raise ValueError(f'Invalid affinity component: {part}')
        bounds = [int(x) for x in part.split('-')]
        first, last = bounds[0], bounds[-1]
        if last < first or last - first > 1048576:
            raise ValueError(f'Invalid affinity range: {part}')
        result.update(range(first, last + 1))
    return result


def apply_affinity(value) -> dict:
    before = set(os.sched_getaffinity(0))
    requested = parse_affinity(value)
    if requested is not None:
        unavailable = requested - before
        if unavailable:
            raise ValueError(f'CPUs unavailable in the inherited affinity/cpuset: {sorted(unavailable)}')
        os.sched_setaffinity(0, requested)
        effective = set(os.sched_getaffinity(0))
        if effective != requested:
            os.sched_setaffinity(0, before)
            raise RuntimeError(f'Affinity did not take effect exactly: requested {sorted(requested)}, got {sorted(effective)}')
    else:
        effective = before
    return dict(requested=sorted(requested) if requested is not None else None,
                inherited=sorted(before), effective=sorted(effective))


def cpu_inventory() -> list[dict]:
    allowed = os.sched_getaffinity(0)
    result = []
    for path in sorted(Path('/sys/devices/system/cpu').glob('cpu[0-9]*'), key=lambda p: int(p.name[3:])):
        def read(relative):
            try:
                return (path / relative).read_text().strip()
            except OSError:
                return None
        cpu = int(path.name[3:])
        result.append(dict(cpu=cpu, allowed=cpu in allowed, socket=read('topology/physical_package_id'),
                           core=read('topology/core_id'), siblings=read('topology/thread_siblings_list'),
                           governor=read('cpufreq/scaling_governor'),
                           max_frequency_khz=read('cpufreq/cpuinfo_max_freq')))
    return result


def jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [jsonable(v) for v in value]
    return value


def manifest(args, qemus, workload_ids, affinity, *, runner):
    all_documents = documents()
    return dict(schema=1, runner=runner,
                reference=qemus[0]['label'] if qemus else getattr(args, 'reference', None),
                experiment=getattr(args, 'experiment_record', None),
                arguments=jsonable({k: v for k, v in vars(args).items() if k != 'experiment_record'}),
                workloads={name: all_documents[name] for name in workload_ids},
                qemu=qemus, affinity=affinity)


def write_manifest(directory, args, qemus, workload_ids, affinity, *, runner, **facts):
    value = manifest(args, qemus, workload_ids, affinity, runner=runner)
    value.update(jsonable(facts))
    (Path(directory) / 'manifest.json').write_text(json.dumps(value, indent=2) + '\n')
