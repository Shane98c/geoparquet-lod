"""The public build publishes only complete indexed files."""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from click.testing import CliRunner
from geoparquet_lod import _layout, write, writer
from geoparquet_lod.cli import cli


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
