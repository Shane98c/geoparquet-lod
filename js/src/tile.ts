import { MVT_BUFFER, MVT_EXTENT, toMvt } from '@cogp/reader'
import { decodeAttributes, decodePoint, PolygonBuilder, type AttributeValue } from './payload.js'
import type { Bbox, LodReader } from './reader.js'

const VECTOR_TILE_SIZE = 512

export interface TileOptions {
  /** MVT layer name for shape LODs (default `features`). */
  layer?: string
  /** MVT layer name for point LODs (default: same as `layer`). */
  pointLayer?: string
  /** Attribute names to copy into MVT properties (default: all carried attributes). */
  properties?: string[]
  signal?: AbortSignal
}

const tileYToLatitude = (y: number, tiles: number) =>
  (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / tiles))) * 180) / Math.PI

export const tileBounds = (z: number, x: number, y: number): Bbox => {
  const tiles = 2 ** z
  const padding = MVT_BUFFER / MVT_EXTENT
  return [
    ((x - padding) / tiles) * 360 - 180,
    tileYToLatitude(y + 1 + padding, tiles),
    ((x + 1 + padding) / tiles) * 360 - 180,
    tileYToLatitude(y - padding, tiles),
  ]
}

/** One MVT tile from the LOD that serves zoom `z`. Feature ids are file row indexes. */
export const renderTile = async (
  reader: LodReader,
  z: number,
  x: number,
  y: number,
  options: TileOptions = {},
): Promise<ArrayBuffer> => {
  const { signal } = options
  const bbox = tileBounds(z, x, y)
  const lod = reader.lodForZoom(z)
  const { rows, values } = await reader.readPayloads(lod, bbox, signal)
  signal?.throwIfAborted()
  const spec = reader.lod.attributes
  const names = options.properties ?? spec.map((a) => a.name)
  const columns: Record<string, AttributeValue[]> = Object.fromEntries(names.map((n) => [n, []]))
  const keptRows: number[] = []
  if (lod.geometry === 'point_f32') {
    const xs: number[] = []
    const ys: number[] = []
    values.forEach((v, i) => {
      const { values: attrs, pos } = decodeAttributes(v, spec)
      const [px, py] = decodePoint(v, pos)
      if (px < bbox[0] || px > bbox[2] || py < bbox[1] || py > bbox[3]) return
      xs.push(px)
      ys.push(py)
      keptRows.push(rows[i])
      for (const n of names) columns[n].push(attrs[n])
    })
    const n = xs.length
    const offsets = Uint32Array.from({ length: n + 1 }, (_, i) => i)
    const geometry = {
      length: n,
      types: new Uint8Array(n).fill(1),
      x: Float64Array.from(xs),
      y: Float64Array.from(ys),
      geometryOffsets: offsets,
      polygonOffsets: offsets,
      ringOffsets: offsets,
      scale: [1, 1] as const,
      offset: [0, 0] as const,
    }
    return toMvt(
      { geometry: geometry as never, rowIndex: keptRows, columns },
      { z, x, y, layer: options.pointLayer ?? options.layer ?? 'features', signal },
    )
  }
  const builder = new PolygonBuilder()
  const type = lod.geometry === 'line_varint' ? 5 : 6
  values.forEach((v, i) => {
    const { values: attrs, pos } = decodeAttributes(v, spec)
    builder.add(v, pos, type)
    keptRows.push(rows[i])
    for (const n of names) columns[n].push(attrs[n])
  })
  const geometry = builder.finish(lod.scale!, lod.offset!)
  return toMvt(
    { geometry: geometry as never, rowIndex: keptRows, columns },
    { z, x, y, layer: options.layer ?? 'features', signal },
  )
}

export const tileResolution = (z: number, y: number) => {
  const latitude = tileYToLatitude(y + 0.5, 2 ** z)
  return (360 * Math.cos((latitude * Math.PI) / 180)) / (VECTOR_TILE_SIZE * 2 ** z)
}
