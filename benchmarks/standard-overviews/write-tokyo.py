"""Build the Tokyo files the coarse-prefix and standard-overview measurements read.

Cuts the Tokyo subset (4,881,955 buildings whose bbox meets 139.39–140.10 E,
35.45–35.90 N) from the hosted Japan GeoParquet LOD file, then writes it with
the Japan recipe's calibrated budgets in one Hilbert order:

    uv run python benchmarks/standard-overviews/write-tokyo.py tokyo.source.parquet tokyo.packed.parquet

`--prefix-zoom 10` stores the rows shown through z10 in a leading row group,
the coarse-first layout QGIS draws first. That option exists only in the writer
at commit 0a06e75^, so run it from a checkout of that commit, passing the
source this script cut:

    git worktree add ../lod-prefix 0a06e75^
    uv run --project ../lod-prefix python benchmarks/standard-overviews/write-tokyo.py \\
      tokyo.source.parquet tokyo.prefix.parquet --prefix-zoom 10

An existing source file is reused. A rebuild matches the recorded request
counts and comes within about 1% of the recorded bytes and feature counts; the
measured files were cut in the source's original row order. measure-packed.mjs
reads either file with tiles.json.
"""

import argparse
import importlib.util
import time
from pathlib import Path

import duckdb
from geoparquet_lod import chain, drop_smallest, tiny_polygons
from geoparquet_lod._layout import finalize_layout
from geoparquet_lod.writer import _write_unindexed

JAPAN = 'https://pub-a9c45d804ff74bd19e8f9b7a52d9a90b.r2.dev/geoparquet-lod/releases/0fcab25/japan_buildings.parquet'
BOX = (139.39, 35.45, 140.10, 35.90)


def cut(out: Path):
    con = duckdb.connect()
    con.execute('INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;')
    xmin, ymin, xmax, ymax = BOX
    con.execute(
        f"""
        COPY (
            SELECT * EXCLUDE (bbox, hilbert, lod) FROM read_parquet('{JAPAN}')
            WHERE bbox.xmax >= {xmin} AND bbox.xmin <= {xmax}
              AND bbox.ymax >= {ymin} AND bbox.ymin <= {ymax}
              AND ST_XMax(geometry) >= {xmin} AND ST_XMin(geometry) <= {xmax}
              AND ST_YMax(geometry) >= {ymin} AND ST_YMin(geometry) <= {ymax}
        ) TO '{out}' (FORMAT parquet, COMPRESSION zstd)
        """
    )


def budgets():
    recipe = Path(__file__).parents[1] / 'write-japan-tippecanoe.py'
    spec = importlib.util.spec_from_file_location('recipe', recipe)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calibrated = module.calibrated_budgets()
    return lambda z: calibrated.get(z, 1_300_000 if z <= 8 else 850_000)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('out')
    parser.add_argument('--prefix-zoom', type=int)
    args = parser.parse_args()
    t0 = time.time()

    def log(message):
        print(f'[{time.time() - t0:6.0f}s] {message}', flush=True)

    source, out = Path(args.source), Path(args.out)
    if not source.exists():
        log(f'cutting {source}')
        cut(source)
    options = {} if args.prefix_zoom is None else {'prefix_zoom': args.prefix_zoom}
    staged = out.with_suffix('.unindexed.parquet')
    _write_unindexed(
        str(source),
        staged,
        thinning=chain(drop_smallest(budgets()), tiny_polygons()),
        carry=['height', 'class'],
        zooms=list(range(8, 17)),
        complete_from=14,
        page_bytes=16384,
        page_rows=2048,
        workers=8,
        log=log,
        **options,
    )
    finalize_layout(staged, out, page_bytes=65536, log=log)
    staged.unlink()
