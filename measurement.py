"""Paired wall-time statistics for guest executables."""

import statistics


def summarize(rows, names, labels):
    summaries = []
    for name in names:
        measured = [r for r in rows if r['case'] == name and r['phase'] == 'measured']
        reference = {r['pair']: r['wall_seconds'] for r in measured if r['variant'] == labels[0]}
        if not reference:
            continue
        base = statistics.median(reference.values())
        for label in labels:
            selected = [r for r in measured if r['variant'] == label]
            if not selected:
                continue
            values = [r['wall_seconds'] for r in selected]
            median = statistics.median(values)
            ratio = median / base
            pairs = [r['wall_seconds'] / reference[r['pair']]
                     for r in selected if r['pair'] in reference]
            summaries.append(dict(case=name, variant=label, runs=len(values), median_seconds=median,
                                  min_seconds=min(values), max_seconds=max(values),
                                  stddev_seconds=statistics.stdev(values) if len(values) > 1 else None,
                                  wall_change_seconds=median - base,
                                  wall_change_percent=100 * (ratio - 1), wall_ratio=ratio,
                                  paired_wall_ratios=pairs,
                                  faster_pairs=sum(value < 1 for value in pairs)))
    return summaries
