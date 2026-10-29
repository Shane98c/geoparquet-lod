/**
 * Decoder of one packed LOD row, the format python/geoparquet_lod/pack.py
 * writes: a row's geometry on the LOD grid, its carried attributes and the
 * straddler copies it hosts, all as LEB128 varints with string payloads
 * trailing in order.
 */

import { GeometryBuilder, type GeometryType } from './geometry.js'

export type AttributeKind = 'int' | 'float32' | 'float64' | 'bool' | 'string' | 'enum'

export interface AttributeField {
  name: string
  kind: AttributeKind
  /** The value table of an `enum` attribute, indexed by the packed value. */
  values?: string[]
}

/** The attribute kinds the packer writes, from the Arrow type names in the metadata. */
export const attributeKind = (type: string): AttributeKind => {
  const t = type.toLowerCase()
  if (t === 'double' || t === 'float64') return 'float64'
  if (t === 'float' || t === 'float32' || t === 'halffloat' || t === 'float16') return 'float32'
  if (t === 'bool' || t === 'boolean') return 'bool'
  if (t === 'string' || t === 'utf8' || t === 'large_string' || t === 'large_utf8') return 'string'
  if (/int/.test(t)) return 'int'
  throw new Error(`unsupported carried attribute type ${type}`)
}

const textDecoder = new TextDecoder()
const f32 = new DataView(new ArrayBuffer(8))

class Cursor {
  pos = 0
  constructor(readonly bytes: Uint8Array) {}

  /** Read an IEEE-754 bit pattern without passing its 64 bits through Number. */
  float64(): number {
    let low = 0,
      high = 0,
      shift = 0
    for (;;) {
      const byte = this.bytes[this.pos++],
        bits = byte & 0x7f
      if (shift < 32) {
        low |= bits << shift
        if (shift > 25) high |= bits >>> (32 - shift)
      } else high |= bits << (shift - 32)
      if (!(byte & 0x80)) break
      shift += 7
    }
    f32.setUint32(0, low >>> 0, true)
    f32.setUint32(4, high >>> 0, true)
    return f32.getFloat64(0, true)
  }

  varint(): number {
    let out = 0
    let scale = 1
    for (;;) {
      const b = this.bytes[this.pos++]
      out += (b & 0x7f) * scale
      if (!(b & 0x80)) return out
      scale *= 128
    }
  }
}

const unzigzag = (v: number): number => (v % 2 ? -(v + 1) / 2 : v / 2)

/** Attribute values with string slots left as byte lengths, filled from the trailing payload later. */
const attributes = (
  c: Cursor,
  fields: AttributeField[],
  strings: [unknown[], number, number][],
) => {
  const present = c.varint()
  const out: unknown[] = new Array(fields.length)
  for (let i = 0; i < fields.length; i++) {
    if (!(Math.floor(present / 2 ** i) % 2)) {
      out[i] = null
      continue
    }
    if (fields[i].kind === 'float64') {
      out[i] = c.float64()
      continue
    }
    const v = c.varint()
    switch (fields[i].kind) {
      case 'int':
        out[i] = unzigzag(v)
        break
      case 'float32':
        f32.setUint32(0, v, true)
        out[i] = f32.getFloat32(0, true)
        break
      case 'bool':
        out[i] = v !== 0
        break
      case 'string':
        strings.push([out, i, v])
        break
      case 'enum':
        out[i] = fields[i].values?.[v] ?? null
        break
    }
  }
  return out
}

/** A row's own attributes, skipping its geometry and the copies it hosts. */
export const decodeAttributes = (
  bytes: Uint8Array,
  type: GeometryType,
  fields: AttributeField[],
): unknown[] => {
  const c = new Cursor(bytes)
  const strings: [unknown[], number, number][] = []
  geometryInto(c, type)
  const attrs = attributes(c, fields, strings)
  if (!strings.length) return attrs
  // The row's own strings lead the trailing payload, after every copy.
  const count = c.varint()
  for (let k = 0; k < count; k++) {
    c.varint()
    c.varint()
    geometryInto(c, type)
    skipAttributes(c, fields)
  }
  for (const [slot, i, length] of strings) {
    slot[i] = textDecoder.decode(bytes.subarray(c.pos, c.pos + length))
    c.pos += length
  }
  return attrs
}

/** Decode selected geometry directly into columnar buffers. Unwanted copies
 * are scanned without allocating geometry arrays; trailing strings still
 * follow the original row-wide payload order. */
export const decodeSelectedRow = (
  bytes: Uint8Array,
  type: GeometryType,
  fields: AttributeField[],
  builder: GeometryBuilder,
  own: boolean,
  id: number,
  tile: number,
  add: (id: number, attrs: unknown[], slot: number) => void,
  withAttributes = true,
): void => {
  const c = new Cursor(bytes)
  if (!withAttributes) {
    const ownGeometry = geometryInto(c, type, own ? builder : undefined)
    skipAttributes(c, fields)
    if (own && ownGeometry) add(id, NO_ATTRIBUTES, 0)
    const count = c.varint()
    for (let k = 0; k < count; k++) {
      const copyTile = c.varint(),
        copyId = c.varint()
      const nonempty = geometryInto(c, type, copyTile === tile ? builder : undefined)
      if (copyTile === tile && !nonempty) {
        builder.begin(type)
        if (type === 'MultiLineString') builder.startPolygon()
      }
      skipAttributes(c, fields)
      if (copyTile === tile) add(copyId, NO_ATTRIBUTES, k + 1)
    }
    // All string bytes trail the row's varints; no requested attributes means
    // they can be left untouched after geometry and copy selection.
    return
  }
  const strings: [unknown[], number, number][] = []
  const selected: [number, unknown[], number][] = []
  const keep = geometryInto(c, type, own ? builder : undefined)
  const attrs = attributes(c, fields, strings)
  if (own && keep) selected.push([id, attrs, 0])
  const count = c.varint()
  for (let k = 0; k < count; k++) {
    const copyTile = c.varint()
    const copyId = c.varint()
    const nonempty = geometryInto(c, type, copyTile === tile ? builder : undefined)
    if (copyTile === tile && !nonempty) {
      builder.begin(type)
      if (type === 'MultiLineString') builder.startPolygon()
    }
    const copyAttrs = attributes(c, fields, strings)
    if (copyTile === tile) selected.push([copyId, copyAttrs, k + 1])
  }
  for (const [slot, i, length] of strings) {
    slot[i] = textDecoder.decode(bytes.subarray(c.pos, c.pos + length))
    c.pos += length
  }
  for (const [row, values, slot] of selected) add(row, values, slot)
}

function geometryInto(c: Cursor, type: GeometryType, builder?: GeometryBuilder): boolean {
  if (type === 'Point') {
    const x = unzigzag(c.varint()),
      y = unzigzag(c.varint())
    if (builder) {
      builder.begin(type)
      builder.startPolygon()
      builder.startRing()
      const i = builder.reserveCoordinates(1)
      builder.x[i] = x
      builder.y[i] = y
    }
    return true
  }
  const parts = c.varint()
  // Empty host geometries are omitted by the own-row path. Copies are never
  // empty in files produced by the writer.
  if (parts && builder) builder.begin(type)
  if (type === 'MultiLineString') {
    if (parts && builder) builder.startPolygon()
    for (let i = 0; i < parts; i++) ringInto(c, builder)
  } else {
    for (let i = 0; i < parts; i++) {
      builder?.startPolygon()
      const rings = c.varint()
      for (let j = 0; j < rings; j++) ringInto(c, builder)
    }
  }
  return parts > 0
}

function ringInto(c: Cursor, builder?: GeometryBuilder) {
  const n = c.varint()
  if (!builder) {
    for (let i = 0; i < n * 2; i++) c.varint()
    return
  }
  builder.startRing()
  const start = builder.reserveCoordinates(n)
  let x = 0,
    y = 0
  for (let i = start; i < start + n; i++) {
    x += unzigzag(c.varint())
    y += unzigzag(c.varint())
    builder.x[i] = x
    builder.y[i] = y
  }
}

const NO_ATTRIBUTES: unknown[] = []
function skipAttributes(c: Cursor, fields: AttributeField[]) {
  const present = c.varint()
  for (let i = 0; i < fields.length; i++) if (Math.floor(present / 2 ** i) % 2) c.varint()
}
