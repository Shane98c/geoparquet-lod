import assert from 'node:assert/strict'
import { gzipSync } from 'node:zlib'
import { SpatialReader, hilbertIndex } from '../dist/index.js'

// One LOD at zoom 2 with depth-2 keys, so a key is a directory cell. Page
// bytes sit past the nodes and are fetched but not decoded here.
const PAGES = 60000
// Thrift metadata whose schema holds an optional binary `geometry`
const SCHEMA = 'FQIZLEgGc2NoZW1hFQIAFQwlAhgIZ2VvbWV0cnkAFgAZDAA='
const EMPTY_LEAF = [0, [], [], [], [], [], [], [], [], [], []]
const reader = (bytes, top, exact = EMPTY_LEAF, reads = []) =>
  new SpatialReader(
    {
      byteLength: bytes.length,
      slice: async (a, b) => {
        reads.push([a, b])
        return bytes.slice(a, b).buffer
      },
    },
    {
      version: 3,
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
      primary_index: { attributes: [], codecs: ['UNCOMPRESSED'], schema: SCHEMA, directory: exact },
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
    size: () => size,
    raw: put,
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

// The primary directory: boxes in cells of 2^-12 degrees over pages of WKB.
const cell = (degrees, origin) => Math.floor((degrees + origin) * 4096)
const at = (x, y) => [cell(x, 180), cell(y, 90), cell(x, 180), cell(y, 90)]
const join = (...boxes) => [
  Math.min(...boxes.map((b) => b[0])),
  Math.min(...boxes.map((b) => b[1])),
  Math.max(...boxes.map((b) => b[2])),
  Math.max(...boxes.map((b) => b[3])),
]
const point = (x, y) => {
  const view = new DataView(new ArrayBuffer(21))
  view.setUint8(0, 1)
  view.setUint32(1, 1, true)
  view.setFloat64(5, x, true)
  view.setFloat64(13, y, true)
  return new Uint8Array(view.buffer)
}
const varint = (n) => {
  const out = []
  for (; n >= 128; n = Math.floor(n / 128)) out.push((n % 128) | 128)
  return [...out, n]
}
/** An uncompressed v1 data page of optional binary values, null for a null. */
const binaryPage = (values) => {
  // definition levels as one bit-packed run of groups of eight
  const levels = [...varint(Math.ceil(values.length / 8) * 2 + 1)]
  for (let i = 0; i < values.length; i += 8)
    levels.push(values.slice(i, i + 8).reduce((bits, v, j) => bits | ((v ? 1 : 0) << j), 0))
  const body = [...new Uint8Array(new Uint32Array([levels.length]).buffer), ...levels]
  for (const v of values.filter(Boolean))
    body.push(...new Uint8Array(new Uint32Array([v.length]).buffer), ...v)
  // Thrift i32 fields one id apart: type DATA_PAGE and both sizes, then a
  // nested header of the value count, PLAIN values and RLE levels
  const i32 = (n) => [0x15, ...varint(n * 2)]
  const page = [values.length, 0, 3, 3].flatMap(i32)
  const header = [...[0, body.length, body.length].flatMap(i32), 0x2c, ...page, 0, 0]
  return Uint8Array.from([...header, ...body])
}
/** A primary leaf over pages {box, offset, length, first, count, pieces}. */
const boxLeaf = (pages) => [
  0,
  delta(pages.map((p) => p.box[0])),
  delta(pages.map((p) => p.box[1])),
  pages.map((p) => p.box[2] - p.box[0]),
  pages.map((p) => p.box[3] - p.box[1]),
  delta(
    pages.map((p) => p.offset),
    pages.map((p) => p.length),
  ),
  pages.map((p) => p.length),
  delta(
    pages.map((p) => p.first),
    pages.map((p) => p.count),
  ),
  pages.map((p) => p.count),
  pages.map((p) => p.pieces ?? []),
  [],
]
/** A primary node at `level` over children {box, offset, length}. */
const boxBranch = (level, children) => boxLeaf(children).slice(0, 7).with(0, level)

{
  const file = store()
  const put = (bytes) => {
    const raw = binaryPage(bytes)
    const [offset, length] = [file.size(), raw.length]
    file.raw(raw)
    return { offset, length }
  }
  const pageA = { ...put([point(10, 10), point(10.5, 10.5)]), first: 0, count: 2 }
  pageA.box = join(at(10, 10), at(10.5, 10.5))
  // page B's rows lie in two distant places, so it has a piece for each
  const pageB = { ...put([point(50, 50), null, point(-20, -20)]), first: 2, count: 3 }
  pageB.box = join(at(50, 50), at(-20, -20))
  const pieceAt = ([x0, y0, x1, y1], rows) => [
    rows,
    x0 - pageB.box[0],
    y0 - pageB.box[1],
    x1 - x0,
    y1 - y0,
  ]
  pageB.pieces = [...pieceAt(at(50, 50), 2), ...pieceAt(at(-20, -20), 1)]
  const leaf = boxLeaf([pageA, pageB])
  const reads = []
  const r = reader(file.bytes, [1, [], [], [], []], leaf, reads)
  const rows = async (box) => (await r.readPrimary(box)).rows
  assert.deepEqual(await rows([9, 9, 11, 11]), [0, 1])
  assert.deepEqual(await rows([49, 49, 51, 51]), [2])
  assert.deepEqual(await rows([-180, -85, 180, 85]), [0, 1, 2, 4])
  reads.length = 0
  assert.deepEqual(await rows([0, 0, 1, 1]), [], 'between pieces')
  assert.deepEqual(reads, [], 'a query between a page’s pieces leaves the page unread')
  assert.deepEqual(await rows([100, 0, 101, 1]), [])

  // the same leaf under a branch: a parent box must be its children's union
  const [offset, length] = file.add(leaf)
  const parent = (box) => boxBranch(1, [{ box, offset, length }])
  const ok = reader(file.bytes, [1, [], [], [], []], parent(join(pageA.box, pageB.box)))
  assert.deepEqual((await ok.readPrimary([9, 9, 11, 11])).rows, [0, 1])
  const loose = join(pageA.box, pageB.box).with(2, cell(60, 180))
  await assert.rejects(
    reader(file.bytes, [1, [], [], [], []], parent(loose)).readPrimary([9, 9, 11, 11]),
    /disagrees with its parent entry/,
    'a parent box larger than its children',
  )
  const below = boxBranch(2, [{ box: join(pageA.box, pageB.box), offset, length }])
  await assert.rejects(
    reader(file.bytes, [1, [], [], [], []], below).readPrimary([9, 9, 11, 11]),
    /disagrees with its parent entry/,
    'a leaf located as a level-one node',
  )

  for (const [node, why] of [
    [leaf.with(0, 33), 'a level above 32'],
    [leaf.slice(0, 10), 'a leaf without runs'],
    [leaf.with(3, [-1, 0]), 'a negative width'],
    [leaf.with(9, [[], pageB.pieces.slice(0, 5)]), 'a single piece'],
    [leaf.with(9, [[], pageB.pieces.with(5, 2)]), 'pieces with more rows than the page'],
    [leaf.with(9, [[], pageB.pieces.with(6, 1e6)]), 'a piece outside its page box'],
    [leaf.with(10, [[[], [], [], []]]), 'runs for an attribute the root lacks'],
    [leaf.with(1, [0.5, 0]), 'a fractional cell'],
  ])
    assert.throws(() => reader(file.bytes, [1, [], [], [], []], node), /malformed/, why)
}

{
  const file = store()
  const box = at(5, 5)
  // a level-one node whose one entry locates itself
  let length = 0,
    self
  for (let i = 0; i < 10; i++) {
    const offset = file.size()
    const node = boxBranch(1, [{ box, offset, length }])
    const packed = gzipSync(JSON.stringify(node))
    if (packed.length === length) {
      file.raw(packed)
      self = [offset, length]
      break
    }
    length = packed.length
  }
  const top = boxBranch(2, [{ box, offset: self[0], length: self[1] }])
  await assert.rejects(
    reader(file.bytes, [1, [], [], [], []], top).readPrimary([4, 4, 6, 6]),
    /disagrees with its parent entry/,
    'a node locating itself is refused at the level it is located for',
  )
}

{
  // two copies of every node, each locating both copies one level down
  const file = store()
  const box = at(5, 5)
  const pair = (node) => [file.add(node), file.add(node)]
  let below = pair(boxLeaf([{ box, offset: PAGES, length: 10, first: 0, count: 1, pieces: [] }]))
  for (let level = 1; level < 24; level++)
    below = pair(
      boxBranch(
        level,
        below.map(([offset, length]) => ({ box, offset, length })),
      ),
    )
  const top = boxBranch(
    24,
    below.map(([offset, length]) => ({ box, offset, length })),
  )
  await assert.rejects(
    reader(file.bytes, [1, [], [], [], []], top).readPrimary([4, 4, 6, 6]),
    /referenced twice/,
    'shared nodes are read once per query',
  )
}
console.log('primary directory: boxes, pieces, unions, levels, self-references, shared nodes')
