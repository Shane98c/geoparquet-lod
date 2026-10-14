// Compare actual production tile semantics with independently paged nested geometry.
import assert from 'node:assert/strict'
import { open, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
const compact = process.argv.includes('--compact')
const directoryArg = process.argv.indexOf('--directory')
const scratch =
  directoryArg >= 0
    ? process.argv[directoryArg + 1]
    : compact
      ? '/private/tmp/overview-japan-compact'
      : '/private/tmp/overview-japan'
const { ExperimentalReader } = await import(`${scratch}/reader.mjs`)
import { openReader, coalescing, renderTile } from '/private/tmp/overview-japan/production.mjs'
import { VectorTile } from '../../js/node_modules/@mapbox/vector-tile/index.js'
import { PbfReader } from '../../js/node_modules/pbf/index.js'

const files = []
async function source(path) {
  const file = await open(path),
    { size } = await file.stat()
  files.push(file)
  const metrics = { requests: 0, bytes: 0 }
  return {
    metrics,
    byteLength: size,
    async slice(start, end = size) {
      metrics.requests++
      metrics.bytes += end - start
      const bytes = Buffer.alloc(end - start)
      let read = 0
      while (read < bytes.length) {
        const r = await file.read(bytes, read, bytes.length - read, start + read)
        if (!r.bytesRead) throw Error('Truncated local source')
        read += r.bytesRead
      }
      return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)
    },
  }
}
const productionFile = await source('/private/tmp/japan.tippecanoe-density.spatial.parquet')
const nestedFile = await source(`${scratch}/japan.${compact ? 'compact' : 'nested'}.parquet`)
const production = await openReader(coalescing(productionFile, { windowMs: 1 }))
const nested = new ExperimentalReader('nested', (start, length) =>
  nestedFile.slice(start, start + length),
)
await nested.open()
let prior
if (compact) {
  const oldFile = await source('/private/tmp/overview-japan/japan.nested.parquet')
  prior = new ExperimentalReader('previous', (start, length) =>
    oldFile.slice(start, start + length),
  )
  await prior.open()
}
const canonical = (data) => {
  const layer = new VectorTile(new PbfReader(new Uint8Array(data))).layers.features
  const features = new Map()
  for (let i = 0; i < layer.length; i++) {
    const f = layer.feature(i)
    assert(!features.has(f.id), `Duplicate feature ${f.id}`)
    features.set(f.id, {
      type: f.type,
      properties: f.properties,
      geometry: f.loadGeometry().map((ring) => ring.map((p) => [p.x, p.y])),
    })
  }
  return features
}
const cases = [
  ['Japan', 4, 137.5, 37],
  ['Tokyo', 8, 139.76, 35.68],
  ['Tokyo', 11, 139.76, 35.68],
  ['Tokyo', 14, 139.76, 35.68],
  ['Tokyo east', 14, 139.78, 35.68],
  ['Tokyo', 16, 139.76, 35.68],
  ['Osaka', 14, 135.5, 34.69],
  ['Osaka', 16, 135.5, 34.69],
  ['rural', 14, 140.12, 36.21],
  ['Sapporo', 11, 141.35, 43.06],
]
const checks = []
for (const [place, z, lon, lat] of cases)
  for (const properties of [[], ['height', 'class']]) {
    const n = 2 ** z,
      x = Math.floor(((lon + 180) / 360) * n)
    const y = Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * n)
    const [a, b] = await Promise.all([
      renderTile(production, z, x, y, { properties }),
      nested.render(z, x, y, properties),
    ])
    const p = canonical(a),
      q = canonical(b.data)
    if (prior) {
      const old = canonical((await prior.render(z, x, y, properties)).data)
      assert.deepEqual(
        q,
        old,
        `${place} z${z}: compact differs from source-aligned nested geometry/properties`,
      )
    }
    const onlyProduction = [...p.keys()].filter((id) => !q.has(id))
    const onlyNested = [...q.keys()].filter((id) => !p.has(id))
    let identical = 0
    for (const [id, feature] of p)
      if (q.has(id)) {
        assert.deepEqual(q.get(id), feature, `${place} z${z} row ${id}`)
        identical++
      }
    const sha256 = createHash('sha256')
      .update(JSON.stringify([...q].sort((a, b) => a[0] - b[0])))
      .digest('hex')
    const check = {
      place,
      z,
      x,
      y,
      properties,
      productionFeatures: p.size,
      nestedFeatures: q.size,
      identicalCommonFeatures: identical,
      onlyProduction,
      onlyNested,
      nestedSha256: sha256,
    }
    checks.push(check)
    console.log(
      JSON.stringify({
        ...check,
        onlyProduction: onlyProduction.length,
        onlyNested: onlyNested.length,
      }),
    )
  }
const result = {
  checks,
  allMembershipsEqual: checks.every((c) => !c.onlyProduction.length && !c.onlyNested.length),
  allCommonGeometriesAndPropertiesEqual: true,
  allPriorNestedFeaturesEqual: compact ? true : undefined,
  productionIo: productionFile.metrics,
  nestedIo: nested.stats(),
}
delete result.nestedIo.trace
await writeFile(`${scratch}/validation.json`, JSON.stringify(result, null, 2) + '\n')
for (const file of files) await file.close()
if (!compact) assert(result.allMembershipsEqual, 'Tile membership differs; inspect validation.json')
else
  console.log(
    'Compact geometry, attributes and source IDs match prior nested in all cases; production boundary-copy differences retained.',
  )
