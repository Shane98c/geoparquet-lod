import { readFileSync, writeFileSync } from 'node:fs'
import { parquetMetadata } from '../../node_modules/hyparquet/src/metadata.js'
import { getSchemaPath } from '../../node_modules/hyparquet/src/schema.js'
import { readColumn } from '../../node_modules/hyparquet/src/column.js'
import { DEFAULT_PARSERS } from '../../node_modules/hyparquet/src/convert.js'
import { compressors } from 'hyparquet-compressors'
import { directCoordinates } from './standard-coordinates.js'
import { GeometryBuilder } from '../../src/geometry.js'
import { toMvt } from '../../src/mvt.js'
const dir = '/private/tmp/overview-standard-geometry'
const result = JSON.parse(readFileSync(`${dir}/results.json`, 'utf8'))
const variants = Object.keys(result.variants)
function binaryBuilder(values: any[]) {
  const b = new GeometryBuilder()
  b.x = new Float64Array(8192) as any
  b.y = new Float64Array(8192) as any
  for (const bytes of values) {
    const d =
      bytes instanceof ArrayBuffer
        ? new DataView(bytes)
        : new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    let at = 0
    const header = () => {
      const le = d.getUint8(at++) === 1
      const type = d.getUint32(at, le)
      at += 4
      return { le, type }
    }
    const count = (le: boolean) => {
      const n = d.getUint32(at, le)
      at += 4
      return n
    }
    const h = header()
    if (h.type !== 6) throw Error('Expected multipolygon')
    b.begin('MultiPolygon')
    for (let p = 0, n = count(h.le); p < n; p++) {
      const poly = header()
      if (poly.type !== 3) throw Error('Expected polygon')
      b.startPolygon()
      for (let r = 0, nr = count(poly.le); r < nr; r++) {
        b.startRing()
        const nc = count(poly.le),
          start = b.reserveCoordinates(nc)
        for (let k = 0; k < nc; k++) {
          b.x[start + k] = d.getFloat64(at, poly.le)
          b.y[start + k] = d.getFloat64(at + 8, poly.le)
          at += 16
        }
      }
    }
    if (at !== d.byteLength) throw Error('Trailing WKB')
  }
  return b
}
const data: any = {}
for (const v of variants) {
  const diskStart = performance.now()
  const raw = readFileSync(`${dir}/${v}.parquet`)
  const buf = raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength)
  data[v] = {
    buf,
    metadata: parquetMetadata(buf, { geoparquet: false }),
    diskMs: performance.now() - diskStart,
  }
}
const runs: any[] = []
const checks: any = {}
const refs = new Map<number, any>()
const referenceTiles = new Map<number, Buffer>()
for (let run = 0; run < 7; run++)
  for (const v of [
    ...variants.slice(run % variants.length),
    ...variants.slice(0, run % variants.length),
  ]) {
    const { buf, metadata } = data[v]
    let decode = 0,
      mvt = 0,
      totalBytes = 0
    let checksum = 0
    for (let i = 0; i < metadata.row_groups.length; i++) {
      const rg = metadata.row_groups[i],
        desc = result.variants[v].groups[i]
      const chunks = rg.columns.filter((c: any) => c.meta_data.path_in_schema[0] === 'geometry')
      const start = performance.now()
      let b: any
      if (v.startsWith('wkb')) {
        const c = chunks[0].meta_data,
          path = getSchemaPath(metadata.schema, c.path_in_schema),
          el = path.at(-1).element
        const bytes = buf.slice(
          Number(c.data_page_offset),
          Number(c.data_page_offset) + Number(c.total_compressed_size),
        )
        const values = readColumn(
          { view: new DataView(bytes), offset: 0 },
          { groupStart: 0, selectStart: 0, selectEnd: Number(rg.num_rows) },
          {
            type: el.type,
            codec: 'ZSTD',
            element: el,
            schemaPath: path,
            pathInSchema: c.path_in_schema,
            compressors,
            parsers: DEFAULT_PARSERS,
            utf8: false,
          },
        ).data.flatMap((a: any) => Array.from(a))
        b = binaryBuilder(values)
      } else {
        const [x, y] = chunks.map((c: any) => c.meta_data)
        b = directCoordinates(
          buf.slice(
            Number(x.data_page_offset),
            Number(x.data_page_offset) + Number(x.total_compressed_size),
          ),
          buf.slice(
            Number(y.data_page_offset),
            Number(y.data_page_offset) + Number(y.total_compressed_size),
          ),
          getSchemaPath(metadata.schema, x.path_in_schema),
          getSchemaPath(metadata.schema, y.path_in_schema),
          Number(rg.num_rows),
        )
        if (!b) throw Error('Unsupported direct schema')
      }
      decode += performance.now() - start
      const lod =
        v === 'quantized'
          ? JSON.parse(
              metadata.key_value_metadata.find((k: any) => k.key === 'overview_experiment').value,
            ).lods[`f${desc.level}`]
          : { scale: [1, 1], offset: [0, 0] }
      const geometry = b.finish(lod.scale, lod.offset)
      if (run === 0) {
        const current = {
          x: Float64Array.from(geometry.x, (n: number) => n * lod.scale[0] + lod.offset[0]),
          y: Float64Array.from(geometry.y, (n: number) => n * lod.scale[1] + lod.offset[1]),
          geometryOffsets: geometry.geometryOffsets,
          polygonOffsets: geometry.polygonOffsets,
          ringOffsets: geometry.ringOffsets,
        }
        if (v === 'quantized') refs.set(i, current)
        else
          for (const key of Object.keys(current)) {
            const a = current[key],
              ref = refs.get(i)[key]
            if (
              !Buffer.from(a.buffer, a.byteOffset, a.byteLength).equals(
                Buffer.from(ref.buffer, ref.byteOffset, ref.byteLength),
              )
            )
              throw Error(`Geometry mismatch ${v} RG${i} ${key}`)
          }
      }
      // Tile centered on each group's bbox; actual MVT work on identical geometry.
      const lon = (desc.bbox[0] + desc.bbox[2]) / 2,
        lat = (desc.bbox[1] + desc.bbox[3]) / 2,
        z = desc.level,
        n = 2 ** z
      const x = Math.floor(((lon + 180) / 360) * n),
        y = Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * n)
      const ms = performance.now()
      const tile = toMvt(
        { geometry, rowIndex: Array.from({ length: b.length }, (_, i) => i), columns: {} },
        { z, x, y, layer: 'features' } as any,
      )
      mvt += performance.now() - ms
      totalBytes += tile.byteLength
      if (run === 0) {
        if (v === 'quantized') referenceTiles.set(i, Buffer.from(tile))
        else if (!referenceTiles.get(i)!.equals(Buffer.from(tile)))
          throw Error(`MVT bytes mismatch ${v} RG${i}`)
      }
      const hash = new Uint8Array(tile).reduce((a, c) => (Math.imul(a, 31) + c) | 0, 0)
      checksum = (checksum + hash) | 0
    }
    if (run === 0) checks[v] = { tileBytes: totalBytes, checksum }
    runs.push({ run, variant: v, decodeMs: decode, mvtMs: mvt })
    console.log(run, v, decode.toFixed(1), mvt.toFixed(1))
  }
if (new Set(Object.values(checks).map((c: any) => `${c.tileBytes}:${c.checksum}`)).size !== 1)
  throw Error('MVT differs')
writeFileSync(
  `${dir}/cpu-results.json`,
  JSON.stringify(
    {
      fullGeometryAndOffsetsEqual: true,
      perTileBytesEqual: true,
      scope:
        'In-memory compressed geometry chunks; one CPU; attributes excluded equally; synthetic bbox-center tile per RG; first run warmup; 6 measured runs; no network',
      checks,
      runs,
      diskReadMs: Object.fromEntries(variants.map((v) => [v, data[v].diskMs])),
    },
    null,
    2,
  ),
)
