/**
 * Decoder of one packed LOD row, the format python/geoparquet_lod/pack.py
 * writes: a row's geometry on the LOD grid, its carried attributes and the
 * straddler copies it hosts, all as LEB128 varints with string payloads
 * trailing in order.
 */

import type { GeometryType } from './geometry.js'

export type AttributeKind = 'int' | 'float32' | 'float64' | 'bool' | 'string' | 'enum'

export interface AttributeField {
  name: string
  kind: AttributeKind
  /** The value table of an `enum` attribute, indexed by the packed value. */
  values?: string[]
}

export interface DecodedCopy {
  /** `zoom * 2^48 + x * 2^zoom + y` of the tile the copy serves. */
  tile: number
  /** File row index of the copied feature. */
  id: number
  geometry: unknown
  attrs: unknown[]
}

export interface DecodedRow {
  /** Nested arrays of grid coordinates in the LOD's family shape; empty for a row that only hosts copies. */
  geometry: unknown
  attrs: unknown[]
  spill: DecodedCopy[]
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

const ring = (c: Cursor): [number, number][] => {
  const n = c.varint()
  const out: [number, number][] = new Array(n)
  let x = 0
  let y = 0
  for (let i = 0; i < n; i++) {
    x += unzigzag(c.varint())
    y += unzigzag(c.varint())
    out[i] = [x, y]
  }
  return out
}

const geometry = (c: Cursor, type: GeometryType): unknown => {
  if (type === 'Point') return [unzigzag(c.varint()), unzigzag(c.varint())]
  const n = c.varint()
  if (type === 'MultiLineString') {
    const parts: [number, number][][] = new Array(n)
    for (let i = 0; i < n; i++) parts[i] = ring(c)
    return parts
  }
  const polygons: [number, number][][][] = new Array(n)
  for (let i = 0; i < n; i++) {
    const rings = c.varint()
    const p: [number, number][][] = new Array(rings)
    for (let j = 0; j < rings; j++) p[j] = ring(c)
    polygons[i] = p
  }
  return polygons
}

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
    const v = c.varint()
    switch (fields[i].kind) {
      case 'int':
        out[i] = unzigzag(v)
        break
      case 'float32':
        f32.setUint32(0, v, true)
        out[i] = f32.getFloat32(0, true)
        break
      case 'float64':
        f32.setBigUint64(0, BigInt(v), true)
        out[i] = f32.getFloat64(0, true)
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

export const decodeRow = (
  bytes: Uint8Array,
  type: GeometryType,
  fields: AttributeField[],
): DecodedRow => {
  const c = new Cursor(bytes)
  const strings: [unknown[], number, number][] = []
  const own = geometry(c, type)
  const attrs = attributes(c, fields, strings)
  const count = c.varint()
  const spill: DecodedCopy[] = new Array(count)
  for (let k = 0; k < count; k++) {
    const tile = c.varint()
    const id = c.varint()
    const g = geometry(c, type)
    spill[k] = { tile, id, geometry: g, attrs: attributes(c, fields, strings) }
  }
  for (const [slot, i, length] of strings) {
    slot[i] = textDecoder.decode(bytes.subarray(c.pos, c.pos + length))
    c.pos += length
  }
  return { geometry: own, attrs, spill }
}
