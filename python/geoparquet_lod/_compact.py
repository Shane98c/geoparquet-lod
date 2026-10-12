"""Lossless Thrift compact metadata reader/writer for the layout optimizer.

Keep field ids and wire types, including unknown fields. Data pages themselves
are never re-encoded. This is deliberately independent of a generated Parquet
schema so a relocation preserves metadata it does not need to understand.
"""

import io
import struct


def _uint(stream):
    value = shift = 0
    while True:
        raw = stream.read(1)
        if not raw or shift >= 70:
            raise ValueError('invalid compact integer')
        byte = raw[0]
        value |= (byte & 127) << shift
        if byte < 128:
            return value
        shift += 7


def _put_uint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _read(stream, kind):
    if kind in (1, 2):
        return kind == 1
    if kind == 3:
        return struct.unpack('b', stream.read(1))[0]
    if kind in (4, 5, 6):
        n = _uint(stream)
        return (n >> 1) ^ -(n & 1)
    if kind == 7:
        return stream.read(8)
    if kind == 8:
        size = _uint(stream)
        data = stream.read(size)
        if len(data) != size:
            raise ValueError('truncated compact binary')
        return data
    if kind in (9, 10):
        byte = stream.read(1)[0]
        count, child = byte >> 4, byte & 15
        if count == 15:
            count = _uint(stream)
        return child, [
            _read(stream, stream.read(1)[0] if child in (1, 2) else child) for _ in range(count)
        ]
    if kind == 11:
        count = _uint(stream)
        types = stream.read(1)[0] if count else 0

        def item(kind):
            return _read(stream, stream.read(1)[0] if kind in (1, 2) else kind)

        return types, [(item(types >> 4), item(types & 15)) for _ in range(count)]
    if kind == 12:
        fields, previous = {}, 0
        while True:
            raw = stream.read(1)
            if not raw:
                raise ValueError('truncated compact struct')
            byte = raw[0]
            if not byte:
                return fields
            delta, child = byte >> 4, byte & 15
            field = previous + delta if delta else _read(stream, 4)
            fields[field] = (child, _read(stream, child))
            previous = field
    raise ValueError(f'unsupported compact type {kind}')


def _write(kind, value):
    if kind in (1, 2):
        return b''
    if kind == 3:
        return struct.pack('b', value)
    if kind in (4, 5, 6):
        return _put_uint(value * 2 if value >= 0 else -value * 2 - 1)
    if kind == 7:
        return value
    if kind == 8:
        return _put_uint(len(value)) + value
    if kind in (9, 10):
        child, items = value
        n = len(items)
        header = bytes([(min(n, 15) << 4) | child]) + (_put_uint(n) if n >= 15 else b'')
        return header + b''.join(
            bytes([1 if v else 2]) if child in (1, 2) else _write(child, v) for v in items
        )
    if kind == 11:
        types, items = value

        def item(kind, value):
            return bytes([1 if value else 2]) if kind in (1, 2) else _write(kind, value)

        return (
            _put_uint(len(items))
            + (bytes([types]) if items else b'')
            + b''.join(item(types >> 4, k) + item(types & 15, v) for k, v in items)
        )
    if kind == 12:
        out, previous = bytearray(), 0
        for field, (child, v) in value.items():
            if child in (1, 2):
                child = 1 if v else 2
            delta = field - previous
            out.extend(
                bytes([(delta << 4) | child])
                if 0 < delta < 16
                else bytes([child]) + _write(4, field)
            )
            out.extend(_write(child, v))
            previous = field
        out.append(0)
        return bytes(out)
    raise ValueError(f'unsupported compact type {kind}')


def loads(data):
    return _read(io.BytesIO(data), 12)


def dumps(value):
    return _write(12, value)


def get(fields, field, default=None):
    return fields[field][1] if field in fields else default


def set_value(fields, field, value):
    fields[field] = (fields[field][0], value)
