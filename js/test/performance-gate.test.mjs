import assert from 'node:assert/strict'
import { checkRegression, median } from './performance-gate.mjs'
const base = { bytes: 1000, requests: 10, coldCpuMs: 200, warmCpuMs: 200 }
checkRegression(base, base)
checkRegression(base, { bytes: 900, requests: 9, coldCpuMs: 260, warmCpuMs: 260 })
for (const candidate of [
  { ...base, bytes: 1001 },
  { ...base, requests: 11 },
  { ...base, coldCpuMs: 266 },
  { ...base, warmCpuMs: 266 },
])
  assert.throws(() => checkRegression(base, candidate), /regression/)
assert.equal(median([999, 1, 3, 2, 4, 5, 6]), 4)
console.log('performance gates reject byte, request and CPU regressions')
