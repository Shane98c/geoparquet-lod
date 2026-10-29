"""Exhaustive validation and logical source projection against independent Arrow values."""

import gzip
import json
import re
import struct
import subprocess
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
import shapely
from click.testing import CliRunner
from geoparquet_lod import _validate_index, validate, write
from geoparquet_lod._compact import dumps, get, loads, set_value
from geoparquet_lod._layout import _node, finalize_layout, read_node, row_metadata
from geoparquet_lod.cli import cli
from geoparquet_lod.validate import BOUNDS, ValidationError

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


@pytest.fixture(scope='module')
def leafy(indexed, tmp_path_factory):
    """`indexed` laid out again with one page per leaf."""
    out = tmp_path_factory.mktemp('leafy') / 'leafy.parquet'
    return finalize_layout(indexed, out, leaf_pages=1, log=lambda *_: None)


class _Appended:
    """Directory nodes added before a file's unchanged footer."""

    def __init__(self, raw):
        self.raw = raw
        self.footer = len(raw) - 8 - int.from_bytes(raw[-8:-4], 'little')
        self.added = bytearray()

    def __call__(self, node):
        """Store `node`, gzip bytes or JSON, and return its offset and length."""
        if not isinstance(node, bytes):
            node = gzip.compress(json.dumps(node).encode(), mtime=0)
        self.added.extend(node)
        return self.footer + len(self.added) - len(node), len(node)

    def write(self, path, root):
        raw = self.raw[: self.footer] + bytes(self.added) + self.raw[self.footer :]
        root['file_bytes'] = len(raw)
        _write_root(path, raw, root)


@pytest.mark.parametrize('arrangement', ['branches', 'same_cell', 'added', 'omitted'])
def test_directory_lists_pages_in_key_order(leafy, tmp_path, arrangement):
    """Leaves copied into another order, under nodes whose bounds match their
    first and last entries, still list every page once, as do leaves with the
    last one repeated or left out. A tile would lose pages, read a cell's
    pages out of row order, or read a page twice."""
    raw, root = _root(leafy)
    name = max(root['directories'], key=lambda n: len(root['directories'][n][1]))
    level, entries = read_node(root['directories'][name])
    low, high = entries['low'], entries['high']
    n = len(low)
    assert level == 1 and n > 4 and (low == high).all() and low[1] == low[2] < low[-1]
    store = _Appended(raw)

    def leaves(order):
        """A level-one node over copies of the leaves in `order`."""
        copies = [
            store(raw[entries['offset'][i] : entries['offset'][i] + entries['length'][i]])
            for i in order
        ]
        return _node(1, low[order], high[order], *zip(*copies, strict=True))

    if arrangement == 'branches':
        parts = [[0, n - 1, 1], list(range(2, n - 1))]
        branches = [store(leaves(part)) for part in parts]
        lows, highs = [low[p[0]] for p in parts], [high[p[-1]] for p in parts]
        root['directories'][name] = _node(2, lows, highs, *zip(*branches, strict=True))
        message = 'out of key order'
    elif arrangement == 'same_cell':
        root['directories'][name] = leaves([0, 2, 1, *range(3, n)])
        message = 'reorders'
    elif arrangement == 'added':
        # every row group matches its pages before the copy is reached
        root['directories'][name] = leaves([*range(n), n - 1])
        message = 'adds'
    else:
        root['directories'][name] = leaves(list(range(n - 1)))
        message = 'omits'
    bad = tmp_path / 'reordered.parquet'
    store.write(bad, root)
    with pytest.raises(ValidationError, match=message):
        validate(bad, full=True)


def test_directory_nodes_are_reached_once(leafy, tmp_path, monkeypatch):
    """A node shared by two parents fails before its subtree is read again,
    however deep the sharing goes."""
    raw, root = _root(leafy)
    name, top = next(iter(root['directories'].items()))
    _, entries = read_node(top)
    cell = int(entries['low'][0])
    assert entries['high'][0] == cell, 'equal one-cell entries are in key order'
    start, length = int(entries['offset'][0]), int(entries['length'][0])
    store = _Appended(raw)
    node = raw[start : start + length]
    for level in range(1, 13):
        # two copies of the node below, both referring to its children
        node = _node(level, [cell] * 2, [cell] * 2, *zip(store(node), store(node), strict=True))
    root['directories'][name] = node
    bad = tmp_path / 'shared.parquet'
    store.write(bad, root)
    decoded = []
    monkeypatch.setattr(
        _validate_index, 'read_node', lambda node: decoded.append(node) or read_node(node)
    )
    with pytest.raises(ValidationError, match='referenced twice'):
        validate(bad, full=True)
    assert len(decoded) < 20


@pytest.mark.parametrize(
    'field, values, message',
    [
        # the first page starts at the int64 maximum, so its end wraps negative
        (1, [2**63 - 1], 'outside data region'),
        # first rows whose differences wrap to nondecreasing values
        (3, [0, 2**63 - 1, -5], 'page ordering'),
    ],
)
def test_offset_index_sums_cannot_wrap(indexed, tmp_path, field, values, message):
    """Offset index values near the int64 limit fail validation, however
    their sums wrap."""
    raw, root = _root(indexed)
    footer = len(raw) - 8 - int.from_bytes(raw[-8:-4], 'little')
    metadata = loads(raw[footer:-8])
    chunk = next(
        c for c in get(get(metadata, 4)[1][0], 1)[1] if get(get(c, 3), 3)[1] == [b'geometry']
    )
    start = get(chunk, 4)
    index = loads(raw[start : start + get(chunk, 5)])
    pages = get(index, 1)[1]
    assert len(pages) > len(values)
    for i, value in enumerate(values):
        set_value(pages[i], field, value)
    moved = dumps(index)
    set_value(chunk, 4, footer)
    set_value(chunk, 5, len(moved))
    flat = [f.name for f in pq.ParquetFile(indexed).schema_arrow if not pa.types.is_nested(f.type)]
    rows = gzip.compress(dumps(row_metadata(metadata, flat)), mtime=0)
    root['row_metadata'] = [footer + len(moved), len(rows)]
    encoded = dumps(metadata)
    root['footer_bytes'] = len(encoded)
    raw = raw[:footer] + moved + rows + encoded + struct.pack('<I', len(encoded)) + b'PAR1'
    root['file_bytes'] = len(raw)
    bad = tmp_path / 'wrapped.parquet'
    _write_root(bad, raw, root)
    with pytest.raises(ValidationError, match=message):
        validate(bad, full=True)


def test_overview_pages_are_v1_plain(indexed, tmp_path, monkeypatch):
    """Readers scan the profile's overview pages directly, so other page
    kinds fail validation."""
    writer = pq.ParquetWriter
    monkeypatch.setattr(
        pq, 'ParquetWriter', lambda *a, **k: writer(*a, **{**k, 'data_page_version': '2.0'})
    )
    out = finalize_layout(indexed, tmp_path / 'v2.parquet', log=lambda *_: None)
    monkeypatch.undo()
    with pytest.raises(ValidationError, match='v1 data pages'):
        validate(out, full=True)


@pytest.mark.parametrize(
    'mutation',
    [
        'root_bounds',
        'level',
        'leaf_rows',
        'leaf_fences',
        'leaf_offset',
        'leaf_nulls',
        'codec',
        'primary',
        'footer_bytes',
        'row_metadata',
        'magic',
    ],
)
def test_spatial_index_corruption(indexed, tmp_path, mutation):
    raw, root = _root(indexed)
    bad = tmp_path / 'bad.parquet'
    if mutation in ('codec', 'primary', 'footer_bytes'):
        if mutation == 'codec':
            root['codecs'][next(iter(root['codecs']))] = 'SNAPPY'
        elif mutation == 'primary':
            root['primary'] = 'wrong_geometry'
        else:
            root['footer_bytes'] += 1
    elif mutation == 'row_metadata':
        # a stale copy: the row metadata of a file with one more row
        offset, length = root['row_metadata']
        rows = loads(gzip.decompress(raw[offset : offset + length]))
        set_value(rows, 3, get(rows, 3) + 1)
        encoded = gzip.compress(dumps(rows), mtime=0)
        footer = len(raw) - 8 - int.from_bytes(raw[-8:-4], 'little')
        raw = raw[:footer] + encoded + raw[footer:]
        root['row_metadata'] = [footer, len(encoded)]
        root['file_bytes'] = len(raw)
    elif mutation in ('root_bounds', 'level'):
        # [level, cells, spans, gaps, lengths] of one LOD's top node
        node = next(node for node in root['directories'].values() if node[1])
        if mutation == 'root_bounds':
            node[1][0] += 1000000
        else:
            node[0] += 1
    elif mutation == 'magic':
        raw = raw[:4] + bytes(8) + raw[12:]
    else:
        # Append one mutated leaf before the unchanged footer and point the last
        # entry of a level-one top node at it; all page offsets stay valid.
        node = next(n for n in root['directories'].values() if n[0] == 1 and n[1])
        start = sum(node[3]) + sum(node[4][:-1])
        leaf = json.loads(gzip.decompress(raw[start : start + node[4][-1]]))
        if mutation == 'leaf_rows':
            leaf[6][0] += 1
        elif mutation == 'leaf_fences':
            leaf[7][0] = [1, 1]
        elif mutation == 'leaf_nulls':
            leaf[8][0] = 1
        else:
            leaf[3][0] += 1
        encoded = gzip.compress(json.dumps(leaf).encode(), mtime=0)
        footer = len(raw) - 8 - int.from_bytes(raw[-8:-4], 'little')
        node[3][-1] += footer - start
        node[4][-1] = len(encoded)
        raw = raw[:footer] + encoded + raw[footer:]
        root['file_bytes'] = len(raw)
    _write_root(bad, raw, root)
    validate(bad, sample_rows=3)  # quick mode intentionally does not traverse directories
    with pytest.raises(ValidationError, match='spatial index'):
        validate(bad, full=True)


def test_corrupt_packed_row_beyond_sample(indexed, tmp_path, monkeypatch):
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
    # batches of a few values each; the row still counts from the row group's first
    monkeypatch.setattr('geoparquet_lod.unpack.BATCH_BYTES', 64)
    where = f'row group {target // 64}, row {target % 64}, lod {name}: invalid packed row'
    with pytest.raises(ValidationError, match=where):
        validate(bad, full=True)
    assert name in validate(indexed, full=True)['lods']


def varints(*values):
    out = bytearray()
    for value in values:
        while value >= 128:
            out.append((value & 127) | 128)
            value >>= 7
        out.append(value)
    return bytes(out)


@pytest.mark.parametrize(
    'value, message',
    [
        # one polygon of one ring storing two vertices, no attributes, no copies
        (varints(1, 1, 2, 0, 0, 2, 0, 0, 0), 'polygon ring with fewer than three vertices'),
        (varints(0, 0, 0), 'empty geometry on a shown row'),
        (varints(1, 1, 3, 2**33, 0, 2, 0, 0, 2, 0, 0), 'a grid coordinate outside int32'),
    ],
)
def test_invalid_geometry_names_its_row(indexed, tmp_path, monkeypatch, value, message):
    table = pq.read_table(indexed)
    lod = table['lod'].combine_chunks()
    children = [lod.field(i).to_pylist() for i in range(lod.type.num_fields)]
    target = next(i for i in range(len(table) - 1, 64, -1) if children[0][i] is not None)
    children[0][target] = value
    replacement = pa.StructArray.from_arrays(
        [pa.array(values, type=pa.binary()) for values in children], fields=list(lod.type)
    )
    table = table.set_column(
        table.schema.get_field_index('lod'), table.schema.field('lod'), replacement
    )
    metadata = dict(table.schema.metadata)
    metadata.pop(b'lod_spatial_index', None)
    bad = tmp_path / 'geometry.parquet'
    table = table.replace_schema_metadata(metadata)
    pq.write_table(table, bad, row_group_size=64, write_page_index=True)
    validate(bad, sample_rows=3)
    where = f'row group {target // 64}, row {target % 64}, lod {lod.type[0].name}: {message}'
    with pytest.raises(ValidationError, match=where):
        validate(bad, full=True)
    monkeypatch.setattr('geoparquet_lod.unpack.BATCH_BYTES', 64)
    with pytest.raises(ValidationError, match=where):
        validate(bad, full=True)


@pytest.mark.parametrize('mutation', ['tile', 'id'])
def test_invalid_spill_identifiers(indexed, tmp_path, mutation):
    from unpacker import Unpacker

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


def _rewritten(indexed, path, change):
    """`indexed` with `change(table, geo)` applied to its rows and geo
    metadata, written without its spatial index."""
    table = pq.read_table(indexed)
    metadata = dict(table.schema.metadata)
    geo = json.loads(metadata[b'geo'])
    table = change(table, geo)
    metadata[b'geo'] = json.dumps(geo).encode()
    metadata.pop(b'lod_spatial_index', None)
    table = table.replace_schema_metadata(metadata)
    pq.write_table(table, path, row_group_size=64, write_page_index=True)
    return path


def _replace(table, name, column, nullable=True):
    field = pa.field(name, column.type, nullable=nullable)
    return table.set_column(table.schema.get_field_index(name), field, column)


def _null_at(column, row):
    """`column` with `row` null. A struct's required children keep their
    values under it, as Arrow's Parquet writer requires."""
    column = column.combine_chunks()
    mask = column.is_null().to_numpy(zero_copy_only=False)
    mask[row] = True
    if pa.types.is_struct(column.type):
        children = [column.field(i) for i in range(column.type.num_fields)]
        return pa.StructArray.from_arrays(children, fields=list(column.type), mask=pa.array(mask))
    return pc.if_else(pa.array(mask), pa.scalar(None, column.type), column)


@pytest.mark.parametrize(
    'mutation, message',
    [
        ('struct', 'bbox must be a struct column, not float'),
        ('required', 'bbox must be nullable'),
        ('names', 'bbox fields must be xmin, ymin, xmax and ymax, not minx, ymin, xmax, ymax'),
        ('double', 'bbox.xmin must be float32, not double'),
        ('nullable', 'bbox.ymin must be required, not nullable'),
        (
            'path',
            'geo.columns.geometry.covering.bbox.xmax must be ["bbox", "xmax"], not ["bbox", "maxx"]',
        ),
        ('declared', 'geo.columns.geometry.covering.bbox must declare xmin, ymin, xmax and ymax'),
        ('geometry', 'primary geometry column geometry missing'),
        ('wkt', 'primary geometry column geometry must be WKB binary, not string'),
        ('key', 'key column hilbert missing'),
    ],
)
def test_covering_geometry_and_key_columns_are_checked(indexed, tmp_path, mutation, message):
    """The covering is a nullable struct of four required float32 bounds
    declared at their own paths, beside a WKB primary geometry and the key
    column. The footer shows each, so a sample of no rows finds them."""

    def change(table, geo):
        bbox = table['bbox'].combine_chunks()
        fields = [pa.field(bound, pa.float32(), nullable=False) for bound in BOUNDS]
        declared = geo['columns']['geometry']['covering']['bbox']
        if mutation == 'struct':
            return _replace(table, 'bbox', bbox.field('xmin'))
        if mutation == 'required':
            return _replace(table, 'bbox', bbox, nullable=False)
        if mutation == 'names':
            fields[0] = fields[0].with_name('minx')
        elif mutation == 'double':
            fields[0] = fields[0].with_type(pa.float64())
        elif mutation == 'nullable':
            fields[1] = fields[1].with_nullable(True)
        elif mutation == 'path':
            declared['xmax'] = ['bbox', 'maxx']
        elif mutation == 'declared':
            del declared['ymax']
        elif mutation == 'wkt':
            wkt = shapely.to_wkt(shapely.from_wkb(table['geometry'].to_numpy()))
            return _replace(table, 'geometry', pa.array(wkt, pa.string()))
        elif mutation == 'geometry':
            return table.drop_columns(['geometry'])
        else:
            return table.drop_columns(['hilbert'])
        bounds = [bbox.field(i).cast(f.type) for i, f in enumerate(fields)]
        return _replace(table, 'bbox', pa.StructArray.from_arrays(bounds, fields=fields))

    bad = _rewritten(indexed, tmp_path / f'{mutation}.parquet', change)
    with pytest.raises(ValidationError, match=re.escape(message)):
        validate(bad, sample_rows=0)


@pytest.mark.parametrize(
    'kind, message',
    [
        ('halffloat', None),
        ('large_string', None),
        ('date32', 'attribute id: type "date32" is not an integer, float, bool or string type'),
        ('decimal', 'attribute id: type "decimal" is not an integer, float, bool or string type'),
        (None, 'attribute id: type null is not an integer, float, bool or string type'),
    ],
)
def test_carried_attribute_types_are_checked(indexed, tmp_path, kind, message):
    """A carried attribute declares the Arrow name of a type the packer writes."""

    def change(table, geo):
        geo['lod_columns']['attributes'] = [{'name': 'id', 'type': kind}]
        return table

    bad = _rewritten(indexed, tmp_path / 'attributes.parquet', change)
    if message is None:
        validate(bad, full=True)
        return
    with pytest.raises(ValidationError, match=re.escape(message)):
        validate(bad, sample_rows=0)


@pytest.mark.parametrize(
    'mutation, message',
    [
        ('covering', 'null bbox covering on a non-empty geometry'),
        ('null', 'bbox covering on a null geometry'),
        ('empty', 'bbox covering on an empty geometry'),
        ('invalid', 'primary geometry is not valid WKB'),
    ],
)
def test_sampled_coverings_follow_their_geometry(indexed, tmp_path, mutation, message):
    """A sampled row's covering is null exactly where its primary geometry is
    null or empty, in either mode; a row past the sample is not checked."""
    row = 1

    def change(table, geo):
        if mutation == 'covering':
            return _replace(table, 'bbox', _null_at(table['bbox'], row))
        wkb = table['geometry'].to_pylist()
        wkb[row] = {'null': None, 'empty': shapely.to_wkb(shapely.Polygon())}.get(
            mutation, b'\x01\x03\x00'
        )
        return _replace(table, 'geometry', pa.array(wkb, pa.binary()))

    bad = _rewritten(indexed, tmp_path / f'{mutation}.parquet', change)
    validate(bad, sample_rows=row)
    for full in (False, True):
        with pytest.raises(ValidationError, match=f'row group 0, row {row}: {message}'):
            validate(bad, sample_rows=row + 1, full=full)


@pytest.mark.parametrize(
    'column, message',
    [
        ('bbox', 'null bbox covering on a row with a Hilbert key'),
        ('hilbert', 'bbox covering on a row with a null Hilbert key'),
    ],
)
def test_coverings_follow_the_key_in_every_row_group(indexed, tmp_path, column, message):
    """Full mode checks in every row group that the covering and the Hilbert
    key are null on the same rows; quick mode samples the first row group only."""
    group, row = 2, 5

    def change(table, geo):
        return _replace(table, column, _null_at(table[column], group * 64 + row))

    bad = _rewritten(indexed, tmp_path / f'{column}.parquet', change)
    validate(bad)
    with pytest.raises(ValidationError, match=f'row group {group}, row {row}: {message}'):
        validate(bad, full=True)
