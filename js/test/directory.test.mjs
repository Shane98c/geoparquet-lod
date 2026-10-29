import assert from 'node:assert/strict'
import { gzipSync } from 'node:zlib'
import { SpatialReader, hilbertIndex } from '../dist/index.js'

// One LOD at zoom 2 with depth-2 keys, so a key is a directory cell. Page
// bytes sit past the nodes and are fetched but not decoded here.
const PAGES = 60000
// Thrift metadata whose schema holds an optional binary `geometry`, and
// another with an optional binary `a` after it
const SCHEMA = 'FQIZLEgGc2NoZW1hFQIAFQwlAhgIZ2VvbWV0cnkAFgAZDAA='
const SCHEMA_A = 'FQIZPEgGc2NoZW1hFQQAFQwlAhgIZ2VvbWV0cnkAFQwlAhgBYQAWABkMAA=='
const EMPTY_TOP = [1, [], [], [], []]
const reader = (bytes, top, exact = EMPTY_TOP, reads = [], root = {}) =>
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
      ...root,
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
/** An uncompressed v1 data page of optional binary values, null for a null,
 * PLAIN unless another encoding is given. */
const binaryPage = (values, encoding = 0) => {
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
  const page = [values.length, encoding, 3, 3].flatMap(i32)
  const header = [...[0, body.length, body.length].flatMap(i32), 0x2c, ...page, 0, 0]
  return Uint8Array.from([...header, ...body])
}
/** A primary leaf over pages {offset, length, first, runs: [{box, count}]}:
 * an entry per run, each but a page's first of length 0. */
const primaryLeaf = (pages) => {
  const runs = pages.flatMap((page) => {
    let first = page.first
    return page.runs.map((run, i) => {
      const entry = { ...run, first, offset: page.offset, length: i ? 0 : page.length }
      first += run.count
      return entry
    })
  })
  const ends = runs.map((r) => (r.length ? r.offset + r.length : null))
  let end = 0
  const gaps = runs.map((r, i) => {
    const gap = r.length ? r.offset - end : 0
    end = ends[i] ?? end
    return gap
  })
  return [
    0,
    delta(runs.map((r) => r.box[0])),
    delta(runs.map((r) => r.box[1])),
    runs.map((r) => r.box[2] - r.box[0]),
    runs.map((r) => r.box[3] - r.box[1]),
    gaps,
    runs.map((r) => r.length),
    delta(
      runs.map((r) => r.first),
      runs.map((r) => r.count),
    ),
    runs.map((r) => r.count),
    [],
  ]
}
/** A level-one node over leaves {low, high, offset, length} spanning cells low..high. */
const cellBranch = (level, children) => [
  level,
  delta(children.map((c) => c.low)),
  children.map((c) => c.high - c.low),
  delta(
    children.map((c) => c.offset),
    children.map((c) => c.length),
  ),
  children.map((c) => c.length),
]

{
  const file = store()
  const put = (bytes) => {
    const raw = binaryPage(bytes)
    const [offset, length] = [file.size(), raw.length]
    file.raw(raw)
    return { offset, length }
  }
  const pageA = {
    ...put([point(10, 10), point(10.5, 10.5)]),
    first: 0,
    runs: [{ box: join(at(10, 10), at(10.5, 10.5)), count: 2 }],
  }
  // page B's rows lie in two distant places, a run for each
  const pageB = {
    ...put([point(50, 50), null, point(-20, -20)]),
    first: 2,
    runs: [
      { box: at(50, 50), count: 2 },
      { box: at(-20, -20), count: 1 },
    ],
  }
  const leaf = primaryLeaf([pageA, pageB])
  // a top node over one leaf of every cell, located as [offset, length]
  const whole = ([offset, length]) => cellBranch(1, [{ low: 0, high: 15, offset, length }])
  const reads = []
  const r = reader(file.bytes, [1, [], [], [], []], whole(file.add(leaf)), reads)
  const rows = async (box) => (await r.readPrimary(box)).rows
  assert.deepEqual(await rows([9, 9, 11, 11]), [0, 1])
  assert.deepEqual(await rows([49, 49, 51, 51]), [2])
  assert.deepEqual(await rows([-180, -85, 180, 85]), [0, 1, 2, 4])
  reads.length = 0
  assert.deepEqual(await rows([0, 0, 1, 1]), [], 'between runs')
  assert.deepEqual(reads, [], 'a query between a page’s runs leaves the page unread')
  assert.deepEqual(await rows([100, 0, 101, 1]), [])

  // two leaves: cells 0..7 hold tile 1/2 at zoom 2 (cell 7) and cells 8..15
  // tile 2/1 (cell 13), each listing the pages whose runs reach it
  const west = file.add(primaryLeaf([pageB]))
  const east = file.add(primaryLeaf([pageA, pageB]))
  const top = cellBranch(1, [
    { low: 0, high: 7, offset: west[0], length: west[1] },
    { low: 8, high: 15, offset: east[0], length: east[1] },
  ])
  const routed = []
  const two = reader(file.bytes, [1, [], [], [], []], top, routed)
  assert.deepEqual((await two.readPrimary([9, 9, 11, 11])).rows, [0, 1])
  assert.deepEqual(routed[0], [east[0], east[0] + east[1]], 'a query reads its tile’s leaf')
  assert.ok(!routed.some(([a]) => a === west[0]), 'and no other')
  routed.length = 0
  assert.deepEqual((await two.readPrimary([-21, -21, -19, -19])).rows, [4])
  assert.ok(!routed.some(([a]) => a === east[0]), 'the other leaf alone serves its tile')
  assert.deepEqual((await two.readPrimary([-180, -85, 180, 85])).rows, [0, 1, 2, 4])

  // a branch's cells must be its children's
  const ok = reader(file.bytes, [1, [], [], [], []], whole(east))
  assert.deepEqual((await ok.readPrimary([9, 9, 11, 11])).rows, [0, 1])
  const node = file.add(cellBranch(1, [{ low: 8, high: 15, offset: east[0], length: east[1] }]))
  const above = (low) => cellBranch(2, [{ low, high: 15, offset: node[0], length: node[1] }])
  await assert.rejects(
    reader(file.bytes, [1, [], [], [], []], above(0)).readPrimary([9, 9, 11, 11]),
    /disagrees with its parent entry/,
    'a parent spanning cells its child does not',
  )
  assert.deepEqual(
    (await reader(file.bytes, [1, [], [], [], []], above(8)).readPrimary([9, 9, 11, 11])).rows,
    [0, 1],
  )
  await assert.rejects(
    reader(
      file.bytes,
      [1, [], [], [], []],
      cellBranch(2, [{ low: 8, high: 15, offset: east[0], length: east[1] }]),
    ).readPrimary([9, 9, 11, 11]),
    /malformed spatial directory node/,
    'a leaf located as a level-one node',
  )

  for (const [bad, why] of [
    [leaf.slice(0, 9), 'a leaf without runs'],
    [leaf.with(3, [-1, 0, 0]), 'a negative width'],
    [leaf.with(6, [0, ...leaf[6].slice(1)]), 'a first entry in the previous entry’s page'],
    [leaf.with(5, [...leaf[5].slice(0, 2), 1]), 'a later run with a byte gap'],
    [leaf.with(7, [...leaf[7].slice(0, 2), 1]), 'a later run with a row gap'],
    [leaf.with(9, [[[], [], [], []]]), 'runs for an attribute the root lacks'],
    [leaf.with(1, [0.5, 0, 0]), 'a fractional cell'],
  ])
    await assert.rejects(
      reader(file.bytes, [1, [], [], [], []], whole(file.add(bad))).readPrimary([-9, -9, 9, 9]),
      /malformed/,
      why,
    )
  for (const [top, error] of [
    [leaf, /malformed spatial directory node/],
    [[0, [], [], [], [], [], [], [], []], /must start above its leaves/],
  ])
    assert.throws(() => reader(file.bytes, [1, [], [], [], []], top), error, 'a leaf as the top')
}

{
  const file = store()
  // a level-one node whose one entry locates itself, as a leaf
  let length = 0,
    self
  for (let i = 0; i < 10; i++) {
    const offset = file.size()
    const packed = gzipSync(JSON.stringify(cellBranch(1, [{ low: 0, high: 15, offset, length }])))
    if (packed.length === length) {
      file.raw(packed)
      self = [offset, length]
      break
    }
    length = packed.length
  }
  const top = cellBranch(2, [{ low: 0, high: 15, offset: self[0], length: self[1] }])
  await assert.rejects(
    reader(file.bytes, [1, [], [], [], []], top).readPrimary([4, 4, 6, 6]),
    /malformed primary directory leaf/,
    'a node locating itself is refused at the level it is located for',
  )
}

{
  // two copies of every node, each locating both copies one level down
  const file = store()
  const pair = (node) => [file.add(node), file.add(node)]
  let below = pair(
    primaryLeaf([{ offset: PAGES, length: 10, first: 0, runs: [{ box: at(5, 5), count: 1 }] }]),
  )
  // every entry spans one cell, so a node's two entries can locate both copies
  const refs = (nodes) => nodes.map(([offset, length]) => ({ low: 5, high: 5, offset, length }))
  for (let level = 1; level < 24; level++) below = pair(cellBranch(level, refs(below)))
  const top = cellBranch(24, refs(below))
  await assert.rejects(
    reader(file.bytes, [1, [], [], [], []], top).readPrimary([-180, -85, 180, 85]),
    /referenced twice/,
    'shared nodes are read once per query',
  )
}
console.log('primary directory: routing, runs, cells, levels, self-references, shared nodes')

{
  // A byte range past the end of the file is refused before any request: an
  // overview node or page, a primary node, geometry or attribute page, or the
  // row metadata.
  const file = store()
  const past = file.bytes.length + 1024
  const inside = file.raw(binaryPage([point(5, 5)]))
  const page = (offset, length) => ({
    offset,
    length,
    first: 0,
    runs: [{ box: at(5, 5), count: 1 }],
  })
  /** A top node over one leaf of `pages`, with `sources` per attribute. */
  const exact = (pages, sources = []) => {
    const [offset, length] = file.add(primaryLeaf(pages).with(9, sources))
    return cellBranch(1, [{ low: 0, high: 15, offset, length }])
  }
  const attributed = {
    attributes: ['a'],
    codecs: ['UNCOMPRESSED', 'UNCOMPRESSED'],
    schema: SCHEMA_A,
    directory: exact([page(...inside)], [[[past], [10], [0], [1]]]),
  }
  const lodLeaf = file.add(leaf([[1, past, 10, 0, 3]]))
  const reads = []
  const rows = (r, attributes = []) => r.readPrimary([4, 4, 6, 6], attributes)
  for (const [what, r, read] of [
    ['an overview node', reader(file.bytes, [1, [1], [0], [past], [99]], EMPTY_TOP, reads)],
    [
      'an overview page',
      reader(file.bytes, [1, [1], [0], ...lodLeaf.map((v) => [v])], EMPTY_TOP, reads),
    ],
    [
      'a primary node',
      reader(
        file.bytes,
        EMPTY_TOP,
        cellBranch(1, [{ low: 0, high: 15, offset: past, length: 99 }]),
        reads,
      ),
      rows,
    ],
    ['a geometry page', reader(file.bytes, EMPTY_TOP, exact([page(past, 10)]), reads), rows],
    [
      'an attribute page',
      reader(file.bytes, EMPTY_TOP, EMPTY_TOP, reads, { primary_index: attributed }),
      (r) => rows(r, ['a']),
    ],
    [
      'the row metadata',
      reader(file.bytes, EMPTY_TOP, EMPTY_TOP, reads, { row_metadata: [past, 99] }),
      (r) => r.readRow(0, ['geometry']),
    ],
  ])
    await assert.rejects((read ?? ((r) => tile(r, 0, 0)))(r), /outside the file/, what)
  assert.ok(
    reads.every(([, end]) => end <= file.bytes.length),
    'no request reaches past the file',
  )
}
console.log('byte ranges: nodes, pages and row metadata past the file are refused unread')

{
  // Before any row is visited, a page's header must count the rows its runs,
  // or the leaf for an attribute page, give it, and not be dictionary encoded.
  const file = store()
  const plain = file.raw(binaryPage([point(5, 5), point(5, 5)]))
  const named = file.raw(binaryPage([new Uint8Array([97]), new Uint8Array([98])]))
  const read = (count, attributeRows, geometry = plain) => {
    const page = { offset: geometry[0], length: geometry[1], first: 0 }
    const leaf = primaryLeaf([{ ...page, runs: [{ box: at(5, 5), count }] }])
    const [offset, length] = file.add(
      leaf.with(9, [[[named[0]], [named[1]], [0], [attributeRows]]]),
    )
    const primary_index = {
      attributes: ['a'],
      codecs: ['UNCOMPRESSED', 'UNCOMPRESSED'],
      schema: SCHEMA_A,
      directory: cellBranch(1, [{ low: 0, high: 15, offset, length }]),
    }
    return reader(file.bytes, EMPTY_TOP, EMPTY_TOP, [], { primary_index }).readPrimary(
      [4, 4, 6, 6],
      ['a'],
    )
  }
  const exact = await read(2, 2)
  assert.deepEqual(exact.rows, [0, 1])
  assert.deepEqual(
    exact.columns.a.map((v) => [...v]),
    [[97], [98]],
  )
  for (const [count, attributeRows, why] of [
    [500_000_000, 2, 'a run far past its page’s rows'],
    [1, 2, 'a run short of its page’s rows'],
    [2, 3, 'an attribute page past its header’s rows'],
  ])
    await assert.rejects(read(count, attributeRows), /page header disagrees/, why)
  const dictionary = file.raw(binaryPage([point(5, 5), point(5, 5)], 8))
  await assert.rejects(read(2, 2, dictionary), /page header disagrees/, 'a dictionary page')
}
console.log('page headers: rows and encoding agree with the directory before rows are read')
