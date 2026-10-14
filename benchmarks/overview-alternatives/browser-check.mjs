import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { open, readFile } from 'node:fs/promises'
import { ExperimentalReader } from '/private/tmp/overview-alternatives/reader.mjs'
import { VectorTile } from '../../js/node_modules/@mapbox/vector-tile/index.js'
import { PbfReader as Pbf } from '../../js/node_modules/pbf/index.js'
const attributes = JSON.parse(
  await readFile('/private/tmp/overview-alternatives/source-attributes.json', 'utf8'),
)
const hash = (bytes) => createHash('sha256').update(new Uint8Array(bytes)).digest('hex')
const readers = []
const grouped = process.argv.includes('--grouped')
for (const variant of ['packed', 'nested']) {
  const file = await open(
    `/private/tmp/overview-alternatives/browser.${grouped ? 'grouped.' : ''}${variant}.parquet`,
  )
  const reader = new ExperimentalReader(variant, async (start, size) => {
    const b = Buffer.alloc(size)
    await file.read(b, 0, size, start)
    return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength)
  })
  await reader.open()
  readers.push({ variant, reader, file })
}
const results = []
for (const [place, lon, lat] of [
  ['city', 139.745, 35.665],
  ['periphery', 139.495, 35.705],
])
  for (const z of [8, 11, 14, 16]) {
    const n = 2 ** z,
      x = Math.floor(((lon + 180) / 360) * n),
      y = Math.floor(((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * n)
    for (const properties of [[], ['height', 'class']]) {
      const tiles = await Promise.all(
        readers.map(async ({ reader }) => reader.render(z, x, y, properties)),
      )
      assert.deepEqual(tiles[0].rows, tiles[1].rows)
      assert.equal(hash(tiles[0].data), hash(tiles[1].data), `${place} z${z} ${properties}`)
      let checkedProperties = 0
      for (const tile of tiles) {
        const layer = new VectorTile(new Pbf(new Uint8Array(tile.data))).layers.features
        for (let index = 0; index < layer.length; index++) {
          const feature = layer.feature(index)
          for (const name of properties) {
            const expected = attributes[name][feature.id]
            assert.equal(feature.properties[name], expected == null ? '' : String(expected))
            checkedProperties++
          }
        }
      }
      results.push({
        place,
        z,
        x,
        y,
        properties,
        source_property_checks: checkedProperties,
        features: tiles[0].rows.length,
        sha256: hash(tiles[0].data),
        rows: tiles[0].rows,
      })
      console.error(
        place,
        z,
        properties,
        tiles.map((t) => [t.rows.length, Math.round(t.ms)]),
      )
    }
  }
for (const { variant, reader, file } of readers) {
  console.error(variant, reader.stats().bytes, reader.stats().requests)
  await file.close()
}
console.log(JSON.stringify({ matches: true, results }))
