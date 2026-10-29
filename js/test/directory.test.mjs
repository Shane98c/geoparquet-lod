import assert from 'node:assert/strict'
import { gzipSync } from 'node:zlib'
import { SpatialReader, hilbertIndex } from '../dist/index.js'

// One LOD at zoom 2 with depth-2 keys, so a key is a directory cell. Page
// bytes sit past the nodes and are fetched but not decoded here.
const PAGES = 60000
const reader = (bytes, top) =>
  new SpatialReader(
    { byteLength: bytes.length, slice: async (a, b) => bytes.slice(a, b).buffer },
    {
      version: 2,
      file_bytes: bytes.length,
      primary: 'geometry',
      codecs: { f2: 'UNCOMPRESSED' },
      lod: {
        column: 'lod',
        key: { column: 'key', depth: 2 },
        zooms: [2],
        attributes: [],
        lods: { f2: { zooms: [2], geometry_type: 'Point', scale: [1, 1], offset: [0, 0] } },
      },
      directories: { f2: top },
    },
  )
const tile = (r, z, h) => {
  for (let x = 0; x < 2 ** z; x++)
    for (let y = 0; y < 2 ** z; y++)
      if (hilbertIndex(z, x, y) === h) return r.prepareTile(r.lods.f2, z, x, y)
}

/** Gzip JSON nodes stored back to back; `add` returns [offset, length]. */
const store = () => {
  const bytes = new Uint8Array(1 << 16)
  let size = 0
  const put = (packed) => {
    bytes.set(packed, size)
    size += packed.length
    return [size - packed.length, packed.length]
  }
  return {
    bytes,
    add: (node) => put(gzipSync(JSON.stringify(node))),
    /** A level-one node in `cell` whose one entry locates itself. */
    addSelf(cell) {
      let length = 0
      for (let i = 0; i < 10; i++) {
        const packed = gzipSync(JSON.stringify([1, [cell], [0], [size], [length]]))
        if (packed.length === length) return put(packed)
        length = packed.length
      }
      throw new Error('no stable self-reference')
    },
  }
}
const delta = (values, ends = values.map(() => 0)) =>
  values.map((v, i) => v - (i ? values[i - 1] + ends[i - 1] : 0))
/** A node at `level` over one-cell children, each [cell, offset, length]. */
const branch = (level, refs) => [
  level,
  delta(refs.map((r) => r[0])),
  refs.map(() => 0),
  delta(
    refs.map((r) => r[1]),
    refs.map((r) => r[2]),
  ),
  refs.map((r) => r[2]),
]
/** A leaf of one-cell pages, each [cell, offset, length, first row, rows]. */
const leaf = (pages) => [
  ...branch(
    0,
    pages.map((p) => p.slice(0, 3)),
  ),
  delta(
    pages.map((p) => p[3]),
    pages.map((p) => p[4]),
  ),
  pages.map((p) => p[4]),
  pages.map(() => []),
  pages.map(() => 0),
]

{
  const file = store()
  const [offset, length] = file.add(
    leaf([
      [1, PAGES, 10, 0, 3],
      [5, PAGES + 10, 10, 3, 2],
    ]),
  )
  const r = reader(file.bytes, [1, [1], [4], [offset], [length]])
  const whole = await tile(r, 0, 0)
  assert.deepEqual(whole.pages, [
    [1, 1, PAGES, 10, 0, 3, [1, 0], 3],
    [5, 5, PAGES + 10, 10, 3, 2, [5, 0], 2],
  ])
  assert.equal(whole.hostRow, 0)
  assert.deepEqual(
    (await tile(r, 2, 9)).pages.map((p) => p[4]),
    [3],
    'a tile past every page reads the last page for copies',
  )
}

{
  const file = store()
  const [offset, length] = file.addSelf(3)
  const r = reader(file.bytes, [2, [3], [0], [offset], [length]])
  await assert.rejects(
    tile(r, 0, 0),
    /disagrees with its parent entry/,
    'a node locating itself is refused at the level it is located for, not descended forever',
  )
}

{
  // Two copies of every node, each locating both copies one level down.
  const file = store()
  const pair = (node) => [file.add(node), file.add(node)]
  let below = pair(leaf([[3, PAGES, 10, 0, 1]]))
  for (let level = 1; level < 24; level++)
    below = pair(
      branch(
        level,
        below.map(([offset, length]) => [3, offset, length]),
      ),
    )
  const r = reader(
    file.bytes,
    branch(
      24,
      below.map(([offset, length]) => [3, offset, length]),
    ),
  )
  await assert.rejects(tile(r, 0, 0), /referenced twice/, 'shared nodes are read once per tile')
}

{
  const file = store()
  const [offset, length] = file.add(leaf([[5, PAGES, 10, 0, 1]]))
  for (const cell of [4, 5]) {
    // parent cells 4..5 or 5..6 over a leaf in cell 5
    const misplaced = reader(file.bytes, [1, [cell], [1], [offset], [length]])
    await assert.rejects(tile(misplaced, 0, 0), /disagrees with its parent entry/, 'cell bounds')
  }
  // Two parents locate the same leaf with different lengths. A tile before
  // the leaf reaches it through the first parent; one after, through the last.
  const parents = [length, length + 1].map((n) => file.add(branch(1, [[5, offset, n]])))
  const r = reader(
    file.bytes,
    branch(
      2,
      parents.map(([o, n]) => [5, o, n]),
    ),
  )
  assert.equal((await tile(r, 2, 3)).pages.length, 1)
  await assert.rejects(tile(r, 2, 9), /disagrees with its parent entry/, 'node length')
}

{
  const file = store()
  // pages in cells 2..6 and 4..8 overlap
  const [offset, length] = file.add([
    0,
    [2, 2],
    [4, 4],
    [PAGES, 0],
    [10, 10],
    [0, 0],
    [1, 1],
    [[], []],
    [0, 0],
  ])
  const r = reader(file.bytes, [1, [2], [6], [offset], [length]])
  await assert.rejects(tile(r, 0, 0), /out of key order/, 'overlapping entries in a leaf')
  assert.throws(
    () => reader(file.bytes, [1, [5, -2], [0, 0], [0, 10], [10, 10]]),
    /out of key order/,
    'a decreasing minimum in a top node',
  )
  for (const [node, error] of [
    [[33, [], [], [], []], /malformed/],
    [[1, [0.5], [0], [0], [1]], /malformed/],
    [[1, ['1'], [0], [0], [1]], /malformed/],
    [[1, [1], [-1], [0], [1]], /malformed/],
    [[0, [], [], [], [], [], [], [], []], /above its leaves/],
  ])
    assert.throws(() => reader(file.bytes, node), error)
}
console.log('directory lookup: self-references, shared nodes, order, bounds, lengths, levels')
