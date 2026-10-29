import assert from 'node:assert/strict'
import { TilePool } from '../dist/tile-pool.js'

// Deterministic transport to exercise cancellation while workers are busy,
// buffer ownership, worker load failure, and queued promise cleanup.
const slots = []
globalThis.Worker = class {
  listeners = new Map()
  sent = []
  constructor() {
    slots.push(this)
    queueMicrotask(() => this.emit('message', { data: { ready: true } }))
  }
  addEventListener(type, callback) {
    this.listeners.set(type, callback)
  }
  emit(type, event) {
    this.listeners.get(type)?.(event)
  }
  postMessage(job, transfer) {
    if (job.type !== 'cpu-init') this.sent.push(structuredClone(job, { transfer }))
  }
  terminate() {
    this.terminated = true
  }
  finish() {
    const job = this.sent.at(-1)
    const reply = job.type === 'decode' ? { tile: job.task, data: new ArrayBuffer(3) } : {}
    this.emit('message', { data: { id: job.id, data: new ArrayBuffer(3), ...reply } })
  }
}
const pool = new TilePool(2)
await pool.ready
const bytes = Uint8Array.from([1, 2, 3]).buffer
const task = { payloads: [bytes] }
const active = new AbortController(),
  queued = new AbortController()
const a = pool.decode(task, undefined, active.signal)
const b = pool.decode(task)
const c = pool.decode(task, undefined, queued.signal)
const d = pool.decode(task, { properties: [] })
assert.equal(slots.flatMap((s) => s.sent).length, 2, 'bounded in-flight dispatch')
const ra = assert.rejects(a, { name: 'AbortError' }),
  rc = assert.rejects(c, { name: 'AbortError' })
active.abort()
queued.abort()
await Promise.all([ra, rc])
assert.deepEqual(
  new Uint8Array(bytes),
  Uint8Array.from([1, 2, 3]),
  'cached original remains attached',
)
slots[0].finish() // discard cancelled output; dispatch d, skipping c
assert.equal(slots[0].sent.length, 2)
assert.deepEqual(slots[0].sent[1].options, { properties: [] }, 'render settings travel with d')
slots[1].finish()
slots[0].finish()
assert.equal((await b).data.byteLength, 3)
assert.deepEqual(new Uint8Array((await d).tile.payloads[0]), Uint8Array.from([1, 2, 3]))

// An encode job copies the cached geometry it sends, so the decoded tile stays usable.
const tile = {
  rows: [4],
  columns: {},
  geometry: {
    types: Uint8Array.of(1),
    x: Int32Array.of(5),
    y: Int32Array.of(6),
    geometryOffsets: Uint32Array.of(0, 1),
    polygonOffsets: Uint32Array.of(0, 1),
    ringOffsets: Uint32Array.of(0, 1),
  },
}
const encoded = pool.render(tile, {})
const encoder = slots.find((s) => s.sent.at(-1).type === 'encode')
encoder.finish()
assert.equal((await encoded).byteLength, 3)
assert.deepEqual([...tile.geometry.x, ...tile.geometry.y], [5, 6], 'cached geometry stays attached')
assert.deepEqual([...encoder.sent.at(-1).tile.geometry.x], [5])

const jobs = Array.from({ length: 4 }, () => pool.decode(task))
const rejected = jobs.map((p) => assert.rejects(p, /load failed/))
slots[0].emit('error', { message: 'load failed' })
await Promise.all(rejected)
assert.ok(slots.every((s) => s.terminated))
await assert.rejects(pool.decode(task), /load failed/)
globalThis.Worker = class {
  constructor() {
    throw new Error('constructor failed')
  }
}
const failed = new TilePool(2)
await assert.rejects(failed.ready, /constructor failed/)
await assert.rejects(failed.decode(task), /constructor failed/)
console.log('tile pool transfer ownership, queued/active cancellation and failure cleanup passed')
