"""Save compact browser evidence and audit every captured range against source chunks."""

import argparse
import gzip
import json
from pathlib import Path

import pyarrow.parquet as pq

scratch = Path('/private/tmp/overview-alternatives')
parser = argparse.ArgumentParser()
parser.add_argument('--grouped', action='store_true')
parser.add_argument('--output', type=Path)
args = parser.parse_args()
grouped = args.grouped
suffix = '-grouped' if grouped else ''
r = json.loads((scratch / f'browser-results{suffix}.json').read_text())
r['layout'] = (
    'independent-pages'
    if grouped
    else 'clustered source geometry, scalar attributes, overview corridor'
)
r['transport'] = {
    'gap_bytes': 8192,
    'gap_ratio': 0.25,
    'max_run_bytes': 4 * 1024 * 1024,
    'source_wkb_and_id_gap_reads_forbidden': True,
    'incidental_corridor_prefetch': (
        'disabled' if grouped else 'Same 8KiB/25% bound; selected spatial pages only'
    ),
}
r['timing_limits'] = (
    'decodeMs includes Parquet decompression and geometry/list conversion; '
    'excludes bbox precomputation and filtered coordinate copying. '
    'mvtMs measures toMvt. Visible-ready includes worker open and map painting opportunity.'
)
r['files'] = {}
for variant in ['packed', 'nested']:
    p = scratch / f'browser.{"grouped." if grouped else ""}{variant}.parquet'
    metadata = pq.read_metadata(p)
    with p.open('rb') as f:
        f.seek(4)
        boot = f.read(16384)
        root = json.loads(gzip.decompress(boot[12 : 12 + int.from_bytes(boot[8:12], 'little')]))
    forbidden = []
    components = {}
    for group in range(metadata.num_row_groups):
        rg = metadata.row_group(group)
        for ci in range(rg.num_columns):
            c = rg.column(ci)
            top = c.path_in_schema.split('.')[0]
            components[top] = components.get(top, 0) + c.total_compressed_size
            if top in ['id', 'geometry']:
                forbidden.append([c.data_page_offset, c.total_compressed_size])
    for sample in r['samples']:
        if sample['variant'] != variant or 'ranges' not in sample:
            continue
        assert sum(length for _, length in sample['ranges']) == sample['bytes']
        assert len(sample['ranges']) == sample['requests']
        for offset, length in sample['ranges']:
            assert not any(
                offset < start + size and offset + length > start for start, size in forbidden
            ), (variant, sample['phase'], offset, length)
    r['files'][variant] = {
        'bytes': p.stat().st_size,
        'rows': metadata.num_rows,
        'groups': metadata.num_row_groups,
        'bootstrap_fetch_bytes': 16384,
        'directory_bytes': root['directory'][1],
        'full_footer_bytes_not_fetched': metadata.serialized_size + 8,
        'compressed_components': components,
        'captured_ranges_exclude_primary_wkb_and_ids': True,
    }
for run in range(3):
    for phase in {s['phase'] for s in r['samples']}:
        selected = [s for s in r['samples'] if s['run'] == run and s['phase'] == phase]
        assert len({s['visibleUniqueIds'] for s in selected}) == 1
r['sequence_median_sums'] = {
    variant: {
        name: sum(s[name] for s in r['summary'] if s['variant'] == variant)
        for name in ['bytes', 'requests', 'visibleReadyMs', 'decodeMs', 'mvtMs']
    }
    for variant in ['packed', 'nested']
}
(args.output or Path(f'benchmarks/overview-alternatives/browser-results{suffix}.json')).write_text(
    json.dumps(r, separators=(',', ':')) + '\n'
)
print('54 samples validated; all captured ranges exclude primary WKB/IDs; visible counts match')
