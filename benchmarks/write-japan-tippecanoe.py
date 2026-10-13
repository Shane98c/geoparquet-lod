"""Test closer PMTiles display density while retaining all Japan analysis rows.

Budgets are scaled from the matched XYZ tile feature counts in dropping/before.json.
Only z8, z11 and z12 change; the preserved demo and library defaults do not.
"""

import json
import sys
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
    src, out, optimized = sys.argv[1:4]
    started = time.monotonic()
    budgets = calibrated_budgets()

    def log(message):
        print(f'[{time.monotonic() - started:7.1f}s] {message}', flush=True)

    def budget(zoom):
        return budgets.get(zoom, 1_300_000 if zoom <= 8 else 850_000)

    log(f'Experimental estimated-byte budgets: {budgets}')
    write(
        src,
        out,
        thinning=chain(drop_smallest(budget), tiny_polygons()),
        carry=['height', 'class'],
        workers=12,
        prefix_zoom=None,
        complete_from=14,
        page_bytes=16384,
        page_rows=2048,
        memory_limit='40GB',
        log=log,
    )
    optimize(out, optimized, page_bytes=65536, log=log)
