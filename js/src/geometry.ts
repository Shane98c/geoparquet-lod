/**
 * Assembles decoded LOD rows (nested arrays of grid coordinates in the
 * family's shape) into the columnar GeometryColumn that @cogp/reader's
 * `toMvt` consumes. Coordinates stay in grid units; `finish` records the
 * LOD's scale and offset so `offset + scale * value` is a CRS coordinate.
 */

export type GeometryType = 'Point' | 'MultiLineString' | 'MultiPolygon'

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
    this.begin(type)
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
  }

  /** Begin one feature when decoding directly into the coordinate buffers. */
  begin(type: GeometryType) {
    if (this.length + 1 >= this.types.length) {
      const t = new Uint8Array(this.types.length * 2)
      t.set(this.types)
      this.types = t
    }
    this.types[this.length] = TYPE_CODE[type]
    this.geometryOffsets = ensure(this.geometryOffsets, this.length + 2)
    this.geometryOffsets[this.length] = this.polygons
    this.length++
  }

  startPolygon() {
    this.polygonOffsets = ensure(this.polygonOffsets, this.polygons + 2)
    this.polygonOffsets[this.polygons++] = this.rings
  }

  startRing() {
    this.ringOffsets = ensure(this.ringOffsets, this.rings + 2)
    this.ringOffsets[this.rings++] = this.coords
  }

  /** Reserve a ring once, then fill its coordinates without pair arrays. */
  reserveCoordinates(count: number): number {
    const start = this.coords
    this.x = ensure(this.x, start + count)
    this.y = ensure(this.y, start + count)
    this.coords += count
    return start
  }

  private push(ring: [number, number][]) {
    this.x = ensure(this.x, this.coords + ring.length)
    this.y = ensure(this.y, this.coords + ring.length)
    for (const pair of ring) {
      if (pair.length !== 2) throw new Error('coordinate is not an [x, y] pair')
      this.x[this.coords] = pair[0]
      this.y[this.coords] = pair[1]
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

function ensure<T extends Uint32Array | Int32Array>(a: T, n: number): T {
  if (n <= a.length) return a
  const b = new (a.constructor as new (n: number) => T)(Math.max(n, a.length * 2))
  b.set(a as never)
  return b
}
