"""Bounded native packed-own decoder used only by the national experiment."""

import ctypes
import hashlib
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa


class NativeOwn:
    def __init__(self, library):
        self.lib = ctypes.CDLL(str(library))
        pointer, number = ctypes.c_void_p, ctypes.c_int64
        self.lib.inspect_own.argtypes = [pointer, pointer, pointer, number, number] + [pointer] * 7
        self.lib.inspect_own.restype = ctypes.c_int
        self.lib.fill_own.argtypes = [pointer, pointer, pointer, number, number] + [pointer] * 6
        self.lib.fill_own.restype = ctypes.c_int

    def decode(self, array, height, height_valid, classes):
        array = array.combine_chunks() if isinstance(array, pa.ChunkedArray) else array
        n = len(array)
        buffers = array.buffers()
        offsets = np.frombuffer(buffers[1], np.int32)[array.offset : array.offset + n + 1]
        valid = buffers[0].address if buffers[0] is not None else None
        common = [offsets.ctypes.data, buffers[2].address, valid, array.offset, n]
        totals = np.zeros(3, np.uint64)
        present = np.zeros(n, np.uint8)
        boxes = np.zeros((n, 4), np.int32)
        height_bits = np.zeros(n, np.uint64)
        class_codes = np.full(n, -1, np.int32)
        presence = np.zeros(n, np.uint8)
        error = np.zeros(1, np.int64)
        status = self.lib.inspect_own(
            *common,
            *[
                a.ctypes.data
                for a in [totals, present, boxes, height_bits, class_codes, presence, error]
            ],
        )
        if status:
            raise ValueError(f'Native geometry inspection failed at local row {error[0]}')
        own = present.astype(bool)
        assert np.array_equal((presence[own] & 1) != 0, height_valid[own]), 'Height null mismatch'
        assert np.array_equal(height_bits[own], height.view(np.uint64)[own]), (
            'Height override mismatch'
        )
        assert np.array_equal(class_codes[own], classes[own]), 'Class override mismatch'
        assert np.array_equal((presence[own] & 2) != 0, classes[own] >= 0), 'Class null mismatch'
        polygons, rings, vertices = (int(v) for v in totals)
        assert max(polygons, rings, vertices) < 2**31
        geom = np.empty(n + 1, np.int32)
        poly = np.empty(polygons + 1, np.int32)
        ring = np.empty(rings + 1, np.int32)
        xs, ys = np.empty(vertices, np.int32), np.empty(vertices, np.int32)
        status = self.lib.fill_own(
            *common, *[a.ctypes.data for a in [geom, poly, ring, xs, ys, error]]
        )
        if status:
            raise ValueError(f'Native geometry materialization failed at local row {error[0]}')
        assert (int(geom[-1]), int(poly[-1]), int(ring[-1])) == (polygons, rings, vertices)
        xy = pa.StructArray.from_arrays([pa.array(xs), pa.array(ys)], names=['x', 'y'])
        coordinates = pa.ListArray.from_arrays(pa.array(ring), xy)
        polygons_array = pa.ListArray.from_arrays(pa.array(poly), coordinates)
        geometry = pa.ListArray.from_arrays(pa.array(geom), polygons_array, mask=pa.array(~own))
        element_type = pa.list_(
            pa.field(
                'element', pa.list_(pa.field('element', pa.list_(pa.field('element', xy.type))))
            )
        )
        geometry = geometry.cast(element_type)
        digest = hashlib.sha256()
        for a in [present, geom, poly, ring, xs, ys]:
            digest.update(memoryview(a))
        return (
            geometry,
            boxes,
            present,
            {
                'selected': int(own.sum()),
                'polygons': polygons,
                'rings': rings,
                'vertices': vertices,
                'geometry_sha256': digest.hexdigest(),
                'all_own_source_properties_equal': True,
            },
        )


def compile_native(directory):
    source = Path(__file__).with_name('overview-native.cpp')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    target = Path(directory) / f'overview-native-{digest}.dylib'
    if not target.exists():
        subprocess.run(
            [
                '/usr/bin/clang++',
                '-std=c++17',
                '-O3',
                '-dynamiclib',
                str(source),
                '-o',
                str(target),
            ],
            check=True,
        )
    return target
