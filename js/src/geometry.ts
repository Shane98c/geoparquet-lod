/*!
 * GeometryBuilder is adapted from @cogp/reader 2.1.4 (geometry.ts),
 * Copyright (c) 2026 Kanahiro Iguchi, MIT License; see COGP-LICENSE.txt.
 */

import { LOD_CRS } from './web-mercator.js'

/**
 * Assembles decoded LOD rows into the columnar GeometryColumn our MVT encoder
 * consumes. Coordinates stay in grid units; `finish` records the LOD's scale
 * and offset so `offset + scale * value` is an EPSG:3857 coordinate in metres.
 */

export type GeometryType = 'Point' | 'MultiLineString' | 'MultiPolygon'

/** GeoArrow family codes `toMvt` understands. */
const TYPE_CODE: Record<GeometryType, 1 | 5 | 6> = { Point: 1, MultiLineString: 5, MultiPolygon: 6 }
const FAMILY: Record<number, GeometryType> = { 1: 'Point', 5: 'MultiLineString', 6: 'MultiPolygon' }

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

  /** Copy feature `i` of a finished column on the same grid. */
  append(source: LodGeometry, i: number) {
    const { geometryOffsets: go, polygonOffsets: po, ringOffsets: ro } = source
    this.begin(FAMILY[source.types[i]])
    for (let p = go[i]; p < go[i + 1]; p++) {
      this.startPolygon()
      for (let r = po[p]; r < po[p + 1]; r++) {
        this.startRing()
        const start = this.reserveCoordinates(ro[r + 1] - ro[r])
        this.x.set(source.x.subarray(ro[r], ro[r + 1]), start)
        this.y.set(source.y.subarray(ro[r], ro[r + 1]), start)
      }
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
      crs: LOD_CRS,
    }
  }
}

/** Finished columnar geometry of one LOD, in grid units. */
export type LodGeometry = ReturnType<GeometryBuilder['finish']>

function ensure<T extends Uint32Array | Int32Array>(a: T, n: number): T {
  if (n <= a.length) return a
  const b = new (a.constructor as new (n: number) => T)(Math.max(n, a.length * 2))
  b.set(a as never)
  return b
}
