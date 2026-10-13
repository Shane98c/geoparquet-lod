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
    this.emit('message', { data: { id: job.id, data: new ArrayBuffer(3) } })
  }
}
const pool = new TilePool(2)
await pool.ready
const bytes = Uint8Array.from([1, 2, 3]).buffer
const task = { payloads: [bytes] }
const active = new AbortController(),
  queued = new AbortController()
const a = pool.run(task, { signal: active.signal })
const b = pool.run(task, {})
const c = pool.run(task, { signal: queued.signal })
const d = pool.run(task, {})
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
slots[1].finish()
slots[0].finish()
assert.equal((await b).byteLength, 3)
assert.equal((await d).byteLength, 3)
const jobs = Array.from({ length: 4 }, () => pool.run(task, {}))
const rejected = jobs.map((p) => assert.rejects(p, /load failed/))
slots[0].emit('error', { message: 'load failed' })
await Promise.all(rejected)
assert.ok(slots.every((s) => s.terminated))
await assert.rejects(pool.run(task, {}), /load failed/)
globalThis.Worker = class {
  constructor() {
    throw new Error('constructor failed')
  }
}
const failed = new TilePool(2)
await assert.rejects(failed.ready, /constructor failed/)
await assert.rejects(failed.run(task, {}), /constructor failed/)
console.log('tile pool transfer ownership, queued/active cancellation and failure cleanup passed')
