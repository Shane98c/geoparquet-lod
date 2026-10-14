import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parquetMetadata } from '../../js/node_modules/hyparquet/src/metadata.js'
import { getSchemaPath } from '../../js/node_modules/hyparquet/src/schema.js'
const scratch = process.argv[2]
const { ExperimentalReader } = await import(`${scratch}/reader.mjs`)
const { ByteCache, retainedBytes } = await import(`${scratch}/byte-cache.mjs`)
const { directCoordinates } = await import(`${scratch}/direct-coordinates.mjs`)

const cache = new ByteCache(128)
let release,
  builds = 0
const pending = cache.get('pending', () => {
  builds++
  return new Promise((r) => (release = r))
})
await Promise.resolve()
await cache.get('other', async () => new ArrayBuffer(128))
assert.equal(
  cache.get('pending', () => {
    throw Error('duplicate')
  }),
  pending,
)
release(new ArrayBuffer(128))
await pending
assert.equal(builds, 1)
assert.equal(cache.bytes, 128)
assert(!cache.values.has('other'))
await cache.get('large', async () => new ArrayBuffer(129))
assert(!cache.values.has('large'))
assert.equal(cache.bytes, 128)
await assert.rejects(
  cache.get('failed', async () => {
    throw Error('retryable')
  }),
)
assert.equal(await cache.get('failed', async () => 1), 1)
const backing = new ArrayBuffer(256)
assert.equal(
  retainedBytes([new Uint8Array(backing, 0, 1), new Uint8Array(backing, 1, 1)]),
  32 + 16 + 128 + 256,
)

for (const name of ['delta-v1', 'delta-v2', 'dictionary', 'empty-null']) {
  const bytes = await readFile(`${scratch}/fixtures/${name}.parquet`)
  const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)
  const metadata = parquetMetadata(buffer, { geoparquet: false }),
    group = metadata.row_groups[0]
  const chunks = Object.fromEntries(
    group.columns.map((c) => {
      const m = c.meta_data
      return [
        m.path_in_schema.join('.'),
        [Number(m.dictionary_page_offset ?? m.data_page_offset), Number(m.total_compressed_size)],
      ]
    }),
  )
  const read = async (start, size) => buffer.slice(start, start + size)
  function reader(direct) {
    const r = new ExperimentalReader(name, read, { directCoordinates: direct })
    r.root = {
      layout: 'compact-rows',
      variant: 'nested',
      lods: { f4: { scale: [1, 1], offset: [0, 0] } },
    }
    r.schema = metadata.schema
    r.fields = [{ name: 'height' }, { name: 'class' }]
    r.pages = [[0, Number(group.num_rows), {}, chunks]]
    return r
  }
  const a = reader(true),
    b = reader(false)
  const actual = await a.geometry(0, 'f4'),
    expected = await b.geometry(0, 'f4')
  assert.deepEqual(actual, expected, name)
  const names = Object.keys(chunks).filter((n) => n.startsWith('geometry.'))
  const raw = await Promise.all(names.map((n) => read(...chunks[n])))
  const fast = directCoordinates(
    raw[0],
    raw[1],
    getSchemaPath(metadata.schema, names[0].split('.')),
    getSchemaPath(metadata.schema, names[1].split('.')),
    Number(group.num_rows),
  )
  assert.equal(!!fast, name === 'delta-v1', `${name} specialization/fallback`)
  if (fast)
    assert.throws(
      () =>
        directCoordinates(
          raw[0],
          raw[1],
          getSchemaPath(metadata.schema, names[0].split('.')),
          getSchemaPath(metadata.schema, names[1].split('.')),
          999,
        ),
      /row\/count/,
    )
  const before = a.metrics.decodeMs
  assert.equal(await a.geometry(0, 'f4'), actual)
  assert.equal(a.metrics.decodeMs, before)
  console.log(`${name}: geometry/IDs/null attributes match ordinary assembly; cache reuses result`)
}
console.log(
  'Cache accounting, in-flight deduplication, eviction, retries, and coordinate fixtures passed',
)
