"""The public build publishes only complete indexed files."""

import json
import multiprocessing
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from click.testing import CliRunner
from geoparquet_lod import _layout, write, writer
from geoparquet_lod.cli import cli
from geoparquet_lod.validate import validate


def test_cli_build(tmp_path):
    source, output = tmp_path / 'source.parquet', tmp_path / 'output.parquet'
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Point']}},
    }
    table = pa.table(
        {
            'id': [1, 2],
            'geometry': shapely.to_wkb(shapely.points([-105, -104.99], [39.7, 39.71])),
        }
    ).replace_schema_metadata({b'geo': json.dumps(geo).encode()})
    pq.write_table(table, source)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'write',
            str(source),
            str(output),
            '--zooms',
            '10-12',
            '--complete-from',
            '12',
            '--workers',
            '1',
            '--lod-page-bytes',
            '32768',
        ],
    )
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert output.read_bytes()[4:12] == b'LODIDX01'
    assert pq.read_table(output, columns=['id', 'geometry']).sort_by('id').equals(table)
    assert set(tmp_path.iterdir()) == {source, output}
    assert set(cli.commands) == {'write', 'inspect', 'validate'}


@pytest.mark.parametrize('declared', ['1.0.0', '1.1.0', '2.0.0'])
def test_output_declares_geoparquet_1_1(tmp_path, declared):
    """The output is GeoParquet 1.1, whose bbox covering 2.0 drops, whatever
    version the source declares."""
    source, output = tmp_path / 'source.parquet', tmp_path / 'output.parquet'
    geo = {
        'version': declared,
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Point']}},
    }
    table = pa.table(
        {'id': [1, 2], 'geometry': shapely.to_wkb(shapely.points([-105, -104.99], [39.7, 39.71]))}
    ).replace_schema_metadata({b'geo': json.dumps(geo).encode()})
    pq.write_table(table, source)
    write(str(source), str(output), zooms=[10, 11, 12], complete_from=12, workers=1)
    assert json.loads(pq.read_schema(output).metadata[b'geo'])['version'] == '1.1.0'
    validate(output)


@pytest.mark.parametrize('phase', ['overview', 'layout'])
def test_failed_build_preserves_output_and_cleans_staging(tmp_path, monkeypatch, phase):
    output = tmp_path / 'output.parquet'
    output.write_bytes(b'existing output')

    def staging(src, path, **kwargs):
        Path(path).write_bytes(b'intermediate')
        Path(path).with_suffix('.sorted.parquet').write_bytes(b'scratch')
        if phase == 'overview':
            raise RuntimeError('injected build failure')

    def layout(*args, **kwargs):
        raise RuntimeError('injected build failure')

    monkeypatch.setattr(writer, '_write_unindexed', staging)
    monkeypatch.setattr(_layout, 'finalize_layout', layout)
    with pytest.raises(RuntimeError, match='injected build failure'):
        write('unused-source', output, log=lambda *_: None)
    assert output.read_bytes() == b'existing output'
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize('failure', ['packing', 'interrupt'])
def test_a_build_failing_midway_leaves_no_workers_or_files(tmp_path, monkeypatch, failure):
    """A worker refusing a value, or an interrupt once the workers have
    rendered the straddlers, ends the build with its workers gone, its staging
    removed and the existing output kept."""
    rng = np.random.default_rng(3)
    x, y = -105 + rng.random(400) * 0.2, 39.6 + rng.random(400) * 0.2
    geo = {
        'version': '1.1.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
    }
    big = [2**63 if failure == 'packing' and i == 200 else i for i in range(400)]
    table = pa.table(
        {
            'big': pa.array(big, pa.uint64()),
            'geometry': shapely.to_wkb(shapely.box(x, y, x + 0.01, y + 0.01)),
        }
    )
    source, output = tmp_path / 'source.parquet', tmp_path / 'output.parquet'
    pq.write_table(table.replace_schema_metadata({b'geo': json.dumps(geo).encode()}), source)
    output.write_bytes(b'existing output')
    before = set(multiprocessing.active_children())
    workers = set()
    spill_for_batch = writer._spill_for_batch

    def watched(*args):
        workers.update(set(multiprocessing.active_children()) - before)
        if failure == 'interrupt':
            raise KeyboardInterrupt
        return spill_for_batch(*args)

    monkeypatch.setattr(writer, '_spill_for_batch', watched)
    error = ValueError if failure == 'packing' else KeyboardInterrupt
    with pytest.raises(error):
        write(
            source,
            output,
            zooms=[12, 13],
            complete_from=12,
            carry=['big'],
            workers=2,
            log=lambda *_: None,
        )
    assert workers, 'the workers rendered the straddlers before the failure'
    assert not any(p.is_alive() for p in workers)
    assert output.read_bytes() == b'existing output'
    assert sorted(tmp_path.iterdir()) == [output, source]


def test_source_column_names_reach_the_overviews_as_written(tmp_path):
    """Carried, thinning and geometry columns named like SQL keywords or with
    dots and quotes build, validate and carry their values."""
    from geoparquet_lod import read_overview, validate

    geometry = 'shape "x"'
    geo = {
        'version': '1.1.0',
        'primary_column': geometry,
        'columns': {geometry: {'encoding': 'WKB', 'geometry_types': ['Point']}},
    }
    n = 40
    lon = -105 + np.arange(n) * 1e-3
    table = pa.table(
        {
            'group': pa.array(np.arange(n) % 3, pa.int32()),
            'odd.name': pa.array([f'v{i % 4}' for i in range(n)]),
            'order': pa.array(np.arange(n), pa.int64()),
            geometry: shapely.to_wkb(shapely.points(lon, np.full(n, 39.7))),
        }
    ).replace_schema_metadata({b'geo': json.dumps(geo).encode()})
    source, output = tmp_path / 'source.parquet', tmp_path / 'output.parquet'
    pq.write_table(table, source)

    def thin(rows, zoom, ctx):
        return rows.filter(pa.array(rows.column('order').to_numpy() % 2 == 0))

    write(
        source,
        output,
        thinning=thin,
        columns=['order'],
        carry=['group', 'odd.name'],
        geometry=geometry,
        zooms=[10, 11],
        complete_from=11,
        workers=1,
        log=lambda *_: None,
    )
    validate(output, full=True)
    shown = read_overview(output, 10).sort_by('row')
    assert shown.num_rows == n // 2
    source_rows = pq.read_table(output, columns=['group', 'odd.name', 'order'])
    picked = source_rows.take(shown.column('row'))
    assert shown.column('group').to_pylist() == picked.column('group').to_pylist()
    assert shown.column('odd.name').to_pylist() == picked.column('odd.name').to_pylist()
    assert all(v % 2 == 0 for v in picked.column('order').to_pylist())
