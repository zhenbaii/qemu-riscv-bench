"""Resolve ordered recipe includes for reproduce.sh."""

from pathlib import Path
import re
import sys

import configuration

HERE = Path(__file__).resolve().parent


def resolve(name, directory=HERE / 'recipes', experiments=HERE / 'experiments'):
    stages = []
    resolved = set()

    def include(recipe, parents):
        if not isinstance(recipe, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', recipe):
            raise ValueError(f'Invalid recipe name: {recipe!r}')
        if recipe in parents:
            raise ValueError('Recipe include cycle: ' + ' -> '.join((*parents, recipe)))
        if recipe in resolved:
            return
        path = directory / f'{recipe}.yml'
        if not path.is_file():
            raise ValueError(f'Unknown recipe: {recipe}')
        document = configuration.read_yaml(path)
        configuration.only_keys(document, ('schema', 'include', 'stages'), str(path))
        includes = document.get('include', [])
        own_stages = document.get('stages', [])
        if not isinstance(includes, list) or not isinstance(own_stages, list):
            raise ValueError(f'{path}: include and stages must be lists')
        if not includes and not own_stages:
            raise ValueError(f'{path}: recipe must include a recipe or define a stage')
        for entry in includes:
            include(entry, (*parents, recipe))
        for stage in own_stages:
            configuration.only_keys(stage, ('config', 'heading'), str(path))
            config, heading = stage.get('config'), stage.get('heading')
            if not isinstance(config, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+\.yml', config):
                raise ValueError(f'{path}: config must be an experiment YAML filename')
            if not (experiments / config).is_file():
                raise ValueError(f'{path}: experiment does not exist: {config}')
            if not isinstance(heading, str) or not heading.strip() or any(c in heading for c in '\t\r\n'):
                raise ValueError(f'{path}: heading must be one nonempty line')
            stages.append((config, heading))
        resolved.add(recipe)

    include(name, ())
    return stages


if __name__ == '__main__':
    try:
        for config, heading in resolve(sys.argv[1]):
            print(f'{config}\t{heading}')
    except (OSError, ValueError) as error:
        sys.exit(f'bench: {error}')
