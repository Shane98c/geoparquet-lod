import assert from 'node:assert/strict'
import { SharedLoad } from '../dist/shared-load.js'
import { coalescing } from '../dist/range-buffer.js'

const deferred = () => {
  let resolve, reject
  const promise = new Promise((a, b) => {
    resolve = a
    reject = b
  })
  return { promise, resolve, reject }
}
let dispatched = false
const lazy = new SharedLoad(async () => {
  dispatched = true
  return 1
})
await Promise.resolve()
assert.equal(dispatched, false, 'unsubscribed page runs do not start I/O')
const early = new AbortController()
const earlyRequest = lazy.get(early.signal)
const earlyRejected = assert.rejects(earlyRequest, { name: 'AbortError' })
early.abort()
await earlyRejected
assert.equal(dispatched, false, 'last caller can leave before dispatch')
const a = new AbortController(),
  b = new AbortController(),
  io = deferred()
let transport
const load = new SharedLoad((signal) => {
  transport = signal
  return io.promise
})
const first = load.get(a.signal),
  second = load.get(b.signal)
const rejected = assert.rejects(first, { name: 'AbortError' })
await Promise.resolve()
a.abort()
await rejected
assert.equal(transport.aborted, false, 'live neighbour protects transport')
io.resolve(42)
assert.equal(await second, 42)
b.abort()
assert.equal(await load.get(), 42, 'completed data remains reusable')

const c = new AbortController(),
  slow = deferred()
const abandoned = new SharedLoad(() => slow.promise)
const old = abandoned.get(c.signal)
const oldRejected = assert.rejects(old, { name: 'AbortError' })
await Promise.resolve()
c.abort()
await oldRejected
assert.equal(abandoned.abandoned, true)
slow.resolve(7)
await assert.rejects(abandoned.promise, { name: 'AbortError' }, 'late output is discarded')

let reads = 0
const queue = []
const source = coalescing({
  byteLength: 1000,
  slice(start, end, signal) {
    reads++
    const io = deferred()
    signal?.addEventListener('abort', () => io.reject(signal.reason), { once: true })
    queue.push({ signal, finish: () => io.resolve(new ArrayBuffer(end - start)) })
    return io.promise
  },
})
const x = new AbortController(),
  y = new AbortController()
const one = source.slice(0, 10, x.signal),
  two = source.slice(20, 30, y.signal)
const oneRejected = assert.rejects(one, { name: 'AbortError' })
await Promise.resolve()
assert.equal(reads, 1, 'adjacent slices share one transport')
x.abort()
await oneRejected
assert.equal(queue[0].signal.aborted, false)
queue[0].finish()
assert.equal((await two).byteLength, 10)
const z = new AbortController()
const gone = source.slice(0, 10, z.signal)
const goneRejected = assert.rejects(gone, { name: 'AbortError' })
z.abort()
await goneRejected
assert.equal(reads, 1, 'pre-dispatch cancellation starts no request')
const last = new AbortController()
const active = source.slice(0, 10, last.signal)
const activeRejected = assert.rejects(active, { name: 'AbortError' })
await Promise.resolve()
last.abort()
await activeRejected
assert.equal(queue[1].signal.aborted, true, 'last caller aborts in-flight transport')
assert.equal(reads, 2)
const batchedReads = []
const batched = coalescing(
  {
    byteLength: 1000,
    async slice(start, end) {
      batchedReads.push([start, end])
      return new ArrayBuffer(end - start)
    },
  },
  { windowMs: 1 },
)
const batchedA = batched.slice(0, 10)
await Promise.resolve()
const batchedB = batched.slice(20, 30)
assert.deepEqual(
  (await Promise.all([batchedA, batchedB])).map((b) => b.byteLength),
  [10, 10],
)
assert.deepEqual(batchedReads, [[0, 30]], 'batch requests across separate microtasks')
console.log('shared-load and coalesced range cancellation passed')
