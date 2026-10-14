"""Collapse repeated samples and add exact transfer component accounting."""

import json
import statistics
from pathlib import Path

import pyarrow.parquet as pq

p = Path('benchmarks/overview-alternatives/results.json')
r = json.loads(p.read_text())
scenarios = {}
for sample in r.pop('samples'):
    key = tuple(sample[k] for k in ['lod', 'place', 'styled', 'variant', 'transport'])
    scenarios.setdefault(key, []).append(sample)
r['scenarios'] = []
for (lod, _place, _styled, variant, _transport), samples in scenarios.items():
    first = samples[0]
    metadata = pq.read_metadata(Path(r['config']['source']) / f'{lod}.{variant}.parquet')
    components = {
        'geometry': 0,
        'source_scalars': 0,
        'footer': r['sizes'][f'{lod}.{variant}']['footer_bytes'],
        'directory': r['sizes'][f'{lod}.{variant}']['directory_bytes'],
    }
    # Only selected chunks were fetched; merged ranges contain no unrequested gaps.
    intervals = first['cold']['ranges'][2:]
    for rg in range(metadata.num_row_groups):
        for ci in range(metadata.num_columns):
            chunk = metadata.row_group(rg).column(ci)
            start, size = chunk.data_page_offset, chunk.total_compressed_size
            if any(
                offset <= start and start + size <= offset + length for offset, length in intervals
            ):
                kind = (
                    'geometry' if chunk.path_in_schema.startswith('overview') else 'source_scalars'
                )
                components[kind] += size
    assert sum(components.values()) == first['cold']['bytes']
    summary = {
        k: first[k]
        for k in [
            'lod',
            'place',
            'styled',
            'variant',
            'transport',
            'features',
            'selected_groups',
            'viewport',
            'boundary_crossers',
        ]
    }
    summary['cold'] = {
        **first['cold'],
        'components': components,
        'trace_note': 'ranges are Parquet IO; one extra sidecar-directory read is counted separately',
    }
    summary['runs'] = len(samples)
    for timer in ['ms', 'warm_ms']:
        if timer in first:
            summary[f'median_{timer}'] = statistics.median(s[timer] for s in samples)
    if 'warm' in first:
        summary['warm'] = first['warm']
    else:
        summary['measurement'] = first['measurement']
    r['scenarios'].append(summary)
r['config']['timing_scope'] = (
    'Payload fetch/decode/filter only; footer and directory setup excluded from timer, included in byte/request totals'
)
r['config']['warm_cache'] = (
    'Unbounded compressed-range cache in a small local fixture; no decoded cache'
)
r['config']['nested_runs'] = (
    'One deterministic compressed-range trace per scenario, no decode timer'
)
p.write_text(json.dumps(r, separators=(',', ':')) + '\n')
print(f'{len(r["scenarios"])} scenarios; {p.stat().st_size:,} bytes')
