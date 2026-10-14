"""Audit the full Japan browser route and preserve its measured evidence."""

import argparse
import json
from pathlib import Path

import pyarrow.parquet as pq

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--compact', action='store_true')
parser.add_argument('--directory', type=Path)
parser.add_argument('--output', type=Path)
args = parser.parse_args()
scratch = args.directory or Path(
    '/private/tmp/overview-japan-compact' if args.compact else '/private/tmp/overview-japan'
)
result = json.loads((scratch / 'browser-results.json').read_text())
assert result['layout'] == ('japan-compact' if args.compact else 'japan-sharded')
assert result['viewport'] == [765, 540]
assert not result['errors'], result['errors']
assert len(result['samples']) == 84
result['worker_configuration'] = {
    'production': 'default two CPU workers plus coordinator',
    'nested': 'one reader/decoder worker',
}
reader_optimized = any(s.get('compactReader') for s in result['samples'])
result['cache'] = {
    'production': 'actual production defaults; visible tile cache 32 MiB / 64 entries, plus reader caches',
    'nested': (
        '32 MiB compressed ranges; 32 MiB estimated geometry + 8 MiB leaves; in-flight work separate; 8 active fetches'
        if reader_optimized
        else '32 MiB compressed ranges; 512 decoded entries (not a heap byte bound); 8 active fetches'
    ),
}
result['limits'] = [
    'Five stored overview levels only: z4/8/11/14/16; other zooms are not matched.',
    '50 ms service delay per request, no bandwidth cap; local HTTP connection scheduling applies.',
    'Settled navigation, not continuous/cancelled gestures.',
    'Production decode and MVT CPU timers unavailable; null does not mean zero work.',
    'Nested directory loads on demand; cache accounting is not a measured browser heap bound.',
]
if args.compact:
    result['limits'].append(
        'Compact file is a companion overview table, not a single-file replacement for source GeoParquet.'
    )
result['files'] = {}
for variant, path in [
    ('production', Path('/private/tmp/japan.tippecanoe-density.spatial.parquet')),
    ('nested', scratch / f'japan.{"compact" if args.compact else "nested"}.parquet'),
]:
    metadata = pq.read_metadata(path)
    result['files'][variant] = {
        'bytes': path.stat().st_size,
        'rows': metadata.num_rows,
        'row_groups': metadata.num_row_groups,
    }
    if variant != 'nested':
        continue
    forbidden = []
    for group in range(metadata.num_row_groups):
        rg = metadata.row_group(group)
        for ci in range(rg.num_columns):
            column = rg.column(ci)
            top = column.path_in_schema.split('.')[0]
            allowed = (
                ['height', 'class', 'source_row', 'geometry', 'level']
                if args.compact
                else ['height', 'class', 'f4', 'f8', 'f11', 'f14', 'f16']
            )
            if top in allowed:
                continue
            start = column.dictionary_page_offset or column.data_page_offset
            forbidden.append((start, start + column.total_compressed_size))
    for sample in result['samples']:
        if sample['variant'] != 'nested' or 'ranges' not in sample:
            continue
        assert len(sample['ranges']) == sample['requests']
        assert sum(size for _, size in sample['ranges']) == sample['bytes']
        for start, size in sample['ranges']:
            assert not any(start < end and start + size > begin for begin, end in forbidden), (
                sample['phase'],
                start,
                size,
            )
    if args.compact:
        result['compact_requests_only_overview_companion'] = True
    else:
        result['nested_ranges_exclude_other_source_columns'] = True

membership = []
for phase in dict.fromkeys(s['phase'] for s in result['samples']):
    samples = {s['variant']: s for s in result['samples'] if s['phase'] == phase and s['run'] == 0}
    a, b = (set(samples[v]['visibleIds']) for v in ['production', 'nested'])
    membership.append(
        {
            'phase': phase,
            'production': len(a),
            'nested': len(b),
            'only_production': sorted(a - b),
            'only_nested': sorted(b - a),
        }
    )
result['visible_membership'] = membership
result['all_visible_memberships_equal'] = all(
    not m['only_production'] and not m['only_nested'] for m in membership
)
result['route_median_sums_excluding_independent_cold_z4'] = {
    variant: {
        metric: sum(
            s[metric]
            for s in result['summary']
            if s['variant'] == variant and not s['phase'].startswith('cold Japan')
        )
        for metric in ['bytes', 'requests', 'visibleReadyMs']
    }
    for variant in ['production', 'nested']
}
out = args.output or Path(
    f'benchmarks/overview-alternatives/japan-{"compact-" if args.compact else ""}browser-results.json'
)
out.write_text(json.dumps(result, separators=(',', ':')) + '\n')
print(
    json.dumps(
        {
            'visible_memberships_equal': result['all_visible_memberships_equal'],
            'route': result['route_median_sums_excluding_independent_cold_z4'],
            'files': result['files'],
        },
        indent=2,
    )
)
assert result['all_visible_memberships_equal'], (
    'Visible content differs; inspect evidence before comparing speed'
)
