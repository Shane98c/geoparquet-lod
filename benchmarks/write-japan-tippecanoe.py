"""Test closer PMTiles display density while retaining all Japan analysis rows.

Budgets are scaled from the matched XYZ tile feature counts in dropping/before.json.
Only z8, z11 and z12 change; the preserved demo and library defaults do not.
"""

import argparse
import json
import time
from pathlib import Path

from geoparquet_lod import chain, drop_smallest, tiny_polygons, write
from geoparquet_lod.optimize import optimize


def calibrated_budgets():
    profile = json.loads((Path(__file__).parent / 'dropping/before.json').read_text())
    result = {}
    for zoom in (8, 11, 12):
        samples = [row for row in profile if row['z'] == zoom]
        baseline = sum(row['lod']['features'] for row in samples)
        reference = sum(row['pmtiles']['features'] for row in samples)
        prior = 1_300_000 if zoom <= 8 else 850_000
        result[zoom] = round(prior * reference / baseline / 10_000) * 10_000
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('src')
    parser.add_argument('out')
    parser.add_argument('optimized')
    parser.add_argument(
        '--share-detail',
        action='store_true',
        help='Share the finest geometry across all complete zooms (Japan z14–16).',
    )
    args = parser.parse_args()
    started = time.monotonic()
    budgets = calibrated_budgets()

    def log(message):
        print(f'[{time.monotonic() - started:7.1f}s] {message}', flush=True)

    def budget(zoom):
        return budgets.get(zoom, 1_300_000 if zoom <= 8 else 850_000)

    log(f'Experimental estimated-byte budgets: {budgets}')
    log(f'Share complete detail levels: {args.share_detail}')
    write(
        args.src,
        args.out,
        thinning=chain(drop_smallest(budget), tiny_polygons()),
        carry=['height', 'class'],
        workers=12,
        prefix_zoom=None,
        zooms=list(range(17)),
        complete_from=14,
        # Reuse the existing small-dataset sharing mechanism for this ablation.
        # Geometry and boundary copies must be generated for every served zoom;
        # merely relabeling the old f16 column would miss z14/15 boundary copies.
        single_lod_bytes=2**60 if args.share_detail else 16 * 1024 * 1024,
        page_bytes=16384,
        page_rows=2048,
        memory_limit='40GB',
        log=log,
    )
    optimize(args.out, args.optimized, page_bytes=65536, log=log)
