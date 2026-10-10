/** Decoders for payload values: a declared attribute block, then geometry. */

export type AttributeType =
  | 'f32'
  | 'f64'
  | 'i32'
  | 'u32'
  | 'i16'
  | 'u16'
  | 'u8'
  | 'i8'
  | 'utf8'
  | 'f32[]'
  | 'i32[]'
  | 'u8[]'

export interface AttributeSpec {
  name: string
  type: AttributeType
}

export type AttributeValue = number | string | number[]

const FIXED: Record<string, [number, (v: DataView, o: number) => number]> = {
  f32: [4, (v, o) => v.getFloat32(o, true)],
  f64: [8, (v, o) => v.getFloat64(o, true)],
  i32: [4, (v, o) => v.getInt32(o, true)],
  u32: [4, (v, o) => v.getUint32(o, true)],
  i16: [2, (v, o) => v.getInt16(o, true)],
  u16: [2, (v, o) => v.getUint16(o, true)],
  u8: [1, (v, o) => v.getUint8(o)],
  i8: [1, (v, o) => v.getInt8(o)],
}

const utf8 = new TextDecoder()

/** Decode the attribute block of one payload; returns the values and the byte position after it. */
export const decodeAttributes = (
  bytes: Uint8Array,
  spec: AttributeSpec[],
): { values: Record<string, AttributeValue>; pos: number } => {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const values: Record<string, AttributeValue> = {}
  let pos = 0
  for (const a of spec) {
    const fixed = FIXED[a.type]
    if (fixed) {
      if (pos + fixed[0] > bytes.length) throw new Error(`payload truncated in ${a.name}`)
      values[a.name] = fixed[1](view, pos)
      pos += fixed[0]
    } else if (a.type === 'utf8') {
      const n = bytes[pos++]
      if (pos + n > bytes.length) throw new Error(`payload truncated in ${a.name}`)
      values[a.name] = utf8.decode(bytes.subarray(pos, pos + n))
      pos += n
    } else {
      const item = FIXED[a.type.slice(0, -2)]
      const n = bytes[pos++]
      if (pos + n * item[0] > bytes.length) throw new Error(`payload truncated in ${a.name}`)
      const arr: number[] = []
      for (let i = 0; i < n; i++, pos += item[0]) arr.push(item[1](view, pos))
      values[a.name] = arr
    }
  }
  return { values, pos }
}

export const decodePoint = (bytes: Uint8Array, pos: number): [number, number] => {
  if (pos + 8 !== bytes.length) throw new Error('point payload has the wrong length')
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  return [view.getFloat32(pos, true), view.getFloat32(pos + 4, true)]
}

/** Quantized polygon tree: n_polys, [n_rings, [n_verts, zigzag deltas with the first vertex absolute]]. */
export const decodePolygonTree = (
  bytes: Uint8Array,
  start: number,
  scale: readonly [number, number],
  offset: readonly [number, number],
): number[][][][] => {
  let pos = start
  const varint = (): number => {
    let result = 0
    let shift = 0
    for (;;) {
      if (pos >= bytes.length || shift > 35) throw new Error('truncated polygon payload')
      const b = bytes[pos++]
      result += (b & 0x7f) * 2 ** shift
      if (!(b & 0x80)) return result
      shift += 7
    }
  }
  const zigzag = (u: number): number => (u % 2 ? -(u + 1) / 2 : u / 2)
  const polygons: number[][][][] = []
  const nPolys = varint()
  for (let p = 0; p < nPolys; p++) {
    const rings: number[][][] = []
    const nRings = varint()
    for (let r = 0; r < nRings; r++) {
      const ring: number[][] = []
      const nVerts = varint()
      if (nVerts > bytes.length - pos) throw new Error('polygon payload declares more vertices than bytes')
      let x = 0
      let y = 0
      for (let k = 0; k < nVerts; k++) {
        const dx = zigzag(varint())
        const dy = zigzag(varint())
        x = k === 0 ? dx : x + dx
        y = k === 0 ? dy : y + dy
        ring.push([offset[0] + scale[0] * x, offset[1] + scale[1] * y])
      }
      rings.push(ring)
    }
    polygons.push(rings)
  }
  if (pos !== bytes.length) throw new Error('trailing bytes after polygon payload')
  return polygons
}

/** Growable multipolygon column in the shape @cogp/reader's toMvt consumes, in grid units. */
export class PolygonBuilder {
  length = 0
  polygons = 0
  rings = 0
  coords = 0
  types: Uint8Array<ArrayBufferLike> = new Uint8Array(1024)
  geometryOffsets = new Uint32Array(1025)
  polygonOffsets = new Uint32Array(1025)
  ringOffsets = new Uint32Array(1025)
  x = new Int32Array(8192)
  y = new Int32Array(8192)

  /** `type` is the GeoArrow family code: 6 for multipolygon, 5 for multilinestring. */
  add(bytes: Uint8Array, start: number, type: 5 | 6 = 6): void {
    let pos = start
    const varint = (): number => {
      let result = 0
      let shift = 0
      for (;;) {
        if (pos >= bytes.length || shift > 35) throw new Error('truncated polygon payload')
        const b = bytes[pos++]
        result += (b & 0x7f) * 2 ** shift
        if (!(b & 0x80)) return result
        shift += 7
      }
    }
    const zigzag = (u: number): number => (u % 2 ? -(u + 1) / 2 : u / 2)
    if (this.length + 1 >= this.types.length) {
      const t = new Uint8Array(this.types.length * 2)
      t.set(this.types)
      this.types = t
    }
    this.types[this.length] = type
    this.geometryOffsets = ensure(this.geometryOffsets, this.length + 2)
    this.geometryOffsets[this.length] = this.polygons
    const nPolys = varint()
    for (let p = 0; p < nPolys; p++) {
      this.polygonOffsets = ensure(this.polygonOffsets, this.polygons + 2)
      this.polygonOffsets[this.polygons++] = this.rings
      const nRings = varint()
      for (let r = 0; r < nRings; r++) {
        this.ringOffsets = ensure(this.ringOffsets, this.rings + 2)
        this.ringOffsets[this.rings++] = this.coords
        const nVerts = varint()
        if (nVerts > bytes.length - pos) throw new Error('polygon payload declares more vertices than bytes')
        this.x = ensure(this.x, this.coords + nVerts)
        this.y = ensure(this.y, this.coords + nVerts)
        let cx = 0
        let cy = 0
        for (let k = 0; k < nVerts; k++) {
          const dx = zigzag(varint())
          const dy = zigzag(varint())
          cx = k === 0 ? dx : cx + dx
          cy = k === 0 ? dy : cy + dy
          this.x[this.coords] = cx
          this.y[this.coords] = cy
          this.coords++
        }
      }
    }
    if (pos !== bytes.length) throw new Error('trailing bytes after polygon payload')
    this.length++
  }

  finish(scale: readonly [number, number], offset: readonly [number, number]) {
    this.geometryOffsets[this.length] = this.polygons
    this.polygonOffsets[this.polygons] = this.rings
    this.ringOffsets[this.rings] = this.coords
    return {
      length: this.length,
      types: this.types.subarray(0, this.length),
      x: this.x.subarray(0, this.coords),
      y: this.y.subarray(0, this.coords),
      geometryOffsets: this.geometryOffsets.subarray(0, this.length + 1),
      polygonOffsets: this.polygonOffsets.subarray(0, this.polygons + 1),
      ringOffsets: this.ringOffsets.subarray(0, this.rings + 1),
      scale,
      offset,
    }
  }
}

function ensure<T extends Uint32Array | Int32Array>(a: T, n: number): T {
  if (n <= a.length) return a
  const b = new (a.constructor as new (n: number) => T)(Math.max(n, a.length * 2))
  b.set(a as never)
  return b
}
