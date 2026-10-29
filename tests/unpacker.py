"""A packed row decoded a varint at a time, apart from `unpack`, as the
oracle tests check the writer and `unpack` against."""

import numpy as np
import pyarrow as pa


def unzigzag(v: int) -> int:
    return (v >> 1) ^ -(v & 1)


class Unpacker:
    """Decoder of one packed row."""

    def __init__(self, blob: bytes, family: str, fields: list[pa.Field], dictionaries=None):
        self.b = blob
        self.pos = 0
        self.family = family
        self.fields = fields
        self.dictionaries = dictionaries or {}
        self.strings: list[tuple[int, int]] = []  # (slot, length) in order

    def varint(self) -> int:
        shift = 0
        out = 0
        while True:
            byte = self.b[self.pos]
            self.pos += 1
            out |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return out
            shift += 7

    def geometry(self):
        if self.family == 'Point':
            return (unzigzag(self.varint()), unzigzag(self.varint()))
        out = []
        if self.family == 'MultiPolygon':
            for _ in range(self.varint()):
                out.append([self.ring(closed=True) for _ in range(self.varint())])
        else:
            for _ in range(self.varint()):
                out.append(self.ring())
        return out

    def ring(self, closed=False):
        """A ring's vertices; a polygon ring is stored open and closed again here."""
        n = self.varint()
        ring, x, y = [], 0, 0
        for _ in range(n):
            x += unzigzag(self.varint())
            y += unzigzag(self.varint())
            ring.append((x, y))
        if closed and ring:
            ring.append(ring[0])
        return ring

    def attrs(self):
        present = self.varint()
        out = {}
        for i, f in enumerate(self.fields):
            if not present >> i & 1:
                out[f.name] = None
                continue
            v = self.varint()
            t = f.type
            if f.name in self.dictionaries:
                out[f.name] = self.dictionaries[f.name][v]
            elif pa.types.is_integer(t):
                out[f.name] = unzigzag(v)
            elif pa.types.is_float64(t):
                out[f.name] = float(np.array([v], np.uint64).view(np.float64)[0])
            elif pa.types.is_floating(t):
                out[f.name] = float(np.array([v], np.uint32).view(np.float32)[0])
            elif pa.types.is_boolean(t):
                out[f.name] = bool(v)
            else:
                out[f.name] = ('string', v)
                self.strings.append(f.name)
        return out

    def row(self):
        geometry = self.geometry()
        attrs = self.attrs()
        spill = []
        for _ in range(self.varint()):
            tile = self.varint()
            rid = self.varint()
            spill.append(
                {'tile': tile, 'id': rid, 'geometry': self.geometry(), 'attrs': self.attrs()}
            )
        # string payloads follow, in order
        for record in [attrs, *[s['attrs'] for s in spill]]:
            for name, v in record.items():
                if isinstance(v, tuple) and v[0] == 'string':
                    n = v[1]
                    record[name] = self.b[self.pos : self.pos + n].decode('utf-8', 'replace')
                    self.pos += n
        return {'geometry': geometry, 'attrs': attrs, 'spill': spill}
