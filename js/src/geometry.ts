/**
 * Assembles interleaved `quantized_geoarrow` rows, as hyparquet returns the
 * one coordinate leaf (nested arrays ending in `[x, y]` pairs of int32), into
 * the columnar GeometryColumn that @cogp/reader's `toMvt` consumes. Each
 * stored pair is the zigzag-coded step from the ring's previous vertex, so
 * rings are summed as they are read. Coordinates stay in grid units;
 * `finish` records the LOD's scale and offset so `offset + scale * value` is
 * a CRS coordinate.
 */

const unzigzag = (v: number) => (v >>> 1) ^ -(v & 1)

export type GeometryType = 'Point' | 'MultiLineString' | 'MultiPolygon'

/** Lists deep from the LOD child to the coordinate struct. */
export const LIST_DEPTH: Record<GeometryType, number> = {
  Point: 0,
  MultiLineString: 2,
  MultiPolygon: 3,
}

/** GeoArrow family codes `toMvt` understands. */
const TYPE_CODE: Record<GeometryType, 1 | 5 | 6> = { Point: 1, MultiLineString: 5, MultiPolygon: 6 }

export class GeometryBuilder {
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

  /** One row of the coordinate leaf: a pair for a point, rings of pairs for shapes. */
  add(type: GeometryType, coords: unknown): void {
    if (this.length + 1 >= this.types.length) {
      const t = new Uint8Array(this.types.length * 2)
      t.set(this.types)
      this.types = t
    }
    this.types[this.length] = TYPE_CODE[type]
    this.geometryOffsets = ensure(this.geometryOffsets, this.length + 2)
    this.geometryOffsets[this.length] = this.polygons
    if (type === 'Point') {
      this.startPolygon()
      this.startRing()
      this.push([coords as [number, number]])
    } else if (type === 'MultiLineString') {
      this.startPolygon()
      for (const ring of coords as [number, number][][]) {
        this.startRing()
        this.push(ring)
      }
    } else {
      for (const polygon of coords as [number, number][][][]) {
        this.startPolygon()
        for (const ring of polygon) {
          this.startRing()
          this.push(ring)
        }
      }
    }
    this.length++
  }

  private startPolygon() {
    this.polygonOffsets = ensure(this.polygonOffsets, this.polygons + 2)
    this.polygonOffsets[this.polygons++] = this.rings
  }

  private startRing() {
    this.ringOffsets = ensure(this.ringOffsets, this.rings + 2)
    this.ringOffsets[this.rings++] = this.coords
  }

  private push(ring: [number, number][]) {
    this.x = ensure(this.x, this.coords + ring.length)
    this.y = ensure(this.y, this.coords + ring.length)
    let x = 0
    let y = 0
    for (const pair of ring) {
      if (pair.length !== 2) throw new Error('coordinate is not an [x, y] pair')
      x += unzigzag(pair[0])
      y += unzigzag(pair[1])
      this.x[this.coords] = x
      this.y[this.coords] = y
      this.coords++
    }
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

/** Decode one row's nested coordinate leaf into CRS coordinates, for tests and inspection. */
export const decodeCoordinates = (
  coords: unknown,
  scale: readonly [number, number],
  offset: readonly [number, number],
): unknown => {
  const a = coords as unknown[]
  const isPair = (v: unknown) => Array.isArray(v) && v.length === 2 && typeof v[0] === 'number'
  if (isPair(a)) {
    const p = a as [number, number]
    return [offset[0] + scale[0] * unzigzag(p[0]), offset[1] + scale[1] * unzigzag(p[1])]
  }
  if (a.length && isPair(a[0])) {
    let x = 0
    let y = 0
    return (a as [number, number][]).map((p) => {
      x += unzigzag(p[0])
      y += unzigzag(p[1])
      return [offset[0] + scale[0] * x, offset[1] + scale[1] * y]
    })
  }
  return a.map((v) => decodeCoordinates(v, scale, offset))
}

function ensure<T extends Uint32Array | Int32Array>(a: T, n: number): T {
  if (n <= a.length) return a
  const b = new (a.constructor as new (n: number) => T)(Math.max(n, a.length * 2))
  b.set(a as never)
  return b
}
