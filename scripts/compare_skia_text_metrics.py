"""Compare P05 canonical-font samples; identities/clusters exact, geometry in pixels."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path

POSITION_TOLERANCE = 1e-4
DISCRETE = {'id', 'cluster_start', 'cluster_end', 'selected_font', 'run_index'}


def compare(reference, candidate, path='$', field='') -> float:
    if isinstance(reference, dict):
        if not isinstance(candidate, dict) or reference.keys() != candidate.keys():
            raise ValueError(f'{path}: fields differ')
        return max((compare(v, candidate[k], f'{path}.{k}', k) for k, v in reference.items()), default=0)
    if isinstance(reference, list):
        if not isinstance(candidate, list) or len(reference) != len(candidate):
            raise ValueError(f'{path}: list lengths differ')
        return max((compare(a, b, f'{path}[{i}]', field) for i, (a, b) in enumerate(zip(reference, candidate))), default=0)
    if type(reference) in (float, int) and type(candidate) in (float, int):
        if not math.isfinite(reference) or not math.isfinite(candidate):
            raise ValueError(f'{path}: non-finite value')
        error = abs(reference - candidate)
        if error > (0 if field in DISCRETE else POSITION_TOLERANCE):
            raise ValueError(f'{path}: {reference} != {candidate}')
        return error
    if type(reference) is not type(candidate) or reference != candidate:
        raise ValueError(f'{path}: identity/value differs')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('samples', type=Path, nargs='+')
    args = parser.parse_args()
    if len(args.samples) < 2: parser.error('Provide at least two platform samples')
    data = [json.loads(p.read_text(encoding='utf-8')) for p in args.samples]
    maximum = max(compare(data[0], sample) for sample in data[1:])
    print(f'Identities, glyphs, clusters and metrics match; max geometry error={maximum:g}px')


if __name__ == '__main__':
    main()
