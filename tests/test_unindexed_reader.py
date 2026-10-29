"""Readers find every tile's copies: the footer reader of a file without the
spatial index, and the spatial reader once the file is laid out."""

import json
import math
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from geoparquet_lod import validate
from geoparquet_lod._layout import finalize_layout
from geoparquet_lod.writer import _write_unindexed as write
from geoparquet_lod.writer import hilbert_key
from unpacker import Unpacker

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('nulls', [10, 300])
def test_copies_past_every_key_reach_their_tile(tmp_path, nulls):
    """A tile later on the curve than every key has its copies on the last
    keyed row, ahead of the rows with null or empty geometry that sort last,
    whether or not those fill the final row groups. The spatial reader takes
    them from the last page of the LOD's directory. Those rows have a null
    covering, so the pages they fill keep their column index and no bbox
    query reads them."""
    z, n = 14, 2**14
    tx = int((-104.99 + 180) / 360 * n)
    ty = int((1 - math.asinh(math.tan(math.radians(39.7))) / math.pi) / 2 * n)

    def h(dx, dy):
        return int(hilbert_key(np.array([tx + dx]), np.array([ty + dy]), z)[0])

    steps = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    dx, dy = next(s for s in steps if h(*s) > h(0, 0))
    ex, ey = next(s for s in steps if h(*s) < h(0, 0))

    def box(x, y, size):
        west, east = (v / n * 360 - 180 for v in (x, x + size))
        north, south = (
            math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * v / n)))) for v in (y, y + size)
        )
        return shapely.box(west, south, east, north)

    # small features inside a tile earlier on the curve, then the feature with
    # the largest key, reaching from its tile into one later on the curve
    corners = 0.2 + np.random.default_rng(1).random((600, 2)) * 0.6
    small = [box(tx + ex + a, ty + ey + b, 0.01) for a, b in corners]
    big = box(tx + 0.3 + dx * 0.4, ty + 0.3 + dy * 0.4, 0.4)
    geometry = np.array([*small, big, *[None, shapely.Polygon()] * (nulls // 2)], dtype=object)
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    table = pa.table(
        {
            'geometry': shapely.to_wkb(geometry),
            'height': pa.array([i % 50 for i in range(len(geometry))], pa.int32()),
        }
    ).replace_schema_metadata({b'geo': json.dumps(geo).encode()})
    src, out = tmp_path / 'source.parquet', tmp_path / 'edge.parquet'
    pq.write_table(table, src)
    write(
        src,
        out,
        zooms=range(12, 15),
        complete_from=12,
        carry=['height'],
        row_group_rows=256,
        page_rows=32,
        batch_rows=32,
        workers=1,
        log=lambda *_: None,
    )
    t = pq.read_table(out)
    meta = json.loads(t.schema.metadata[b'geo'])['lod_columns']
    last_key = np.nanmax(t.column('hilbert').to_numpy(zero_copy_only=False).astype(float))
    fields = [pa.field(f['name'], pa.type_for_alias(f['type'])) for f in meta['attributes']]
    cases = []
    for name, info in meta['lods'].items():
        for blob in t.column('lod').combine_chunks().field(name).to_pylist():
            for copy in (
                Unpacker(blob, info['geometry_type'], fields).row()['spill'] if blob else []
            ):
                zoom = copy['tile'] >> 48
                x, y = divmod(copy['tile'] - (zoom << 48), 2**zoom)
                start = hilbert_key(np.array([x]), np.array([y]), zoom)[0]
                if start * 4 ** (meta['key']['depth'] - zoom) > last_key:
                    cases.append(
                        {'z': zoom, 'x': x, 'y': y, 'id': copy['id'], 'attribute': 'height'}
                    )
    assert cases, 'the fixture puts copies on tiles past every key'
    (tmp_path / 'cases.json').write_text(json.dumps(cases))
    assert t.column('bbox').null_count == nulls
    validate(out, full=True)
    laid_out = finalize_layout(out, tmp_path / 'laid-out.parquet', log=lambda *_: None)
    validate(laid_out, full=True)
    result = subprocess.run(
        [
            'node',
            str(ROOT / 'js/test/spill.test.mjs'),
            str(out),
            str(tmp_path / 'cases.json'),
            str(laid_out),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
