// Columnar LOD geometry from nested [x, y] arrays, and back, for tests.
import { GeometryBuilder } from '../dist/geometry.js'

const parts = (type, coords) =>
  type === 'Point' ? [[[coords]]] : type === 'MultiLineString' ? [coords] : coords

/** One feature per entry: a pair for a point, lines of pairs, or polygons of rings of pairs. */
export function columnar(type, features, scale = [1, 1], offset = [0, 0]) {
  const builder = new GeometryBuilder()
  for (const coords of features) {
    builder.begin(type)
    for (const rings of parts(type, coords)) {
      builder.startPolygon()
      for (const ring of rings) {
        builder.startRing()
        const start = builder.reserveCoordinates(ring.length)
        ring.forEach(([x, y], i) => {
          builder.x[start + i] = x
          builder.y[start + i] = y
        })
      }
    }
  }
  return builder.finish(scale, offset)
}

/** Feature `i` of a columnar geometry as the nested arrays `columnar` takes. */
export function nested(geometry, i, type) {
  const { geometryOffsets: go, polygonOffsets: po, ringOffsets: ro, x, y } = geometry
  const polygons = []
  for (let p = go[i]; p < go[i + 1]; p++) {
    const rings = []
    for (let r = po[p]; r < po[p + 1]; r++) {
      const ring = []
      for (let c = ro[r]; c < ro[r + 1]; c++) ring.push([x[c], y[c]])
      rings.push(ring)
    }
    polygons.push(rings)
  }
  if (type === 'Point') return polygons[0][0][0]
  return type === 'MultiLineString' ? (polygons[0] ?? []) : polygons
}
