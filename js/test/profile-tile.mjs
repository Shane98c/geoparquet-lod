// Warm-cache paired CPU comparison. Run after the writer/optimizer has finished.
// node [--cpu-prof --cpu-prof-dir=/private/tmp] test/profile-tile.mjs spatial.parquet
import { open } from 'node:fs/promises'
import assert from 'node:assert/strict'
import { toMvt as referenceMvt } from '@cogp/reader'
import { toMvt as streamMvt } from '../dist/mvt.js'
import { openReader, coalescing, GeometryBuilder } from '../dist/index.js'

const [path] = process.argv.slice(2)
const file = await open(path)
const source = coalescing({
  byteLength: (await file.stat()).size,
  async slice(start, end) {
    const bytes = new Uint8Array(end - start)
    const { bytesRead } = await file.read(bytes, 0, bytes.length, start)
    if (bytesRead !== bytes.length) throw new Error('truncated read')
    return bytes.buffer
  },
})
const reader = await openReader(source)
const baselineModule = process.env.BASELINE_MODULE
const baseline = baselineModule ? await import(baselineModule) : undefined
const referenceReader = baseline ? await baseline.openReader(source) : reader
const variants = ['nested/reference', 'columnar/reference', 'columnar/stream']
const samples = []
for (const [z, lon, lat] of [
  [8, 139.76, 35.68],
  [11, 139.76, 35.68],
  [13, 139.7, 35.66],
]) {
  const x = Math.floor(((lon + 180) / 360) * 2 ** z)
  const y = Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * 2 ** z)
  const lod = reader.lodForZoom(z)
  for (const properties of [[], ['height', 'class']]) {
    let expected
    for (let round = 0; round < 15; round++) {
      // Rotate the order so GC, warmup and background load don't consistently
      // favour the same variant. The first three rounds are warmup.
      for (let j = 0; j < variants.length; j++) {
        const implementation = variants[(j + round) % variants.length]
        const [mode, encoder] = implementation.split('/')
        const a = performance.now()
        const data =
          mode === 'columnar'
            ? await reader.readGeometryByTile(lod, z, x, y, properties)
            : await referenceReader.readLodByTile(
                referenceReader.lodForZoom(z),
                z,
                x,
                y,
                properties,
              )
        const b = performance.now()
        let geometry = data.geometry
        if (!geometry) {
          const builder = new (baseline?.GeometryBuilder ?? GeometryBuilder)()
          for (const coords of data.coords) builder.add(lod.geometry_type, coords)
          geometry = builder.finish(lod.scale, lod.offset)
        }
        const c = performance.now()
        const out = (encoder === 'stream' ? streamMvt : referenceMvt)(
          { geometry, rowIndex: data.rows, columns: data.columns },
          { z, x, y, layer: 'features' },
        )
        const d = performance.now()
        if (!expected) expected = new Uint8Array(out)
        else
          assert.deepEqual(
            new Uint8Array(out),
            expected,
            `${z}/${implementation}: identical MVT bytes`,
          )
        if (round >= 3)
          samples.push({
            z,
            properties,
            implementation,
            round,
            readMs: b - a,
            flattenMs: c - b,
            mvtMs: d - c,
            totalMs: d - a,
            bytes: out.byteLength,
          })
      }
    }
  }
}
const results = []
for (const sample of samples) {
  if (
    results.some(
      (r) =>
        r.z === sample.z &&
        r.properties.length === sample.properties.length &&
        r.implementation === sample.implementation,
    )
  )
    continue
  const group = samples.filter(
    (r) =>
      r.z === sample.z &&
      r.properties.length === sample.properties.length &&
      r.implementation === sample.implementation,
  )
  const entry = { ...sample }
  delete entry.round
  for (const key of ['readMs', 'flattenMs', 'mvtMs', 'totalMs'])
    entry[key] = group.map((r) => r[key]).sort((a, b) => a - b)[Math.floor(group.length / 2)]
  results.push(entry)
}
console.log(JSON.stringify({ path, baselineModule, runs: 12, results, samples }, null, 2))
await file.close()
