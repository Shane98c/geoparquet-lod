import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { VectorTile } from '@mapbox/vector-tile'
import { PbfReader } from 'pbf'
import { LodReader, TileDirectory, coalescing, renderTile } from '../dist/index.js'
import { tileId } from '../dist/tiledir.js'

const morton = (x, y, z) => {
  let id = 0
  for (let i = 0; i < z; i++)
    id += ((x >> i) & 1) * 2 ** (2 * i) + ((y >> i) & 1) * 2 ** (2 * i + 1)
  return id
}
for (let z = 0; z < 6; z++) {
  const n = 2 ** z
  for (let x = 0; x < n; x++) assert.equal(tileId(z, x, n - 1), morton(x, n - 1, z))
}
assert.equal(tileId(24, 2 ** 24 - 1, 2 ** 24 - 1), 2 ** 48 - 1)
assert.throws(() => tileId(4, 16, 0), /unsupported/)
assert.throws(() => tileId(25, 0, 0), /unsupported/)

if (process.argv[2]) {
  const path = process.argv[2]
  const parquet = await readFile(path)
  const sidecar = await readFile(process.argv[3] ?? `${path}.tiledir.parquet`)
  const source = {
    byteLength: parquet.length,
    slice: async (a, b) => Uint8Array.from(parquet.subarray(a, b)).buffer,
  }
  const requests = []
  globalThis.fetch = async (url, options) => {
    const range = options.headers.Range
    const suffix = /^bytes=-(\d+)$/.exec(range)
    const [a, b] = suffix
      ? [Math.max(0, sidecar.length - Number(suffix[1])), sidecar.length - 1]
      : /^bytes=(\d+)-(\d+)$/.exec(range).slice(1).map(Number)
    requests.push(range)
    return new Response(Uint8Array.from(sidecar.subarray(a, b + 1)), {
      status: 206,
      headers: { 'Content-Range': `bytes ${a}-${b}/${sidecar.length}` },
    })
  }
  const reader = await LodReader.open(coalescing(source))
  const warn = console.warn
  let warnings = 0
  console.warn = () => warnings++
  const directory = await TileDirectory.open(reader, 'fixture')
  console.warn = warn
  assert.deepEqual(requests, ['bytes=-65536'], 'one suffix request opens the sidecar')
  assert.equal(warnings, reader.lod.tile_directory ? 0 : 1, 'warn when no generation id binds it')
  const descriptor = reader.lod.tile_directory
  if (descriptor) {
    const other = await LodReader.open(source)
    other.lod.tile_directory = { ...descriptor, generation: 'another' }
    await assert.rejects(TileDirectory.open(other, 'bad-generation'), /generation mismatch/)
  } else {
    const other = await LodReader.open(source)
    other.lod.tile_directory = {
      path: 'x',
      profile: 'parquet-table-1',
      zooms: [4, 5, 6],
      generation: 'g',
    }
    await assert.rejects(TileDirectory.open(other, 'no-generation'), /generation mismatch/)
  }
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
  console.warn = () => undefined
  const footerReader = await LodReader.open(source)
  footerReader.metadata.metadata_length++
  await assert.rejects(TileDirectory.open(footerReader, 'bad-footer'), /binding mismatch/)
  const sizeReader = await LodReader.open(source)
  Object.defineProperty(sizeReader, 'fileSize', { value: reader.fileSize + 1 })
  await assert.rejects(TileDirectory.open(sizeReader, 'bad-size'), /binding mismatch/)
  const expected = canonical(await renderTile(reader, 6, 32, 32))
  warnings = 0
  console.warn = () => warnings++
  sidecar.fill(0, sidecar.length - 4)
  assert.deepEqual(
    canonical(await renderTile(reader, 6, 32, 32, { tiledir: 'bad-magic' })),
    expected,
  )
  console.warn = warn
  assert.equal(warnings, 1)
}
console.log('tile directory tests passed')
