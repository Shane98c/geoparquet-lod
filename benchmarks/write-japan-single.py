"""Rebuild the parity fixture with identical thinning and one Hilbert sequence."""

import sys
import time

from geoparquet_lod import chain, drop_smallest, tiny_polygons
from geoparquet_lod._layout import finalize_layout
from geoparquet_lod.writer import _write_unindexed as write

if __name__ == '__main__':
    src, out, optimized = sys.argv[1:4]
    started = time.monotonic()

    def log(message):
        print(f'[{time.monotonic() - started:7.1f}s] {message}', flush=True)

    write(
        src,
        out,
        thinning=chain(drop_smallest(lambda z: 1_300_000 if z <= 8 else 850_000), tiny_polygons()),
        carry=['height', 'class'],
        workers=12,
        prefix_zoom=None,
        complete_from=14,
        page_bytes=16384,
        page_rows=2048,
        memory_limit='40GB',
        log=log,
    )
    finalize_layout(out, optimized, log=log)
