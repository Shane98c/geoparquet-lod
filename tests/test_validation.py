"""Exhaustive validation and logical source projection against independent Arrow values."""

import gzip
import json
import struct
import subprocess
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from click.testing import CliRunner
from geoparquet_lod import validate, write
from geoparquet_lod.cli import cli
from geoparquet_lod.validate import ValidationError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def indexed(tmp_path_factory):
    work = tmp_path_factory.mktemp('validation')
    src, out = work / 'source.parquet', work / 'indexed.parquet'
    n = 240
    table = pa.table(
        {
            'id': range(n),
            'odd.name': range(n),
            'bracket[0]': range(n),
            'details': pa.array(
                [
                    None if i % 5 == 0 else {'height': None if i % 7 == 0 else i, 'list': i + 1}
                    for i in range(n)
                ],
                type=pa.struct([('height', pa.int32()), ('list', pa.int32())]),
            ),
            'floors': pa.array(
                [
                    None
                    if i % 5 == 0
                    else []
                    if i % 7 == 0
                    else [None, {'name': f'a{i}'}, {'name': None}]
                    for i in range(n)
                ],
                type=pa.list_(pa.struct([('name', pa.string())])),
            ),
            'nums': pa.array(
                [None if i % 5 == 0 else [] if i % 7 == 0 else [i, None, i + 1] for i in range(n)],
                type=pa.list_(pa.int32()),
            ),
            'tags': pa.array(
                [
                    None
                    if i % 5 == 0
                    else []
                    if i % 7 == 0
                    else [('name', f't{i}'), ('nullable', None)]
                    for i in range(n)
                ],
                type=pa.map_(pa.string(), pa.string()),
            ),
            'geometry': shapely.to_wkb(
                [
                    shapely.box(
                        -105 + i % 20 * 0.06,
                        39.7 + i // 20 * 0.06,
                        -104.92 + i % 20 * 0.06,
                        39.78 + i // 20 * 0.06,
                    )
                    for i in range(n)
                ]
            ),
        }
    ).replace_schema_metadata(
        {
            b'geo': json.dumps(
                {
                    'version': '1.1.0',
                    'primary_column': 'geometry',
                    'columns': {'geometry': {'encoding': 'WKB', 'geometry_types': ['Polygon']}},
                }
            ).encode()
        }
    )
    pq.write_table(table, src)
    write(
        src,
        out,
        zooms=[6, 8],
        complete_from=8,
        row_group_rows=64,
        page_rows=16,
        workers=1,
        log=lambda *_: None,
    )
    return out


def test_logical_projection(indexed):
    expected = []
    for row in pq.read_table(indexed).to_pylist():
        details, floors, nums, tags = [row[n] for n in ('details', 'floors', 'nums', 'tags')]
        tags = dict(tags) if tags is not None else None
        expected.append(
            {
                'id': row['id'],
                'odd.name': row['odd.name'],
                'bracket[0]': row['bracket[0]'],
                'details': details,
                'details.height': details['height'] if details else None,
                'details.list': details['list'] if details else None,
                'floors': floors,
                'floors[1].name': floors[1]['name'] if floors and floors[1] else None,
                'floors[9].name': None,
                'nums': nums,
                'nums[2]': nums[2] if nums else None,
                'tags': tags,
                'tags.name': tags.get('name') if tags else None,
                'tags.missing': None,
            }
        )
    path = indexed.parent / 'expected.json'
    path.write_text(json.dumps(expected))
    subprocess.run(
        ['node', str(ROOT / 'js/test/projection.test.mjs'), str(indexed), str(path)], check=True
    )


def test_full_and_cli(indexed):
    quick, full = validate(indexed, sample_rows=3), validate(indexed, full=True)
    assert quick['mode'] == 'quick' and full['mode'] == 'full'
    assert sum(full['checked_non_null'].values()) > sum(quick['checked_non_null'].values())
    result = CliRunner().invoke(cli, ['validate', str(indexed), '--full'])
    assert result.exit_code == 0, result.output
    assert '"mode": "full"' in result.output
    with pytest.raises(ValueError):
        validate(indexed, sample_rows=-1)


def _root(path):
    raw = path.read_bytes()
    length = int.from_bytes(raw[12:16], 'little')
    return raw, json.loads(gzip.decompress(raw[16 : 16 + length]))


def _write_root(path, raw, root):
    encoded = gzip.compress(json.dumps(root, separators=(',', ':')).encode(), mtime=0)
    assert len(encoded) <= 16368
    path.write_bytes(
        raw[:12]
        + struct.pack('<I', len(encoded))
        + encoded
        + bytes(16384 - 16 - len(encoded))
        + raw[16384:]
    )


@pytest.mark.parametrize(
    'mutation', ['root_bounds', 'leaf_rows', 'leaf_fences', 'leaf_offset', 'codec', 'primary']
)
def test_spatial_index_corruption(indexed, tmp_path, mutation):
    raw, root = _root(indexed)
    bad = tmp_path / 'bad.parquet'
    if mutation in ('codec', 'primary'):
        if mutation == 'codec':
            root['codecs'][next(iter(root['codecs']))] = 'SNAPPY'
        else:
            root['primary'] = 'wrong_geometry'
    elif mutation == 'root_bounds':
        stream = next(s for sections in root['streams'].values() for s in sections if s[2])
        stream[2][0] += 1000000
    else:
        name = next(iter(root['streams']))
        # Replace delta directory with equivalent explicit references, then append
        # one mutated leaf before the unchanged footer (all page offsets stay valid).
        converted = {}
        for name_, sections in root['streams'].items():
            span = 4 ** max(
                0, root['lod']['key']['depth'] - max(root['lod']['lods'][name_]['zooms'])
            )
            converted[name_] = []
            for offset, depth, triples in sections:
                refs, previous = [], 0
                for i in range(0, len(triples), 3):
                    low = previous + triples[i]
                    refs.append(
                        [
                            low * span,
                            (low + triples[i + 1] + 1) * span - 1,
                            offset,
                            triples[i + 2],
                            depth,
                        ]
                    )
                    previous, offset = low, offset + triples[i + 2]
                converted[name_].append(refs)
        ref = next(ref for section in converted[name] for ref in section)
        assert ref[4] == 0
        entries = json.loads(gzip.decompress(raw[ref[2] : ref[2] + ref[3]]))
        if mutation == 'leaf_rows':
            entries[0][5] += 1
        elif mutation == 'leaf_fences':
            entries[0][6][1] += 1
        else:
            entries[0][2] += 1
        encoded = gzip.compress(json.dumps(entries).encode(), mtime=0)
        footer = len(raw) - 8 - int.from_bytes(raw[-8:-4], 'little')
        ref[2], ref[3] = footer, len(encoded)
        raw = raw[:footer] + encoded + raw[footer:]
        root['file_bytes'] = len(raw)
        root.pop('directory_encoding')
        root['streams'] = converted
    _write_root(bad, raw, root)
    validate(bad, sample_rows=3)  # quick mode intentionally does not traverse directories
    with pytest.raises(ValidationError, match='spatial index'):
        validate(bad, full=True)


def test_corrupt_packed_row_beyond_sample(indexed, tmp_path):
    table = pq.read_table(indexed)
    lod = table['lod'].combine_chunks()
    name = lod.type[0].name
    children = [lod.field(i).to_pylist() for i in range(lod.type.num_fields)]
    target = next(i for i in range(len(table) - 1, 64, -1) if children[0][i] is not None)
    children[0][target] += b'\x00'
    replacement = pa.StructArray.from_arrays(
        [pa.array(values, type=pa.binary()) for values in children], fields=list(lod.type)
    )
    table = table.set_column(
        table.schema.get_field_index('lod'), table.schema.field('lod'), replacement
    )
    metadata = dict(table.schema.metadata)
    metadata.pop(b'lod_spatial_index', None)
    table = table.replace_schema_metadata(metadata)
    bad = tmp_path / 'late.parquet'
    pq.write_table(table, bad, row_group_size=64, write_page_index=True)
    validate(bad, sample_rows=3)
    with pytest.raises(ValidationError, match='trailing bytes'):
        validate(bad, full=True)
    assert name in validate(indexed, full=True)['lods']


@pytest.mark.parametrize('mutation', ['tile', 'id'])
def test_invalid_spill_identifiers(indexed, tmp_path, mutation):
    from geoparquet_lod.pack import Unpacker

    table = pq.read_table(indexed)
    lod = table['lod'].combine_chunks()
    geo = json.loads(table.schema.metadata[b'geo'])
    children = [lod.field(i).to_pylist() for i in range(lod.type.num_fields)]
    found = False
    for child_index, field in enumerate(lod.type):
        family = geo['lod_columns']['lods'][field.name]['geometry_type']
        for index, blob in enumerate(children[child_index]):
            if blob is None or index < 64:
                continue
            u = Unpacker(blob, family, [], {})
            u.geometry()
            u.attrs()
            if not u.varint():
                continue
            start = u.pos
            u.varint()  # tile
            if mutation == 'id':
                start = u.pos
                u.varint()
            end = u.pos
            value = (99 << 48) if mutation == 'tile' else len(table) + 1
            encoded = bytearray()
            while value >= 128:
                encoded.append((value & 127) | 128)
                value >>= 7
            encoded.append(value)
            children[child_index][index] = blob[:start] + encoded + blob[end:]
            found = True
            break
        if found:
            break
    assert found, 'fixture exercises spill copies outside the quick sample'
    replacement = pa.StructArray.from_arrays(
        [pa.array(values, type=pa.binary()) for values in children], fields=list(lod.type)
    )
    table = table.set_column(
        table.schema.get_field_index('lod'), table.schema.field('lod'), replacement
    )
    metadata = dict(table.schema.metadata)
    metadata.pop(b'lod_spatial_index', None)
    table = table.replace_schema_metadata(metadata)
    bad = tmp_path / f'spill-{mutation}.parquet'
    pq.write_table(table, bad, row_group_size=64, write_page_index=True)
    validate(bad, sample_rows=3)
    with pytest.raises(ValidationError, match='spill'):
        validate(bad, full=True)
