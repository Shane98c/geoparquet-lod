import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { LodReader, TileDirectory, coalescing, renderTile } from '../dist/index.js'
import { tileIntervals } from '../dist/tiledir.js'

const morton = (x, y, z) => {
  let id = 0
  for (let i = 0; i < z; i++)
    id += ((x >> i) & 1) * 2 ** (2 * i) + ((y >> i) & 1) * 2 ** (2 * i + 1)
  return id
}
for (let z = 0; z < 6; z++) {
  const n = 2 ** z
  for (let x = 0; x < n; x++) {
    assert.deepEqual(tileIntervals(z, z, x, 0), [[morton(x, 0, z), morton(x, 0, z)]])
    const ranges = tileIntervals(z, 0, 0, 0)
    assert.deepEqual(ranges, [[0, n * n - 1]])
  }
}
const parent = tileIntervals(10, 2, 1, 1)
for (const [x, y] of [
  [256, 256],
  [511, 511],
  [253, 253],
  [514, 514],
]) {
  const id = morton(x, y, 10)
  assert.ok(
    parent.some(([a, b]) => a <= id && id <= b),
    'parent buffer includes neighboring children',
  )
}
assert.throws(() => tileIntervals(4, 5, 0, 0), /unsupported/)

if (process.argv[2]) {
  const path = process.argv[2]
  const parquet = await readFile(path)
  const sidecar = await readFile(`${path}.tiledir`)
  const source = {
    byteLength: parquet.length,
    slice: async (a, b) => Uint8Array.from(parquet.subarray(a, b)).buffer,
  }
  const requests = []
  globalThis.fetch = async (url, options) => {
    const [, a, b] = /^bytes=(\d+)-(\d+)$/.exec(options.headers.Range).map(Number)
    requests.push([a, b])
    return new Response(Uint8Array.from(sidecar.subarray(a, b + 1)), { status: 206 })
  }
  const reader = await LodReader.open(coalescing(source))
  const directory = await TileDirectory.open(reader, 'fixture')
  assert.deepEqual(requests, [[0, 65535]], 'one header + root request')
  const canonical = (buffer) => {
    const tile = new VectorTile(new PbfReader(new Uint8Array(buffer)))
    return Object.values(tile.layers)
      .flatMap((layer) =>
        Array.from({ length: layer.length }, (_, i) => {
          const f = layer.feature(i)
          return [f.id, f.type, f.properties, f.loadGeometry()]
        }),
      )
      .sort((a, b) => a[0] - b[0])
  }
  for (const z of [4, 5, 6]) {
    for (const x of [2 ** (z - 1) - 1, 2 ** (z - 1), 2 ** (z - 1) + 1]) {
      for (const y of [2 ** (z - 1) - 1, 2 ** (z - 1), 0]) {
        for (const properties of [[], ['label'], ['value', 'label']]) {
          const expected = canonical(await renderTile(reader, z, x, y, { properties }))
          const fallback = reader.readLod
          reader.readLod = () => {
            throw new Error('unexpected fallback')
          }
          const actual = canonical(
            await renderTile(reader, z, x, y, { properties, tiledir: directory }),
          )
          reader.readLod = fallback
          assert.deepEqual(actual, expected, `tile ${z}/${x}/${y}, properties ${properties}`)
        }
      }
    }
  }
  const footerReader = await LodReader.open(source)
  footerReader.metadata.metadata_length++
  await assert.rejects(TileDirectory.open(footerReader, 'bad-footer'), /binding mismatch/)
  const sizeReader = await LodReader.open(source)
  Object.defineProperty(sizeReader, 'fileSize', { value: reader.fileSize + 1 })
  await assert.rejects(TileDirectory.open(sizeReader, 'bad-size'), /binding mismatch/)
  const expected = canonical(await renderTile(reader, 6, 32, 32))
  const warn = console.warn
  let warnings = 0
  console.warn = () => warnings++
  sidecar[0] = 0
  assert.deepEqual(
    canonical(await renderTile(reader, 6, 32, 32, { tiledir: 'bad-magic' })),
    expected,
  )
  console.warn = warn
  assert.equal(warnings, 1)
}
console.log('experimental tile directory tests passed')
