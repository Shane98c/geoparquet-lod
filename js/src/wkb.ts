import type { Bbox } from './reader.js'

const bits = new Float32Array(1)
const word = new Uint32Array(bits.buffer)

/** The largest float32 at most `v`. */
const float32Below = (v: number): number => {
  const nearest = Math.fround(v)
  if (nearest <= v) return nearest
  bits[0] = nearest
  if (nearest > 0) word[0]--
  else if (nearest < 0) word[0]++
  else word[0] = 0x80000001
  return bits[0]
}

/** A bbox rounded outward to float32, as a GeoParquet covering stores it. */
export const float32Outward = ([xmin, ymin, xmax, ymax]: Bbox): Bbox => [
  float32Below(xmin),
  float32Below(ymin),
  -float32Below(-xmax),
  -float32Below(-ymax),
]

/**
 * The x/y extent of a WKB geometry, ISO or extended, or null when it has no
 * finite extent: no coordinates, no x or no y but NaN, or an infinite one,
 * where its covering is null. Each nested geometry carries its own byte
 * order; Z and M values are skipped, and a NaN x or y, as in an empty point,
 * is left out of its own axis, as GEOS and DuckDB take bounds.
 */
export function wkbBounds(wkb: Uint8Array): Bbox | null {
  const view = new DataView(wkb.buffer, wkb.byteOffset, wkb.byteLength)
  let at = 0,
    xmin = Infinity,
    ymin = Infinity,
    xmax = -Infinity,
    ymax = -Infinity
  const geometry = (depth: number): void => {
    if (depth > 32) throw new Error('WKB nested too deeply')
    const little = view.getUint8(at) === 1
    const code = view.getUint32(at + 1, little)
    at += 5
    if (code & 0x20000000) at += 4
    const base = code & 0xffff
    const iso = Math.floor(base / 1000)
    const type = base % 1000
    const step =
      8 *
      (2 +
        (code & 0x80000000 || iso === 1 || iso === 3 ? 1 : 0) +
        (code & 0x40000000 || iso === 2 || iso === 3 ? 1 : 0))
    const points = (n: number) => {
      for (let i = 0; i < n; i++, at += step) {
        const x = view.getFloat64(at, little),
          y = view.getFloat64(at + 8, little)
        if (x < xmin) xmin = x
        if (x > xmax) xmax = x
        if (y < ymin) ymin = y
        if (y > ymax) ymax = y
      }
    }
    const count = () => {
      const n = view.getUint32(at, little)
      at += 4
      return n
    }
    if (type === 1) points(1)
    else if (type === 2) points(count())
    else if (type === 3) for (let rings = count(); rings > 0; rings--) points(count())
    else if (type >= 4 && type <= 7)
      for (let parts = count(); parts > 0; parts--) geometry(depth + 1)
    else throw new Error(`unsupported WKB geometry type ${base}`)
  }
  geometry(0)
  const extent: Bbox = [xmin, ymin, xmax, ymax]
  return extent.every(Number.isFinite) ? extent : null
}
